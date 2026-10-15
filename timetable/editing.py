"""
timetable/editing.py
────────────────────
Moving lessons by hand (with clash checks, swapping when the target period is
taken), telling the teachers affected, teacher-load and clash reports, and
pausing normal lessons while a class sits its exams.
"""
from collections import Counter, defaultdict

from django.db import transaction
from django.utils import timezone

from .models import TeacherUnavailability, TimetableChange, TimetableSlot, TimeSlot

HEAVY_WEEK = 30          # periods a week
HEAVY_DAY = 7            # periods in one day


class EditError(ValueError):
    pass


def _busy(timetable, day, time_slot, exclude=()):
    return TimetableSlot.objects.filter(timetable=timetable, day=day, time_slot=time_slot).exclude(pk__in=exclude)


def problems_for(entry, day, time_slot, ignore=()):
    """Why `entry` can't go to (day, time_slot), ignoring the lessons in `ignore`."""
    out = []
    tt = entry.timetable
    if time_slot.is_break:
        out.append(f'{time_slot.name} is a break.')
    if day not in tt.config.active_days:
        out.append('The school is closed that day.')
    others = _busy(tt, day, time_slot, exclude=[entry.pk, *ignore])
    clash = others.filter(teacher=entry.teacher).select_related('school_class').first()
    if clash:
        out.append(f'{entry.teacher.full_name} already teaches {clash.school_class.name} then.')
    if TeacherUnavailability.objects.filter(config=tt.config, staff=entry.teacher, day=day, time_slot=time_slot).exists():
        out.append(f'{entry.teacher.full_name} is unavailable then.')
    if entry.room_id and others.filter(room_id=entry.room_id).exists():
        out.append(f'{entry.room.name} is in use then.')
    return out


@transaction.atomic
def move(entry, day, time_slot, user=None, notify=True):
    """
    Move a lesson. If the class already has a lesson in the target period,
    the two swap places — as long as neither ends up clashing.
    """
    tt = entry.timetable
    if (entry.day, entry.time_slot_id) == (day, time_slot.id):
        return []
    occupant = TimetableSlot.objects.filter(timetable=tt, school_class=entry.school_class, day=day,
                                            time_slot=time_slot).exclude(pk=entry.pk).first()
    issues = problems_for(entry, day, time_slot, ignore=[occupant.pk] if occupant else [])
    if occupant:
        issues += [f'Swapping {occupant.subject.name}: ' + p
                   for p in problems_for(occupant, entry.day, entry.time_slot, ignore=[entry.pk])]
    if issues:
        raise EditError(' '.join(issues))
    from_day, from_slot = entry.day, entry.time_slot
    changed = []
    # Park the moving lesson so the unique (class, day, period) constraint isn't tripped mid-swap.
    if occupant:
        parked = TimeSlot.objects.filter(tenant_id=entry.tenant_id).exclude(
            pk__in=TimetableSlot.objects.filter(timetable=tt, school_class=entry.school_class, day=from_day)
            .values('time_slot')).exclude(pk__in=TimetableSlot.objects.filter(timetable=tt, teacher=entry.teacher,
                                                                             day=from_day).values('time_slot')).first()
        if not parked:
            raise EditError('Could not swap those lessons — try moving one to a free period first.')
        entry.time_slot = parked
        entry.save(update_fields=['time_slot'])
        occupant.day, occupant.time_slot = from_day, from_slot
        occupant.save(update_fields=['day', 'time_slot'])
        changed.append(_log(occupant, day, time_slot, from_day, from_slot, user))
    entry.day, entry.time_slot = day, time_slot
    entry.save(update_fields=['day', 'time_slot'])
    changed.insert(0, _log(entry, from_day, from_slot, day, time_slot, user))
    if notify and tt.is_published:
        from lms_project.tasks import run_in_background
        run_in_background(_notify, [c.id for c in changed], tenant=tt.config.tenant)
    return changed


def _log(entry, from_day, from_slot, to_day, to_slot, user):
    return TimetableChange.objects.create(
        tenant_id=entry.tenant_id, timetable=entry.timetable, school_class=entry.school_class, subject=entry.subject,
        teacher=entry.teacher, from_day=from_day, from_slot=from_slot, to_day=to_day, to_slot=to_slot, changed_by=user)


DAY_NAMES = dict([('MON', 'Monday'), ('TUE', 'Tuesday'), ('WED', 'Wednesday'), ('THU', 'Thursday'),
                  ('FRI', 'Friday'), ('SAT', 'Saturday')])


def _notify(change_ids):
    from communications.services import send_email
    by_teacher = defaultdict(list)
    for c in TimetableChange.objects.filter(pk__in=change_ids).select_related(
            'teacher__user', 'school_class', 'subject', 'from_slot', 'to_slot', 'tenant'):
        by_teacher[c.teacher].append(c)
    for teacher, changes in by_teacher.items():
        email = getattr(teacher.user, 'email', '')
        if not email:
            continue
        lines = ''.join(f'<li><b>{c.subject.name}</b> with {c.school_class.name}: {DAY_NAMES.get(c.from_day)} '
                        f'{c.from_slot.name} → <b>{DAY_NAMES.get(c.to_day)} {c.to_slot.name}</b></li>' for c in changes)
        ok, _ = send_email(email, 'Your timetable has changed',
                           f'<p>Hello {teacher.full_name},</p><p>These lessons have moved:</p><ul>{lines}</ul>'
                           f'<p>{changes[0].tenant.name}</p>')
        if ok:
            TimetableChange.objects.filter(pk__in=[c.pk for c in changes]).update(notified_at=timezone.now())


# ─────────────────────────────────────────────────────────────────────────────
# Reports
# ─────────────────────────────────────────────────────────────────────────────

def teacher_load(timetable):
    entries = list(timetable.slots.select_related('teacher', 'time_slot'))
    days = timetable.config.active_days
    periods = [s for s in timetable.config.get_active_slots()]
    order = {s.pk: i for i, s in enumerate(periods)}
    rows = []
    by_teacher = defaultdict(list)
    for e in entries:
        by_teacher[e.teacher].append(e)
    for teacher, mine in by_teacher.items():
        per_day = Counter(e.day for e in mine)
        gaps = 0
        for d in days:
            idx = sorted(order[e.time_slot_id] for e in mine if e.day == d and e.time_slot_id in order)
            if len(idx) > 1:
                gaps += (idx[-1] - idx[0] + 1) - len(idx)
        warn = []
        if len(mine) > HEAVY_WEEK:
            warn.append(f'{len(mine)} periods a week')
        busiest = max(per_day.values()) if per_day else 0
        if busiest > HEAVY_DAY:
            warn.append(f'{busiest} periods on one day')
        rows.append({'teacher': teacher, 'week': len(mine), 'free': len(days) * len(periods) - len(mine),
                     'per_day': [per_day.get(d, 0) for d in days], 'gaps': gaps, 'warn': warn})
    return sorted(rows, key=lambda r: -r['week']), days


def clash_report(timetable):
    """Everything that breaks a rule after hand edits, plus lessons the plan asks for that aren't on the timetable."""
    out = []
    entries = list(timetable.slots.select_related('teacher', 'school_class', 'subject', 'time_slot', 'room'))
    seen_t, seen_r = defaultdict(list), defaultdict(list)
    for e in entries:
        seen_t[(e.teacher_id, e.day, e.time_slot_id)].append(e)
        if e.room_id:
            seen_r[(e.room_id, e.day, e.time_slot_id)].append(e)
    for group in seen_t.values():
        if len(group) > 1:
            out.append(f'{group[0].teacher.full_name} is in two places on {DAY_NAMES.get(group[0].day)} '
                       f'{group[0].time_slot.name}: ' + ', '.join(e.school_class.name for e in group) + '.')
    for group in seen_r.values():
        if len(group) > 1:
            out.append(f'{group[0].room.name} is double-booked on {DAY_NAMES.get(group[0].day)} {group[0].time_slot.name}.')
    blocked = {(u.staff_id, u.day, u.time_slot_id) for u in TeacherUnavailability.objects.filter(config=timetable.config)}
    for e in entries:
        if (e.teacher_id, e.day, e.time_slot_id) in blocked:
            out.append(f'{e.teacher.full_name} is unavailable on {DAY_NAMES.get(e.day)} {e.time_slot.name} but teaches '
                       f'{e.school_class.name} then.')
        if e.time_slot.is_break:
            out.append(f'{e.subject.name} for {e.school_class.name} is placed in a break ({e.time_slot.name}).')
    placed = Counter((e.school_class_id, e.subject_id, e.teacher_id) for e in entries)
    for a in timetable.config.teacher_subject_classes.select_related('school_class', 'subject', 'staff'):
        n = placed.get((a.school_class_id, a.subject_id, a.staff_id), 0)
        if n != a.periods_per_week:
            out.append(f'{a.school_class.name} {a.subject.name} ({a.staff.full_name}): {n} of {a.periods_per_week} '
                       f'periods on the timetable.')
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Exams pause normal lessons
# ─────────────────────────────────────────────────────────────────────────────

def in_exams(school_class, day):
    """The published exam timetable the class is sitting on `day`, if any."""
    from cbt.models import ExamTimetable
    return ExamTimetable.objects.filter(tenant_id=school_class.tenant_id, published=True, classes=school_class,
                                        start_date__lte=day, end_date__gte=day).first()
