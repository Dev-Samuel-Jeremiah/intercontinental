"""
security/password_help.py
─────────────────────────
Password resets for people with no email address on their account.

The person asks online (or at the office). An administrator checks who they
are and gives them a temporary password in person or by phone. Nothing is
sent anywhere, and the public form never says whether an account exists.

    Staff    → new password; they must choose their own at the next sign-in
    Student  → new term password (what the student types to sign in)
    Parent   → new parent password for that child (with the admission number)
"""
from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from . import services as svc
from .models import PasswordHelpRequest

PER_IP_PER_HOUR = 5


class PasswordHelpError(Exception):
    pass


def find_account(tenant, kind, identifier):
    """(user, student) for this school only; (None, None) when nothing matches."""
    from users.models import Student, User
    ident = (identifier or '').strip()
    if not ident:
        return None, None
    if kind == 'STAFF':
        user = (User._default_manager.filter(tenant=tenant, username__iexact=ident, is_active=True,
                                             is_student=False, is_parent=False)
                .filter(Q(is_staff_user=True) | Q(is_superuser=True) | Q(staff_profiles__tenant=tenant))
                .distinct().first())
        return user, None
    student = Student._default_manager.filter(tenant=tenant, admission_number__iexact=ident).first()
    if student is None:
        return None, None
    return (student.user if kind == 'STUDENT' else None), student


def ask(tenant, data, request=None):
    """A public request. Returns the request, or None when it was quietly dropped
    (a repeat of one still waiting). Never reveals whether the account exists."""
    kind = data.get('kind', '')
    if kind not in dict(PasswordHelpRequest.KINDS):
        raise PasswordHelpError('Choose who the password is for.')
    identifier = (data.get('identifier') or '').strip()[:150]
    name = (data.get('name') or '').strip()[:120]
    phone = (data.get('phone') or '').strip()[:30]
    if not identifier or not name or not phone:
        raise PasswordHelpError('Fill in every box so the school can find you and call you back.')
    ip = svc.client_ip(request)
    if ip and PasswordHelpRequest._default_manager.filter(
            tenant=tenant, ip=ip, created_at__gte=timezone.now() - timedelta(hours=1)).count() >= PER_IP_PER_HOUR:
        raise PasswordHelpError('Too many requests from this connection. Please try again in an hour.')
    if PasswordHelpRequest._default_manager.filter(tenant=tenant, kind=kind, identifier__iexact=identifier,
                                                   status='OPEN').exists():
        return None
    user, student = find_account(tenant, kind, identifier)
    req = PasswordHelpRequest._default_manager.create(
        tenant=tenant, kind=kind, identifier=identifier, name=name, phone=phone,
        note=(data.get('note') or '').strip()[:500], user=user, student=student, ip=ip)
    _tell_admins(req)
    return req


def _tell_admins(req):
    from django.urls import reverse
    from notifications import services as notify
    notify.to_admins(req.tenant, 'password', f'Password help: {req.name} ({req.get_kind_display().lower()})',
                     body=f'{req.identifier} · call back on {req.phone}' + ('' if req.matched else ' · no account matches'),
                     url=reverse('security:password_help'), icon='bi-key', level='warning',
                     dedupe=f'password-help:{req.pk}')


def can_reset(actor, req):
    """Only a school superuser may reset another superuser's password."""
    if req.kind == 'STAFF' and req.user is not None and req.user.is_superuser and not actor.is_superuser:
        return False
    return True


def account_label(req):
    if req.kind == 'STAFF' and req.user:
        staff = req.user.staff_profiles.filter(tenant=req.tenant).first()
        return f'{staff.full_name} · {staff.get_role_display()}' if staff else (req.user.get_full_name() or req.user.username)
    if req.student:
        cls = req.student.class_assigned
        who = req.student.full_name + (f' · {cls}' if cls else '') + f' · {req.student.status}'
        return (f'Parent of {who}' + (f' · parent on file: {req.student.parent_name}' if req.student.parent_name else '')
                if req.kind == 'PARENT' else who)
    return ''


def phone_on_file(req):
    if req.kind == 'STAFF' and req.user:
        from hr.models import StaffRecord
        rec = StaffRecord._default_manager.filter(tenant=req.tenant, staff__user=req.user).first()
        return rec.phone if rec else ''
    return (req.student.parent_phone or '') if req.student else ''


@transaction.atomic
def reset(req, actor, request=None):
    """Give the matched account a temporary password. Returns it (shown to the admin once)."""
    from users.passwords import generate_term_password
    req = PasswordHelpRequest._default_manager.select_for_update().get(pk=req.pk)
    if req.status != 'OPEN':
        raise PasswordHelpError('This request has already been dealt with.')
    if not req.matched:
        raise PasswordHelpError('No account matches this request, so there is nothing to reset.')
    if not can_reset(actor, req):
        raise PasswordHelpError("Only the school's main administrator can reset this account.")
    password = generate_term_password()
    if req.kind == 'STAFF':
        user = req.user
        user.set_password(password)
        user.must_change_password = True
        user.save(update_fields=['password', 'must_change_password'])
        svc.remember_password(user, user.password)
        who = user.username
    elif req.kind == 'STUDENT':
        user = req.student.user
        user.set_password(password)
        user.term_password = password
        user.save(update_fields=['password', 'term_password'])
        who = req.student.admission_number
    else:
        req.student.parent_term_password = password
        req.student.save(update_fields=['parent_term_password'])
        who = req.student.admission_number
    if request is not None:
        svc.clear_failures(request, who)          # an account locked by wrong guesses can sign in again
    req.status, req.handled_at, req.handled_by = 'DONE', timezone.now(), actor
    req.save(update_fields=['status', 'handled_at', 'handled_by'])
    svc.log_event('PASSWORD', f'Password reset by the office for {req.get_kind_display().lower()} {who} '
                              f'(asked by {req.name}, {req.phone})', request=request, user=actor, obj=req)
    return password


def decline(req, actor, request=None):
    if req.status != 'OPEN':
        raise PasswordHelpError('This request has already been dealt with.')
    req.status, req.handled_at, req.handled_by = 'DECLINED', timezone.now(), actor
    req.save(update_fields=['status', 'handled_at', 'handled_by'])
    svc.log_event('ACCOUNT', f'Password help declined for {req.identifier} (asked by {req.name})',
                  request=request, user=actor, obj=req)


def at_desk(tenant, data, actor, request=None):
    """Reset for someone standing at the office: record it, then reset it straight away."""
    kind = data.get('kind', '')
    if kind not in dict(PasswordHelpRequest.KINDS):
        raise PasswordHelpError('Choose whose password to reset.')
    identifier = (data.get('identifier') or '').strip()[:150]
    user, student = find_account(tenant, kind, identifier)
    if (kind == 'STAFF' and user is None) or (kind != 'STAFF' and student is None):
        raise PasswordHelpError('No staff username or admission number matches that.' if identifier
                                else 'Type the staff username or the admission number.')
    req = PasswordHelpRequest._default_manager.create(
        tenant=tenant, kind=kind, identifier=identifier, user=user, student=student, from_desk=True,
        name=(data.get('name') or '').strip()[:120] or 'At the office',
        phone=(data.get('phone') or '').strip()[:30], ip=svc.client_ip(request))
    try:
        return req, reset(req, actor, request)
    except PasswordHelpError:
        req.delete()
        raise
