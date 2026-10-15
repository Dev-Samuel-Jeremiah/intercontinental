"""
cbt/engine.py
─────────────
The rules of sitting a CBT paper: each candidate's fixed question and option
order, marking from saved answers, time allowed (including an invigilator's
extension), integrity events, the invigilator's live view and answer review.
"""
import random
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from users.models import ENROLLED_STATUSES, Student
from users.support import exam_seconds

from .models import ExamAttempt, IntegrityEvent, Question, StudentAnswer

STALE_AFTER = 90     # seconds without an autosave before a candidate shows as disconnected


class EngineError(ValueError):
    pass


# ─────────────────────────────────────────────────────────────────────────────
# The candidate's own paper
# ─────────────────────────────────────────────────────────────────────────────

def paper(attempt):
    """
    The attempt's questions in this candidate's order, each with its options in
    the candidate's order. The order is fixed the first time and stored, so a
    reload, resume or review shows the same paper. Questions added later go at
    the end.
    """
    exam = attempt.exam_part.exam
    questions = {q.id: q for q in Question.objects.filter(exam_part=attempt.exam_part).prefetch_related('options')}
    order = [qid for qid in (attempt.question_order or []) if qid in questions]
    rng = random.Random(f'{attempt.id}-{attempt.retake_number}')
    pool = attempt.exam_part.pool_size
    if not order and pool and pool < len(questions):
        # A random draw from the section, the same for this candidate every time.
        order = rng.sample(sorted(questions), pool)
        if not exam.shuffle_questions:
            order.sort()
    elif not pool or not attempt.question_order:
        new = sorted(set(questions) - set(order))
        if exam.shuffle_questions:
            rng.shuffle(new)
        order += new
    opt_order = {str(k): v for k, v in (attempt.option_order or {}).items()}
    changed = order != (attempt.question_order or [])
    out = []
    for qid in order:
        q = questions[qid]
        opts = {o.id: o for o in q.options.all()}
        saved = [oid for oid in opt_order.get(str(qid), []) if oid in opts]
        missing = sorted(set(opts) - set(saved))
        if missing:
            if exam.shuffle_options:
                rng.shuffle(missing)
            saved += missing
            opt_order[str(qid)] = saved
            changed = True
        q.ordered_options = [opts[oid] for oid in saved]
        out.append(q)
    if changed:
        attempt.question_order, attempt.option_order = order, opt_order
        attempt.save(update_fields=['question_order', 'option_order'])
    return out


def grade(attempt):
    """Score from the answers saved so far (multiple choice and true/false)."""
    score = 0
    for a in StudentAnswer.objects.filter(attempt=attempt).select_related('question', 'selected_option'):
        if a.selected_option_id and a.selected_option.is_correct and a.selected_option.question_id == a.question_id:
            score += a.question.marks or 1
    return score


@transaction.atomic
def finish(attempt):
    """Submit: mark from saved answers and pass the score on to the result sheet."""
    from results.services import update_term_result
    attempt = ExamAttempt.objects.select_for_update().get(pk=attempt.pk)
    if attempt.completed:
        return attempt
    attempt.score = grade(attempt)
    attempt.completed = True
    attempt.submitted_at = timezone.now()
    attempt.save(update_fields=['score', 'completed', 'submitted_at'])
    update_term_result(attempt)
    return attempt


# ─────────────────────────────────────────────────────────────────────────────
# Time
# ─────────────────────────────────────────────────────────────────────────────

def allowance(attempt):
    """Seconds this candidate has in total (their extra-time entitlement plus any extension), or None if untimed."""
    exam = attempt.exam_part.exam
    base = exam_seconds(attempt.exam_part.duration_minutes or exam.duration_minutes, attempt.student)
    return None if base is None else base + (attempt.extra_seconds or 0)


def seconds_left(attempt):
    total = allowance(attempt)
    if total is None:
        return None
    elapsed = max(attempt.elapsed_seconds or 0, (timezone.now() - attempt.started_at).total_seconds())
    return max(0, int(total - elapsed))


def extend(attempt, minutes, user=None):
    minutes = int(minutes)
    if not 1 <= minutes <= 120:
        raise EngineError('Extend by 1 to 120 minutes.')
    if attempt.completed:
        raise EngineError('That candidate has already submitted.')
    attempt.extra_seconds = (attempt.extra_seconds or 0) + minutes * 60
    attempt.save(update_fields=['extra_seconds'])
    return attempt


def may_continue(attempt):
    """Whether an unfinished attempt may still be worked on (the window, plus any extra time or extension)."""
    from users.support import exam_open_for, extra_time_percent
    exam = attempt.exam_part.exam
    if attempt.completed or seconds_left(attempt) == 0:
        return False
    minutes = attempt.exam_part.duration_minutes or exam.duration_minutes
    if exam_open_for(exam, attempt.student, True, minutes):
        return True
    if not exam.published or timezone.now() < exam.start_time or not attempt.extra_seconds:
        return False
    grace = (minutes or 0) * 60 * extra_time_percent(attempt.student) // 100 + attempt.extra_seconds
    return timezone.now() <= exam.end_time + timedelta(seconds=grace)


# ─────────────────────────────────────────────────────────────────────────────
# Integrity
# ─────────────────────────────────────────────────────────────────────────────

def record_event(attempt, kind, seconds_away=0):
    if kind not in dict(IntegrityEvent.KINDS) or attempt.completed:
        return None
    # A burst of the same event (e.g. many blur events) within a few seconds counts once.
    recent = IntegrityEvent.objects.filter(attempt=attempt, kind=kind,
                                           at__gte=timezone.now() - timedelta(seconds=3)).first()
    if recent:
        return recent
    return IntegrityEvent.objects.create(tenant_id=attempt.exam_part.exam.tenant_id, attempt=attempt, kind=kind,
                                         seconds_away=min(int(seconds_away or 0), 24 * 3600))


# ─────────────────────────────────────────────────────────────────────────────
# Invigilator's view
# ─────────────────────────────────────────────────────────────────────────────

def console(exam_part):
    """One row per candidate who may sit the paper: status, progress, connection, integrity events."""
    exam = exam_part.exam
    from .models import ExamStudentRestriction
    restricted = list(ExamStudentRestriction.objects.filter(exam=exam).values_list('student_id', flat=True))
    students = Student.objects.filter(tenant_id=exam.tenant_id, class_assigned__in=exam.classes.all(),
                                      status__in=ENROLLED_STATUSES)
    if restricted:
        students = students.filter(id__in=restricted)
    attempts = {a.student_id: a for a in ExamAttempt.objects.filter(exam_part=exam_part).select_related('student')}
    total_q = Question.objects.filter(exam_part=exam_part).count()
    answered = {}
    for a in StudentAnswer.objects.filter(attempt__exam_part=exam_part).exclude(
            selected_option__isnull=True, text_answer__in=[None, '']).values('attempt_id'):
        answered[a['attempt_id']] = answered.get(a['attempt_id'], 0) + 1
    events = {}
    for e in IntegrityEvent.objects.filter(attempt__exam_part=exam_part).values('attempt_id'):
        events[e['attempt_id']] = events.get(e['attempt_id'], 0) + 1
    now = timezone.now()
    rows = []
    for st in students.select_related('class_assigned').order_by('class_assigned__name', 'full_name'):
        a = attempts.get(st.id)
        row = {'student': st, 'attempt': a, 'answered': 0, 'events': 0, 'left': None, 'connected': None}
        if not a:
            row['state'] = 'not_started'
        elif a.completed:
            row.update(state='submitted', answered=answered.get(a.id, 0), events=events.get(a.id, 0))
        else:
            last = a.last_autosave_at or a.started_at
            row.update(state='writing', answered=answered.get(a.id, 0), events=events.get(a.id, 0),
                       left=seconds_left(a), connected=(now - last).total_seconds() <= STALE_AFTER)
        rows.append(row)
    counts = {k: sum(1 for r in rows if r['state'] == k) for k in ('not_started', 'writing', 'submitted')}
    return {'rows': rows, 'counts': counts, 'total_questions': total_q}


# ─────────────────────────────────────────────────────────────────────────────
# Answer review
# ─────────────────────────────────────────────────────────────────────────────

def review_open(attempt):
    exam = attempt.exam_part.exam
    if not attempt.completed or exam.answer_review == 'NEVER':
        return False
    if exam.answer_review == 'AFTER_SUBMIT':
        return True
    return timezone.now() > exam.end_time


def review(attempt):
    saved = {a.question_id: a for a in StudentAnswer.objects.filter(attempt=attempt).select_related('selected_option')}
    out = []
    for q in paper(attempt):
        a = saved.get(q.id)
        correct = [o for o in q.ordered_options if o.is_correct]
        chosen = a.selected_option if a else None
        out.append({'question': q, 'chosen': chosen, 'text': a.text_answer if a else '',
                    'correct': correct, 'right': bool(chosen and chosen.is_correct),
                    'auto_marked': q.question_type in ('MCQ', 'TF')})
    return out
