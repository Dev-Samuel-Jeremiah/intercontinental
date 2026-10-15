"""
cbt/practice.py
───────────────
Practice from the question bank — a UTME-style mock across up to four
subjects, or a short drill on one topic — and topic mastery built from
practice and from exam questions that came from the bank.
"""
import math
from datetime import timedelta
import random
import re
from collections import defaultdict

from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone

from users.models import ENROLLED_STATUSES, Student, Subject

from .models import BankOption, BankQuestion, PracticeAnswer, PracticeSession, StudentAnswer

MAX_SUBJECTS = 4
JAMB_PER_SUBJECT = 40
SECONDS_PER_QUESTION = 40          # 160 questions ≈ 107 minutes, close to the UTME's 2 hours for 180
MASTERED, DEVELOPING = 80, 50
MIN_ANSWERS = 3


class PracticeError(ValueError):
    pass


def _levels(student):
    cls = student.class_assigned
    return {v.strip().lower() for v in (getattr(cls, 'level', '') or '', getattr(cls, 'name', '') or '') if v.strip()}


def pool(student, subject, topic=''):
    """Practice questions suitable for the student's class: matching level, or no level given."""
    qs = BankQuestion.objects.filter(tenant_id=student.tenant_id, subject=subject, for_practice=True)
    if topic:
        qs = qs.filter(topic__iexact=topic)
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


@transaction.atomic
def start(student, subject_ids, mode='JAMB', per_subject=None, topic=''):
    subject_ids = [int(i) for i in subject_ids][:MAX_SUBJECTS + 1]
    if not subject_ids:
        raise PracticeError('Choose at least one subject.')
    if len(subject_ids) > MAX_SUBJECTS:
        raise PracticeError(f'Choose up to {MAX_SUBJECTS} subjects, as in the UTME.')
    if mode == 'TOPIC' and len(subject_ids) != 1:
        raise PracticeError('A topic drill is for one subject.')
    if PracticeSession.objects.filter(tenant_id=student.tenant_id, student=student, submitted_at__isnull=True,
                                      started_at__gte=timezone.now() - timedelta(hours=4)).exists():
        raise PracticeError('Finish or submit your current practice first.')
    per = int(per_subject or (JAMB_PER_SUBJECT if mode == 'JAMB' else 15))
    per = max(5, min(60, per))
    subjects = list(Subject.objects.filter(tenant_id=student.tenant_id, id__in=subject_ids))
    rng = random.Random()
    paper, total = {}, 0
    for s in subjects:
        ids = pool(student, s, topic if mode == 'TOPIC' else '')
        if not ids:
            raise PracticeError(f'There are no practice questions for {s.name}' + (f' on {topic}' if topic else '') + ' yet.')
        chosen = rng.sample(ids, min(per, len(ids)))
        paper[str(s.id)] = chosen
        total += len(chosen)
    option_order = {}
    for o in BankOption.objects.filter(question_id__in=[q for qs in paper.values() for q in qs]).values('id', 'question_id'):
        option_order.setdefault(str(o['question_id']), []).append(o['id'])
    for v in option_order.values():
        rng.shuffle(v)
    session = PracticeSession.objects.create(
        tenant_id=student.tenant_id, student=student, mode=mode, topic=topic if mode == 'TOPIC' else '',
        paper=paper, option_order=option_order, total=total,
        duration_seconds=max(300, int(math.ceil(total * SECONDS_PER_QUESTION / 60.0)) * 60))
    session.subjects.set(subjects)
    return session


def seconds_left(session):
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
    if session.finished or seconds_left(session) == 0:
        raise PracticeError('This practice has ended.')
    qid = int(question_id)
    if qid not in {q for qs in session.paper.values() for q in qs}:
        raise PracticeError('That question is not in this practice.')
    option = BankOption.objects.filter(pk=option_id, question_id=qid).first() if option_id else None
    PracticeAnswer.objects.update_or_create(session=session, question_id=qid,
                                            defaults={'option': option, 'correct': bool(option and option.is_correct)})


@transaction.atomic
def submit(session):
    session = PracticeSession.objects.select_for_update().get(pk=session.pk)
    if session.finished:
        return session
    session.score = session.answers.filter(correct=True).count()
    session.submitted_at = timezone.now()
    session.save(update_fields=['score', 'submitted_at'])
    return session


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
