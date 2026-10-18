"""
cbt/practice.py
───────────────
Practice from the question bank — the Quiz Centre's quick quizzes, daily
challenge and "fix my mistakes" rounds, a UTME-style mock across up to four
subjects, or a short drill on one topic — with points, day streaks and a class
leaderboard, and topic mastery built from practice and from exam questions
that came from the bank. Only questions a teacher has released
(for_practice) are ever drawn.
"""
import math
from datetime import timedelta
import random
import re
from collections import defaultdict

from django.db import transaction
from django.db.models import Count, Q, Sum
from django.utils import timezone

from users.models import ENROLLED_STATUSES, Student, Subject

from .models import BankOption, BankQuestion, PracticeAnswer, PracticeSession, StudentAnswer

MAX_SUBJECTS = 4
JAMB_PER_SUBJECT = 40
SECONDS_PER_QUESTION = 40          # 160 questions ≈ 107 minutes, close to the UTME's 2 hours for 180
MASTERED, DEVELOPING = 80, 50
MIN_ANSWERS = 3
QUIZ_SIZES = (5, 10, 15, 20, 30)
DAILY_SIZE = 10
STALE_HOURS = 4                    # an unfinished practice older than this is marked as it stands
POINTS = {'EASY': 5, 'MEDIUM': 10, 'HARD': 15}
PERFECT_BONUS = 20                 # every question right, on a quiz of at least 5
DAILY_BONUS = 25


class PracticeError(ValueError):
    pass


def _levels(student):
    cls = student.class_assigned
    return {v.strip().lower() for v in (getattr(cls, 'level', '') or '', getattr(cls, 'name', '') or '') if v.strip()}


def pool(student, subject, topic='', difficulty=''):
    """Released questions suitable for the student's class: matching level, or no level given."""
    qs = BankQuestion.objects.filter(tenant_id=student.tenant_id, subject=subject, for_practice=True)
    if topic:
        qs = qs.filter(topic__iexact=topic)
    if difficulty:
        qs = qs.filter(difficulty=difficulty)
    names = _levels(student)
    wanted = Q(level='')
    for n in names:
        wanted |= Q(level__iexact=n)
    matched = [q.id for q in qs.filter(wanted)]
    # A class called "SS 1A" also matches bank level "SS 1".
    for q in qs.exclude(wanted).only('id', 'level'):
        lv = q.level.strip().lower()
        if any(re.match(re.escape(lv) + r'(?![\d])', n) for n in names):
            matched.append(q.id)
    return matched


def available(student):
    """[(subject, question count, [topics])] the student can practise."""
    out = []
    subject_ids = BankQuestion.objects.filter(tenant_id=student.tenant_id, for_practice=True) \
        .values_list('subject_id', flat=True).distinct()
    for s in Subject.objects.filter(id__in=subject_ids).order_by('name'):
        ids = pool(student, s)
        if ids:
            topics = sorted(set(BankQuestion.objects.filter(id__in=ids).exclude(topic='')
                                .values_list('topic', flat=True)))
            out.append((s, len(ids), topics))
    return out


def mistakes(student, subject=None):
    """Released questions the student got wrong the last time they met them in practice."""
    latest = {}
    answers = PracticeAnswer.objects.filter(session__student=student, session__submitted_at__isnull=False,
                                            question__for_practice=True)
    if subject:
        answers = answers.filter(question__subject=subject)
    for qid, right in answers.order_by('answered_at', 'id').values_list('question_id', 'correct'):
        latest[qid] = right
    return [qid for qid, right in latest.items() if not right]


def current(student):
    """The student's practice still in progress, if any (marking any that have run out of time or been left)."""
    found = None
    for s in PracticeSession.objects.filter(student=student, submitted_at__isnull=True):
        if seconds_left(s) == 0 or s.started_at < timezone.now() - timedelta(hours=STALE_HOURS):
            submit(s)
        elif found is None:
            found = s
    return found


def _paper_by_subject(ids):
    paper = defaultdict(list)
    for qid, sid in BankQuestion.objects.filter(id__in=ids).values_list('id', 'subject_id'):
        paper[str(sid)].append(qid)
    order = {qid: i for i, qid in enumerate(ids)}
    return {sid: sorted(qids, key=order.get) for sid, qids in paper.items()}


def _check_size(n, default):
    try:
        n = int(n or default)
    except (TypeError, ValueError):
        n = default
    return max(5, min(60, n))


@transaction.atomic
def start(student, subject_ids, mode='JAMB', per_subject=None, topic='', difficulty='', feedback='END',
          timed=True, source=None):
    """
    Begin a practice. QUICK and TOPIC take one subject (with an optional topic
    and difficulty); JAMB up to four subjects; MISTAKES retries what the student
    got wrong (in one finished practice, given as source, or anywhere); DAILY is
    the same ten questions for the whole day, once.
    """
    subject_ids = [int(i) for i in subject_ids if i][:MAX_SUBJECTS + 1]
    difficulty = difficulty if difficulty in POINTS else ''
    feedback = 'INSTANT' if feedback == 'INSTANT' else 'END'
    if mode not in dict(PracticeSession.MODES):
        raise PracticeError('Choose a kind of practice.')
    if mode in ('JAMB', 'QUICK', 'TOPIC') and not subject_ids:
        raise PracticeError('Choose at least one subject.')
    if len(subject_ids) > MAX_SUBJECTS:
        raise PracticeError(f'Choose up to {MAX_SUBJECTS} subjects, as in the UTME.')
    if mode in ('TOPIC', 'QUICK') and len(subject_ids) != 1:
        raise PracticeError('A quick quiz or topic drill is for one subject.')
    if current(student):
        raise PracticeError('Finish or submit your current practice first.')
    rng = random.Random()
    today = timezone.localdate()
    if mode == 'JAMB':
        feedback, timed = 'END', True          # as in the real examination
        per = _check_size(per_subject, JAMB_PER_SUBJECT)
        subjects = list(Subject.objects.filter(tenant_id=student.tenant_id, id__in=subject_ids))
        paper = {}
        for s in subjects:
            ids = pool(student, s)
            if not ids:
                raise PracticeError(f'There are no practice questions for {s.name} yet.')
            paper[str(s.id)] = rng.sample(ids, min(per, len(ids)))
    elif mode in ('QUICK', 'TOPIC'):
        per = _check_size(per_subject, 10 if mode == 'QUICK' else 15)
        subject = Subject.objects.filter(tenant_id=student.tenant_id, id=subject_ids[0]).first()
        ids = pool(student, subject, topic, difficulty) if subject else []
        if not ids:
            what = ' '.join(x for x in ((dict(BankQuestion.DIFFICULTY).get(difficulty) or '').lower(),
                                        subject.name if subject else '') if x)
            raise PracticeError(f'There are no {what} practice questions' + (f' on {topic}' if topic else '') + ' yet.')
        paper = {str(subject.id): rng.sample(ids, min(per, len(ids)))}
    elif mode == 'MISTAKES':
        per = _check_size(per_subject, 10)
        if source is not None:
            right = set(source.answers.filter(correct=True).values_list('question_id', flat=True))
            ids = list(BankQuestion.objects.filter(id__in=[q for qs in source.paper.values() for q in qs],
                                                   for_practice=True).exclude(id__in=right)
                       .values_list('id', flat=True))
        else:
            subject = Subject.objects.filter(tenant_id=student.tenant_id, id=subject_ids[0]).first() if subject_ids else None
            ids = mistakes(student, subject)
        if not ids:
            raise PracticeError('No mistakes to fix — well done!')
        paper = _paper_by_subject(rng.sample(ids, min(per, len(ids))))
    else:   # DAILY
        if PracticeSession.objects.filter(student=student, mode='DAILY', challenge_date=today).exists():
            raise PracticeError("You've already taken today's challenge. Come back tomorrow!")
        ids = sorted(q for s, n, t in available(student) for q in pool(student, s))
        if not ids:
            raise PracticeError('There are no practice questions for your class yet.')
        day = random.Random(f'{student.tenant_id}-{student.class_assigned_id}-{today.isoformat()}')
        paper = _paper_by_subject(day.sample(ids, min(DAILY_SIZE, len(ids))))
        feedback, timed = 'INSTANT', False
    subjects = list(Subject.objects.filter(id__in=[int(k) for k in paper]))
    total = sum(len(v) for v in paper.values())
    option_order = {}
    for o in BankOption.objects.filter(question_id__in=[q for qs in paper.values() for q in qs]).values('id', 'question_id'):
        option_order.setdefault(str(o['question_id']), []).append(o['id'])
    for v in option_order.values():
        rng.shuffle(v)
    session = PracticeSession.objects.create(
        tenant_id=student.tenant_id, student=student, mode=mode,
        topic=topic if mode in ('TOPIC', 'QUICK') else '', difficulty=difficulty if mode in ('TOPIC', 'QUICK') else '',
        feedback=feedback, timed=bool(timed), challenge_date=today if mode == 'DAILY' else None,
        paper=paper, option_order=option_order, total=total,
        duration_seconds=max(300, int(math.ceil(total * SECONDS_PER_QUESTION / 60.0)) * 60) if timed else 0)
    session.subjects.set(subjects)
    return session


def seconds_left(session):
    """Seconds remaining, or None for an untimed practice."""
    if not session.timed:
        return None
    return max(0, int(session.duration_seconds - (timezone.now() - session.started_at).total_seconds()))


def sections(session):
    """[{subject, questions: [q with .ordered_options and .chosen]}] in the session's fixed order."""
    ids = [q for qs in session.paper.values() for q in qs]
    questions = {q.id: q for q in BankQuestion.objects.filter(id__in=ids).prefetch_related('options')}
    chosen = {a.question_id: a for a in session.answers.all()}
    subjects = {s.id: s for s in session.subjects.all()}
    out = []
    for sid, qids in session.paper.items():
        qs = []
        for qid in qids:
            q = questions.get(qid)
            if not q:
                continue        # removed from the bank since
            opts = {o.id: o for o in q.options.all()}
            order = [o for o in session.option_order.get(str(qid), []) if o in opts] or list(opts)
            q.ordered_options = [opts[o] for o in order]
            a = chosen.get(qid)
            q.chosen_id = a.option_id if a else None
            q.was_right = a.correct if a else False
            qs.append(q)
        out.append({'subject': subjects.get(int(sid)), 'questions': qs})
    return out


def answer(session, question_id, option_id):
    """
    Save a choice. With instant feedback the answer is final and the reply
    carries the verdict: {'correct', 'correct_option', 'explanation'};
    otherwise the choice can be changed until the end and the reply is {}.
    """
    if session.finished or seconds_left(session) == 0:
        raise PracticeError('This practice has ended.')
    qid = int(question_id)
    if qid not in {q for qs in session.paper.values() for q in qs}:
        raise PracticeError('That question is not in this practice.')
    option = BankOption.objects.filter(pk=option_id, question_id=qid).first() if option_id else None
    if session.feedback != 'INSTANT':
        PracticeAnswer.objects.update_or_create(session=session, question_id=qid,
                                                defaults={'option': option, 'correct': bool(option and option.is_correct)})
        return {}
    if option is None:
        raise PracticeError('Choose an answer.')
    _, created = PracticeAnswer.objects.get_or_create(session=session, question_id=qid,
                                                      defaults={'option': option, 'correct': option.is_correct})
    if not created:
        raise PracticeError('You have already answered this question.')
    question = option.question
    right = question.options.filter(is_correct=True).first()
    return {'correct': option.is_correct, 'correct_option': right.id if right else None,
            'explanation': question.explanation or ''}


@transaction.atomic
def submit(session):
    session = PracticeSession.objects.select_for_update().get(pk=session.pk)
    if session.finished:
        return session
    session.score = session.answers.filter(correct=True).count()
    session.points = points_for(session)
    session.submitted_at = timezone.now()
    session.save(update_fields=['score', 'points', 'submitted_at'])
    return session


def points_for(session):
    """Points by difficulty for each right answer, plus bonuses for a perfect quiz and the daily challenge."""
    levels = session.answers.filter(correct=True).values_list('question__difficulty', flat=True)
    points = sum(POINTS.get(d, POINTS['MEDIUM']) for d in levels)
    if session.total >= 5 and len(levels) == session.total:
        points += PERFECT_BONUS
    if session.mode == 'DAILY' and session.answers.exists():
        points += DAILY_BONUS
    return points


# ─────────────────────────────────────────────────────────────────────────────
# Points, streaks, leaderboard and progress
# ─────────────────────────────────────────────────────────────────────────────

def _finished(student):
    return PracticeSession.objects.filter(student=student, submitted_at__isnull=False)


def streak(student):
    """Days in a row, ending today or yesterday, with at least one finished practice."""
    days = {timezone.localtime(t).date() for t in _finished(student).values_list('submitted_at', flat=True)}
    day = timezone.localdate()
    if day not in days:
        day -= timedelta(days=1)
    n = 0
    while day in days:
        n += 1
        day -= timedelta(days=1)
    return n


def stats(student):
    done = _finished(student)
    totals = done.aggregate(points=Sum('points'), right=Sum('score'), asked=Sum('total'))
    today = timezone.localdate()
    return {
        'points': totals['points'] or 0, 'quizzes': done.count(), 'streak': streak(student),
        'accuracy': round(100 * (totals['right'] or 0) / totals['asked']) if totals['asked'] else None,
        'mistakes': len(mistakes(student)),
        'daily': PracticeSession.objects.filter(student=student, mode='DAILY', challenge_date=today).first(),
    }


def leaderboard(student, days=7, size=10):
    """Classmates ranked by practice points (the last `days` days, or all time if None): (top rows, the student's row)."""
    if not student.class_assigned_id:
        return [], None
    done = PracticeSession.objects.filter(tenant_id=student.tenant_id, submitted_at__isnull=False,
                                          student__class_assigned_id=student.class_assigned_id,
                                          student__status__in=ENROLLED_STATUSES)
    if days:
        done = done.filter(submitted_at__gte=timezone.now() - timedelta(days=days))
    rows = list(done.values('student_id', 'student__full_name')
                .annotate(points=Sum('points'), quizzes=Count('id'), right=Sum('score'), asked=Sum('total'))
                .filter(points__gt=0).order_by('-points', '-right', 'student__full_name'))
    me = None
    for i, r in enumerate(rows, 1):
        r.update(rank=i, name=r['student__full_name'], me=r['student_id'] == student.id,
                 accuracy=round(100 * r['right'] / r['asked']) if r['asked'] else 0)
        if r['me']:
            me = r
    return rows[:size], me


def progress(student, n=10):
    """The student's last n finished practices, oldest first, for a progress chart."""
    return list(reversed(list(_finished(student).order_by('-submitted_at')[:n])))


def subject_accuracy(student):
    """[{subject, right, total, percent}] from every practice answer, weakest first."""
    rows = (PracticeAnswer.objects.filter(session__student=student, session__submitted_at__isnull=False)
            .values('question__subject__name').annotate(n=Count('id'), right=Count('id', filter=Q(correct=True))))
    return sorted([{'subject': r['question__subject__name'], 'right': r['right'], 'total': r['n'],
                    'percent': round(100 * r['right'] / r['n'])} for r in rows], key=lambda r: r['percent'])


def breakdown(session):
    """Per subject and per topic: right / total."""
    by_subject, by_topic = defaultdict(lambda: [0, 0]), defaultdict(lambda: [0, 0])
    right = set(session.answers.filter(correct=True).values_list('question_id', flat=True))
    topics = dict(BankQuestion.objects.filter(id__in=[q for qs in session.paper.values() for q in qs])
                  .values_list('id', 'topic'))
    names = {s.id: s.name for s in session.subjects.all()}
    for sid, qids in session.paper.items():
        for q in qids:
            if q not in topics:
                continue
            by_subject[names.get(int(sid), '')][1] += 1
            key = (names.get(int(sid), ''), topics[q] or 'General')
            by_topic[key][1] += 1
            if q in right:
                by_subject[names.get(int(sid), '')][0] += 1
                by_topic[key][0] += 1
    return ([{'subject': k, 'right': v[0], 'total': v[1], 'percent': round(100 * v[0] / v[1]) if v[1] else 0}
             for k, v in by_subject.items()],
            sorted([{'subject': k[0], 'topic': k[1], 'right': v[0], 'total': v[1],
                     'percent': round(100 * v[0] / v[1]) if v[1] else 0} for k, v in by_topic.items()],
                   key=lambda r: r['percent']))


# ─────────────────────────────────────────────────────────────────────────────
# Mastery
# ─────────────────────────────────────────────────────────────────────────────

def _label(percent, n):
    if n < MIN_ANSWERS:
        return 'few'
    return 'mastered' if percent >= MASTERED else 'developing' if percent >= DEVELOPING else 'weak'


def answers_by_topic(students, subject=None):
    """{(student_id, subject_id, topic): [right, total]} from practice and from exam questions taken from the bank."""
    ids = [s.id for s in students]
    tally = defaultdict(lambda: [0, 0])
    pa = PracticeAnswer.objects.filter(session__student_id__in=ids, session__submitted_at__isnull=False) \
        .select_related('question').values('session__student_id', 'question__subject_id', 'question__topic', 'correct')
    if subject:
        pa = pa.filter(question__subject=subject)
    for a in pa:
        k = (a['session__student_id'], a['question__subject_id'], a['question__topic'] or 'General')
        tally[k][1] += 1
        tally[k][0] += a['correct']
    ea = StudentAnswer.objects.filter(attempt__student_id__in=ids, attempt__completed=True,
                                      question__bank_question__isnull=False) \
        .values('attempt__student_id', 'question__bank_question__subject_id', 'question__bank_question__topic',
                'selected_option__is_correct')
    if subject:
        ea = ea.filter(question__bank_question__subject=subject)
    for a in ea:
        k = (a['attempt__student_id'], a['question__bank_question__subject_id'],
             a['question__bank_question__topic'] or 'General')
        tally[k][1] += 1
        tally[k][0] += bool(a['selected_option__is_correct'])
    return tally


def student_mastery(student):
    """[{subject, topics: [{topic, right, total, percent, label}]}]."""
    tally = answers_by_topic([student])
    subjects = {s.id: s for s in Subject.objects.filter(id__in={k[1] for k in tally})}
    grouped = defaultdict(list)
    for (_, sid, topic), (r, n) in tally.items():
        pct = round(100 * r / n) if n else 0
        grouped[sid].append({'topic': topic, 'right': r, 'total': n, 'percent': pct, 'label': _label(pct, n)})
    return [{'subject': subjects[sid], 'topics': sorted(rows, key=lambda x: x['percent'])}
            for sid, rows in sorted(grouped.items(), key=lambda kv: subjects[kv[0]].name)]


def class_mastery(school_class, subject):
    """Topics × students for a class in one subject, with each topic's class average."""
    students = list(Student.objects.filter(tenant_id=school_class.tenant_id, class_assigned=school_class,
                                           status__in=ENROLLED_STATUSES).order_by('full_name'))
    tally = answers_by_topic(students, subject)
    topics = sorted({k[2] for k in tally})
    rows = []
    for st in students:
        cells = []
        for t in topics:
            r, n = tally.get((st.id, subject.id, t), (0, 0))
            pct = round(100 * r / n) if n else None
            cells.append({'percent': pct, 'n': n, 'label': _label(pct or 0, n) if n else 'none'})
        rows.append({'student': st, 'cells': cells})
    averages = []
    for i, t in enumerate(topics):
        r = sum(tally.get((st.id, subject.id, t), (0, 0))[0] for st in students)
        n = sum(tally.get((st.id, subject.id, t), (0, 0))[1] for st in students)
        averages.append({'topic': t, 'percent': round(100 * r / n) if n else None, 'n': n,
                         'weak': [row['student'] for row in rows if row['cells'][i]['label'] == 'weak']})
    return {'topics': topics, 'rows': rows, 'averages': averages}
