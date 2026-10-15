"""
timetable/scheduling.py
───────────────────────
Timetable part 2: room booking with conflict checks, the free period finder,
timetable version history (save, compare, restore) and parent–teacher meeting
slots.
"""
from collections import defaultdict
from datetime import datetime, timedelta

from django.db import IntegrityError, transaction
from django.db.models import Max
from django.utils import timezone

from users.models import ENROLLED_STATUSES, Class, Staff, StaffSubjectClass, Student, Subject

from .lessons import DAY_CODES, published_timetable
from .models import (GeneratedTimetable, MeetingBooking, RoomBooking, TeacherUnavailability,
                     TeachingRoom, TimetableConfiguration, TimetableSlot, TimetableVersion, TimeSlot)

DAY_NAMES = dict(TimetableConfiguration.DAYS)
MAX_REPEAT_WEEKS = 15


class SchedulingError(ValueError):
    pass


def _overlaps(a_start, a_end, b_start, b_end):
    return a_start < b_end and b_start < a_end


def _fmt(t):
    return t.strftime('%H:%M')


# ─────────────────────────────────────────────────────────────────────────────
# Room booking
# ─────────────────────────────────────────────────────────────────────────────

def room_conflicts(room, day, start, end, exclude=None):
    """Everything already using `room` on `day` between start and end, as plain sentences."""
    out = []
    bookings = RoomBooking.objects.filter(tenant_id=room.tenant_id, room=room, date=day, cancelled=False,
                                          start_time__lt=end, end_time__gt=start)
    if exclude:
        bookings = bookings.exclude(pk=exclude)
    for b in bookings.select_related('booked_by'):
        out.append(f'Booked {_fmt(b.start_time)}–{_fmt(b.end_time)} for {b.purpose}.')
    tt = published_timetable(room.tenant)
    if tt and day.weekday() < 6:
        lessons = (TimetableSlot.objects.filter(timetable=tt, room=room, day=DAY_CODES[day.weekday()],
                                                time_slot__start_time__lt=end, time_slot__end_time__gt=start)
                   .select_related('school_class', 'subject', 'time_slot'))
        for e in lessons:
            out.append(f'{e.school_class.name} {e.subject.name} is timetabled there '
                       f'{_fmt(e.time_slot.start_time)}–{_fmt(e.time_slot.end_time)}.')
    return out


def book_room(room, day, start, end, purpose, user, repeat_weeks=1):
    """
    Book a room, optionally on the same weekday for several weeks.
    Returns (created bookings, [(date, reasons)] skipped because of a clash).
    """
    purpose = (purpose or '').strip()[:160]
    if not purpose:
        raise SchedulingError('Say what the room is booked for.')
    if not (start and end) or start >= end:
        raise SchedulingError('The end time must be after the start time.')
    if not room.active:
        raise SchedulingError(f'{room.name} is not in use.')
    repeat_weeks = max(1, min(int(repeat_weeks or 1), MAX_REPEAT_WEEKS))
    created, skipped = [], []
    with transaction.atomic():
        for week in range(repeat_weeks):
            d = day + timedelta(weeks=week)
            problems = room_conflicts(room, d, start, end)
            if problems:
                skipped.append((d, problems))
                continue
            created.append(RoomBooking.objects.create(tenant_id=room.tenant_id, room=room, date=d, start_time=start,
                                                      end_time=end, purpose=purpose, booked_by=user))
    return created, skipped


def day_plan(tenant, day):
    """For every active room: what is using it on `day` (lessons and bookings), in time order."""
    rooms = list(TeachingRoom.objects.filter(tenant=tenant, active=True))
    tt = published_timetable(tenant)
    plan = []
    for room in rooms:
        items = []
        if tt and day.weekday() < 6:
            for e in (TimetableSlot.objects.filter(timetable=tt, room=room, day=DAY_CODES[day.weekday()])
                      .select_related('school_class', 'subject', 'time_slot')):
                items.append({'start': e.time_slot.start_time, 'end': e.time_slot.end_time, 'kind': 'lesson',
                              'what': f'{e.school_class.name} · {e.subject.name}'})
        for b in RoomBooking.objects.filter(tenant=tenant, room=room, date=day, cancelled=False) \
                .select_related('booked_by'):
            items.append({'start': b.start_time, 'end': b.end_time, 'kind': 'booking', 'what': b.purpose,
                          'booking': b})
        items.sort(key=lambda i: i['start'])
        plan.append({'room': room, 'items': items})
    return plan


# ─────────────────────────────────────────────────────────────────────────────
# Free period finder
# ─────────────────────────────────────────────────────────────────────────────

def free_grid(timetable, teachers, room=None, on_date=None):
    """
    Week grid of the timetable's periods: for each (day, period), whether every
    chosen teacher (and the room) is free, and if not, who or what is busy.
    With `on_date`, only that weekday is shown and one-off room bookings count.
    """
    config = timetable.config
    days = [c for c, _ in TimetableConfiguration.DAYS if c in config.active_days]
    if on_date is not None:
        code = DAY_CODES[on_date.weekday()]
        days = [code] if code in days else []
    periods = list(config.get_active_slots())
    teacher_ids = [t.pk for t in teachers]
    names = {t.pk: t.full_name for t in teachers}
    busy = defaultdict(list)
    for e in TimetableSlot.objects.filter(timetable=timetable, teacher_id__in=teacher_ids, day__in=days) \
            .select_related('school_class'):
        busy[(e.day, e.time_slot_id)].append(f'{names[e.teacher_id]} teaches {e.school_class.name}')
    for u in TeacherUnavailability.objects.filter(config=config, staff_id__in=teacher_ids, day__in=days):
        busy[(u.day, u.time_slot_id)].append(f'{names[u.staff_id]} is unavailable')
    if room is not None:
        for e in TimetableSlot.objects.filter(timetable=timetable, room=room, day__in=days) \
                .select_related('school_class'):
            busy[(e.day, e.time_slot_id)].append(f'{room.name} is used by {e.school_class.name}')
        if on_date is not None and days:
            for b in RoomBooking.objects.filter(tenant_id=room.tenant_id, room=room, date=on_date, cancelled=False):
                for p in periods:
                    if _overlaps(b.start_time, b.end_time, p.start_time, p.end_time):
                        busy[(days[0], p.pk)].append(f'{room.name} is booked for {b.purpose}')
    rows, free_count = [], 0
    for p in periods:
        cells = []
        for d in days:
            reasons = busy.get((d, p.pk), [])
            free_count += not reasons
            cells.append({'day': d, 'free': not reasons, 'reasons': reasons})
        rows.append({'period': p, 'cells': cells})
    return {'days': [(d, DAY_NAMES[d]) for d in days], 'rows': rows, 'free_count': free_count}


# ─────────────────────────────────────────────────────────────────────────────
# Version history
# ─────────────────────────────────────────────────────────────────────────────

def snapshot(timetable):
    out = []
    for e in timetable.slots.select_related('school_class', 'subject', 'teacher', 'time_slot', 'room') \
            .order_by('school_class__name', 'day', 'time_slot__order'):
        out.append({'c': e.school_class_id, 'cn': e.school_class.name, 'd': e.day, 's': e.time_slot_id,
                    'sn': e.time_slot.name, 'so': e.time_slot.order, 'sub': e.subject_id, 'subn': e.subject.name,
                    't': e.teacher_id, 'tn': e.teacher.full_name, 'r': e.room_id,
                    'rn': e.room.name if e.room_id else ''})
    return out


def save_version(timetable, user=None, label=''):
    entries = snapshot(timetable)
    with transaction.atomic():
        last = TimetableVersion.objects.filter(config=timetable.config).aggregate(n=Max('number'))['n'] or 0
        return TimetableVersion.objects.create(
            tenant_id=timetable.tenant_id, config=timetable.config, number=last + 1, label=(label or '')[:120],
            source=timetable, entries=entries, entry_count=len(entries), saved_by=user)


def _describe(e):
    if not e:
        return ''
    room = f' · {e["rn"]}' if e.get('rn') else ''
    return f'{e["subn"]} ({e["tn"]}){room}'


def compare(old_entries, new_entries):
    """Lesson-by-lesson differences between two snapshots."""
    old = {(e['c'], e['d'], e['s']): e for e in old_entries}
    new = {(e['c'], e['d'], e['s']): e for e in new_entries}
    rows = []
    for key in set(old) | set(new):
        a, b = old.get(key), new.get(key)
        if a and b and (a['sub'], a['t'], a.get('r')) == (b['sub'], b['t'], b.get('r')):
            continue
        ref = b or a
        kind = 'added' if not a else 'removed' if not b else 'changed'
        rows.append({'kind': kind, 'class': ref['cn'], 'day': DAY_NAMES.get(ref['d'], ref['d']),
                     'day_order': DAY_CODES.index(ref['d']) if ref['d'] in DAY_CODES else 9,
                     'period': ref['sn'], 'period_order': ref.get('so', 0),
                     'before': _describe(a), 'after': _describe(b)})
    rows.sort(key=lambda r: (r['class'], r['day_order'], r['period_order']))
    return rows


def restore(version, user=None):
    """
    Bring an old version back as a new published timetable. Lessons whose
    class, subject, teacher or period has since been deleted are skipped.
    Returns (timetable, skipped count).
    """
    tid = version.tenant_id
    classes = set(Class.objects.filter(tenant_id=tid).values_list('pk', flat=True))
    subjects = set(Subject.objects.filter(tenant_id=tid).values_list('pk', flat=True))
    staff = set(Staff.objects.filter(tenant_id=tid).values_list('pk', flat=True))
    slots = set(TimeSlot.objects.filter(tenant_id=tid).values_list('pk', flat=True))
    rooms = set(TeachingRoom.objects.filter(tenant_id=tid).values_list('pk', flat=True))
    skipped = 0
    with transaction.atomic():
        tt = GeneratedTimetable.objects.create(tenant_id=tid, config=version.config,
                                               notes=f'Restored from version {version.number}')
        seen_class, seen_teacher = set(), set()
        for e in version.entries:
            ok = e['c'] in classes and e['sub'] in subjects and e['t'] in staff and e['s'] in slots
            kc, kt = (e['c'], e['d'], e['s']), (e['t'], e['d'], e['s'])
            if not ok or kc in seen_class or kt in seen_teacher:
                skipped += 1
                continue
            seen_class.add(kc)
            seen_teacher.add(kt)
            TimetableSlot.objects.create(tenant_id=tid, timetable=tt, school_class_id=e['c'], day=e['d'],
                                         time_slot_id=e['s'], subject_id=e['sub'], teacher_id=e['t'],
                                         room_id=e['r'] if e.get('r') in rooms else None)
        tt.publish()
        save_version(tt, user, label=f'Restored version {version.number}')
    return tt, skipped


# ─────────────────────────────────────────────────────────────────────────────
# Parent–teacher meeting slots
# ─────────────────────────────────────────────────────────────────────────────

def slot_times(evening):
    """Start times of every slot in the session."""
    step = timedelta(minutes=max(1, evening.slot_minutes))
    t = datetime.combine(evening.date, evening.start_time)
    end = datetime.combine(evening.date, evening.end_time)
    out = []
    while t + step <= end:
        out.append(t.time())
        t += step
    return out


def end_of(evening, start):
    return (datetime.combine(evening.date, start) + timedelta(minutes=evening.slot_minutes)).time()


def evening_covers(evening, school_class):
    if school_class is None:
        return False
    ids = list(evening.classes.values_list('pk', flat=True))
    return not ids or school_class.pk in ids


def teachers_for(evening, student):
    """Teachers a parent can meet for this child, with the subjects they teach the child: [(staff, label)]."""
    cls = student.class_assigned
    if not evening_covers(evening, cls):
        return []
    subjects = defaultdict(set)
    tt = published_timetable(student.tenant)
    if tt:
        for e in TimetableSlot.objects.filter(timetable=tt, school_class=cls).select_related('subject'):
            subjects[e.teacher_id].add(e.subject.name)
    for ssc in StaffSubjectClass.objects.filter(tenant_id=student.tenant_id, school_class=cls) \
            .select_related('subject'):
        subjects[ssc.staff_id].add(ssc.subject.name)
    if cls.form_teacher_id:
        subjects[cls.form_teacher_id].add('Form teacher')
    allowed = set(evening.teachers.values_list('pk', flat=True))
    ids = [i for i in subjects if not allowed or i in allowed]
    staff = Staff.objects.filter(tenant_id=student.tenant_id, pk__in=ids).order_by('full_name')
    return [(s, ', '.join(sorted(subjects[s.pk], key=lambda x: (x != 'Form teacher', x)))) for s in staff]


def free_times(evening, teacher):
    taken = set(MeetingBooking.objects.filter(evening=evening, teacher=teacher).values_list('start_time', flat=True))
    return [t for t in slot_times(evening) if t not in taken]


def family_of(student):
    """The child and their siblings (same parent email), as the school's parent portal treats them."""
    qs = Student.objects.filter(tenant_id=student.tenant_id, status__in=ENROLLED_STATUSES)
    if student.parent_email:
        return qs.filter(parent_email=student.parent_email)
    return qs.filter(pk=student.pk)


def book_meeting(evening, teacher, student, start, user=None, note=''):
    if not evening.is_open:
        raise SchedulingError('Booking for this meeting is closed.')
    if evening.date < timezone.localdate():
        raise SchedulingError('This meeting has already taken place.')
    if teacher.pk not in {s.pk for s, _ in teachers_for(evening, student)}:
        raise SchedulingError(f'{teacher.full_name} is not meeting parents of {student.full_name} at this session.')
    if start not in slot_times(evening):
        raise SchedulingError('Pick one of the listed times.')
    if MeetingBooking.objects.filter(evening=evening, teacher=teacher, student=student).exists():
        raise SchedulingError(f'You already have a slot with {teacher.full_name} for {student.full_name}.')
    family = family_of(student)
    clash = MeetingBooking.objects.filter(evening=evening, student__in=family, start_time=start) \
        .select_related('teacher').first()
    if clash:
        raise SchedulingError(f'You are already seeing {clash.teacher.full_name} at {_fmt(start)}.')
    try:
        with transaction.atomic():
            return MeetingBooking.objects.create(tenant_id=evening.tenant_id, evening=evening, teacher=teacher,
                                                 student=student, start_time=start, booked_by=user,
                                                 note=(note or '').strip()[:255])
    except IntegrityError:
        raise SchedulingError(f'Someone has just taken {_fmt(start)} with {teacher.full_name}. Pick another time.')


def booking_details(b):
    """Everything the confirmation email needs, captured so it still works after a booking is deleted."""
    ev = b.evening
    return {'parent_email': b.student.parent_email or '', 'parent_name': b.student.parent_name or 'Parent',
            'teacher_email': getattr(b.teacher.user, 'email', '') or '', 'teacher': b.teacher.full_name,
            'student': b.student.full_name, 'title': ev.title, 'location': ev.location, 'school': b.tenant.name,
            'when': f'{ev.date:%A %d %B} at {_fmt(b.start_time)}–{_fmt(end_of(ev, b.start_time))}'}


def send_booking_email(d, cancelled=False):
    from django.utils.html import escape as e
    from communications.services import send_email
    verb = 'cancelled' if cancelled else 'booked'
    where = f' in {e(d["location"])}' if d['location'] else ''
    if d['parent_email']:
        send_email(d['parent_email'], f'Meeting {verb}: {d["teacher"]}',
                   f'<p>Dear {e(d["parent_name"])},</p>'
                   f'<p>Your meeting with <b>{e(d["teacher"])}</b> about {e(d["student"])} '
                   f'on <b>{e(d["when"])}</b>{where} has been {verb}.</p><p>{e(d["title"])} — {e(d["school"])}</p>')
    if d['teacher_email']:
        send_email(d['teacher_email'], f'Parent meeting {verb}: {d["student"]}',
                   f'<p>{e(d["student"])}\'s parent has {verb} {e(d["when"])} with you ({e(d["title"])}).</p>')


def notify_booking(booking_id):
    """Email the parent and the teacher that a slot was booked. Runs in the background."""
    b = MeetingBooking.objects.filter(pk=booking_id).select_related(
        'evening', 'teacher__user', 'student', 'tenant').first()
    if b:
        send_booking_email(booking_details(b))


def cancel_booking(booking):
    """Remove a booking now and tell the parent and teacher in the background."""
    from lms_project.tasks import run_in_background
    details = booking_details(booking)
    tenant = booking.tenant
    booking.delete()
    run_in_background(send_booking_email, details, True, tenant=tenant)


def teacher_schedule(evening, teacher):
    """Every slot for one teacher: (start, end, booking or None)."""
    by_time = {b.start_time: b for b in MeetingBooking.objects.filter(evening=evening, teacher=teacher)
               .select_related('student__class_assigned')}
    return [{'start': t, 'end': end_of(evening, t), 'booking': by_time.get(t)} for t in slot_times(evening)]


def evening_teachers(evening):
    """Teachers taking part: the chosen list, or everyone who teaches one of the evening's classes."""
    chosen = list(evening.teachers.all().order_by('full_name'))
    if chosen:
        return chosen
    class_ids = list(evening.classes.values_list('pk', flat=True))
    ids = set()
    tt = published_timetable(evening.tenant)
    lessons = TimetableSlot.objects.filter(timetable=tt) if tt else TimetableSlot.objects.none()
    ssc = StaffSubjectClass.objects.filter(tenant_id=evening.tenant_id)
    forms = Class.objects.filter(tenant_id=evening.tenant_id, form_teacher__isnull=False)
    if class_ids:
        lessons, ssc, forms = (lessons.filter(school_class_id__in=class_ids), ssc.filter(school_class_id__in=class_ids),
                               forms.filter(pk__in=class_ids))
    ids |= set(lessons.values_list('teacher_id', flat=True))
    ids |= set(ssc.values_list('staff_id', flat=True))
    ids |= set(forms.values_list('form_teacher_id', flat=True))
    return list(Staff.objects.filter(tenant_id=evening.tenant_id, pk__in=ids).order_by('full_name'))
