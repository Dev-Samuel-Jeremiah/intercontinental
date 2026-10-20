"""
classroom/extras.py
───────────────────
Course discussion, assignment rubrics, spotting very similar submissions,
completion certificates, whiteboard photos and voice feedback.
"""
import hashlib
import os
import re
from itertools import combinations

from django.db import transaction
from django.utils import timezone

from .access import enrolled, teaches
from .models import (CourseCertificate, LessonProgress, Material, Post, QuizSubmission, RubricCriterion, RubricLevel,
                     RubricScore, Submission, Thread)

CERT_SALT = 'course-certificate'
SIMILAR = 0.5            # share of word sequences in common before two submissions are flagged
MIN_WORDS = 25
AUDIO = {'.webm': 'audio/webm', '.ogg': 'audio/ogg', '.mp3': 'audio/mpeg', '.m4a': 'audio/mp4', '.wav': 'audio/wav'}
IMAGES = ('.jpg', '.jpeg', '.png', '.webp')


class ExtrasError(ValueError):
    pass


# ─────────────────────────────────────────────────────────────────────────────
# Discussion
# ─────────────────────────────────────────────────────────────────────────────

def start_thread(course, user, title, body):
    if not (teaches(user, course) or enrolled(user, course)):
        raise ExtrasError("You aren't in this course.")
    title, body = (title or '').strip()[:160], (body or '').strip()
    if not title or not body:
        raise ExtrasError('Write a title and your question.')
    t = Thread.objects.create(tenant_id=course.tenant_id, course=course, author=user, title=title, body=body)
    _screen(t, course, user, f'{title}\n{body}', _thread_path(t.pk))
    return t


def reply(thread, user, body):
    course = thread.course
    if not (teaches(user, course) or enrolled(user, course)):
        raise ExtrasError("You aren't in this course.")
    if thread.locked and not teaches(user, course):
        raise ExtrasError('This discussion is closed.')
    body = (body or '').strip()
    if not body:
        raise ExtrasError('Write your reply.')
    p = Post.objects.create(tenant_id=course.tenant_id, thread=thread, author=user, body=body)
    if not _screen(p, course, user, body, _thread_path(thread.pk)):
        Thread.objects.filter(pk=thread.pk).update(last_activity=timezone.now())
    return p


def _thread_path(pk):
    from django.urls import reverse
    return reverse('classroom:thread', args=[pk], urlconf='lms_project.tenant_urls')


def _screen(obj, course, user, text, link_path):
    """Run the safety check on what a student wrote; hold it if flagged. Teachers' posts aren't checked."""
    if teaches(user, course):
        return False
    from assistant import safety
    verdict = safety.check(course.tenant, text, user)
    if not verdict.flagged:
        return False
    obj.held, obj.held_reason = True, verdict.reason[:160]
    obj.save(update_fields=['held', 'held_reason'])
    if verdict.urgent:
        from lms_project.tasks import run_in_background
        run_in_background(safety.tell_teacher, course.pk, text, link_path, tenant=course.tenant)
    return True


def release(obj):
    obj.held, obj.held_reason = False, ''
    obj.save(update_fields=['held', 'held_reason'])


def mark_answer(post, user):
    """The teacher, or whoever asked, marks the reply that answered it."""
    t = post.thread
    if not (teaches(user, t.course) or t.author_id == user.id):
        raise ExtrasError('Only the teacher or the person who asked can mark an answer.')
    Post.objects.filter(thread=t).update(is_answer=False)
    post.is_answer = True
    post.save(update_fields=['is_answer'])
    return post


# ─────────────────────────────────────────────────────────────────────────────
# Rubrics
# ─────────────────────────────────────────────────────────────────────────────

def parse_levels(raw):
    """'Excellent 4, Good 3, Fair 2, Poor 1' → [('Excellent', 4), …]."""
    out = []
    for part in re.split(r'[,|;\n]', raw or ''):
        part = part.strip()
        if not part:
            continue
        m = re.match(r'^(.*?)[\s:=-]+(\d{1,3})$', part)
        if not m or not m.group(1).strip():
            raise ExtrasError(f'"{part}" — write each level as a name and points, e.g. "Good 3".')
        out.append((m.group(1).strip()[:60], int(m.group(2))))
    if len(out) < 2:
        raise ExtrasError('Give each criterion at least two levels.')
    if len({p for _, p in out}) != len(out):
        raise ExtrasError('Each level needs different points.')
    return out


@transaction.atomic
def set_rubric(assignment, rows):
    """rows: [(criterion name, description, levels text)]. Replaces the rubric if nobody has been marked on it."""
    if RubricScore.objects.filter(criterion__assignment=assignment).exists():
        raise ExtrasError('Work has already been marked with this rubric, so it can no longer be changed.')
    parsed = []
    for name, desc, levels in rows:
        name = (name or '').strip()[:120]
        if not name:
            continue
        parsed.append((name, (desc or '').strip()[:255], parse_levels(levels)))
    assignment.criteria.all().delete()
    for i, (name, desc, levels) in enumerate(parsed):
        c = RubricCriterion.objects.create(tenant_id=assignment.tenant_id, assignment=assignment, name=name,
                                           description=desc, order=i)
        RubricLevel.objects.bulk_create([RubricLevel(tenant_id=assignment.tenant_id, criterion=c, label=l, points=p)
                                         for l, p in levels])
    return len(parsed)


@transaction.atomic
def mark_with_rubric(submission, choices, feedback=None):
    """choices: {criterion id: level id}. Sets the mark out of 10 from the rubric."""
    criteria = list(submission.assignment.criteria.prefetch_related('levels'))
    if not criteria:
        raise ExtrasError('This assignment has no rubric.')
    total = top = 0
    picks = []
    for c in criteria:
        level = next((l for l in c.levels.all() if str(l.id) == str(choices.get(str(c.id)) or choices.get(c.id))), None)
        if not level:
            raise ExtrasError(f'Choose a level for "{c.name}".')
        picks.append((c, level))
        total += level.points
        top += c.max_points
    submission.rubric_scores.all().delete()
    RubricScore.objects.bulk_create([RubricScore(tenant_id=submission.tenant_id, submission=submission, criterion=c,
                                                 level=l) for c, l in picks])
    submission.grade = round(10 * total / top, 1) if top else 0
    if feedback is not None:
        submission.feedback = feedback
    submission.save(update_fields=['grade', 'feedback'])
    return submission


# ─────────────────────────────────────────────────────────────────────────────
# Similar submissions
# ─────────────────────────────────────────────────────────────────────────────

def _words(text):
    return re.findall(r"[a-z0-9']+", (text or '').lower())


def _shingles(words, k=5):
    return {' '.join(words[i:i + k]) for i in range(max(0, len(words) - k + 1))}


def _file_hash(f):
    try:
        h = hashlib.sha1(usedforsecurity=False)
        with f.open('rb') as fh:
            for chunk in iter(lambda: fh.read(65536), b''):
                h.update(chunk)
        return h.hexdigest()
    except (OSError, ValueError):
        return None


def similar_pairs(assignment):
    """[{a, b, score, why}] for submissions that look copied from each other, most similar first."""
    subs = list(Submission.objects.filter(assignment=assignment, submitted_at__isnull=False)
                .select_related('student').prefetch_related('answers'))
    info = {}
    for s in subs:
        text = ' '.join(a.answer_text for a in s.answers.all())
        words = _words(text)
        info[s.id] = {'shingles': _shingles(words) if len(words) >= MIN_WORDS else set(),
                      'hash': _file_hash(s.file) if s.file else None}
    pairs = []
    for a, b in combinations(subs, 2):
        ia, ib = info[a.id], info[b.id]
        if ia['hash'] and ia['hash'] == ib['hash']:
            pairs.append({'a': a, 'b': b, 'score': 100, 'why': 'Identical uploaded file'})
            continue
        if ia['shingles'] and ib['shingles']:
            j = len(ia['shingles'] & ib['shingles']) / len(ia['shingles'] | ib['shingles'])
            if j >= SIMILAR:
                pairs.append({'a': a, 'b': b, 'score': round(100 * j), 'why': 'Same wording in much of the answer'})
    return sorted(pairs, key=lambda p: -p['score'])


# ─────────────────────────────────────────────────────────────────────────────
# Completion and certificates
# ─────────────────────────────────────────────────────────────────────────────

def progress(course, user):
    """What the student has done out of everything in the course."""
    lessons = [l.id for s in course.sections.all() for l in s.lessons.all()]
    assignments = [a.id for s in course.sections.all() for a in s.assignments.all()]
    quizzes = [q for s in course.sections.all() for q in s.quizzes.all()]
    watched = LessonProgress.objects.filter(student=user, lesson_id__in=lessons, watched=True).count()
    submitted = Submission.objects.filter(student=user, assignment_id__in=assignments, submitted_at__isnull=False).count()
    taken = {q.quiz_id: q for q in QuizSubmission.objects.filter(student=user, quiz__in=quizzes, submitted_at__isnull=False)}
    passed = sum(1 for q in quizzes if q.id in taken and q.pass_mark <= _quiz_percent(taken[q.id]))
    total = len(lessons) + len(assignments) + len(quizzes)
    done = watched + submitted + passed
    return {'lessons': (watched, len(lessons)), 'assignments': (submitted, len(assignments)),
            'quizzes': (passed, len(quizzes)), 'done': done, 'total': total,
            'percent': round(100 * done / total) if total else 0, 'complete': bool(total) and done == total}


def _quiz_percent(sub):
    if not sub.total_questions:
        return 0
    return 100 * (sub.score or 0) / sub.total_questions


def certificate_for(course, user):
    """Issue (once) and return the certificate if the course is complete, else None."""
    existing = CourseCertificate.objects.filter(course=course, student=user).first()
    if existing:
        return existing
    if not progress(course, user)['complete']:
        return None
    return CourseCertificate.objects.create(tenant_id=course.tenant_id, course=course, student=user)


def reference(cert):
    from django.core import signing
    return signing.dumps({'c': cert.id, 't': str(cert.tenant_id)}, salt=CERT_SALT, compress=True)


def verify(ref, tenant):
    from django.core import signing
    try:
        data = signing.loads(ref, salt=CERT_SALT)
    except signing.BadSignature:
        return None
    if data.get('t') != str(tenant.id):
        return None
    return CourseCertificate.objects.filter(tenant=tenant, pk=data.get('c')).select_related('course', 'student').first()


# ─────────────────────────────────────────────────────────────────────────────
# Whiteboard photos and voice feedback
# ─────────────────────────────────────────────────────────────────────────────

def add_board_photo(section, upload, title=''):
    if not upload:
        raise ExtrasError('Take or choose a photo of the board.')
    ext = os.path.splitext(upload.name or '')[1].lower()
    if ext not in IMAGES:
        raise ExtrasError('Upload a photo (JPG, PNG or WEBP).')
    if upload.size > 12 * 1024 * 1024:
        raise ExtrasError('The photo is larger than 12 MB.')
    head = upload.read(12)
    upload.seek(0)
    if not (head.startswith(b'\xff\xd8\xff') or head.startswith(b'\x89PNG') or head[8:12] == b'WEBP'):
        raise ExtrasError("That file isn't a photo.")
    title = (title or '').strip()[:255] or f'Board notes — {timezone.localtime():%d %b %Y, %H:%M}'
    return Material.objects.create(tenant_id=section.tenant_id, section=section, title=title, file=upload,
                                   description='Photo of the board')


def set_voice_feedback(submission, upload):
    if not upload:
        raise ExtrasError('Record or choose an audio file.')
    ext = os.path.splitext(upload.name or '')[1].lower()
    if ext not in AUDIO:
        raise ExtrasError('Upload audio (WEBM, OGG, MP3, M4A or WAV).')
    if upload.size > 10 * 1024 * 1024:
        raise ExtrasError('Keep voice feedback under 10 MB (about 10 minutes).')
    if submission.feedback_audio:
        submission.feedback_audio.delete(save=False)
    submission.feedback_audio = upload
    submission.save(update_fields=['feedback_audio'])
    return submission
