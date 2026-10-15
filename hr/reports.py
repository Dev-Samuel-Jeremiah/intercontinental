"""
hr/reports.py
─────────────
Staff work reports: each staff member writes a daily or a weekly report,
an administrator reads it, can leave feedback, and marks it reviewed.

The register for a day or week lists every staff member as submitted, late,
draft (started, not sent), missing, on leave, or reviewed. Weekends and
public holidays are not "missing" for daily reports.
"""
from collections import Counter
from datetime import timedelta

from django.db import IntegrityError
from django.utils import timezone

from users.models import Staff

from .models import StaffReport

R = StaffReport
TEXT_FIELDS = ('work_done', 'highlights', 'challenges', 'support_needed', 'next_plan')
MAX_TEXT = 5000
MAX_FILE_MB = 10
ALLOWED_EXT = ('.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.jpg', '.jpeg', '.png', '.txt')


class ReportError(ValueError):
    pass


# ─────────────────────────────────────────────────────────────────────────────
# Periods
# ─────────────────────────────────────────────────────────────────────────────

def bounds(period, day):
    """(start, end) of the daily or weekly period containing `day`. A week runs
    Monday to Friday, the school week."""
    if period == R.WEEKLY:
        start = day - timedelta(days=day.weekday())
        return start, start + timedelta(days=4)
    return day, day


def label(period, start):
    if period == R.WEEKLY:
        end = start + timedelta(days=4)
        return f'Week of {start:%d %b} – {end:%d %b %Y}'
    return f'{start:%A %d %B %Y}'


def shift(period, start, steps):
    return start + timedelta(days=(7 if period == R.WEEKLY else 1) * steps)


# ─────────────────────────────────────────────────────────────────────────────
# Writing
# ─────────────────────────────────────────────────────────────────────────────

def report_for(staff, period, day):
    """The staff member's report for that period, or a new unsaved draft."""
    start, end = bounds(period, day)
    found = R.objects.filter(tenant_id=staff.tenant_id, staff=staff, period=period, period_start=start).first()
    return found or R(tenant_id=staff.tenant_id, staff=staff, period=period, period_start=start, period_end=end)


def save(report, data, upload=None, submit=False, remove_file=False):
    """Save the staff member's words; with submit=True, send it to the administrators."""
    if not report.editable:
        raise ReportError('This report has been reviewed and can no longer be changed.')
    if report.period_start > bounds(report.period, timezone.localdate())[0]:
        raise ReportError('You cannot write a report for a period that has not started yet.')
    for f in TEXT_FIELDS:
        setattr(report, f, (data.get(f) or '').strip()[:MAX_TEXT])
    if submit and len(report.work_done) < 20:
        raise ReportError('Describe what you did in a little more detail (at least a sentence) before submitting.')
    if not any(getattr(report, f) for f in TEXT_FIELDS) and not upload:
        raise ReportError('The report is empty.')
    if upload:
        import os
        ext = os.path.splitext(upload.name)[1].lower()
        if ext not in ALLOWED_EXT:
            raise ReportError('Attach a PDF, Word, Excel, PowerPoint, image or text file.')
        if upload.size > MAX_FILE_MB * 1024 * 1024:
            raise ReportError(f'The attachment is larger than {MAX_FILE_MB} MB.')
        report.attachment = upload
    elif remove_file and report.attachment:
        report.attachment.delete(save=False)
        report.attachment = None
    first_submission = submit and report.status == R.DRAFT
    if submit:
        report.status = R.SUBMITTED
        report.submitted_at = report.submitted_at or timezone.now()
    try:
        report.save()
    except IntegrityError:
        raise ReportError('A report for this period already exists — open it from My Work Reports.')
    if first_submission:
        _tell_admins(report)
    return report


def review(report, user, feedback=''):
    if report.status == R.DRAFT:
        raise ReportError('This report has not been submitted yet.')
    if report.staff.user_id == user.pk:
        raise ReportError('Someone else has to review your own report.')
    report.feedback = (feedback or '').strip()[:MAX_TEXT]
    report.status = R.REVIEWED
    report.reviewed_by, report.reviewed_at = user, timezone.now()
    report.save(update_fields=['feedback', 'status', 'reviewed_by', 'reviewed_at', 'updated_at'])
    _tell_staff(report)
    return report


# ─────────────────────────────────────────────────────────────────────────────
# Notifications
# ─────────────────────────────────────────────────────────────────────────────

def _tell_admins(report):
    from django.urls import reverse
    from notifications import services as notify
    notify.to_admins(report.tenant, 'report', f'{report.get_period_display()} report from {report.staff.full_name}',
                     body=label(report.period, report.period_start) + (' · submitted late' if report.late else ''),
                     url=reverse('hr:report', args=[report.pk]), actor=report.staff.user,
                     level='warning' if report.late else 'info', dedupe=f'report:{report.pk}:submitted')


def _tell_staff(report):
    from django.urls import reverse
    from notifications import services as notify
    notify.to_user(report.staff.user, 'report', f'Your {report.get_period_display().lower()} report was reviewed',
                   tenant=report.tenant, body=(report.feedback[:200] if report.feedback else
                                               label(report.period, report.period_start)),
                   url=reverse('hr:report', args=[report.pk]), actor=report.reviewed_by, level='success',
                   admin_feed=False)


# ─────────────────────────────────────────────────────────────────────────────
# The administrators' register
# ─────────────────────────────────────────────────────────────────────────────

def register(tenant, period, day, staff_qs=None):
    """One row per staff member for the period containing `day`."""
    from safety.models import StaffLeave
    from safety.staff_attendance import working_days
    start, end = bounds(period, day)
    today = timezone.localdate()
    school_days = working_days(start, end, tenant)
    closed = not school_days
    staff = list((staff_qs if staff_qs is not None else Staff.objects.filter(tenant=tenant))
                 .select_related('user').order_by('full_name'))
    reports = {r.staff_id: r for r in R.objects.filter(tenant=tenant, period=period, period_start=start)}
    on_leave = set()
    if school_days:
        for lv in StaffLeave.objects.filter(tenant=tenant, status=StaffLeave.APPROVED,
                                            start_date__lte=school_days[0], end_date__gte=school_days[-1]):
            on_leave.add(lv.staff_id)        # away for the whole period
    rows = []
    for s in staff:
        r = reports.get(s.id)
        if r and r.status == R.REVIEWED:
            state = 'reviewed'
        elif r and r.status == R.SUBMITTED:
            state = 'late' if r.late else 'submitted'
        elif s.id in on_leave:
            state = 'leave'
        elif closed:
            state = 'closed'
        elif start > today:
            state = 'upcoming'
        elif r:
            state = 'draft'
        else:
            state = 'overdue' if today > end + timedelta(days=1 if period == R.DAILY else 3) else 'missing'
        rows.append({'staff': s, 'report': r, 'state': state})
    counts = Counter(r['state'] for r in rows)
    counts['in'] = counts['submitted'] + counts['late'] + counts['reviewed']
    counts['to_review'] = counts['submitted'] + counts['late']
    counts['outstanding'] = counts['missing'] + counts['overdue'] + counts['draft']
    return rows, counts, closed, (start, end)
