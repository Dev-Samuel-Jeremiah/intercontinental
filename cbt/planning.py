"""
cbt/planning.py
───────────────
Seating candidates in exam halls so that neighbours are from different
classes, and building a clash-free exam timetable.
"""
import random
import re
from collections import defaultdict
from datetime import datetime, timedelta

from django.db import transaction
from django.utils import timezone

from users.models import ENROLLED_STATUSES, ClassSubject, Student

from .models import Exam, ExamHall, ExamTimetableEntry, Seat


class PlanningError(ValueError):
    pass


# ─────────────────────────────────────────────────────────────────────────────
# Seating
# ─────────────────────────────────────────────────────────────────────────────

def candidates(exam):
    from .models import ExamStudentRestriction
    restricted = list(ExamStudentRestriction.objects.filter(exam=exam).values_list('student_id', flat=True))
    qs = Student.objects.filter(tenant_id=exam.tenant_id, class_assigned__in=exam.classes.all(),
                                status__in=ENROLLED_STATUSES).select_related('class_assigned')
    if restricted:
        qs = qs.filter(id__in=restricted)
    return list(qs.order_by('class_assigned__name', 'full_name'))


@transaction.atomic
def allocate(exam, halls):
    """
    Seat every candidate, filling halls row by row. Each seat takes a candidate
    from a different class to the one on its left and the one in front, where
    possible, choosing from the largest class left so classes stay mixed.
    """
    halls = [h for h in halls if h.capacity]
    people = candidates(exam)
    room = sum(h.capacity for h in halls)
    if not people:
        raise PlanningError('This exam has no candidates — check its classes.')
    if room < len(people):
        raise PlanningError(f'{len(people)} candidates but only {room} seats in the chosen halls. '
                            f'Add {len(people) - room} more seats or another hall.')
    rng = random.Random(f'seat-{exam.id}')
    groups = defaultdict(list)
    for p in people:
        groups[p.class_assigned_id].append(p)
    for g in groups.values():
        rng.shuffle(g)
    Seat.objects.filter(exam=exam).delete()
    seats = []
    for hall in halls:
        grid = {}
        for r in range(1, hall.rows + 1):
            for c in range(1, hall.columns + 1):
                if not any(groups.values()):
                    break
                avoid = {getattr(grid.get((r, c - 1)), 'class_assigned_id', None),
                         getattr(grid.get((r - 1, c)), 'class_assigned_id', None)}
                options = sorted((k for k, g in groups.items() if g), key=lambda k: -len(groups[k]))
                pick = next((k for k in options if k not in avoid), options[0])
                person = groups[pick].pop()
                grid[(r, c)] = person
                seats.append(Seat(tenant_id=exam.tenant_id, exam=exam, student=person, hall=hall, row=r, column=c))
    Seat.objects.bulk_create(seats)
    return len(seats)


def chart(exam):
    """[{hall, grid: rows of [seat or None]}] plus an alphabetical door list."""
    seats = list(Seat.objects.filter(exam=exam).select_related('hall', 'student', 'student__class_assigned'))
    by_hall = defaultdict(dict)
    halls = {}
    for s in seats:
        by_hall[s.hall_id][(s.row, s.column)] = s
        halls[s.hall_id] = s.hall
    plans = []
    for hid, cells in sorted(by_hall.items(), key=lambda kv: halls[kv[0]].name):
        h = halls[hid]
        plans.append({'hall': h, 'count': len(cells),
                      'grid': [[cells.get((r, c)) for c in range(1, h.columns + 1)] for r in range(1, h.rows + 1)]})
    door = sorted(seats, key=lambda s: s.student.full_name.lower())
    return {'plans': plans, 'door': door, 'total': len(seats)}


def neighbours_same_class(exam):
    """How many side-by-side or front-and-back pairs share a class (0 is ideal)."""
    cells = {(s.hall_id, s.row, s.column): s.student.class_assigned_id
             for s in Seat.objects.filter(exam=exam).select_related('student')}
    return sum(1 for (h, r, c), k in cells.items()
               for nb in ((h, r, c + 1), (h, r + 1, c)) if cells.get(nb) == k)


# ─────────────────────────────────────────────────────────────────────────────
# Exam timetable
# ─────────────────────────────────────────────────────────────────────────────

def _level_key(cls):
    """Arms of one level sit the same paper: 'JSS 1A' and 'JSS 1B' → 'jss 1'."""
    if cls.level:
        return cls.level.strip().lower()
    return re.sub(r'\s*[a-z]$', '', cls.name.strip().lower()) if re.search(r'\d\s*[a-z]$', cls.name.strip().lower()) \
        else cls.name.strip().lower()


def papers(timetable):
    """[(subject, [classes])] — one paper per subject per level across the chosen classes."""
    grouped = defaultdict(set)
    subjects = {}
    for cs in ClassSubject.objects.filter(tenant_id=timetable.tenant_id, school_class__in=timetable.classes.all()) \
            .select_related('subject', 'school_class'):
        key = (cs.subject_id, _level_key(cs.school_class))
        grouped[key].add(cs.school_class)
        subjects[cs.subject_id] = cs.subject
    return [(subjects[sid], sorted(classes, key=lambda c: c.name)) for (sid, _), classes in grouped.items()]


def sittings(timetable):
    """[(date, 'AM'|'PM')] between the dates, skipping weekends and public holidays."""
    from academics.models import PublicHoliday
    holidays = set(PublicHoliday.objects.filter(tenant_id=timetable.tenant_id,
                                                date__range=(timetable.start_date, timetable.end_date))
                   .values_list('date', flat=True))
    out, day = [], timetable.start_date
    while day <= timetable.end_date:
        if day.weekday() < 5 and day not in holidays:
            out.append((day, ExamTimetableEntry.MORNING))
            if timetable.afternoon:
                out.append((day, ExamTimetableEntry.AFTERNOON))
        day += timedelta(days=1)
    return out


@transaction.atomic
def generate(timetable):
    """
    Place every paper so no class sits two papers at once, spreading each
    class's papers across the days it has fewest. Returns the papers that
    couldn't be placed (none if it all fits).
    """
    if timetable.end_date < timetable.start_date:
        raise PlanningError('The timetable ends before it starts.')
    slots = sittings(timetable)
    if not slots:
        raise PlanningError('There are no school days between those dates.')
    work = sorted(papers(timetable), key=lambda p: (-len(p[1]), p[0].name))
    if not work:
        raise PlanningError('The chosen classes have no subjects. Add subjects to the classes first.')
    timetable.entries.all().delete()
    taken = defaultdict(set)             # slot → class ids sitting a paper then
    per_day = defaultdict(int)           # (class id, date) → papers that day
    unplaced = []
    for subject, classes in work:
        ids = {c.id for c in classes}
        best = None
        for i, slot in enumerate(slots):
            if taken[slot] & ids:
                continue
            load = max(per_day[(cid, slot[0])] for cid in ids)
            score = (load, i)
            if best is None or score < best[0]:
                best = (score, slot)
        if not best:
            unplaced.append((subject, classes))
            continue
        slot = best[1]
        taken[slot] |= ids
        for cid in ids:
            per_day[(cid, slot[0])] += 1
        entry = ExamTimetableEntry.objects.create(
            tenant_id=timetable.tenant_id, timetable=timetable, subject=subject, date=slot[0], sitting=slot[1],
            start_time=timetable.morning_start if slot[1] == 'AM' else timetable.afternoon_start,
            minutes=timetable.paper_minutes)
        entry.classes.set(classes)
    return unplaced


def clashes(timetable):
    """[(date, sitting, class name, [subjects])] where a class has two papers at once."""
    seen = defaultdict(list)
    for e in timetable.entries.prefetch_related('classes').select_related('subject'):
        for c in e.classes.all():
            seen[(e.date, e.sitting, c.name)].append(e.subject.name)
    return [(d, s, c, subs) for (d, s, c), subs in sorted(seen.items()) if len(subs) > 1]


def move(entry, date, sitting):
    tt = entry.timetable
    if not tt.start_date <= date <= tt.end_date:
        raise PlanningError('Choose a date within the timetable.')
    if sitting == 'PM' and not tt.afternoon:
        raise PlanningError('This timetable has morning papers only.')
    others = ExamTimetableEntry.objects.filter(timetable=tt, date=date, sitting=sitting).exclude(pk=entry.pk) \
        .filter(classes__in=entry.classes.all()).distinct()
    if others.exists():
        names = ', '.join(sorted({o.subject.name for o in others}))
        raise PlanningError(f'Some of these classes already sit {names} then.')
    entry.date, entry.sitting = date, sitting
    entry.start_time = tt.morning_start if sitting == 'AM' else tt.afternoon_start
    entry.save(update_fields=['date', 'sitting', 'start_time'])
    return entry


@transaction.atomic
def make_exam(entry, staff=None):
    """Create the CBT exam for a timetable entry (unpublished, for the teacher to fill)."""
    if entry.exam_id:
        return entry.exam
    tt = entry.timetable
    start = timezone.make_aware(datetime.combine(entry.date, entry.start_time))
    exam = Exam.objects.create(
        tenant_id=entry.tenant_id, title=f'{entry.subject.name} — {tt.name}', subject=entry.subject,
        session=tt.term.session if tt.term_id else None, term=tt.term, exam_type='EXAM', start_time=start,
        end_time=start + timedelta(minutes=entry.minutes), duration_minutes=entry.minutes, created_by=staff,
        published=False)
    exam.classes.set(entry.classes.all())
    entry.exam = exam
    entry.save(update_fields=['exam'])
    return exam


def for_class(school_class):
    """Published timetable entries for a class, upcoming first."""
    return list(ExamTimetableEntry.objects.filter(tenant_id=school_class.tenant_id, timetable__published=True,
                                                  classes=school_class, date__gte=timezone.localdate())
                .select_related('subject', 'timetable').order_by('date', 'sitting'))
