"""
security/signals.py
───────────────────
Sign-in events (sessions, devices, two-step sign-in, audit) and the audit
trail for sensitive changes made anywhere in the system (F444).
"""
import logging
import threading

from django.contrib.auth.signals import user_logged_in, user_logged_out
from django.db.models.signals import post_delete, post_save, pre_delete, pre_save
from django.dispatch import receiver

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Sign-in
# ─────────────────────────────────────────────────────────────────────────────

@receiver(user_logged_in)
def on_login(sender, request, user, **kwargs):
    if request is None or not hasattr(request, 'session'):
        return
    from . import services as svc
    try:
        if svc.two_factor_for(user):
            request.session['sec_2fa'] = 'pending'
        else:
            request.session.pop('sec_2fa', None)
        request.session['sec_seen'] = 0
        if request.META.get('REMOTE_ADDR') or request.META.get('HTTP_USER_AGENT'):   # a real browser request
            svc.track_login(request, user)
            if not getattr(request, '_sec_impersonation', False):
                svc.check_device(request, user)
        svc.log_event('LOGIN', f'{user.username} signed in', request=request, user=user,
                      tenant=getattr(request, 'tenant', None) or getattr(user, 'tenant', None))
    except Exception:
        log.exception('Sign-in bookkeeping failed')


@receiver(user_logged_out)
def on_logout(sender, request, user, **kwargs):
    if request is None or user is None:
        return
    from . import services as svc
    try:
        key = getattr(request.session, 'session_key', None)
        if key:
            svc.end_session_record(key, 'Signed out')
        svc.log_event('LOGOUT', f'{user.username} signed out', request=request, user=user,
                      tenant=getattr(request, 'tenant', None) or getattr(user, 'tenant', None))
    except Exception:
        log.exception('Sign-out bookkeeping failed')


# ─────────────────────────────────────────────────────────────────────────────
# Watched changes
# ─────────────────────────────────────────────────────────────────────────────

USER_FIELDS = {'is_active': 'active', 'is_superuser': 'administrator', 'restricted': 'restricted',
               'is_staff_user': 'staff access'}


def _staff_fields():
    from users.models import Staff
    return ['role'] + [f.name for f in Staff._meta.fields if f.name.startswith('can_')]


@receiver(pre_save, sender='users.User')
def user_before(sender, instance, update_fields=None, **kwargs):
    if not instance.pk:
        return
    watched = set(USER_FIELDS) | {'password'}
    if update_fields is not None and not (set(update_fields) & watched):
        return
    old = sender._base_manager.filter(pk=instance.pk).values(*watched).first()
    instance._sec_old = old


@receiver(post_save, sender='users.User')
def user_after(sender, instance, created, **kwargs):
    from . import services as svc
    old = getattr(instance, '_sec_old', None)
    instance._sec_old = None
    try:
        if created:
            if instance.password and instance.has_usable_password() and not instance.is_student \
                    and not instance.is_parent:
                svc.remember_password(instance, instance.password)
            return
        if not old:
            return
        if old['password'] != instance.password and instance.has_usable_password():
            svc.remember_password(instance, instance.password)
            if not getattr(instance, 'is_student', False) and not getattr(instance, 'is_parent', False):
                svc.log_event('PASSWORD', f'Password changed for {instance.username}',
                              tenant=getattr(instance, 'tenant', None), obj=instance)
        changes = {label: [old[f], getattr(instance, f)] for f, label in USER_FIELDS.items()
                   if old[f] != getattr(instance, f)}
        if changes:
            text = ', '.join(f'{k} {"on" if v[1] else "off"}' for k, v in changes.items())
            svc.log_event('ACCOUNT', f'{instance.username}: {text}', tenant=getattr(instance, 'tenant', None),
                          obj=instance, changes=changes)
    except Exception:
        log.exception('User audit failed')


@receiver(pre_save, sender='users.Staff')
def staff_before(sender, instance, **kwargs):
    if instance.pk:
        instance._sec_old = sender._base_manager.filter(pk=instance.pk).values(*_staff_fields()).first()


@receiver(post_save, sender='users.Staff')
def staff_after(sender, instance, created, **kwargs):
    from . import services as svc
    old = getattr(instance, '_sec_old', None)
    instance._sec_old = None
    if created or not old:
        return
    try:
        labels = {n: label for n, _, label in svc.permission_fields()}
        changes = {f: [old[f], getattr(instance, f)] for f in _staff_fields() if old[f] != getattr(instance, f)}
        if not changes:
            return
        parts = []
        if 'role' in changes:
            parts.append(f'role {changes["role"][0]} → {changes["role"][1]}')
        on = [labels.get(f, f) for f, v in changes.items() if f != 'role' and v[1]]
        off = [labels.get(f, f) for f, v in changes.items() if f != 'role' and not v[1]]
        if on:
            parts.append('can now: ' + ', '.join(on).lower())
        if off:
            parts.append('can no longer: ' + ', '.join(off).lower())
        svc.log_event('PERMISSIONS', f'{instance.full_name}: ' + '; '.join(parts), tenant=instance.tenant,
                      obj=instance, changes={k: list(v) for k, v in changes.items()})
    except Exception:
        log.exception('Staff audit failed')


AUDITED_DELETES = ['users.Student', 'users.Staff', 'users.Guardian', 'users.StudentDocument', 'finance.Payment',
                   'finance.Invoice', 'finance.FeePayment', 'finance.Expense', 'admissions.Application',
                   'welfare.MedicalProfile', 'welfare.Concern', 'welfare.CounsellingNote', 'results.HeadTeacherSignature',
                   'results.TermResult', 'results.ClassApproval', 'finance.FeeStructure', 'finance.FeeDiscount',
                   'finance.Refund', 'cbt.Exam', 'cbt.Question', 'labs.Lab', 'labs.Breakage', 'moments.Moment',
                   'academics.Term', 'academics.AcademicSession']


# Schools being deleted right now. Their rows go with them (audit trail included),
# so logging each cascaded row would only point new audit entries at a tenant
# that no longer exists.
_closing = threading.local()


def _closing_ids():
    if not hasattr(_closing, 'ids'):
        _closing.ids = set()
    return _closing.ids


@receiver(pre_delete, sender='tenants.Tenant')
def tenant_closing(sender, instance, **kwargs):
    _closing_ids().add(instance.pk)


@receiver(post_delete, sender='tenants.Tenant')
def tenant_closed(sender, instance, **kwargs):
    _closing_ids().discard(instance.pk)


def _on_delete(sender, instance, **kwargs):
    from . import services as svc
    tenant_id = getattr(instance, 'tenant_id', None)
    if tenant_id is not None and tenant_id in _closing_ids():
        return
    try:
        label = str(instance)[:200]
    except Exception:
        label = f'#{instance.pk}'
    if sender._meta.label in ('welfare.Concern', 'welfare.CounsellingNote', 'welfare.MedicalProfile'):
        label = f'#{instance.pk}'        # never copy sensitive text into the audit trail
    tenant = None
    if tenant_id is not None:
        from tenants.models import Tenant
        tenant = Tenant.objects.filter(pk=tenant_id).first()
        if tenant is None:
            return                      # the school itself was deleted in this same cascade
    svc.log_event('DELETED', f'{sender._meta.verbose_name.title()} deleted: {label}', tenant=tenant, obj=instance)


def connect_deletes():
    from django.apps import apps
    for label in AUDITED_DELETES:
        try:
            model = apps.get_model(label)
        except LookupError:
            continue
        post_delete.connect(_on_delete, sender=model, dispatch_uid=f'security-delete-{label}', weak=False)
