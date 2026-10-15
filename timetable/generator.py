"""
Timetable Generator
===================
Greedy slot-filling scheduler with randomisation for variety.

Strategy:
  Instead of pure backtracking (which can hang indefinitely on large inputs),
  we use a greedy round-robin approach:
    1. Build a remaining_demand dict: {(class, subject, teacher): periods_left}
    2. Shuffle the order of (class, day, slot) cells.
    3. For each cell, pick the best valid assignment from remaining demand.
    4. Repeat with different shuffles until all demand is satisfied or timeout.

Constraints enforced:
  1. A teacher cannot teach two classes at the same time (same day+slot).
  2. A class cannot have two subjects at the same time (same day+slot).
  3. Each teacher-subject-class appears exactly `periods_per_week` times.
  4. No more than 2 periods of the same subject per class per day.
  5. Nothing is placed in a period the teacher has marked unavailable.
  6. Double periods sit in back-to-back periods with no break between.
  7. A lesson that needs a special room (e.g. a lab) gets a free one of that kind.
"""

import random
import time
from collections import defaultdict
from .models import (
    TimetableConfiguration, TeacherSubjectClass,
    GeneratedTimetable, TimetableSlot, TeacherUnavailability, TeachingRoom,
)


class TimetableGenerationError(Exception):
    pass


def generate_timetable(config_id: int, max_attempts: int = 30, timeout_seconds: int = 20) -> GeneratedTimetable:
    """
    Generate a timetable for the given TimetableConfiguration.

    Tries up to `max_attempts` greedy passes, each with a different random shuffle,
    but will stop after `timeout_seconds` regardless.

    Returns a saved (unpublished) GeneratedTimetable on success.
    Raises TimetableGenerationError with a clear message on failure.
    """
    config = TimetableConfiguration.objects.get(pk=config_id)
    slots = list(config.get_active_slots())
    days = config.active_days

    if not slots:
        raise TimetableGenerationError("No time slots configured (excluding breaks).")
    if not days:
        raise TimetableGenerationError("No active days configured.")

    assignments = list(
        config.teacher_subject_classes.select_related('staff', 'subject', 'school_class')
    )
    if not assignments:
        raise TimetableGenerationError(
            "No teacher-subject-class assignments found for this configuration. "
            "Please add assignments before generating."
        )

    # ------------------------------------------------------------------
    # Capacity pre-check (fast fail with a clear message)
    # ------------------------------------------------------------------
    total_cells = len(days) * len(slots)

    class_demand = defaultdict(int)
    class_names = {}
    for a in assignments:
        class_demand[a.school_class.pk] += a.periods_per_week
        class_names[a.school_class.pk] = a.school_class.name

    overloaded = []
    for cid, demand in class_demand.items():
        if demand > total_cells:
            overloaded.append(
                f"  • {class_names[cid]}: needs {demand} periods but only "
                f"{total_cells} slots available ({len(days)} days × {len(slots)} periods)"
            )
    if overloaded:
        raise TimetableGenerationError(
            "Cannot generate — the following classes exceed weekly capacity. "
            "Reduce periods_per_week or add more time slots/days.\n\n"
            + "\n".join(overloaded)
        )

    teacher_demand = defaultdict(int)
    teacher_names = {}
    for a in assignments:
        teacher_demand[a.staff.pk] += a.periods_per_week
        teacher_names[a.staff.pk] = a.staff.full_name

    unavailable = {(u.staff_id, u.day, u.time_slot_id) for u in
                   TeacherUnavailability.objects.filter(config=config)}
    blocked = defaultdict(int)
    for tid, day, sid in unavailable:
        if day in days:
            blocked[tid] += 1
    overloaded_t = []
    for tid, demand in teacher_demand.items():
        if demand > total_cells - blocked[tid]:
            overloaded_t.append(
                f"  • {teacher_names[tid]}: assigned {demand} periods but only "
                f"{total_cells - blocked[tid]} periods when they are available"
            )
    if overloaded_t:
        raise TimetableGenerationError(
            "Cannot generate — the following teachers exceed weekly capacity.\n\n"
            + "\n".join(overloaded_t)
        )

    # ------------------------------------------------------------------
    # Greedy generation with timeout
    # ------------------------------------------------------------------
    rooms = defaultdict(list)
    for r in TeachingRoom.objects.filter(tenant_id=config.tenant_id, active=True):
        rooms[r.kind].append(r)
    for a in assignments:
        if a.room_kind and not rooms.get(a.room_kind):
            raise TimetableGenerationError(
                f"{a.subject.name} for {a.school_class.name} needs a room of type "
                f"{dict(TeachingRoom.KINDS).get(a.room_kind, a.room_kind)}, but none is set up. Add one under Rooms.")
    # Back-to-back pairs for doubles: neighbours in the full period list (so never across a break).
    ordered = list(config.time_slots.all().order_by('order', 'start_time'))
    pairs = [(ordered[i], ordered[i + 1]) for i in range(len(ordered) - 1)
             if not ordered[i].is_break and not ordered[i + 1].is_break]
    for a in assignments:
        if a.double_periods and not pairs:
            raise TimetableGenerationError("Doubles need two periods in a row without a break between them.")

    deadline = time.time() + timeout_seconds

    for attempt in range(max_attempts):
        if time.time() > deadline:
            break
        result = _greedy_attempt(assignments, slots, days, deadline, unavailable, rooms, pairs)
        if result is not None:
            timetable = GeneratedTimetable.objects.create(tenant_id=config.tenant_id, config=config)
            bulk = [
                TimetableSlot(
                    tenant_id=config.tenant_id,
                    timetable=timetable,
                    school_class=cls,
                    day=day,
                    time_slot=slot,
                    subject=subject,
                    teacher=teacher,
                    room=room,
                )
                for (cls, day, slot), (subject, teacher, room) in result.items()
            ]
            TimetableSlot.objects.bulk_create(bulk)
            return timetable

    raise TimetableGenerationError(
        f"Could not generate a valid timetable within {timeout_seconds} seconds "
        f"after {max_attempts} attempts. "
        "Try: reducing periods_per_week, adding more time slots, or adding more days."
    )


# ---------------------------------------------------------------------------
# Internal greedy attempt
# ---------------------------------------------------------------------------

def _greedy_attempt(assignments, slots, days, deadline, unavailable=frozenset(), rooms=None, pairs=()):
    """
    One greedy attempt. Returns schedule dict or None if it fails.

    Schedule dict: {(class_obj, day_str, slot_obj): (subject_obj, teacher_obj, room_or_None)}
    Doubles are placed first (they're the hardest to fit), then single periods.
    """
    rooms = rooms or {}
    remaining = {}
    doubles = {}
    kind = {}
    for a in assignments:
        key = (a.school_class, a.subject, a.staff)
        remaining[key] = a.periods_per_week - 2 * a.double_periods
        doubles[key] = a.double_periods
        kind[key] = a.room_kind

    teacher_busy = defaultdict(set)   # teacher.pk → {(day, slot.pk)}
    class_busy = defaultdict(set)     # class.pk   → {(day, slot.pk)}
    room_busy = defaultdict(set)      # room.pk    → {(day, slot.pk)}
    day_subj_count = defaultdict(int) # (class.pk, day, subject.pk) → count
    schedule = {}

    def free_room(key, cells):
        if not kind[key]:
            return None, True
        options = list(rooms.get(kind[key], []))
        random.shuffle(options)
        for r in options:
            if not any(c in room_busy[r.pk] for c in cells):
                return r, True
        return None, False

    def ok(cls, subj, teacher, day, slot_ids):
        return (all((day, sid) not in class_busy[cls.pk] for sid in slot_ids)
                and all((day, sid) not in teacher_busy[teacher.pk] for sid in slot_ids)
                and all((teacher.pk, day, sid) not in unavailable for sid in slot_ids)
                and day_subj_count[(cls.pk, day, subj.pk)] + len(slot_ids) <= 2)

    def place(key, day, slot_objs, room):
        cls, subj, teacher = key
        for slot in slot_objs:
            schedule[(cls, day, slot)] = (subj, teacher, room)
            class_busy[cls.pk].add((day, slot.pk))
            teacher_busy[teacher.pk].add((day, slot.pk))
            if room:
                room_busy[room.pk].add((day, slot.pk))
            day_subj_count[(cls.pk, day, subj.pk)] += 1

    # 1. Doubles
    double_keys = [k for k, n in doubles.items() for _ in range(n)]
    random.shuffle(double_keys)
    for key in double_keys:
        cls, subj, teacher = key
        options = [(d, p) for d in days for p in pairs]
        random.shuffle(options)
        for day, (a, b) in options:
            if not ok(cls, subj, teacher, day, (a.pk, b.pk)):
                continue
            room, found = free_room(key, [(day, a.pk), (day, b.pk)])
            if not found:
                continue
            place(key, day, (a, b), room)
            break
        else:
            return None
        if time.time() > deadline:
            return None

    # 2. Single periods
    seen, cells = set(), []
    for (cls, _, _) in remaining:
        for day in days:
            for slot in slots:
                if (cls.pk, day, slot.pk) not in seen:
                    seen.add((cls.pk, day, slot.pk))
                    cells.append((cls, day, slot))
    random.shuffle(cells)

    for cls, day, slot in cells:
        if time.time() > deadline:
            return None
        if (day, slot.pk) in class_busy[cls.pk]:
            continue
        candidates = [k for k, left in remaining.items() if k[0].pk == cls.pk and left > 0]
        random.shuffle(candidates)
        for key in candidates:
            _, subj, teacher = key
            if not ok(cls, subj, teacher, day, (slot.pk,)):
                continue
            room, found = free_room(key, [(day, slot.pk)])
            if not found:
                continue
            place(key, day, (slot,), room)
            remaining[key] -= 1
            break

    if any(v > 0 for v in remaining.values()):
        return None
    return schedule
