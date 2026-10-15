"""
users/lifecycle.py
──────────────────
A student's status: active, suspended, withdrawn, graduated or deceased.

Changing it records why and from when, keeps the history, and does what the
new status implies:

  • suspended — still on the roll (registers, results, fee reminders), with
    an optional return date;
  • withdrawn or graduated — off the roll; the class they left from and the
    day they left are kept, and a graduate appears in the alumni register;
  • deceased — off the roll, handled the same way, with care in the wording;
  • back to active — the student's own login works again.

A student who has left cannot sign in with their own account. Their record
is never deleted, so results, payments and history stay intact.
"""
from django.db import transaction
from django.utils import timezone

from users.audit import audit_source
from users.models import ENROLLED_STATUSES, LEFT_STATUSES, STATUS_OPTIONS, StudentStatusChange

VALID = dict(STATUS_OPTIONS)


class StatusError(ValueError):
    pass


@transaction.atomic
def change_status(student, to_status, *, reason='', effective_on=None, until=None,
                  destination='', user=None):
    if to_status not in VALID:
        raise StatusError('Choose a valid status.')
    if to_status == student.status:
        raise StatusError(f'{student.full_name} is already {to_status.lower()}.')
    if to_status in ('Suspended', 'Withdrawn', 'Deceased') and not reason.strip():
        raise StatusError(f'Give a reason for marking the student {to_status.lower()}.')
    effective_on = effective_on or timezone.localdate()
    if until and until <= effective_on:
        raise StatusError('The return date must be after the suspension starts.')

    from_status = student.status
    student.status = to_status
    student.status_reason = reason.strip()[:255]
    student.status_changed_on = effective_on

    if to_status in LEFT_STATUSES:
        student.left_on = effective_on
        student.leaving_class = getattr(student.class_assigned, 'name', '') or student.leaving_class
        if destination:
            student.destination = destination.strip()[:255]
    elif from_status in LEFT_STATUSES:
        # Re-admitted: they are on the roll again.
        student.left_on = None

    with audit_source('Status change', user):
        student.save()

    login = student.user
    if login is not None:
        should_be_active = to_status in ENROLLED_STATUSES
        if login.is_active != should_be_active:
            login.is_active = should_be_active
            login.save(update_fields=['is_active'])

    return StudentStatusChange.objects.create(
        tenant_id=student.tenant_id, student=student, from_status=from_status,
        to_status=to_status, effective_on=effective_on, reason=student.status_reason,
        until=until if to_status == 'Suspended' else None, changed_by=user)
