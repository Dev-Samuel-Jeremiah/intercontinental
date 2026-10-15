"""
timetable/lessons.py
────────────────────
Registers taken lesson by lesson from the published timetable.

A teacher opens today's lessons, and each register starts from the morning
register: a child marked absent from school starts absent, everyone else
present. The useful result is the list of children who were in school but
missed a lesson — the ones nobody notices otherwise.
"""
from collections import defaultdict
from datetime import timedelta

from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone

from timetable.models import (GeneratedTimetable, LessonMark, LessonRegister, TimetableConfiguration,
                              TimetableSlot)
from users.models import ENROLLED_STATUSES, Attendance, Student

DAY_CODES = ['MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT', 'SUN']


class LessonError(ValueError):
    pass


def published_timetable(tenant):
    """The published timetable of the active configuration (or the latest published one)."""
    qs = GeneratedTimetable.objects.filter(tenant=tenant, is_published=True)
    active = qs.filter(config__is_active=True).order_by('-generated_at').first()
    return active or qs.order_by('-generated_at').first()


def lessons_on(tenant, day, teacher=None, school_class=None):
    tt = published_timetable(tenant)
    if not tt:
        return []
    qs = (TimetableSlot.objects.filter(tenant=tenant, timetable=tt, day=DAY_CODES[day.weekday()])
          .select_related('school_class', 'subject', 'teacher', 'time_slot').order_by('time_slot__order'))
    if teacher:
        qs = qs.filter(teacher=teacher)
    if school_class:
        qs = qs.filter(school_class=school_class)
    # Classes sitting their exams that day have no normal lessons.
    from cbt.models import ExamTimetable
    in_exams = ExamTimetable.objects.filter(tenant=tenant, published=True, start_date__lte=day, end_date__gte=day) \
        .values_list('classes', flat=True)
    qs = qs.exclude(school_class_id__in=[c for c in in_exams if c])
    registers = {r.slot_id: r for r in LessonRegister.objects.filter(tenant=tenant, date=day,
                                                                      slot__in=qs)}
    return [{'slot': s, 'register': registers.get(s.id),
             'taken': bool(registers.get(s.id) and registers[s.id].taken_at)} for s in qs]


def roster(slot, day):
    """The class list for a lesson, with each child's starting mark."""
    students = list(Student.objects.filter(tenant_id=slot.tenant_id, class_assigned=slot.school_class,
                                           status__in=ENROLLED_STATUSES).order_by('full_name'))
    daily = {a.student_id: a for a in Attendance.objects.filter(
        tenant_id=slot.tenant_id, school_class=slot.school_class, date=day, subject__isnull=True)}
    register = LessonRegister.objects.filter(tenant_id=slot.tenant_id, slot=slot, date=day).first()
    marks = {m.student_id: m for m in register.marks.all()} if register else {}
    rows = []
    for s in students:
        d = daily.get(s.id)
        if s.id in marks:
            status, late = marks[s.id].status, marks[s.id].minutes_late
        else:
            status, late = ('A' if d and d.status == 'A' else 'P'), 0
        rows.append({'student': s, 'status': status, 'minutes_late': late,
                     'away_today': bool(d and d.status == 'A'),
                     'left_early': bool(d and d.remarks and 'Left ' in d.remarks)})
    return rows, register


@transaction.atomic
def save_register(slot, day, marks, staff=None, note=''):
    """`marks` is {student_id: (status, minutes_late)} for the class."""
    today = timezone.localdate()
    if day > today:
        raise LessonError("You can't take a register for a lesson that hasn't happened.")
    if day < today - timedelta(days=7):
        raise LessonError('Registers older than a week can only be changed by an administrator.')
    if DAY_CODES[day.weekday()] != slot.day:
        raise LessonError('That lesson is not on the timetable for that day.')
    register, _ = LessonRegister.objects.get_or_create(tenant_id=slot.tenant_id, slot=slot, date=day)
    valid = set(Student.objects.filter(tenant_id=slot.tenant_id, class_assigned=slot.school_class,
                                       status__in=ENROLLED_STATUSES).values_list('id', flat=True))
    for student_id, (status, late) in marks.items():
        if student_id not in valid:
            continue
        status = status if status in ('P', 'L', 'A') else 'P'
        LessonMark.objects.update_or_create(
            tenant_id=slot.tenant_id, register=register, student_id=student_id,
            defaults={'status': status, 'minutes_late': max(0, min(int(late or 0), 120)) if status == 'L' else 0})
    register.taken_by = staff
    register.taken_at = timezone.now()
    register.note = (note or '').strip()[:255]
    register.save(update_fields=['taken_by', 'taken_at', 'note'])
    return register


def missed_lessons(tenant, day):
    """
    Children marked present at school that day but absent from a lesson —
    possible truancy, or a child unwell somewhere. Children who left early
    with a recorded pickup are left out.
    """
    present = set(Attendance.objects.filter(tenant=tenant, date=day, status='P', subject__isnull=True)
                  .exclude(remarks__contains='Left ').values_list('student_id', flat=True))
    out = defaultdict(list)
    for m in (LessonMark.objects.filter(tenant=tenant, register__date=day, status='A', student_id__in=present)
              .select_related('student', 'student__class_assigned', 'register__slot__subject',
                              'register__slot__time_slot', 'register__slot__teacher')):
        out[m.student].append(m.register.slot)
    return sorted(((s, sorted(slots, key=lambda x: x.time_slot.order)) for s, slots in out.items()),
                  key=lambda r: (-len(r[1]), r[0].full_name))


def subject_attendance(school_class, start, end):
    """Per student and subject: lessons attended out of lessons with a register."""
    rows = (LessonMark.objects.filter(register__slot__school_class=school_class,
                                      register__date__gte=start, register__date__lte=end)
            .values('student_id', 'register__slot__subject__name')
            .annotate(n=Count('id'), present=Count('id', filter=Q(status__in=('P', 'L')))))
    table = defaultdict(dict)
    subjects = set()
    for r in rows:
        subjects.add(r['register__slot__subject__name'])
        table[r['student_id']][r['register__slot__subject__name']] = round(100 * r['present'] / r['n'])
    students = list(Student.objects.filter(class_assigned=school_class, status__in=ENROLLED_STATUSES)
                    .order_by('full_name'))
    return students, sorted(subjects), table
