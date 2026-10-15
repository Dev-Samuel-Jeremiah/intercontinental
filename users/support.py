"""
users/support.py
────────────────
Support needs in practice: how much time a student gets in a CBT exam, and
whether a running exam is still open to them after the scheduled end.
"""
from datetime import timedelta

from django.utils import timezone

from users.models import SupportNeeds


def extra_time_percent(student):
    if student is None:
        return 0
    row = SupportNeeds.objects.filter(tenant_id=student.tenant_id, student=student) \
        .values_list('extra_time_percent', flat=True).first()
    return row or 0


def needs_large_print(student):
    return bool(student and SupportNeeds.objects.filter(
        tenant_id=student.tenant_id, student=student, large_print=True).exists())


def exam_seconds(duration_minutes, student):
    """The student's own time allowance for an exam part, in seconds."""
    if not duration_minutes:
        return None
    base = int(duration_minutes) * 60
    return base + (base * extra_time_percent(student)) // 100


def exam_open_for(exam, student, attempt_in_progress=False, duration_minutes=None):
    """
    Whether the student may work on the exam now. Normally that is the exam
    window. A student with extra time who has already started may run past the
    end of the window by their extra time — no one else, and never to start.
    """
    if exam.is_active:
        return True
    if not attempt_in_progress or not exam.published:
        return False
    now = timezone.now()
    if now < exam.start_time:
        return False
    extra = extra_time_percent(student)
    minutes = duration_minutes or exam.duration_minutes or 0
    if not extra or not minutes:
        return False
    grace = timedelta(seconds=(minutes * 60 * extra) // 100)
    return now <= exam.end_time + grace


def report_awards(student, term, session):
    """Awards to print on a term's report card: those marked for it, dated in that term."""
    from django.db.models import Q
    from users.models import Award
    if not student or not term:
        return []
    qs = Award.objects.filter(tenant_id=student.tenant_id, student=student, show_on_report=True)
    match = Q(term=term)
    if term.start_date:
        end = term.end_date or timezone.localdate()
        match |= Q(term__isnull=True, awarded_on__gte=term.start_date, awarded_on__lte=end)
    return list(qs.filter(match).order_by('awarded_on'))
