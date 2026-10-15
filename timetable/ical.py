"""
timetable/ical.py
─────────────────
Calendar feeds (iCalendar / .ics) that teachers, parents and students add to
Google Calendar, Apple Calendar or Outlook. Each person gets a private link
with a random token; resetting it switches the old link off.

What is in a feed:
  teachers  — their weekly lessons (repeating until the end of term, skipping
              public holidays), parent meetings, their room bookings, staff
              and whole-school events
  parents   — parent meetings for their children and school events
  students  — their class's weekly lessons and school events
"""
import secrets
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.utils import timezone

from .models import CalendarFeed, ExtraLessonEnrolment, ExtraLessonGroup, MeetingBooking, RoomBooking, TimetableSlot
from .lessons import DAY_CODES, published_timetable

DEFAULT_WEEKS = 16


# ─────────────────────────────────────────────────────────────────────────────
# Tokens
# ─────────────────────────────────────────────────────────────────────────────

def feed_for(user, tenant):
    feed = CalendarFeed.objects.filter(tenant=tenant, user=user).first()
    if not feed:
        feed = CalendarFeed.objects.create(tenant=tenant, user=user, token=secrets.token_urlsafe(32))
    return feed


def reset_feed(user, tenant):
    CalendarFeed.objects.filter(tenant=tenant, user=user).delete()
    return feed_for(user, tenant)


# ─────────────────────────────────────────────────────────────────────────────
# Building the file
# ─────────────────────────────────────────────────────────────────────────────

def _esc(text):
    return (str(text or '').replace('\\', '\\\\').replace(';', '\\;').replace(',', '\\,')
            .replace('\r\n', '\\n').replace('\n', '\\n'))


def _fold(line):
    """RFC 5545: lines longer than 75 octets continue on the next line after a space."""
    raw = line.encode('utf-8')
    if len(raw) <= 75:
        return line
    parts, chunk = [], b''
    for ch in line:
        b = ch.encode('utf-8')
        if len(chunk) + len(b) > (75 if not parts else 74):
            parts.append(chunk.decode('utf-8'))
            chunk = b''
        chunk += b
    parts.append(chunk.decode('utf-8'))
    return '\r\n '.join(parts)


def _local(d, t):
    return datetime.combine(d, t).strftime('%Y%m%dT%H%M%S')


def _offset(tz):
    off = timezone.now().astimezone(tz).utcoffset() or timedelta(0)
    minutes = int(off.total_seconds() // 60)
    sign = '+' if minutes >= 0 else '-'
    minutes = abs(minutes)
    return f'{sign}{minutes // 60:02d}{minutes % 60:02d}'


class Calendar:
    def __init__(self, name, tenant):
        self.tzid = settings.TIME_ZONE
        self.name = name
        self.tenant = tenant
        self.events = []
        self.stamp = timezone.now().astimezone(ZoneInfo('UTC')).strftime('%Y%m%dT%H%M%SZ')

    def add(self, uid, summary, start_date, start_time=None, end_date=None, end_time=None, location='',
            description='', rrule='', exdates=()):
        lines = ['BEGIN:VEVENT', f'UID:{uid}@{self.tenant.subdomain}.school', f'DTSTAMP:{self.stamp}',
                 f'SUMMARY:{_esc(summary)}']
        if start_time is None:
            lines.append(f'DTSTART;VALUE=DATE:{start_date:%Y%m%d}')
            lines.append(f'DTEND;VALUE=DATE:{(end_date or start_date) + timedelta(days=1):%Y%m%d}')
        else:
            lines.append(f'DTSTART;TZID={self.tzid}:{_local(start_date, start_time)}')
            lines.append(f'DTEND;TZID={self.tzid}:{_local(end_date or start_date, end_time or start_time)}')
        if rrule:
            lines.append(f'RRULE:{rrule}')
        for d in exdates:
            lines.append(f'EXDATE;TZID={self.tzid}:{_local(d, start_time)}')
        if location:
            lines.append(f'LOCATION:{_esc(location)}')
        if description:
            lines.append(f'DESCRIPTION:{_esc(description)}')
        lines.append('END:VEVENT')
        self.events.append(lines)

    def render(self):
        tz = ZoneInfo(self.tzid)
        off = _offset(tz)
        lines = ['BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//School Portal//Timetable//EN', 'CALSCALE:GREGORIAN',
                 'METHOD:PUBLISH', f'X-WR-CALNAME:{_esc(self.name)}', f'X-WR-TIMEZONE:{self.tzid}',
                 'REFRESH-INTERVAL;VALUE=DURATION:PT6H', 'X-PUBLISHED-TTL:PT6H',
                 'BEGIN:VTIMEZONE', f'TZID:{self.tzid}', 'BEGIN:STANDARD', 'DTSTART:19700101T000000',
                 f'TZOFFSETFROM:{off}', f'TZOFFSETTO:{off}', 'END:STANDARD', 'END:VTIMEZONE']
        for ev in self.events:
            lines.extend(ev)
        lines.append('END:VCALENDAR')
        return '\r\n'.join(_fold(line) for line in lines) + '\r\n'


# ─────────────────────────────────────────────────────────────────────────────
# What goes in
# ─────────────────────────────────────────────────────────────────────────────

def term_window(tenant):
    """(first day, last day) the weekly lessons repeat over: the active term, or the next 16 weeks."""
    from academics.models import Term
    today = timezone.localdate()
    term = Term.objects.filter(tenant=tenant, is_active=True).first()
    start = (term.start_date if term and term.start_date else None) or today - timedelta(days=7)
    end = (term.end_date if term and term.end_date else None) or start + timedelta(weeks=DEFAULT_WEEKS)
    if end < today:
        start, end = today - timedelta(days=7), today + timedelta(weeks=DEFAULT_WEEKS)
    return start, end


def _add_lessons(cal, tenant, lessons, title):
    from academics.models import PublicHoliday
    start, end = term_window(tenant)
    holidays = set(PublicHoliday.objects.filter(tenant=tenant, date__gte=start, date__lte=end)
                   .values_list('date', flat=True))
    until = (datetime.combine(end, datetime.max.time().replace(microsecond=0))
             .replace(tzinfo=ZoneInfo(settings.TIME_ZONE)).astimezone(ZoneInfo('UTC')).strftime('%Y%m%dT%H%M%SZ'))
    for e in lessons:
        weekday = DAY_CODES.index(e.day)
        first = start + timedelta(days=(weekday - start.weekday()) % 7)
        if first > end:
            continue
        skip = sorted(d for d in holidays if d.weekday() == weekday)
        room = e.room.name if e.room_id else ''
        cal.add(f'lesson-{e.pk}', title(e), first, e.time_slot.start_time, end_time=e.time_slot.end_time,
                location=room, rrule=f'FREQ=WEEKLY;UNTIL={until}', exdates=skip,
                description=f'{e.time_slot.name} · {e.subject.name} · {e.school_class.name} · {e.teacher.full_name}')


def _add_extras(cal, tenant, groups):
    """After-school groups as weekly events over the term window."""
    start, end = term_window(tenant)
    until = (datetime.combine(end, datetime.max.time().replace(microsecond=0))
             .replace(tzinfo=ZoneInfo(settings.TIME_ZONE)).astimezone(ZoneInfo('UTC')).strftime('%Y%m%dT%H%M%SZ'))
    for g in groups:
        weekday = DAY_CODES.index(g.day)
        first = start + timedelta(days=(weekday - start.weekday()) % 7)
        if first > end:
            continue
        cal.add(f'extra-{g.pk}', g.name, first, g.start_time, end_time=g.end_time,
                location=g.room.name if g.room_id else '', rrule=f'FREQ=WEEKLY;UNTIL={until}',
                description='After-school lessons' + (f' with {g.teacher.full_name}' if g.teacher_id else ''))


def _add_events(cal, tenant, audiences):
    from curriculum.models import CalendarEvent
    since = timezone.localdate() - timedelta(days=60)
    for ev in CalendarEvent.objects.filter(tenant=tenant, audience__in=audiences, start_date__gte=since):
        if ev.start_time and not ev.end_date:
            end = ev.end_time or (datetime.combine(ev.start_date, ev.start_time) + timedelta(hours=1)).time()
            if end <= ev.start_time:
                end = ev.start_time.replace(hour=23, minute=59)
            cal.add(f'event-{ev.pk}', ev.title, ev.start_date, ev.start_time, end_time=end, location=ev.venue,
                    description=ev.description or ev.get_kind_display())
            continue
        cal.add(f'event-{ev.pk}', ev.title, ev.start_date, end_date=ev.end_date or ev.start_date,
                location=ev.venue, description=ev.description or ev.get_kind_display())


def _add_meetings(cal, bookings, for_parent):
    from .scheduling import end_of
    for b in bookings.select_related('evening', 'teacher', 'student'):
        ev = b.evening
        title = (f'Meeting: {b.teacher.full_name} ({b.student.full_name})' if for_parent
                 else f'Parent meeting: {b.student.full_name}')
        cal.add(f'meeting-{b.pk}', title, ev.date, b.start_time, end_time=end_of(ev, b.start_time),
                location=ev.location, description=ev.title)


def parent_children(user, tenant):
    """The same rule the parent portal uses: parent_<id> accounts and siblings sharing the parent email."""
    from users.models import Student
    qs = Student.objects.filter(tenant=tenant)
    if user.username.startswith('parent_'):
        try:
            primary = qs.get(pk=int(user.username.split('_', 1)[1]))
        except (ValueError, Student.DoesNotExist):
            return qs.none()
        return qs.filter(parent_email=primary.parent_email) if primary.parent_email else qs.filter(pk=primary.pk)
    return qs.filter(parent_email=user.email) if user.email else qs.none()


def build(user, tenant):
    """The .ics text for this person."""
    staff = user.staff_profiles.filter(tenant=tenant).first()
    student = user.student_profiles.filter(tenant=tenant).first()
    cal = Calendar(f'{tenant.name} — {user.get_full_name() or user.username}', tenant)
    tt = published_timetable(tenant)
    lessons = TimetableSlot.objects.none()
    if tt:
        lessons = TimetableSlot.objects.filter(timetable=tt).select_related('school_class', 'subject', 'teacher',
                                                                            'time_slot', 'room')
    if staff:
        cal.name = f'{tenant.name} — {staff.full_name}'
        _add_lessons(cal, tenant, lessons.filter(teacher=staff),
                     lambda e: f'{e.subject.name} — {e.school_class.name}')
        _add_meetings(cal, MeetingBooking.objects.filter(tenant=tenant, teacher=staff), for_parent=False)
        for b in RoomBooking.objects.filter(tenant=tenant, booked_by=user, cancelled=False,
                                            date__gte=timezone.localdate() - timedelta(days=30)).select_related('room'):
            cal.add(f'room-{b.pk}', f'{b.room.name}: {b.purpose}', b.date, b.start_time, end_time=b.end_time,
                    location=b.room.name)
        _add_extras(cal, tenant, ExtraLessonGroup.objects.filter(tenant=tenant, teacher=staff, active=True)
                    .select_related('room', 'teacher'))
        _add_events(cal, tenant, ['ALL', 'STAFF'])
    elif student:
        cal.name = f'{tenant.name} — {student.full_name}'
        if student.class_assigned_id:
            _add_lessons(cal, tenant, lessons.filter(school_class_id=student.class_assigned_id),
                         lambda e: e.subject.name)
        _add_extras(cal, tenant, ExtraLessonGroup.objects.filter(
            tenant=tenant, active=True, pk__in=ExtraLessonEnrolment.objects.filter(student=student)
            .values('group')).select_related('room', 'teacher'))
        _add_events(cal, tenant, ['ALL', 'PARENTS'])
    else:
        children = parent_children(user, tenant) if user.is_parent else []
        if children:
            _add_meetings(cal, MeetingBooking.objects.filter(tenant=tenant, student__in=children), for_parent=True)
        _add_events(cal, tenant, ['ALL', 'PARENTS'] if user.is_parent else ['ALL'])
    return cal.render()
