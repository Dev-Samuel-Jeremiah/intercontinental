"""
users/history.py
────────────────
Each student's class history: every class (and section) they have been in,
in which session, from when to when, and how they got there.

Rows are written automatically — when a student is created in a class, when
their class changes (edit form, import, promotion) and when they leave — so
nothing has to remember to call this. Students who were on the roll before
the history existed get theirs rebuilt from the change log the first time
their record is opened (or with `manage.py build_class_history`).
"""
from django.utils import timezone

LEFT = ('Withdrawn', 'Graduated', 'Deceased')


def section_of(school_class):
    """The section (Nursery & Primary, Secondary…) a class belongs to, or None."""
    if school_class is None:
        return None
    try:
        return school_class.campuses.first()
    except Exception:          # sections not set up
        return None


def _active_session(tenant_id):
    from academics.models import AcademicSession
    return AcademicSession.objects.filter(tenant_id=tenant_id, is_active=True).first()


def _how(old_class, new_class, source):
    if old_class is None:
        return 'ADMITTED'
    if source == 'Promotion':
        return 'REPEATED' if old_class.pk == new_class.pk else 'PROMOTED'
    if old_class.pk == new_class.pk:
        return 'REPEATED'
    return 'MOVED'


def open_row(student):
    from .models import ClassHistory
    return ClassHistory.objects.filter(tenant_id=student.tenant_id, student=student, ended_on__isnull=True).first()


def move(student, new_class, how=None, user=None, on=None, source=None):
    """Close the open spell and start one in `new_class` (None = no class). Returns the new row or None."""
    from .models import ClassHistory
    on = on or timezone.localdate()
    current = open_row(student)
    if current and new_class is not None and current.school_class_id == new_class.pk and source != 'Promotion':
        return current                                         # nothing changed
    old_class = current.school_class if current else None
    if current:
        current.ended_on = max(on, current.started_on)
        current.save(update_fields=['ended_on'])
    if new_class is None:
        return None
    section = section_of(new_class)
    session = _active_session(student.tenant_id)
    return ClassHistory.objects.create(
        tenant_id=student.tenant_id, student=student, school_class=new_class, class_name=new_class.name,
        section=section, section_name=section.name if section else '', session=session,
        session_name=session.name if session else '', started_on=on,
        how=how or _how(old_class, new_class, source), recorded_by=user)


def move_many(students, new_class, user=None, source='Promotion'):
    for st in students:
        move(st, new_class, user=user, source=source)


def close(student, on=None):
    current = open_row(student)
    if current:
        current.ended_on = max(on or timezone.localdate(), current.started_on)
        current.save(update_fields=['ended_on'])


def ensure(student):
    """Rebuild the history from the change log for a student who has none yet."""
    from .models import Class, ClassHistory, StudentChange
    if ClassHistory.objects.filter(tenant_id=student.tenant_id, student=student).exists():
        return False
    changes = list(StudentChange.objects.filter(tenant_id=student.tenant_id, student=student, field='class_assigned')
                   .order_by('changed_at', 'id'))
    classes = {c.name: c for c in Class.objects.filter(tenant_id=student.tenant_id)}
    first_name = changes[0].old_value if changes else (student.class_assigned.name if student.class_assigned else '')
    spells = []                                                # (class name, started, how)
    if first_name:
        # Joined on the day the record was made — or earlier, if the change log says they moved before that
        joined = timezone.localdate(student.created_at) if student.created_at else timezone.localdate()
        if changes:
            joined = min(joined, timezone.localdate(changes[0].changed_at))
        spells.append((first_name, joined, 'ADMITTED'))
    for ch in changes:
        if ch.new_value:
            spells.append((ch.new_value, timezone.localdate(ch.changed_at),
                           'PROMOTED' if ch.source == 'Promotion' else 'MOVED'))
    rows = []
    for i, (name, started, how) in enumerate(spells):
        ended = spells[i + 1][1] if i + 1 < len(spells) else None
        if ended is None and student.status in LEFT:
            ended = student.left_on or student.status_changed_on or timezone.localdate()
        cls = classes.get(name)
        section = section_of(cls)
        rows.append(ClassHistory(tenant_id=student.tenant_id, student=student, school_class=cls, class_name=name[:50],
                                 section=section, section_name=section.name if section else '',
                                 started_on=started, ended_on=max(ended, started) if ended else None,
                                 how=how))
    if rows:
        rows[0].how = 'ADMITTED'                               # the first class we know of
    ClassHistory.objects.bulk_create(rows)
    return bool(rows)


def history(student):
    """The student's class spells, oldest first, with section changes marked."""
    from .models import ClassHistory
    ensure(student)
    rows = list(ClassHistory.objects.filter(tenant_id=student.tenant_id, student=student)
                .select_related('session', 'recorded_by').order_by('started_on', 'id'))
    previous = None
    for r in rows:
        r.new_section = bool(previous is not None and r.section_name and r.section_name != previous.section_name)
        end = r.ended_on or timezone.localdate()
        r.days = (end - r.started_on).days
        previous = r
    return rows


def sections_attended(rows):
    seen = []
    for r in rows:
        if r.section_name and r.section_name not in seen:
            seen.append(r.section_name)
    return seen
