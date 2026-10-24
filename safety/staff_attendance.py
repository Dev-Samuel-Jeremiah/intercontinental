"""
safety/staff_attendance.py
──────────────────────────
Staff attendance that knows about leave: a day is "present" when the staff
member clocked in at reception, "on leave" when approved leave covers it,
a public holiday or weekend when the school is closed, and "absent" only
when none of those apply.

The school sets the working hours and days (Staff attendance → Times & days).
Clocking in on a weekend or public holiday is a "visit": recorded, never late
or absent, and not counted as a working day. A school can also refuse
clock-ins on those days.
"""
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

from django.db import transaction
from django.utils import timezone

from academics.models import PublicHoliday
from idcards.models import StaffClockLog
from safety.models import StaffLeave
from users.models import Staff


class LeaveError(ValueError):
    pass


class ClockClosed(ValueError):
    """Clocking in is not allowed today (a day off, and the school does not take off-day clock-ins)."""


DAY_NAMES = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']


def hours(tenant):
    from safety.services import settings_for
    return settings_for(tenant)


def _workdays(cfg):
    return {int(c) for c in (cfg.working_days or '') if c.isdigit() and int(c) < 7}


def working_days(start, end, tenant):
    holidays = set(PublicHoliday.objects.filter(tenant=tenant, date__gte=start, date__lte=end)
                   .values_list('date', flat=True))
    workdays = _workdays(hours(tenant))
    day, out = start, []
    while day <= end:
        if day.weekday() in workdays and day not in holidays:
            out.append(day)
        day += timedelta(days=1)
    return out


def off_day(tenant, day):
    """'Holiday' or 'Weekend' when staff are not due at work on `day`, else ''."""
    holiday = PublicHoliday.objects.filter(tenant=tenant, date=day).first()
    if holiday:
        return 'Holiday'
    return '' if day.weekday() in _workdays(hours(tenant)) else 'Weekend'


def late_after(cfg, day):
    return (datetime.combine(day, cfg.staff_starts_at) + timedelta(minutes=cfg.staff_late_grace_minutes or 0)).time()


def check_clock_in(staff, direction):
    """Refuse a clock-in on a day off when the school does not take them. Clocking out is always allowed."""
    if direction != 'IN':
        return
    today = timezone.localdate()
    kind = off_day(staff.tenant, today)
    if kind and not hours(staff.tenant).off_day_clock_in:
        what = 'a public holiday' if kind == 'Holiday' else f'{DAY_NAMES[today.weekday()]}, not a working day'
        raise ClockClosed(f'Today is {what}, and the school does not take clock-ins on days off.')


def request_leave(staff, leave_type, start, end, reason='', user=None):
    if leave_type not in dict(StaffLeave.TYPES):
        raise LeaveError('Choose the kind of leave.')
    if end < start:
        raise LeaveError('Leave cannot end before it starts.')
    if (end - start).days > 365:
        raise LeaveError('That is more than a year — check the dates.')
    if start < timezone.localdate() - timedelta(days=30):
        raise LeaveError('Leave more than 30 days in the past must be recorded by an administrator.')
    clash = StaffLeave.objects.filter(tenant_id=staff.tenant_id, staff=staff,
                                      status__in=(StaffLeave.PENDING, StaffLeave.APPROVED),
                                      start_date__lte=end, end_date__gte=start).first()
    if clash:
        raise LeaveError(f'This overlaps leave already requested for '
                         f'{clash.start_date:%d %b}–{clash.end_date:%d %b}.')
    return StaffLeave.objects.create(tenant_id=staff.tenant_id, staff=staff, leave_type=leave_type,
                                     start_date=start, end_date=end, reason=(reason or '').strip()[:255],
                                     requested_by=user)


def review(leave, approve, user, note=''):
    if leave.status != StaffLeave.PENDING:
        raise LeaveError('That request was already decided.')
    if leave.staff.user_id == getattr(user, 'id', None):
        raise LeaveError('Someone else has to approve your own leave.')
    leave.status = StaffLeave.APPROVED if approve else StaffLeave.REJECTED
    leave.reviewed_by, leave.reviewed_at = user, timezone.now()
    leave.review_note = (note or '').strip()[:255]
    leave.save(update_fields=['status', 'reviewed_by', 'reviewed_at', 'review_note'])
    return leave


def cancel(leave):
    if leave.status not in (StaffLeave.PENDING, StaffLeave.APPROVED):
        raise LeaveError('That leave cannot be cancelled.')
    leave.status = StaffLeave.CANCELLED
    leave.save(update_fields=['status'])
    return leave


def _leave_by_day(tenant, start, end):
    out = {}
    for lv in StaffLeave.objects.filter(tenant=tenant, status=StaffLeave.APPROVED,
                                        start_date__lte=end, end_date__gte=start):
        day = max(lv.start_date, start)
        while day <= min(lv.end_date, end):
            out[(lv.staff_id, day)] = lv
            day += timedelta(days=1)
    return out


def _first_in(tenant, start, end):
    out = {}
    for log in (StaffClockLog.objects.filter(tenant=tenant, direction='IN',
                                             scanned_at__date__gte=start, scanned_at__date__lte=end)
                .order_by('scanned_at')):
        key = (log.staff_id, timezone.localtime(log.scanned_at).date())
        out.setdefault(key, timezone.localtime(log.scanned_at))
    return out


def _day_scans(tenant, day):
    """Every clock scan on `day`, by staff id, oldest first."""
    out = defaultdict(list)
    for log in StaffClockLog.objects.filter(tenant=tenant, scanned_at__date=day).order_by('scanned_at'):
        out[log.staff_id].append(log)
    return out


def _time_on_site(scans, until=None):
    """Total of the IN→OUT spells in `scans`. A spell still open counts up to `until`
    (now, when looking at today); on a past day it is left out."""
    total, opened = timedelta(), None
    for log in scans:
        if log.direction == 'IN' and opened is None:
            opened = log.scanned_at
        elif log.direction == 'OUT' and opened is not None:
            total += log.scanned_at - opened
            opened = None
    if opened is not None and until is not None:
        total += max(until - opened, timedelta())
    return total


def _hours_label(spent):
    minutes = int(spent.total_seconds() // 60)
    return f'{minutes // 60}h {minutes % 60:02d}m' if minutes else ''


def day_report(tenant, day):
    """One row per staff member for `day`: state (present / late / leave / absent /
    closed / upcoming), first clock-in, clock-out, time on site and whether they are
    still on site (today) or never clocked out (past days)."""
    cfg = hours(tenant)
    due_by = late_after(cfg, day)
    today = timezone.localdate()
    closed = day not in working_days(day, day, tenant)
    kind = off_day(tenant, day) if closed else ''
    leave = _leave_by_day(tenant, day, day)
    scans_by_staff = _day_scans(tenant, day)
    now = timezone.now()
    rows = []
    for s in Staff.objects.filter(tenant=tenant).order_by('full_name'):
        scans = scans_by_staff.get(s.id, [])
        first_in = next((log for log in scans if log.direction == 'IN'), None)
        arrived = timezone.localtime(first_in.scanned_at) if first_in else None
        last = scans[-1] if scans else None
        left = timezone.localtime(last.scanned_at) if last and last.direction == 'OUT' else None
        lv = leave.get((s.id, day))
        if arrived and closed:
            state = 'visit'                       # came in on a weekend or holiday: a visit, not a working day
        elif arrived:
            state = 'late' if arrived.time() > due_by else 'present'
        elif lv:
            state = 'leave'
        elif closed:
            state = 'closed'
        elif day > today:
            state = 'upcoming'
        else:
            state = 'absent'
        still_in = bool(last and last.direction == 'IN')
        spent = _time_on_site(scans, until=now if day == today else None)
        rows.append({'staff': s, 'state': state, 'arrived': arrived, 'leave': lv, 'visit_kind': kind,
                     'left': left, 'left_early': bool(left and not closed and left.time() < cfg.staff_ends_at),
                     'on_site': still_in and day == today,
                     'no_clock_out': still_in and day < today,
                     'hours': _hours_label(spent), 'scans': len(scans),
                     'method': first_in.get_method_display() if first_in else ''})
    counts = Counter(r['state'] for r in rows)
    counts['on_site'] = sum(1 for r in rows if r['on_site'])
    counts['clocked_out'] = sum(1 for r in rows if r['left'])
    counts['no_clock_out'] = sum(1 for r in rows if r['no_clock_out'])
    counts['left_early'] = sum(1 for r in rows if r['left_early'])
    return rows, counts, closed


def month_summary(tenant, first, last):
    days = [d for d in working_days(first, last, tenant) if d <= timezone.localdate()]
    workdays = set(days)
    leave = _leave_by_day(tenant, first, last)
    arrivals = _first_in(tenant, first, last)
    out = []
    for s in Staff.objects.filter(tenant=tenant).order_by('full_name'):
        present = sum(1 for d in days if (s.id, d) in arrivals)
        on_leave = sum(1 for d in days if (s.id, d) not in arrivals and (s.id, d) in leave)
        visits = sum(1 for (sid, d) in arrivals if sid == s.id and d not in workdays)
        out.append({'staff': s, 'days': len(days), 'present': present, 'leave': on_leave, 'visits': visits,
                    'absent': len(days) - present - on_leave,
                    'rate': round(100 * present / (len(days) - on_leave)) if len(days) - on_leave else None})
    return out, len(days)


def days_taken(staff, year):
    """Working days of approved leave this year, by type."""
    start, end = date(year, 1, 1), date(year, 12, 31)
    totals = defaultdict(int)
    for lv in StaffLeave.objects.filter(tenant_id=staff.tenant_id, staff=staff, status=StaffLeave.APPROVED,
                                        start_date__lte=end, end_date__gte=start):
        totals[lv.get_leave_type_display()] += len(working_days(max(lv.start_date, start),
                                                                min(lv.end_date, end), staff.tenant))
    return dict(totals)
