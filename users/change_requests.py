"""
users/change_requests.py
────────────────────────
Parents point out a mistake on their child's record; the school checks it
and applies it. Nothing a parent submits changes the record by itself.
"""
from datetime import date

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.utils import timezone

from users.audit import audit_source, display
from users.models import ProfileChangeRequest


class RequestError(ValueError):
    pass


def clean_new_value(field, raw):
    value = (raw or '').strip()
    if not value:
        raise RequestError('Enter the correct value.')
    if field == 'parent_email':
        try:
            validate_email(value)
        except ValidationError:
            raise RequestError('That email address does not look right.')
    elif field == 'date_of_birth':
        try:
            when = date.fromisoformat(value)
        except ValueError:
            raise RequestError('Enter the date of birth as a date.')
        if when >= timezone.localdate() or when.year < 1900:
            raise RequestError('That date of birth is not possible.')
    elif field == 'parent_phone':
        digits = ''.join(ch for ch in value if ch.isdigit())
        if len(digits) < 10:
            raise RequestError('Enter the full phone number.')
    return value[:255]


def submit(student, field, new_value, user, note=''):
    if field not in dict(ProfileChangeRequest.FIELDS):
        raise RequestError('Choose what needs correcting.')
    value = clean_new_value(field, new_value)
    current = display(student, field)
    if current == value or (field == 'date_of_birth' and student.date_of_birth
                            and student.date_of_birth.isoformat() == value):
        raise RequestError('That is already what the school has.')
    if ProfileChangeRequest.objects.filter(tenant_id=student.tenant_id, student=student,
                                           field=field, status=ProfileChangeRequest.PENDING).exists():
        raise RequestError('You already asked to change this — the school will review it soon.')
    return ProfileChangeRequest.objects.create(
        tenant_id=student.tenant_id, student=student, field=field, current_value=current,
        new_value=value, note=(note or '').strip()[:255], requested_by=user)


@transaction.atomic
def review(request_obj, approve, user, note=''):
    if request_obj.status != ProfileChangeRequest.PENDING:
        raise RequestError('That request was already reviewed.')
    if approve:
        student = request_obj.student
        value = request_obj.new_value
        if request_obj.field == 'date_of_birth':
            value = date.fromisoformat(value)
        setattr(student, request_obj.field, value)
        with audit_source('Parent request', user):
            student.save(update_fields=[request_obj.field])
    request_obj.status = ProfileChangeRequest.APPROVED if approve else ProfileChangeRequest.REJECTED
    request_obj.reviewed_by = user
    request_obj.reviewed_at = timezone.now()
    request_obj.review_note = (note or '').strip()[:255]
    request_obj.save(update_fields=['status', 'reviewed_by', 'reviewed_at', 'review_note'])
    return request_obj
