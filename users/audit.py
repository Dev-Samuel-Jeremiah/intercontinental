"""
users/audit.py
──────────────
Who changed a student's record, when, and what it said before.

Every save of a Student is compared with what is in the database and each
changed field is written to StudentChange, with the person logged in at the
time (remembered per request by CurrentUserMiddleware). Bulk updates, which
skip save signals, call `record_bulk` themselves.

Passwords are never recorded, not even as "changed".
"""
import threading
from contextlib import contextmanager

from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from django.utils import timezone

_local = threading.local()

# field → label shown in the history. Anything not listed is not recorded.
TRACKED = {
    'full_name': 'Name',
    'gender': 'Gender',
    'date_of_birth': 'Date of birth',
    'admission_number': 'Admission number',
    'class_assigned': 'Class',
    'house': 'House',
    'status': 'Status',
    'current_term': 'Term',
    'current_session': 'Session',
    'passport': 'Passport photo',
    'parent_name': 'Parent name',
    'parent_phone': 'Parent phone',
    'parent_email': 'Parent email',
    'address': 'Address',
    'left_on': 'Left on',
    'leaving_class': 'Leaving class',
    'destination': 'Destination',
    'nin': 'NIN',
}


# ── who is making the change ─────────────────────────────────────────────────

def get_current_user():
    user = getattr(_local, 'user', None)
    return user if user is not None and getattr(user, 'is_authenticated', False) else None


class CurrentUserMiddleware:
    """Remember the logged-in user for the length of the request."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        _local.user = getattr(request, 'user', None)
        _local.request = request
        try:
            return self.get_response(request)
        finally:
            _local.user = None
            _local.source = None
            _local.request = None


@contextmanager
def audit_source(name, user=None):
    """Label changes made inside the block — 'Promotion', 'Bulk edit', 'Status change'."""
    previous_source, previous_user = getattr(_local, 'source', None), getattr(_local, 'user', None)
    _local.source = name
    if user is not None:
        _local.user = user
    try:
        yield
    finally:
        _local.source = previous_source
        _local.user = previous_user


# ── turning values into something readable ───────────────────────────────────

def display(student, field, raw=None, use_raw=False):
    value = raw if use_raw else getattr(student, field, None)
    if field == 'passport':
        return 'photo' if value else ''
    if field == 'nin':
        # Never keep a full NIN in the history.
        return f"•••••••{value[-4:]}" if value else ''
    if value is None or value == '':
        return ''
    if hasattr(value, 'strftime'):
        return value.strftime('%d %b %Y')
    return str(value)


def snapshot(student):
    return {f: display(student, f) for f in TRACKED}


# ── recording ────────────────────────────────────────────────────────────────

def record(student, before, after, source=None, user=None):
    from users.models import StudentChange
    rows = []
    for field, label in TRACKED.items():
        old, new = before.get(field, ''), after.get(field, '')
        if old == new:
            continue
        if field == 'passport':
            old, new = ('photo' if old else ''), ('new photo' if new else 'removed')
        rows.append(StudentChange(
            tenant_id=student.tenant_id, student=student, field=field, label=label,
            old_value=old, new_value=new, changed_by=user or get_current_user(),
            source=source or getattr(_local, 'source', None) or 'Edit',
            changed_at=timezone.now()))
    if rows:
        StudentChange.objects.bulk_create(rows)
    return rows


def record_bulk(students, field, new_display, source, user=None):
    """For queryset.update() paths: pass the students as they were before the update."""
    from users.models import StudentChange
    label = TRACKED[field]
    rows = []
    for student in students:
        old = display(student, field)
        if old != new_display:
            rows.append(StudentChange(
                tenant_id=student.tenant_id, student=student, field=field, label=label,
                old_value=old, new_value=new_display, changed_by=user or get_current_user(),
                source=source, changed_at=timezone.now()))
    if rows:
        StudentChange.objects.bulk_create(rows)
    return rows


@receiver(pre_save, sender='users.Student')
def _remember_before(sender, instance, **kwargs):
    instance._audit_before = None
    instance._class_before = instance._status_before = None
    if instance.pk:
        # The plain base manager: the record as stored, whichever school is active.
        old = sender._base_manager.filter(pk=instance.pk).first()
        if old is not None:
            instance._audit_before = snapshot(old)
            instance._class_before, instance._status_before = old.class_assigned_id, old.status
            if old.class_assigned_id != instance.class_assigned_id or old.status != instance.status:
                from users import history
                history.ensure(old)            # keep what came before, built from the change log


@receiver(post_save, sender='users.Student')
def _record_after(sender, instance, created, **kwargs):
    from users import history
    if created:
        if instance.class_assigned_id:
            history.move(instance, instance.class_assigned, how='ADMITTED', user=get_current_user(),
                         on=timezone.localdate(instance.created_at) if instance.created_at else None)
        return
    before = getattr(instance, '_audit_before', None)
    if before is None:
        return
    record(instance, before, snapshot(instance))
    instance._audit_before = None
    if instance.status in history.LEFT and instance._status_before not in history.LEFT:
        history.close(instance, instance.left_on)
    elif instance._class_before != instance.class_assigned_id or (
            instance._status_before in history.LEFT and instance.status not in history.LEFT):
        history.move(instance, instance.class_assigned, user=get_current_user(),
                     source=getattr(_local, 'source', None))
