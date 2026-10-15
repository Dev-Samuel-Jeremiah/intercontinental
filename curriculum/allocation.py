"""
curriculum/allocation.py
────────────────────────
Who teaches what: the subject × class grid of teacher assignments, the gaps in
it, and each teacher's load.
"""
from collections import Counter, defaultdict

from django.db import transaction

from users.models import Class, ClassSubject, Staff, StaffSubjectClass, Subject

HEAVY_PERIODS = 30       # timetabled periods a week before a teacher is flagged
HEAVY_ALLOCATIONS = 12   # subject-class pairs before a teacher is flagged (no timetable)


def matrix(tenant):
    classes = list(Class.objects.filter(tenant=tenant).order_by('name'))
    offered = set(ClassSubject.objects.filter(tenant=tenant).values_list('subject_id', 'school_class_id'))
    assigned = defaultdict(list)
    for a in StaffSubjectClass.objects.filter(tenant=tenant).select_related('staff'):
        assigned[(a.subject_id, a.school_class_id)].append(a.staff)
    subject_ids = {s for s, _ in offered} | {s for s, _ in assigned}
    subjects = list(Subject.objects.filter(tenant=tenant, id__in=subject_ids).order_by('name'))
    rows, gaps = [], 0
    for subject in subjects:
        cells = []
        for cls in classes:
            key = (subject.id, cls.id)
            teachers = assigned.get(key, [])
            is_offered = key in offered
            if is_offered and not teachers:
                gaps += 1
            cells.append({'school_class': cls, 'offered': is_offered, 'teachers': teachers,
                          'state': ('gap' if is_offered and not teachers else
                                    'extra' if teachers and not is_offered else
                                    'ok' if teachers else 'none')})
        rows.append({'subject': subject, 'cells': cells})
    return {'classes': classes, 'rows': rows, 'gaps': gaps}


def loads(tenant):
    """Each teacher's allocations, classes and timetabled periods a week, heaviest first."""
    from timetable.lessons import published_timetable
    from timetable.models import TimetableSlot
    tt = published_timetable(tenant)
    periods = Counter(TimetableSlot.objects.filter(tenant=tenant, timetable=tt)
                      .values_list('teacher_id', flat=True)) if tt else Counter()
    pairs = defaultdict(list)
    for a in StaffSubjectClass.objects.filter(tenant=tenant).select_related('subject', 'school_class'):
        pairs[a.staff_id].append(a)
    out = []
    for staff in Staff.objects.filter(tenant=tenant, role__in=('TEACHER', 'ADMIN')).order_by('full_name'):
        mine = pairs.get(staff.id, [])
        n_periods = periods.get(staff.id, 0)
        heavy = n_periods > HEAVY_PERIODS if tt else len(mine) > HEAVY_ALLOCATIONS
        if not mine and staff.role == 'ADMIN':
            continue  # administrators who don't teach
        out.append({'staff': staff, 'allocations': len(mine), 'periods': n_periods if tt else None,
                    'classes': len({a.school_class_id for a in mine}),
                    'subjects': sorted({a.subject.name for a in mine}),
                    'state': 'heavy' if heavy else 'idle' if not mine else 'ok'})
    out.sort(key=lambda r: (-(r['periods'] or 0), -r['allocations'], r['staff'].full_name))
    return {'rows': out, 'has_timetable': bool(tt)}


@transaction.atomic
def set_teacher(tenant, subject, school_class, staff):
    """Make `staff` the (only) teacher of the subject in that class; None clears it."""
    StaffSubjectClass.objects.filter(tenant=tenant, subject=subject, school_class=school_class) \
        .exclude(staff=staff).delete()
    if staff:
        StaffSubjectClass.objects.get_or_create(tenant=tenant, staff=staff, subject=subject,
                                                school_class=school_class)
        ClassSubject.objects.get_or_create(tenant=tenant, subject=subject, school_class=school_class)
