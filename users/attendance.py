"""
users/attendance.py
───────────────────
Daily attendance beyond present/absent: lateness from the gate, telling
parents within minutes when a child is absent, parents' reasons and whether
the school accepts them, follow-up flags for children missing too much
school, calendar heatmaps, and the counts printed on report cards.

A "daily mark" is the register entry with no subject. Where a school marks
attendance per subject, each date is counted once.
"""
from collections import defaultdict
from datetime import date, datetime, timedelta

from django.db import transaction
from django.db.models import Count, Q, Sum
from django.utils import timezone

from users.models import (ABSENCE_REASONS, ENROLLED_STATUSES, Attendance, AttendanceSettings,
                          Student)


class AttendanceError(ValueError):
    pass


def settings_for(tenant):
    obj, _ = AttendanceSettings.objects.get_or_create(tenant=tenant)
    return obj


# ─────────────────────────────────────────────────────────────────────────────
# Counting
# ─────────────────────────────────────────────────────────────────────────────

def _term_range(term):
    if not term or not term.start_date:
        return None, None
    return term.start_date, min(term.end_date or timezone.localdate(), timezone.localdate())


def daily_marks(student, start=None, end=None):
    """{date: Attendance} — one mark per day, preferring the daily register entry."""
    qs = Attendance.objects.filter(tenant_id=student.tenant_id, student=student)
    if start:
        qs = qs.filter(date__gte=start)
    if end:
        qs = qs.filter(date__lte=end)
    marks = {}
    for a in qs.order_by('date', 'subject_id'):     # NULL subject (daily) sorts first
        if a.date not in marks or (a.subject_id is None and marks[a.date].subject_id is not None):
            marks[a.date] = a
    return marks


def term_counts(student, term):
    """Present, absent and late days in the term so far."""
    start, end = _term_range(term)
    if not start:
        return {'present': 0, 'absent': 0, 'late': 0, 'days': 0, 'excused': 0}
    marks = daily_marks(student, start, end).values()
    present = sum(1 for m in marks if m.status == 'P')
    absent = sum(1 for m in marks if m.status == 'A')
    return {'present': present, 'absent': absent, 'days': present + absent,
            'late': sum(1 for m in marks if m.status == 'P' and m.minutes_late),
            'excused': sum(1 for m in marks if m.status == 'A' and m.excused == 'EXCUSED')}


def report_card_days(student, term, session):
    """
    (present, absent) for a report card: the school's manual figure when one
    was entered, otherwise counted from this term's register — never from
    the student's whole history.
    """
    from results.models import StudentTermAttendance
    counts = term_counts(student, term)
    manual = StudentTermAttendance.objects.filter(student=student, term=term, session=session).first()
    if manual is not None:
        opened = term.number_of_school_days or 0
        absent = max(0, opened - manual.days_present) if opened else counts['absent']
        return manual.days_present, absent
    return counts['present'], counts['absent']


# ─────────────────────────────────────────────────────────────────────────────
# Gate arrivals and lateness
# ─────────────────────────────────────────────────────────────────────────────

def minutes_late(tenant, moment):
    cfg = settings_for(tenant)
    local = timezone.localtime(moment)
    start = datetime.combine(local.date(), cfg.school_starts_at)
    if timezone.is_naive(start):
        start = timezone.make_aware(start, timezone.get_current_timezone())
    late = int((local - start).total_seconds() // 60) - cfg.late_grace_minutes
    return max(0, late)


@transaction.atomic
def record_arrival(student, moment=None):
    """
    A student arrived (gate scan). The first arrival of the day marks them
    present with any lateness; a teacher's earlier "absent" becomes "present,
    late" because the child is in school.
    """
    cfg = settings_for(student.tenant)
    moment = moment or timezone.now()
    if not cfg.gate_marks_present or not student.class_assigned_id:
        return None
    today = timezone.localtime(moment).date()
    late = minutes_late(student.tenant, moment)
    mark, created = Attendance.objects.get_or_create(
        tenant_id=student.tenant_id, student=student, school_class_id=student.class_assigned_id,
        subject=None, date=today,
        defaults={'status': 'P', 'minutes_late': late, 'arrived_at': timezone.localtime(moment).time()})
    if not created and mark.arrived_at is None:
        mark.status = 'P'
        mark.minutes_late = late
        mark.arrived_at = timezone.localtime(moment).time()
        mark.save(update_fields=['status', 'minutes_late', 'arrived_at'])
    return mark


def lateness_report(tenant, start, end, school_class=None):
    """Per student: late days and total minutes in the period, worst first."""
    qs = (Attendance.objects.filter(tenant=tenant, date__gte=start, date__lte=end, status='P',
                                    minutes_late__gt=0, subject__isnull=True)
          .select_related('student', 'student__class_assigned'))
    if school_class:
        qs = qs.filter(student__class_assigned=school_class)
    rows = defaultdict(lambda: {'days': 0, 'minutes': 0, 'dates': []})
    for a in qs:
        r = rows[a.student]
        r['days'] += 1
        r['minutes'] += a.minutes_late
        r['dates'].append(a.date)
    out = [{'student': s, **r, 'average': round(r['minutes'] / r['days'])} for s, r in rows.items()]
    return sorted(out, key=lambda r: (-r['days'], -r['minutes']))


# ─────────────────────────────────────────────────────────────────────────────
# Telling parents
# ─────────────────────────────────────────────────────────────────────────────

def alert_message(student, day, school):
    return (f"{school}: {student.full_name} was marked absent today "
            f"({day:%a %d %b}). If this is unexpected please call the school. "
            f"You can give the reason in the parent portal.")


def absences_to_alert(school_class, day):
    """Absences on `day` the parent has not been told about and has not explained."""
    return (Attendance.objects.filter(school_class=school_class, date=day, status='A',
                                      subject__isnull=True, alerted_at__isnull=True,
                                      absence_reason='')
            .select_related('student'))


def send_absence_alerts(tenant, school_class, day, user=None):
    """
    Queue a message to each absent child's family. Only for today's register
    — nobody wants a "your child is absent" text about last Tuesday. Returns
    how many families will be told.
    """
    cfg = settings_for(tenant)
    if not cfg.absence_alerts or day != timezone.localdate():
        return 0
    marks = list(absences_to_alert(school_class, day))
    if not marks:
        return 0
    Attendance.objects.filter(pk__in=[m.pk for m in marks]).update(alerted_at=timezone.now())
    from lms_project.tasks import run_in_background
    run_in_background(_deliver_alerts, [m.pk for m in marks], cfg.alert_channel, tenant=tenant)
    return len(marks)


def _deliver_alerts(mark_ids, channel):
    from communications.services import _is_opted_out, send_email, send_sms
    from users.records import contacts
    for mark in Attendance.objects.filter(pk__in=mark_ids).select_related('student', 'student__tenant'):
        student = mark.student
        if mark.status != 'A' or mark.absence_reason:
            continue          # marked present or explained since the alert was queued
        text = alert_message(student, mark.date, student.tenant.name)
        if channel in ('SMS', 'BOTH') and not _is_opted_out(student, 'SMS'):
            for _name, phone in contacts(student, 'SMS'):
                send_sms(phone, text)
        if channel in ('EMAIL', 'BOTH') and not _is_opted_out(student, 'EMAIL'):
            for _name, email in contacts(student, 'EMAIL'):
                send_email(email, f'Absence today — {student.full_name}',
                           f'<p>{text}</p>', student)


# ─────────────────────────────────────────────────────────────────────────────
# Reasons for absence
# ─────────────────────────────────────────────────────────────────────────────

def give_reason(student, day, reason, note='', by='PARENT', user=None):
    """
    Record why a child was (or will be) absent. A parent's reason waits for
    the school to accept it; a reason entered by staff is accepted at once.
    A reason given in advance creates the absence mark, so the morning alert
    is not sent.
    """
    if reason not in dict(ABSENCE_REASONS):
        raise AttendanceError('Choose a reason.')
    today = timezone.localdate()
    if day > today + timedelta(days=30):
        raise AttendanceError('You can tell the school up to 30 days ahead.')
    if day < today - timedelta(days=60):
        raise AttendanceError('That is too long ago to explain here — please contact the school.')
    if not student.class_assigned_id:
        raise AttendanceError('The student has no class yet.')
    mark = Attendance.objects.filter(tenant_id=student.tenant_id, student=student, date=day,
                                     subject__isnull=True).first()
    if mark and mark.status == 'P' and day <= today:
        raise AttendanceError(f'The register shows {student.full_name} was present that day.')
    if mark is None:
        mark = Attendance(tenant_id=student.tenant_id, student=student,
                          school_class_id=student.class_assigned_id, date=day, status='A')
    mark.absence_reason = reason
    mark.reason_note = (note or '').strip()[:255]
    mark.reason_from = by
    mark.excused = 'EXCUSED' if by == 'STAFF' else 'PENDING'
    mark.save()
    return mark


def review_reason(mark, accept):
    if not mark.absence_reason:
        raise AttendanceError('No reason has been given for that absence.')
    mark.excused = 'EXCUSED' if accept else 'UNEXCUSED'
    mark.save(update_fields=['excused'])
    return mark


def unexplained_absences(student, days=60):
    since = timezone.localdate() - timedelta(days=days)
    return list(Attendance.objects.filter(tenant_id=student.tenant_id, student=student,
                                          status='A', subject__isnull=True, date__gte=since,
                                          absence_reason='').order_by('-date'))


# ─────────────────────────────────────────────────────────────────────────────
# Children missing too much school
# ─────────────────────────────────────────────────────────────────────────────

def _streak(marks):
    """Longest run of consecutive recorded school days absent, ending most recently."""
    current = 0
    for day in sorted(marks, reverse=True):
        if marks[day].status == 'A' and marks[day].excused != 'EXCUSED':
            current += 1
        else:
            break
    return current


def needing_follow_up(tenant, term):
    """
    Students on the roll whose attendance this term is below the school's
    threshold, or who are absent several school days running right now.
    Excused absences still count towards the rate (the child still missed
    school) but not towards the current streak.
    """
    cfg = settings_for(tenant)
    start, end = _term_range(term)
    if not start:
        return []
    out = []
    for s in (Student.objects.filter(tenant=tenant, status__in=ENROLLED_STATUSES)
              .select_related('class_assigned')):
        marks = daily_marks(s, start, end)
        if not marks:
            continue
        present = sum(1 for m in marks.values() if m.status == 'P')
        rate = round(100 * present / len(marks))
        streak = _streak(marks)
        reasons = []
        if rate < cfg.chronic_rate:
            reasons.append(f'{rate}% attendance this term')
        if streak >= cfg.chronic_streak:
            reasons.append(f'absent {streak} school days running')
        if reasons:
            out.append({'student': s, 'rate': rate, 'streak': streak, 'days': len(marks),
                        'absent': len(marks) - present, 'reasons': reasons})
    return sorted(out, key=lambda r: (r['rate'], -r['streak']))


# ─────────────────────────────────────────────────────────────────────────────
# Heatmaps
# ─────────────────────────────────────────────────────────────────────────────

def _weeks(start, end):
    """Monday-aligned weeks of school days (Mon–Fri) between two dates."""
    day = start - timedelta(days=start.weekday())
    weeks = []
    while day <= end:
        weeks.append([day + timedelta(days=i) for i in range(5)])
        day += timedelta(days=7)
    return weeks


def student_heatmap(student, start, end):
    marks = daily_marks(student, start, end)
    weeks = []
    for week in _weeks(start, end):
        row = []
        for d in week:
            m = marks.get(d)
            if d < start or d > end:
                state = 'out'
            elif m is None:
                state = 'none'
            elif m.status == 'A':
                state = 'excused' if m.excused == 'EXCUSED' else 'absent'
            else:
                state = 'late' if m.minutes_late else 'present'
            row.append({'date': d, 'state': state,
                        'title': f"{d:%a %d %b}: " + {'present': 'present', 'late': f'late ({m.minutes_late} min)' if m else '',
                                                      'absent': 'absent', 'excused': 'absent (excused)',
                                                      'none': 'not marked', 'out': ''}[state]})
        weeks.append(row)
    return weeks


def class_heatmap(school_class, start, end):
    """Each school day's presence rate for the class, as a calendar."""
    rows = (Attendance.objects.filter(school_class=school_class, date__gte=start, date__lte=end,
                                      subject__isnull=True)
            .values('date').annotate(p=Count('id', filter=Q(status='P')), n=Count('id')))
    rate = {r['date']: round(100 * r['p'] / r['n']) for r in rows if r['n']}
    weeks = []
    for week in _weeks(start, end):
        row = []
        for d in week:
            r = rate.get(d)
            if d < start or d > end:
                level = 'out'
            elif r is None:
                level = 'none'
            else:
                level = 'l4' if r >= 95 else 'l3' if r >= 85 else 'l2' if r >= 70 else 'l1'
            row.append({'date': d, 'level': level, 'rate': r,
                        'title': f"{d:%a %d %b}: " + (f'{r}% present' if r is not None else 'not marked')})
        weeks.append(row)
    return weeks, (round(sum(rate.values()) / len(rate)) if rate else None)


# ─────────────────────────────────────────────────────────────────────────────
# Attendance certificates
# ─────────────────────────────────────────────────────────────────────────────

CERT_SALT = 'attendance-certificate'


def certificate(student, start, end, purpose=''):
    """Figures and a tamper-proof reference for a printable attendance certificate."""
    from django.core import signing
    if end < start:
        raise AttendanceError('The period cannot end before it starts.')
    end = min(end, timezone.localdate())
    marks = daily_marks(student, start, end)
    present = sum(1 for m in marks.values() if m.status == 'P')
    absent = sum(1 for m in marks.values() if m.status == 'A')
    excused = sum(1 for m in marks.values() if m.status == 'A' and m.excused == 'EXCUSED')
    days = present + absent
    if not days:
        raise AttendanceError('No attendance was recorded for this student in that period.')
    ref = signing.dumps({'s': student.id, 't': str(student.tenant_id), 'a': start.isoformat(),
                         'b': end.isoformat(), 'i': timezone.localdate().isoformat()},
                        salt=CERT_SALT, compress=True)
    return {'student': student, 'start': start, 'end': end, 'present': present, 'absent': absent,
            'excused': excused, 'days': days, 'rate': round(100 * present / days),
            'purpose': (purpose or '').strip()[:120], 'reference': ref,
            'issued_on': timezone.localdate()}


def verify_certificate(reference, tenant):
    """Recompute a certificate from its reference; None if forged or from another school."""
    from django.core import signing
    try:
        data = signing.loads(reference, salt=CERT_SALT)
    except signing.BadSignature:
        return None
    if data.get('t') != str(tenant.id):
        return None
    student = Student.objects.filter(tenant=tenant, pk=data.get('s')).first()
    if not student:
        return None
    result = certificate(student, date.fromisoformat(data['a']), date.fromisoformat(data['b']))
    result['issued_on'] = date.fromisoformat(data['i'])
    return result
