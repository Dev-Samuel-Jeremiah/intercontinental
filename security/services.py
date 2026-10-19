"""
security/services.py
────────────────────
The logic behind the Security, Privacy & Compliance pages. Views, middleware
and signals stay thin and call into here.
"""
import hashlib
import io
import logging
import re
import secrets
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.contrib.auth.hashers import check_password
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.html import escape

from .models import (AuditEvent, Breach, BreachAction, Consent, DataPurpose, DataRequest, KnownDevice,
                     LoginFailure, PasswordHistory, PrivacyNotice, RetentionRule, Role, RoleAssignment,
                     SecuritySettings, TwoFactor, UserSession, VulnerabilityReport)

log = logging.getLogger(__name__)


class SecurityError(ValueError):
    pass


# ─────────────────────────────────────────────────────────────────────────────
# Small helpers
# ─────────────────────────────────────────────────────────────────────────────

def client_ip(request):
    if request is None:
        return None
    raw = request.META.get('HTTP_X_REAL_IP') or (request.META.get('HTTP_X_FORWARDED_FOR') or '').split(',')[0] \
        or request.META.get('REMOTE_ADDR') or ''
    raw = raw.strip()
    return raw if re.match(r'^[0-9a-fA-F:.]{3,45}$', raw) else None


def user_agent(request):
    return (request.META.get('HTTP_USER_AGENT', '') if request is not None else '')[:255]


def device_label(ua):
    ua = ua or ''
    browser = next((name for key, name in (('Edg/', 'Edge'), ('OPR/', 'Opera'), ('Chrome/', 'Chrome'),
                                            ('Firefox/', 'Firefox'), ('Safari/', 'Safari')) if key in ua), 'Browser')
    system = next((name for key, name in (('Android', 'Android'), ('iPhone', 'iPhone'), ('iPad', 'iPad'),
                                           ('Windows', 'Windows'), ('Mac OS', 'Mac'), ('Linux', 'Linux')) if key in ua),
                  'unknown device')
    return f'{browser} on {system}'


def _email(to, subject, html):
    if not to:
        return
    from communications.services import send_email
    try:
        send_email(to, subject, html)
    except Exception:
        log.exception('Security email failed')


def staff_of(user, tenant):
    from users.models import Staff
    if not getattr(user, 'is_authenticated', False) or tenant is None:
        return None
    return Staff.objects.filter(user=user, tenant=tenant).first()


def is_admin(user, tenant=None):
    """School administrator (superuser inside the school or ADMIN role), or platform staff."""
    if not getattr(user, 'is_authenticated', False):
        return False
    if user.is_superuser:
        return True
    tenant = tenant or getattr(user, 'tenant', None)
    s = staff_of(user, tenant)
    return bool(s and s.role == 'ADMIN')


def is_staff_user(user, tenant=None):
    return bool(getattr(user, 'is_authenticated', False) and (
        user.is_superuser or getattr(user, 'is_staff_user', False) or staff_of(user, tenant or user.tenant)))


def settings_for(tenant):
    if tenant is None:
        return SecuritySettings(require_2fa=getattr(settings, 'PLATFORM_2FA', 'ADMINS'))
    cfg = SecuritySettings.objects.filter(tenant=tenant).first()
    if cfg is None:
        cfg = SecuritySettings.objects.create(tenant=tenant)
    return cfg


def admin_emails(tenant):
    if tenant is None:
        from saas_admin.services import platform_emails
        return platform_emails()
    from hr.services import admins_emails
    out = set(admins_emails(tenant))
    n = PrivacyNotice.objects.filter(tenant=tenant).first()
    if n and n.dpo_email:
        out.add(n.dpo_email)
    return sorted(out)


def next_ref(model, prefix, tenant=None):
    stem = f'{prefix}-{timezone.localdate():%Y}-'
    n = model._base_manager.filter(reference__startswith=stem).count()
    for i in range(n + 1, n + 200):
        ref = f'{stem}{i:04d}'
        if not model._base_manager.filter(reference=ref).exists():
            return ref
    return f'{stem}{secrets.token_hex(3).upper()}'


# ─────────────────────────────────────────────────────────────────────────────
# F444 — audit trail
# ─────────────────────────────────────────────────────────────────────────────

ACTIONS = {
    'LOGIN': ('AUTH', 'Signed in'), 'LOGOUT': ('AUTH', 'Signed out'), 'LOGIN_FAILED': ('AUTH', 'Failed sign-in'),
    'LOCKED': ('AUTH', 'Locked out'), '2FA_ON': ('AUTH', 'Two-step sign-in on'),
    '2FA_OFF': ('AUTH', 'Two-step sign-in off'), '2FA_FAILED': ('AUTH', 'Wrong two-step code'),
    'NEW_DEVICE': ('AUTH', 'New device'), 'SESSION_REVOKED': ('AUTH', 'Session signed out'),
    'PASSWORD': ('AUTH', 'Password changed'), 'PERMISSIONS': ('ACCESS', 'Permissions changed'),
    'ROLE': ('ACCESS', 'Role changed'), 'ACCOUNT': ('ACCESS', 'Account changed'),
    'CREATED': ('DATA', 'Created'), 'UPDATED': ('DATA', 'Changed'), 'DELETED': ('DATA', 'Deleted'),
    'EXPORT': ('PRIVACY', 'Data exported'), 'CONSENT': ('PRIVACY', 'Consent'), 'REQUEST': ('PRIVACY', 'Data request'),
    'ANONYMISED': ('PRIVACY', 'Anonymised'), 'BREACH': ('PRIVACY', 'Breach'), 'RETENTION': ('PRIVACY', 'Retention'),
    'SETTINGS': ('ADMIN', 'Settings changed'), 'IMPERSONATION': ('ACCESS', 'Support sign-in'),
    'SCORES': ('RESULTS', 'Scores changed'), 'APPROVAL': ('RESULTS', 'Results approval'),
    'EXAM': ('RESULTS', 'Exam changed'), 'PAYMENT': ('MONEY', 'Payment'), 'FEES': ('MONEY', 'Fees & charges'),
    'STUDENT': ('DATA', 'Student record'), 'DENIED': ('ACCESS', 'Access refused'),
    'BACKUP': ('ADMIN', 'Backup'),
}


def log_event(action, summary, request=None, user=None, tenant: Any = 'current', obj=None, changes=None,
              category=None):
    """Write one audit event. Never raises: auditing must not break the action being audited."""
    try:
        if user is None and request is not None and getattr(request, 'user', None) is not None \
                and request.user.is_authenticated:
            user = request.user
        if user is None:
            from users.audit import get_current_user
            user = get_current_user()
        if tenant == 'current':
            tenant = getattr(request, 'tenant', None) if request is not None else None
            if tenant is None:
                from tenants.middleware import get_current_tenant
                tenant = get_current_tenant()
            if tenant is None and user is not None:
                tenant = getattr(user, 'tenant', None)
        if request is None:
            from users.audit import _local
            request = getattr(_local, 'request', None)
        return AuditEvent.objects.create(
            tenant=tenant, user=user if getattr(user, 'pk', None) else None,
            username=(getattr(user, 'username', '') or '')[:150], action=action[:30],
            category=category or ACTIONS.get(action, ('DATA',))[0],
            object_type=obj._meta.verbose_name.title()[:60] if obj is not None else '',
            object_id=str(getattr(obj, 'pk', '') or '')[:64], summary=str(summary)[:500], changes=changes or {},
            ip=client_ip(request), user_agent=user_agent(request)[:200])
    except Exception:
        log.exception('Audit event could not be written')
        return None


def audit_feed(tenant, q='', category='', user_id='', since=None, until=None, limit=300):
    """Audit events, finance audit entries and student record changes, newest first."""
    rows = []
    from django.db.models import Q
    # The school's events, plus platform-wide ones every school should see (backups)
    ev = AuditEvent.objects.filter(Q(tenant=tenant) | Q(tenant__isnull=True, action='BACKUP')).select_related('user')
    if category:
        ev = ev.filter(category=category)
    if user_id:
        ev = ev.filter(user_id=user_id)
    if since:
        ev = ev.filter(at__date__gte=since)
    if until:
        ev = ev.filter(at__date__lte=until)
    if q:
        ev = ev.filter(Q(summary__icontains=q) | Q(username__icontains=q) | Q(object_type__icontains=q))
    for e in ev[:limit]:
        rows.append({'at': e.at, 'who': e.username or 'System', 'category': e.get_category_display(),
                     'action': ACTIONS.get(e.action, ('', e.action))[1], 'summary': e.summary, 'ip': e.ip or '',
                     'source': 'Security', 'changes': e.changes})
    if category in ('', 'DATA'):
        try:
            from finance.models import AuditLog
            fl = AuditLog.objects.filter(tenant=tenant).select_related('performed_by')
            if user_id:
                fl = fl.filter(performed_by_id=user_id)
            if since:
                fl = fl.filter(created_at__date__gte=since)
            if until:
                fl = fl.filter(created_at__date__lte=until)
            if q:
                fl = fl.filter(description__icontains=q)
            for e in fl[:limit]:
                rows.append({'at': e.created_at, 'who': getattr(e.performed_by, 'username', '') or 'System',
                             'category': 'Records', 'action': e.get_action_display(), 'summary': e.description[:500],
                             'ip': e.ip_address or '', 'source': 'Finance', 'changes': {}})
        except Exception:
            log.exception('Finance audit feed failed')
        from users.models import StudentChange
        sc = StudentChange.objects.filter(tenant=tenant).select_related('student', 'changed_by')
        if user_id:
            sc = sc.filter(changed_by_id=user_id)
        if since:
            sc = sc.filter(changed_at__date__gte=since)
        if until:
            sc = sc.filter(changed_at__date__lte=until)
        if q:
            sc = sc.filter(Q(student__full_name__icontains=q) | Q(label__icontains=q))
        for c in sc[:limit]:
            rows.append({'at': c.changed_at, 'who': getattr(c.changed_by, 'username', '') or 'System',
                         'category': 'Records', 'action': 'Student record',
                         'summary': f'{c.student.full_name}: {c.label} changed' + (f' ({c.source})' if c.source else ''),
                         'ip': '', 'source': 'Students', 'changes': {}})
    rows.sort(key=lambda r: r['at'], reverse=True)
    return rows[:limit]


def feed_csv(rows):
    import csv
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(['When', 'Who', 'Category', 'Action', 'Details', 'IP address', 'Source'])
    for r in rows:
        w.writerow([timezone.localtime(r['at']).strftime('%Y-%m-%d %H:%M:%S'), r['who'], r['category'], r['action'],
                    r['summary'], r['ip'], r['source']])
    return out.getvalue()


# ─────────────────────────────────────────────────────────────────────────────
# F442 — two-step sign-in
# ─────────────────────────────────────────────────────────────────────────────

CODE_TTL = 600


def two_factor_for(user):
    tf = TwoFactor.objects.filter(user=user).first() if getattr(user, 'pk', None) else None
    return tf if tf and tf.active else None


def required_2fa(user):
    """Does the school's policy (or the platform's) require two-step sign-in for this user?"""
    if not getattr(user, 'is_authenticated', False) or getattr(user, 'is_student', False) \
            or getattr(user, 'is_parent', False):
        return False
    tenant = getattr(user, 'tenant', None)
    policy = settings_for(tenant).require_2fa
    if policy == 'STAFF':
        return is_staff_user(user, tenant)
    if policy == 'ADMINS':
        return is_admin(user, tenant)
    return False


def _hash_code(code):
    return hashlib.sha256(('2fa:' + settings.SECRET_KEY + ':' + code.strip().lower().replace('-', '')).encode()) \
        .hexdigest()


def start_setup(user, method, phone=''):
    from . import totp
    if method not in dict(TwoFactor.METHODS):
        raise SecurityError('Choose how you want to get your codes.')
    if method == 'EMAIL' and not user.email:
        raise SecurityError('Add an email address to your profile first, or use an authenticator app.')
    phone = re.sub(r'[^\d+]', '', phone or '')
    if method == 'SMS' and len(phone) < 10:
        raise SecurityError('Enter the mobile number the codes should go to.')
    tf, _ = TwoFactor.objects.get_or_create(user=user)
    if tf.active:
        raise SecurityError('Two-step sign-in is already on. Turn it off first to change method.')
    tf.method, tf.phone, tf.secret = method, phone, totp.new_secret()
    tf.last_step, tf.backup_codes = 0, []
    tf.save()
    return tf


def send_code(request, tf):
    """Email or text a 6-digit code (kept hashed in the session for 10 minutes).

    Returns True if the email or SMS provider accepted it, False if sending failed."""
    code = f'{secrets.randbelow(1000000):06d}'
    request.session['sec_code'] = {'h': _hash_code(code), 'exp': timezone.now().timestamp() + CODE_TTL, 'tries': 0}
    tenant = getattr(request, 'tenant', None)
    name = getattr(tenant, 'name', 'School Portal')
    if settings.DEBUG:
        # Local development only: email/SMS is often not set up, so don't lock the developer out.
        print(f'[DEV] two-step code for {tf.user.username}: {code}', flush=True)
    try:
        if tf.method == 'SMS':
            from communications.services import send_sms
            ok, detail = send_sms(tf.phone, f'{code} is your {name} sign-in code. It expires in 10 minutes. '
                                            f'Never share it.')
        else:
            from communications.services import send_email
            ok, detail = send_email(
                tf.user.email, f'Your {name} sign-in code: {code}',
                f'<p>Your sign-in code is <b style="font-size:20px;letter-spacing:3px">{code}</b>.</p>'
                f'<p>It expires in 10 minutes. If you did not try to sign in, change your password now.</p>')
    except Exception as e:
        ok, detail = False, str(e)
    if not ok:
        log.error('2FA %s code for %s could not be sent: %s', tf.method, tf.user.username, detail)
    return ok


def check_code(request, tf, code):
    """True if the code is right: authenticator code, emailed/texted code, or an unused backup code."""
    from . import totp
    code = (code or '').strip()
    if not code:
        return False
    if tf.method == 'TOTP':
        step = totp.verify(tf.secret, code, tf.last_step)
        if step:
            tf.last_step, tf.last_used_at = step, timezone.now()
            tf.save(update_fields=['last_step', 'last_used_at'])
            return True
    else:
        held = request.session.get('sec_code') or {}
        if held and held.get('exp', 0) > timezone.now().timestamp() and held.get('tries', 0) < 5:
            if secrets.compare_digest(held.get('h', ''), _hash_code(code)):
                request.session.pop('sec_code', None)
                tf.last_used_at = timezone.now()
                tf.save(update_fields=['last_used_at'])
                return True
            held['tries'] = held.get('tries', 0) + 1
            request.session['sec_code'] = held
    h = _hash_code(code)
    if tf.active and h in (tf.backup_codes or []):
        tf.backup_codes = [c for c in tf.backup_codes if c != h]
        tf.last_used_at = timezone.now()
        tf.save(update_fields=['backup_codes', 'last_used_at'])
        return True
    return False


def new_backup_codes(tf, n=10):
    codes = [f'{secrets.token_hex(2)}-{secrets.token_hex(2)}' for _ in range(n)]
    tf.backup_codes = [_hash_code(c) for c in codes]
    tf.save(update_fields=['backup_codes'])
    return codes


def confirm_setup(request, tf, code):
    if not check_code(request, tf, code):
        raise SecurityError('That code is not right. Check the time on your phone and try again.')
    tf.confirmed_at = timezone.now()
    tf.save(update_fields=['confirmed_at'])
    codes = new_backup_codes(tf)
    request.session['sec_2fa'] = 'ok'
    log_event('2FA_ON', f'Two-step sign-in switched on ({tf.get_method_display().lower()})', request=request)
    _email(tf.user.email, 'Two-step sign-in is now on',
           '<p>Two-step sign-in was switched on for your account. Keep your backup codes somewhere safe.</p>')
    return codes


def disable_2fa(user, by, request=None):
    n = TwoFactor.objects.filter(user=user).delete()[0]
    if n:
        who = 'themselves' if by == user else f'by {by.get_full_name() or by.username}'
        log_event('2FA_OFF', f'Two-step sign-in switched off for {user.username} ({who})', request=request, user=by,
                  tenant=getattr(user, 'tenant', None))
        _email(user.email, 'Two-step sign-in was switched off',
               '<p>Two-step sign-in was switched off for your account. If this was not you or your administrator, '
               'change your password and tell the school straight away.</p>')
    return bool(n)


def qr_svg(data):
    import qrcode
    import qrcode.image.svg
    img = qrcode.make(data, image_factory=qrcode.image.svg.SvgPathImage, box_size=8, border=2)
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue().decode()


# ─────────────────────────────────────────────────────────────────────────────
# F446 — failed sign-ins and lockout
# ─────────────────────────────────────────────────────────────────────────────

LOGIN_FIELDS = {'/staff/login/': 'username', '/student/login/': 'admission_number',
                '/parent/login/': 'admission_number', '/admin/login/': 'username'}
IP_LIMIT = 30


def _identity(raw):
    return (raw or '').strip().lower()[:150]


def lock_minutes_left(request, identity):
    """Minutes until this account (or this IP address) may try again; 0 if not locked."""
    cfg = settings_for(getattr(request, 'tenant', None))
    host = request.get_host().split(':')[0].lower()
    window = timezone.now() - timedelta(minutes=cfg.lockout_minutes)
    ident = _identity(identity)
    if ident:
        fails = list(LoginFailure.objects.filter(host=host, identity=ident, at__gte=window)
                     .order_by('-at').values_list('at', flat=True)[:cfg.lockout_attempts])
        if len(fails) >= cfg.lockout_attempts:
            return max(1, int((fails[0] + timedelta(minutes=cfg.lockout_minutes) - timezone.now())
                              .total_seconds() // 60) + 1)
    ip = client_ip(request)
    if ip and LoginFailure.objects.filter(ip=ip, at__gte=timezone.now() - timedelta(minutes=15)).count() >= IP_LIMIT:
        return 15
    return 0


def record_failure(request, identity):
    cfg = settings_for(getattr(request, 'tenant', None))
    host = request.get_host().split(':')[0].lower()
    ident = _identity(identity)
    LoginFailure.objects.create(host=host, identity=ident, ip=client_ip(request), path=request.path[:60])
    log_event('LOGIN_FAILED', f'Failed sign-in for "{ident or "?"}" on {request.path}', request=request, user=None)
    if ident and lock_minutes_left(request, ident):
        recent = LoginFailure.objects.filter(host=host, identity=ident,
                                             at__gte=timezone.now() - timedelta(minutes=cfg.lockout_minutes)).count()
        if recent == cfg.lockout_attempts:        # just locked
            log_event('LOCKED', f'"{ident}" locked out for {cfg.lockout_minutes} minutes after {recent} failed '
                                f'sign-ins', request=request, user=None)
            tenant = getattr(request, 'tenant', None)
            from users.models import User
            target = User.objects.filter(tenant=tenant, username__iexact=ident).exclude(email='').first() \
                if tenant else None
            when = timezone.localtime().strftime('%d %b %Y %H:%M')
            if target:
                _email(target.email, 'Your account was locked after failed sign-ins',
                       f'<p>There were {recent} failed attempts to sign in to your account ({escape(ident)}) on '
                       f'{when}. Sign-in is paused for {cfg.lockout_minutes} minutes.</p><p>If this was not you, '
                       f'change your password and switch on two-step sign-in.</p>')
            if cfg.notify_admins_lockout and tenant is not None:
                for e in admin_emails(tenant):
                    _email(e, f'Account locked: {ident}',
                           f'<p>{recent} failed sign-ins for <b>{escape(ident)}</b> from {client_ip(request) or "?"} '
                           f'on {when}. Sign-in is paused for {cfg.lockout_minutes} minutes.</p>')


def clear_failures(request, identity):
    host = request.get_host().split(':')[0].lower()
    LoginFailure.objects.filter(host=host, identity=_identity(identity)).delete()


# ─────────────────────────────────────────────────────────────────────────────
# F445 — new device alerts
# ─────────────────────────────────────────────────────────────────────────────

DEVICE_COOKIE = 'tm_dev'


def _token_hash(token):
    return hashlib.sha256(('device:' + token).encode()).hexdigest()


def check_device(request, user):
    """Remember the browser; email the user the first time a new one signs in. Returns (device, is_new)."""
    token = request.COOKIES.get(DEVICE_COOKIE, '')
    if not re.match(r'^[A-Za-z0-9_-]{20,64}$', token):
        token = secrets.token_urlsafe(24)
        request._sec_device_token = token
    h = _token_hash(token)
    ua = user_agent(request)
    dev = KnownDevice.objects.filter(user=user, token_hash=h).first()
    if dev:
        dev.ip = client_ip(request)
        dev.save(update_fields=['ip', 'last_seen'])
        return dev, False
    had_devices = KnownDevice.objects.filter(user=user).exists()
    dev = KnownDevice.objects.create(user=user, token_hash=h, label=device_label(ua), ip=client_ip(request))
    staffish = user.is_superuser or getattr(user, 'is_staff_user', False)
    if had_devices and staffish and settings_for(getattr(user, 'tenant', None)).alert_new_device:
        log_event('NEW_DEVICE', f'{user.username} signed in from a new device: {dev.label}', request=request, user=user)
        when = timezone.localtime().strftime('%d %b %Y at %H:%M')
        host = request.get_host()
        _email(user.email, 'New sign-in to your account',
               f'<p>Your account was just used to sign in from a new device:</p>'
               f'<p><b>{escape(dev.label)}</b><br>IP address {escape(dev.ip or "unknown")}<br>{when}</p>'
               f'<p>If this was you, there is nothing to do. If not, change your password now and sign out of other '
               f'devices at <a href="https://{escape(host)}/security/sessions/">{escape(host)}/security/sessions/</a>.'
               f'</p>')
    return dev, had_devices


# ─────────────────────────────────────────────────────────────────────────────
# F456 — sessions
# ─────────────────────────────────────────────────────────────────────────────

TOUCH_EVERY = timedelta(minutes=5)


def track_login(request, user):
    key = request.session.session_key
    if not key:
        request.session.save()
        key = request.session.session_key
    UserSession.objects.update_or_create(session_key=key, defaults={
        'user': user, 'tenant': getattr(request, 'tenant', None) or getattr(user, 'tenant', None),
        'ip': client_ip(request), 'user_agent': user_agent(request), 'last_seen': timezone.now(),
        'ended_at': None, 'ended_reason': ''})


def touch(request):
    key = request.session.session_key
    if not key:
        return
    stamp = request.session.get('sec_touch', 0)
    now = timezone.now()
    if now.timestamp() - stamp < TOUCH_EVERY.total_seconds():
        return
    request.session['sec_touch'] = now.timestamp()
    n = UserSession.objects.filter(session_key=key, ended_at__isnull=True).update(last_seen=now, ip=client_ip(request))
    if not n:
        track_login(request, request.user)


def end_session_record(key, reason):
    UserSession.objects.filter(session_key=key, ended_at__isnull=True).update(ended_at=timezone.now(),
                                                                              ended_reason=reason[:40])


def revoke_session(us, by, request=None, reason='Signed out remotely'):
    from importlib import import_module
    engine = import_module(settings.SESSION_ENGINE)
    engine.SessionStore(session_key=us.session_key).delete()
    end_session_record(us.session_key, reason)
    log_event('SESSION_REVOKED', f'Session of {us.user.username} on {device_label(us.user_agent)} signed out',
              request=request, user=by, tenant=us.tenant)


def active_sessions(user=None, tenant=None):
    from django.contrib.sessions.models import Session
    qs = UserSession.objects.filter(ended_at__isnull=True).select_related('user')
    if user is not None:
        qs = qs.filter(user=user)
    if tenant is not None:
        qs = qs.filter(tenant=tenant)
    live = set(Session.objects.filter(session_key__in=list(qs.values_list('session_key', flat=True)),
                                      expire_date__gt=timezone.now()).values_list('session_key', flat=True))
    dead = [s.session_key for s in qs if s.session_key not in live]
    if dead and settings.SESSION_ENGINE.endswith('.db'):
        UserSession.objects.filter(session_key__in=dead).update(ended_at=timezone.now(), ended_reason='Expired')
        qs = qs.exclude(session_key__in=dead)
    return list(qs)


def revoke_others(user, keep_key, request=None):
    n = 0
    for us in active_sessions(user=user):
        if us.session_key != keep_key:
            revoke_session(us, user, request, 'Signed out everywhere else')
            n += 1
    return n


# ─────────────────────────────────────────────────────────────────────────────
# F447 — password policy
# ─────────────────────────────────────────────────────────────────────────────

def policy_problems(password, user=None):
    tenant = getattr(user, 'tenant', None) if user is not None else None
    if tenant is None:
        from tenants.middleware import get_current_tenant
        tenant = get_current_tenant()
    cfg = settings_for(tenant)
    out = []
    password = password or ''
    if len(password) < max(cfg.min_length, 8):
        out.append(f'Use at least {max(cfg.min_length, 8)} characters.')
    if cfg.require_mix and not (re.search(r'[A-Za-z]', password) and re.search(r'\d', password)):
        out.append('Use both letters and numbers.')
    if cfg.require_symbol and not re.search(r'[^A-Za-z0-9]', password):
        out.append('Include a symbol such as ! # or @.')
    if cfg.history and user is not None and getattr(user, 'pk', None):
        recent = list(PasswordHistory.objects.filter(user=user).values_list('password', flat=True)[:cfg.history])
        if user.password:
            recent.append(user.password)
        if any(check_password(password, h) for h in recent if h):
            out.append(f'Choose a password you have not used before (the last {cfg.history} are remembered).')
    return out


def password_changed_on(user):
    last = PasswordHistory.objects.filter(user=user).values_list('created_at', flat=True).first()
    return last or user.date_joined


def password_expired(user):
    if getattr(user, 'is_student', False) or getattr(user, 'is_parent', False):
        return False
    cfg = settings_for(getattr(user, 'tenant', None))
    if not cfg.expiry_days:
        return False
    return password_changed_on(user) < timezone.now() - timedelta(days=cfg.expiry_days)


def remember_password(user, password_hash):
    if not password_hash or password_hash.startswith('!'):
        return
    PasswordHistory.objects.create(user=user, password=password_hash)
    old = PasswordHistory.objects.filter(user=user).values_list('pk', flat=True)[24:]
    PasswordHistory.objects.filter(pk__in=list(old)).delete()


# ─────────────────────────────────────────────────────────────────────────────
# F443 — roles
# ─────────────────────────────────────────────────────────────────────────────

PERMISSION_LABELS = {
    'can_generate_password': ('Students', 'Reset student and parent passwords'),
    'can_create_staff': ('Staff', 'Add and edit staff'),
    'can_manage_students': ('Students', 'Add and edit students'),
    'can_manage_exams': ('Exams & results', 'Set up CBT exams'),
    'can_view_all_exams': ('Exams & results', 'See every exam'),
    'can_enter_scores': ('Exams & results', 'Enter scores'),
    'can_view_all_results': ('Exams & results', 'See all results'),
    'can_publish_results': ('Exams & results', 'Publish results'),
    'can_access_finance': ('Finance', 'Open finance'),
    'can_approve_payments': ('Finance', 'Approve payments'),
    'can_access_hostel': ('Boarding', 'Open boarding'),
    'can_manage_boarders': ('Boarding', 'Manage boarders'),
    'can_send_messages': ('Communication', 'Send messages to families'),
    'can_mark_attendance': ('Students', 'Mark attendance'),
    'can_access_inventory': ('Store', 'Open the store and inventory'),
}


def permission_fields():
    from users.models import Staff
    names = [f.name for f in Staff._meta.fields if f.name.startswith('can_') and f.get_internal_type() == 'BooleanField']
    return [(n, *PERMISSION_LABELS.get(n, ('Other', n.replace('can_', '').replace('_', ' ').capitalize())))
            for n in names]


def permission_groups():
    groups = {}
    for name, group, label in permission_fields():
        groups.setdefault(group, []).append((name, label))
    return groups


STARTER_ROLES = [
    ('Bursar', 'ACCOUNT', 'Fees, payments and the family wallet',
     ['can_access_finance', 'can_approve_payments', 'can_send_messages']),
    ('Exams officer', 'TEACHER', 'Exams, scores and publishing results',
     ['can_manage_exams', 'can_view_all_exams', 'can_enter_scores', 'can_view_all_results', 'can_publish_results']),
    ('Form teacher', 'TEACHER', 'Registers, scores and parents of one class',
     ['can_mark_attendance', 'can_enter_scores', 'can_manage_students', 'can_send_messages']),
    ('Boarding master', 'TEACHER', 'Hostels and boarders', ['can_access_hostel', 'can_manage_boarders',
                                                            'can_mark_attendance']),
    ('Store keeper', 'TEACHER', 'Store and inventory', ['can_access_inventory']),
    ('Librarian', 'LIB', 'Library desk and catalogue', []),
]


def save_role(tenant, data, user, role=None):
    from users.models import Staff
    name = (data.get('name') or '').strip()[:80]
    if not name:
        raise SecurityError('Give the role a name.')
    if Role.objects.filter(tenant=tenant, name__iexact=name).exclude(pk=getattr(role, 'pk', None)).exists():
        raise SecurityError('There is already a role with that name.')
    valid = {f for f, _, _ in permission_fields()}
    perms = sorted(p for p in (data.getlist('permissions') if hasattr(data, 'getlist') else data.get('permissions', []))
                   if p in valid)
    base = data.get('base_role') or ''
    if base and base not in dict(Staff.ROLE_CHOICES):
        base = ''
    if base == 'ADMIN':
        raise SecurityError('Administrators already have every permission; give them the Administrator role directly.')
    created = role is None
    role = role or Role(tenant=tenant, created_by=user)
    before = list(role.permissions or [])
    role.name, role.description, role.base_role, role.permissions = name, (data.get('description') or '')[:255], \
        base, perms
    role.save()
    n = sync_role(role, user)
    log_event('ROLE', f'Role "{role.name}" {"created" if created else "changed"}'
                      f'{f" — {n} staff updated" if n else ""}', user=user, tenant=tenant, obj=role,
              changes={'added': sorted(set(perms) - set(before)), 'removed': sorted(set(before) - set(perms))})
    return role


def _apply(role, staff):
    changed = {}
    for name, _, _ in permission_fields():
        want = name in (role.permissions or [])
        if getattr(staff, name) != want:
            changed[name] = want
            setattr(staff, name, want)
    if role.base_role and staff.role != role.base_role and staff.role != 'ADMIN':
        changed['role'] = role.base_role
        staff.role = role.base_role
    if changed:
        staff.save()
    return changed


def sync_role(role, user=None):
    n = 0
    for a in role.assignments.select_related('staff'):
        if _apply(role, a.staff):
            n += 1
    return n


def assign_role(role, staff_list, user):
    out = 0
    for s in staff_list:
        if s.role == 'ADMIN' or s.tenant_id != role.tenant_id:
            continue
        RoleAssignment.objects.update_or_create(staff=s, defaults={'role': role, 'assigned_by': user,
                                                                   'tenant': role.tenant})
        _apply(role, s)
        out += 1
    if out:
        log_event('ROLE', f'{out} staff given the role "{role.name}"', user=user, tenant=role.tenant, obj=role)
    return out


def unassign_role(staff, user):
    a = RoleAssignment.objects.filter(staff=staff).select_related('role').first()
    if a:
        log_event('ROLE', f'{staff.full_name} removed from role "{a.role.name}" (their switches stay as they were)',
                  user=user, tenant=staff.tenant)
        a.delete()


def create_starter_roles(tenant, user):
    made = 0
    valid = {f for f, _, _ in permission_fields()}
    for name, base, desc, perms in STARTER_ROLES:
        if not Role.objects.filter(tenant=tenant, name=name).exists():
            Role.objects.create(tenant=tenant, name=name, base_role=base, description=desc,
                                permissions=[p for p in perms if p in valid], created_by=user)
            made += 1
    return made


# ─────────────────────────────────────────────────────────────────────────────
# F448 / F449 — purposes, consent and the privacy notice
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_PURPOSES = [
    ('Running the school place', 'CONTRACT', 'Name, date of birth, class, attendance, results, fees',
     'Teaching, attendance, results, report cards and fees.'),
    ('Exams and government returns', 'LEGAL', 'Name, date of birth, results, NIN',
     'Registration with examination bodies and returns the law requires.'),
    ('Health and safety', 'VITAL', 'Medical details, emergency contacts',
     'First aid, the sick bay, medication and emergencies.'),
    ('School photos on the website and social media', 'CONSENT', 'Photos, first name, class',
     'Showing school life on our website, newsletters and social media pages.'),
    ('Celebrating achievements publicly', 'CONSENT', 'Name, class, award',
     'Naming pupils for prizes and achievements in public announcements.'),
    ('Text and WhatsApp updates', 'CONSENT', 'Parent phone number',
     'School news by SMS and WhatsApp (fees and emergencies are sent regardless).'),
]


def ensure_purposes(tenant):
    if not DataPurpose.objects.filter(tenant=tenant).exists():
        for i, (name, basis, data, desc) in enumerate(DEFAULT_PURPOSES):
            DataPurpose.objects.create(tenant=tenant, name=name, lawful_basis=basis, data_used=data, description=desc,
                                       sort_order=i)
    return DataPurpose.objects.filter(tenant=tenant)


def default_notice(tenant):
    name = tenant.name
    return f"""{name} ("the school", "we") decides how the personal information of pupils, parents, guardians and staff is used, and is responsible for it under the Nigeria Data Protection Act 2023.

## What we collect
- Pupils: name, date of birth, gender, photo, class, attendance, results, conduct, health details, NIN where provided.
- Parents and guardians: names, phone numbers, email and home addresses, payments.
- Staff: employment, payroll and contact details.

## Why we use it
We use personal information only for the purposes listed on this page, each with its lawful basis. Where the basis is consent, a parent or guardian gives it for children under 18 and can withdraw it at any time on the parent portal or by contacting us.

## Who we share it with
- Examination bodies and government agencies, where the law requires it.
- Service providers who work for us under contract, including our school software maintainers, payment processors (Paystack) and SMS and email providers. They may only use the information to provide their service to us.
We do not sell personal information.

## How long we keep it
We keep records only as long as we need them. Our retention periods are listed on this page.

## Keeping it safe
Access is limited to staff who need it. Sign-ins are protected, sensitive notes are encrypted, every sensitive action is recorded, and files are only reachable through private, expiring links.

## Your rights
You can ask to see a copy of the information we hold, to correct it, to delete it, to limit or object to how it is used, or to receive it in a form you can take elsewhere. Use the request form on this page or contact our Data Protection Officer. We will reply within one month. You may also complain to the Nigeria Data Protection Commission (ndpc.gov.ng).

## Changes
We will update this notice when our practices change and show the date it was last updated."""


def notice_for(tenant):
    n = PrivacyNotice.objects.filter(tenant=tenant).first()
    if n is None:
        n = PrivacyNotice.objects.create(tenant=tenant, controller=tenant.name, notice=default_notice(tenant),
                                         dpo_email=tenant.email or '', dpo_phone=tenant.phone or '',
                                         dpo_address=(tenant.address or '')[:255])
    return n


def set_consent(student, purpose, granted, user, given_by='', relationship='', method='PORTAL', request=None):
    if student.tenant_id != purpose.tenant_id:
        raise SecurityError('That purpose belongs to another school.')
    c, _ = Consent.objects.get_or_create(student=student, purpose=purpose, defaults={'tenant_id': student.tenant_id})
    if c.granted == granted and c.pk and c.history:
        return c
    entry = {'at': timezone.now().isoformat(timespec='seconds'), 'granted': granted,
             'by': (given_by or getattr(user, 'get_full_name', lambda: '')() or getattr(user, 'username', ''))[:150],
             'method': method}
    c.granted, c.given_by, c.relationship, c.method, c.recorded_by = granted, entry['by'], relationship[:60], method, \
        user if getattr(user, 'pk', None) else None
    c.history = (c.history or []) + [entry]
    c.save()
    log_event('CONSENT', f'{"Consent given" if granted else "Consent withdrawn"}: {purpose.name} — {student.full_name}',
              request=request, user=user, tenant=student.tenant, obj=c)
    return c


def consent_coverage(tenant):
    from users.models import ENROLLED_STATUSES, Student
    students = Student._base_manager.filter(tenant=tenant, status__in=ENROLLED_STATUSES).count()
    rows = []
    for p in DataPurpose.objects.filter(tenant=tenant, active=True, lawful_basis='CONSENT'):
        answered = Consent.objects.filter(purpose=p, student__status__in=ENROLLED_STATUSES)
        granted = answered.filter(granted=True).count()
        rows.append({'purpose': p, 'granted': granted, 'refused': answered.count() - granted,
                     'unanswered': max(students - answered.count(), 0), 'students': students,
                     'pct': round(100 * answered.count() / students) if students else 0})
    return rows


def consent_allowed(student, purpose_name):
    """For other modules: may this child's data be used for a consent-based purpose (e.g. photos online)?"""
    p = DataPurpose.objects.filter(tenant_id=student.tenant_id, name__iexact=purpose_name, active=True).first()
    if p is None or not p.needs_consent:
        return True
    return Consent.objects.filter(student=student, purpose=p, granted=True).exists()


# ─────────────────────────────────────────────────────────────────────────────
# F451 — data subject requests
# ─────────────────────────────────────────────────────────────────────────────

RESPONSE_DAYS = 30


def submit_request(tenant, data, student=None, user=None, request=None):
    name = (data.get('requester_name') or '').strip()
    details = (data.get('details') or '').strip()
    email = (data.get('requester_email') or '').strip()
    kind = data.get('kind') or 'ACCESS'
    if kind not in dict(DataRequest.KINDS):
        raise SecurityError('Choose what you are asking for.')
    if not name or not details:
        raise SecurityError('Give your name and describe your request.')
    if not email and not (data.get('requester_phone') or '').strip():
        raise SecurityError('Give an email address or phone number so we can reply.')
    if student is not None and student.tenant_id != tenant.pk:
        raise SecurityError('Unknown student.')
    dr = DataRequest.objects.create(
        tenant=tenant, reference=next_ref(DataRequest, 'DSR'), kind=kind, requester_name=name[:150],
        requester_email=email[:254], requester_phone=(data.get('requester_phone') or '')[:20],
        relationship=(data.get('relationship') or '')[:60], student=student, details=details[:5000],
        due_on=timezone.localdate() + timedelta(days=RESPONSE_DAYS), submitted_by=user if getattr(user, 'pk', None)
        else None, identity_checked=bool(student is not None and user is not None and getattr(user, 'is_parent', False)))
    log_event('REQUEST', f'{dr.reference}: {dr.get_kind_display()} request from {name}', request=request, user=user,
              tenant=tenant, obj=dr)
    for e in admin_emails(tenant):
        _email(e, f'New data protection request {dr.reference}',
               f'<p>{escape(name)} ({escape(dr.relationship or "requester")}) asked to: '
               f'<b>{escape(dr.get_kind_display().lower())}</b>.</p><p>{escape(details)}</p>'
               f'<p>Reply by <b>{dr.due_on:%d %B %Y}</b> (one month).</p>')
    if email:
        _email(email, f'We have received your request ({dr.reference})',
               f'<p>Dear {escape(name)},</p><p>{escape(tenant.name)} has received your request to '
               f'{escape(dr.get_kind_display().lower())}. Your reference is <b>{dr.reference}</b>. We will reply by '
               f'{dr.due_on:%d %B %Y}. We may contact you to confirm your identity first.</p>')
    return dr


def add_note(dr, user, text):
    text = (text or '').strip()
    if not text:
        raise SecurityError('Write a note.')
    dr.notes = (dr.notes or []) + [{'at': timezone.now().isoformat(timespec='minutes'),
                                    'by': user.get_full_name() or user.username, 'text': text[:2000]}]
    dr.save(update_fields=['notes'])


def build_request_export(dr, user, request=None):
    from django.core.files.base import ContentFile

    from users import data_export
    if dr.student is None:
        raise SecurityError('Link the request to a student first.')
    if not dr.identity_checked:
        raise SecurityError("Confirm the requester's identity first.")
    filename, data = data_export.build(dr.student, user, f'Data request {dr.reference}')
    dr.export.save(filename, ContentFile(data), save=False)
    if dr.status in ('NEW', 'VERIFYING'):
        dr.status = 'IN_PROGRESS'
    dr.handled_by = user
    dr.save()
    log_event('EXPORT', f'{dr.reference}: data export prepared for {dr.student.full_name}', request=request, user=user,
              tenant=dr.tenant, obj=dr)
    return dr


def anonymise_student(student, user, request=None):
    """Remove what identifies a pupil who has left, keeping anonymous academic and financial records."""
    from users.models import LEFT_STATUSES, Guardian, StudentChange, StudentDocument, SupportNeeds
    if student.status not in LEFT_STATUSES:
        raise SecurityError('Only a pupil who has left (withdrawn, graduated or deceased) can be anonymised. '
                            'Change their status first.')
    old = student.full_name
    with transaction.atomic():
        if student.passport:
            student.passport.delete(save=False)
        student.full_name = f'Former student {student.pk}'
        for f in ('parent_name', 'parent_phone', 'parent_email', 'address', 'date_of_birth', 'parent_term_password'):
            setattr(student, f, None)
        student.nin = ''
        student.destination = ''
        student.status_reason = ''
        student.save()
        for d in StudentDocument._base_manager.filter(student=student):
            d.file.delete(save=False)
            d.delete()
        for g in Guardian._base_manager.filter(student=student):
            if g.photo:
                g.photo.delete(save=False)
            g.delete()
        SupportNeeds._base_manager.filter(student=student).delete()
        try:
            from welfare.models import MedicalProfile
            MedicalProfile._base_manager.filter(student=student).delete()
        except Exception:
            log.exception('Medical profile not removed')
        Consent._base_manager.filter(student=student).delete()
        u = student.user
        u.first_name = u.last_name = u.email = ''
        u.is_active = False
        u.term_password = ''
        u.set_unusable_password()
        u.save()
        StudentChange._base_manager.filter(student=student).delete()   # old values live in the history
    log_event('ANONYMISED', f'Student record {student.pk} anonymised (was {old[0]}…)', request=request, user=user,
              tenant=student.tenant)
    return student


def close_request(dr, user, status, response, request=None):
    if status not in ('DONE', 'REFUSED'):
        raise SecurityError('Choose completed or refused.')
    response = (response or '').strip()
    if not response:
        raise SecurityError('Write the reply that goes to the requester.')
    dr.status, dr.response, dr.handled_by, dr.closed_at = status, response[:5000], user, timezone.now()
    dr.save()
    log_event('REQUEST', f'{dr.reference} {dr.get_status_display().lower()}', request=request, user=user,
              tenant=dr.tenant, obj=dr)
    if dr.requester_email:
        extra = '<p>You can download the file from the Privacy page of the parent portal.</p>' if dr.export else ''
        _email(dr.requester_email, f'Your request {dr.reference}: {dr.get_status_display().lower()}',
               f'<p>Dear {escape(dr.requester_name)},</p><p>{escape(response).replace(chr(10), "<br>")}</p>{extra}'
               f'<p>If you are not satisfied, you may complain to the Nigeria Data Protection Commission.</p>')
    return dr


# ─────────────────────────────────────────────────────────────────────────────
# F452 — breaches
# ─────────────────────────────────────────────────────────────────────────────

def _dt(raw):
    from datetime import datetime
    try:
        return timezone.make_aware(datetime.strptime(raw or '', '%Y-%m-%dT%H:%M'))
    except ValueError:
        return None


def report_breach(tenant, data, user, request=None):
    title = (data.get('title') or '').strip()
    description = (data.get('description') or '').strip()
    if not title or not description:
        raise SecurityError('Give a short title and say what happened.')
    discovered = _dt(data.get('discovered_at')) or timezone.now()
    if discovered > timezone.now() + timedelta(minutes=5):
        raise SecurityError('The time it was discovered cannot be in the future.')
    try:
        people = max(int(data.get('people_affected') or 0), 0)
    except ValueError:
        people = 0
    b = Breach.objects.create(
        tenant=tenant, reference=next_ref(Breach, 'BR'), title=title[:200], description=description,
        discovered_at=discovered, occurred_at=_dt(data.get('occurred_at')), data_types=(data.get('data_types') or '')[:255],
        people_affected=people, children_involved=bool(data.get('children_involved')),
        severity=data.get('severity') if data.get('severity') in dict(Breach.SEVERITY) else 'MEDIUM',
        containment=(data.get('containment') or '').strip(), created_by=user)
    BreachAction.objects.create(tenant=tenant, breach=b, by=user, text='Breach recorded.')
    log_event('BREACH', f'{b.reference}: breach recorded — {b.title}', request=request, user=user, tenant=tenant, obj=b)
    for e in admin_emails(tenant):
        _email(e, f'URGENT: personal data breach {b.reference}',
               f'<p><b>{escape(b.title)}</b> was recorded by {escape(user.get_full_name() or user.username)}.</p>'
               f'<p>{escape(description)}</p><p>If people are likely to be harmed, the Nigeria Data Protection '
               f'Commission must be told within 72 hours of discovery — by <b>'
               f'{timezone.localtime(b.deadline):%d %B %Y %H:%M}</b>.</p>')
    return b


def update_breach(b, op, user, data, request=None):
    now = timezone.now()
    if op == 'note':
        text = (data.get('text') or '').strip()
        if not text:
            raise SecurityError('Write what was done.')
        BreachAction.objects.create(tenant=b.tenant, breach=b, by=user, text=text[:4000])
        return b
    if op == 'contained':
        b.status, b.contained_at = 'CONTAINED' if b.status == 'OPEN' else b.status, b.contained_at or now
        b.containment = (data.get('containment') or b.containment).strip()
        msg = 'Marked as contained.'
    elif op == 'reported':
        b.reported_at = _dt(data.get('reported_at')) or now
        b.ndpc_reference = (data.get('ndpc_reference') or '').strip()[:80]
        b.status = 'REPORTED'
        late = b.reported_at > b.deadline
        msg = f'Reported to the NDPC{f" (ref {b.ndpc_reference})" if b.ndpc_reference else ""}' + \
              (' — after the 72-hour deadline; record the reason for the delay.' if late else '.')
    elif op == 'notified':
        b.people_notified_at = _dt(data.get('notified_at')) or now
        msg = 'The people affected were told.'
    elif op == 'not_reportable':
        reason = (data.get('reason') or '').strip()
        if not reason:
            raise SecurityError('Record why it does not need reporting.')
        b.not_reported_reason = reason
        msg = f'Decided not to report: {reason}'
    elif op == 'close':
        if not (b.reported_at or b.not_reported_reason):
            raise SecurityError('Report it to the NDPC or record why it did not need reporting before closing.')
        b.status, b.closed_at = 'CLOSED', now
        msg = 'Closed.'
    else:
        raise SecurityError('Unknown action.')
    b.save()
    BreachAction.objects.create(tenant=b.tenant, breach=b, by=user, text=msg)
    log_event('BREACH', f'{b.reference}: {msg}', request=request, user=user, tenant=b.tenant, obj=b)
    return b


def ndpc_letter(b):
    n = notice_for(b.tenant)
    t = b.tenant
    return f"""To: The National Commissioner, Nigeria Data Protection Commission

Notification of a personal data breach — {t.name} ({b.reference})

1. Data controller: {n.controller or t.name}, {t.address or '[address]'}. Data Protection Officer: {n.dpo_name or '[name]'}, {n.dpo_email or '[email]'}, {n.dpo_phone or '[phone]'}.
2. When it happened: {timezone.localtime(b.occurred_at).strftime('%d %B %Y %H:%M') if b.occurred_at else 'not yet known'}. When we became aware: {timezone.localtime(b.discovered_at):%d %B %Y %H:%M}.
3. What happened: {b.description}
4. Personal data involved: {b.data_types or '[categories of data]'}.
5. People affected: about {b.people_affected}{', including children' if b.children_involved else ''}.
6. Likely consequences: [describe the likely harm to the people affected].
7. What we have done and will do: {b.containment or '[measures taken to contain the breach and reduce harm]'}.
8. Whether the people affected have been told: {timezone.localtime(b.people_notified_at).strftime('yes, on %d %B %Y') if b.people_notified_at else 'not yet'}.

Signed: ____________________   Date: __________"""


def people_notice(b):
    n = notice_for(b.tenant)
    return f"""Dear Parent/Guardian,

We are writing to tell you about an incident involving personal information held by {b.tenant.name}.

What happened: {b.description}

What information was involved: {b.data_types or '[the information involved]'}.

What we are doing: {b.containment or '[the steps we have taken]'}

What you can do: be careful of unexpected calls, messages or emails that mention the school or ask for payment or passwords. The school will never ask for your password.

We are sorry this happened. If you have questions, contact our Data Protection Officer, {n.dpo_name or 'the school office'}, at {n.dpo_email or n.dpo_phone or 'the school'}. You also have the right to complain to the Nigeria Data Protection Commission.

{b.tenant.name}"""


# ─────────────────────────────────────────────────────────────────────────────
# F450 — retention
# ─────────────────────────────────────────────────────────────────────────────

def _delete(qs, files=(), dry=False):
    n = qs.count()
    if dry or not n:
        return n
    for obj in qs.iterator() if files else ():
        for f in files:
            fh = getattr(obj, f, None)
            if fh:
                try:
                    fh.delete(save=False)
                except Exception:
                    log.exception('Could not delete file')
    qs.delete()
    return n


def _r_audit(t, cutoff, dry):
    return _delete(AuditEvent.objects.filter(tenant=t, at__lt=cutoff), dry=dry)


def _r_logins(t, cutoff, dry):
    n = _delete(UserSession.objects.filter(tenant=t, last_seen__lt=cutoff, ended_at__isnull=False), dry=dry)
    return n + _delete(KnownDevice.objects.filter(user__tenant=t, last_seen__lt=cutoff), dry=dry)


def _r_messages(t, cutoff, dry):
    from communications.models import MessageLog
    return _delete(MessageLog._base_manager.filter(tenant=t, created_at__lt=cutoff), dry=dry)


def _r_visits(t, cutoff, dry):
    from safety.models import Visit
    return _delete(Visit._base_manager.filter(tenant=t, created_at__lt=cutoff), files=('photo',), dry=dry)


def _r_applications(t, cutoff, dry):
    from admissions.models import Application, ApplicationDocument
    apps = Application._base_manager.filter(tenant=t, status__in=('REJECTED', 'WITHDRAWN'), created_at__lt=cutoff)
    if not dry:
        _delete(ApplicationDocument._base_manager.filter(application__in=apps), files=('file',))
    return _delete(apps, files=('passport',), dry=dry)


def _r_leavers(t, cutoff, dry):
    from users.models import LEFT_STATUSES, Student
    qs = Student._base_manager.filter(tenant=t, status__in=LEFT_STATUSES, left_on__lt=cutoff.date()) \
        .exclude(full_name__startswith='Former student ')
    if dry:
        return qs.count()
    n = 0
    for s in qs:
        anonymise_student(s, None)
        n += 1
    return n


def _r_requests(t, cutoff, dry):
    return _delete(DataRequest._base_manager.filter(tenant=t, status__in=('DONE', 'REFUSED'), closed_at__lt=cutoff),
                   files=('export',), dry=dry)


RETENTION = {
    'audit': {'label': 'Audit trail entries', 'min': 365, 'days': 730, 'fn': _r_audit, 'action': 'Delete'},
    'logins': {'label': 'Old sign-in sessions and devices', 'min': 30, 'days': 180, 'fn': _r_logins, 'action': 'Delete'},
    'messages': {'label': 'Sent SMS and email logs', 'min': 90, 'days': 365, 'fn': _r_messages, 'action': 'Delete'},
    'visits': {'label': 'Visitor log and visitor photos', 'min': 30, 'days': 365, 'fn': _r_visits, 'action': 'Delete'},
    'applications': {'label': 'Unsuccessful and withdrawn applications', 'min': 90, 'days': 365,
                     'fn': _r_applications, 'action': 'Delete'},
    'leavers': {'label': 'Personal details of pupils who left', 'min': 365, 'days': 365 * 7, 'fn': _r_leavers,
                'action': 'Anonymise (results and payments are kept without names)'},
    'requests': {'label': 'Closed data protection requests', 'min': 365, 'days': 730, 'fn': _r_requests,
                 'action': 'Delete'},
}


def rules_for(tenant):
    have = {r.kind: r for r in RetentionRule.objects.filter(tenant=tenant)}
    out = []
    for kind, spec in RETENTION.items():
        r = have.get(kind) or RetentionRule.objects.create(tenant=tenant, kind=kind, days=spec['days'])
        r.spec = spec
        out.append(r)
    return out


def save_rule(rule, enabled, days, user):
    spec = RETENTION[rule.kind]
    try:
        days = int(days)
    except (TypeError, ValueError):
        raise SecurityError('Give a number of days.')
    if days < spec['min']:
        raise SecurityError(f'{spec["label"]}: keep for at least {spec["min"]} days.')
    rule.enabled, rule.days = bool(enabled), days
    rule.save(update_fields=['enabled', 'days'])
    log_event('RETENTION', f'Retention for {spec["label"].lower()}: {"on" if enabled else "off"}, {days} days',
              user=user, tenant=rule.tenant)
    return rule


def run_rule(rule, dry=False):
    from tenants.middleware import clear_current_tenant, set_current_tenant
    spec = RETENTION[rule.kind]
    cutoff = timezone.now() - timedelta(days=max(rule.days, spec['min']))
    set_current_tenant(rule.tenant)
    try:
        n = spec['fn'](rule.tenant, cutoff, dry)
    finally:
        clear_current_tenant()
    if not dry:
        rule.last_run, rule.last_count = timezone.now(), n
        rule.save(update_fields=['last_run', 'last_count'])
        if n:
            log_event('RETENTION', f'Retention removed {n} — {spec["label"].lower()} older than {rule.days} days',
                      user=None, tenant=rule.tenant)
    return n


def run_retention(tenant):
    return {r.kind: run_rule(r) for r in rules_for(tenant) if r.enabled}


# ─────────────────────────────────────────────────────────────────────────────
# Overview and F458 compliance report
# ─────────────────────────────────────────────────────────────────────────────

def admins_without_2fa(tenant):
    from users.models import User
    out = []
    for u in User.objects.filter(tenant=tenant, is_active=True).filter(Q(is_superuser=True) | Q(is_staff_user=True)):
        if is_admin(u, tenant) and not two_factor_for(u):
            out.append(u)
    return out


def overview(tenant):
    from users.models import User
    since = timezone.now() - timedelta(days=30)
    staff = User.objects.filter(tenant=tenant, is_active=True, is_staff_user=True) | \
        User.objects.filter(tenant=tenant, is_active=True, is_superuser=True)
    staff = staff.distinct()
    with_2fa = TwoFactor.objects.filter(user__in=staff, confirmed_at__isnull=False).count()
    return {
        'cfg': settings_for(tenant), 'staff': staff.count(), 'with_2fa': with_2fa,
        'admins_no_2fa': admins_without_2fa(tenant),
        'failed_30': AuditEvent.objects.filter(tenant=tenant, action='LOGIN_FAILED', at__gte=since).count(),
        'locked_30': AuditEvent.objects.filter(tenant=tenant, action='LOCKED', at__gte=since).count(),
        'new_devices_30': AuditEvent.objects.filter(tenant=tenant, action='NEW_DEVICE', at__gte=since).count(),
        'sessions': len(active_sessions(tenant=tenant)),
        'open_requests': DataRequest.objects.filter(tenant=tenant).exclude(status__in=('DONE', 'REFUSED')),
        'open_breaches': Breach.objects.filter(tenant=tenant).exclude(status='CLOSED'),
        'recent': AuditEvent.objects.filter(tenant=tenant).exclude(action__in=('LOGIN', 'LOGOUT'))[:12],
        'notice': PrivacyNotice.objects.filter(tenant=tenant).first(),
    }


def compliance(tenant, year):
    """Evidence for an NDPA compliance audit for one calendar year."""
    from users.models import ENROLLED_STATUSES, Student
    from datetime import datetime
    start = timezone.make_aware(datetime(year, 1, 1))
    end = timezone.make_aware(datetime(year + 1, 1, 1))
    n = PrivacyNotice.objects.filter(tenant=tenant).first()
    cfg = settings_for(tenant)
    sections = []

    def sec(title, status, lines):
        sections.append({'title': title, 'status': status, 'lines': lines})

    sec('Accountability', 'ok' if n and n.dpo_name and n.dpo_email else 'bad', [
        f'Data Protection Officer: {n.dpo_name} ({n.dpo_email})' if n and n.dpo_name else 'No Data Protection Officer named.',
        f'Data Protection Compliance Organisation: {n.dpco}' if n and n.dpco else 'No DPCO recorded.',
        f'Privacy notice {"published" if n and n.published else "not published"}'
        f'{f", last updated {timezone.localtime(n.updated_at):%d %B %Y}" if n else ""}.'])
    purposes = list(DataPurpose.objects.filter(tenant=tenant, active=True))
    cov = consent_coverage(tenant)
    low = [c for c in cov if c['pct'] < 80]
    sec('Lawful basis and consent', 'ok' if purposes and not low else 'warn' if purposes else 'bad',
        [f'{len(purposes)} purposes recorded with a lawful basis.'] +
        [f'{c["purpose"].name}: {c["granted"]} consented, {c["refused"]} refused, {c["unanswered"]} not yet answered '
         f'({c["pct"]}% answered).' for c in cov])
    reqs = DataRequest.objects.filter(tenant=tenant, created_at__gte=start, created_at__lt=end)
    late = [r for r in reqs if (r.closed_at and r.closed_at.date() > r.due_on) or r.overdue]
    sec('Data subject requests', 'ok' if not late else 'warn',
        [f'{reqs.count()} requests received; {reqs.filter(status__in=("DONE", "REFUSED")).count()} closed; '
         f'{len(late)} answered late or overdue.'] +
        [f'{k}: {reqs.filter(kind=k).count()}' for k, _ in DataRequest.KINDS if reqs.filter(kind=k).exists()])
    br = Breach.objects.filter(tenant=tenant, discovered_at__gte=start, discovered_at__lt=end)
    late_br = [b for b in br if b.reported_at and b.reported_at > b.deadline]
    sec('Breaches', 'ok' if not br.exists() else 'warn' if late_br or br.exclude(status='CLOSED').exists() else 'ok',
        [f'{br.count()} breaches recorded; {br.filter(reported_at__isnull=False).count()} reported to the NDPC '
         f'({len(late_br)} after 72 hours); {br.exclude(status="CLOSED").count()} still open.'] +
        [f'{b.reference} {b.title} — {b.get_status_display()}' for b in br[:20]])
    rules = rules_for(tenant)
    sec('Retention', 'ok' if any(r.enabled for r in rules) else 'warn',
        [f'{r.spec["label"]}: {"kept " + str(r.days) + " days" if r.enabled else "no automatic limit"}'
         f'{f" (last run {timezone.localtime(r.last_run):%d %b %Y}, {r.last_count} removed)" if r.last_run else ""}'
         for r in rules])
    no2fa = admins_without_2fa(tenant)
    sec('Security of processing', 'ok' if not no2fa and cfg.require_2fa != 'NONE' else 'warn', [
        f'Two-step sign-in: {cfg.get_require_2fa_display().lower()}; {len(no2fa)} administrators without it.',
        f'Passwords: at least {max(cfg.min_length, 8)} characters'
        f'{", letters and numbers" if cfg.require_mix else ""}{", a symbol" if cfg.require_symbol else ""}'
        f'{f", changed every {cfg.expiry_days} days" if cfg.expiry_days else ""}'
        f'{f", last {cfg.history} not reused" if cfg.history else ""}.',
        f'Lockout after {cfg.lockout_attempts} failed sign-ins for {cfg.lockout_minutes} minutes; '
        f'{AuditEvent.objects.filter(tenant=tenant, action="LOCKED", at__gte=start, at__lt=end).count()} lockouts.',
        'Medical, counselling, safeguarding and support notes are encrypted at rest; personal files use private, '
        'expiring links.',
        f'{AuditEvent.objects.filter(tenant=tenant, at__gte=start, at__lt=end).count()} audit trail entries this year.'])
    try:
        from saas_admin.models import Backup
        last = Backup.objects.filter(tenant=tenant).order_by('-created_at').first()
        sec('Backups', 'ok' if last and last.created_at > timezone.now() - timedelta(days=2) else 'warn',
            [f'Last backup {timezone.localtime(last.created_at):%d %B %Y %H:%M}.' if last else 'No backups recorded.'])
    except Exception:
        pass
    pupils = Student._base_manager.filter(tenant=tenant, status__in=ENROLLED_STATUSES).count()
    return {'year': year, 'sections': sections, 'pupils': pupils, 'notice': n,
            'score': sum(1 for s in sections if s['status'] == 'ok'), 'total': len(sections)}


# ─────────────────────────────────────────────────────────────────────────────
# F460 — disclosure and the platform self-check
# ─────────────────────────────────────────────────────────────────────────────

def report_vulnerability(data):
    name = (data.get('reporter_name') or '').strip()
    email = (data.get('email') or '').strip()
    title = (data.get('title') or '').strip()
    details = (data.get('details') or '').strip()
    if not (name and email and title and details):
        raise SecurityError('Fill in your name, email, a title and the details.')
    if len(details) < 30:
        raise SecurityError('Describe the problem and how to reproduce it.')
    v = VulnerabilityReport.objects.create(
        reference=next_ref(VulnerabilityReport, 'VR'), reporter_name=name[:150], email=email[:254], title=title[:200],
        url=(data.get('url') or '')[:300], details=details[:20000],
        severity=data.get('severity') if data.get('severity') in dict(VulnerabilityReport.SEVERITY) else 'MEDIUM')
    from saas_admin.services import platform_emails
    for e in platform_emails():
        _email(e, f'Security report {v.reference}: {v.title}',
               f'<p>From {escape(name)} &lt;{escape(email)}&gt; — claimed severity {v.get_severity_display()}.</p>'
               f'<p>{escape(details[:3000]).replace(chr(10), "<br>")}</p>')
    _email(email, f'Thank you — security report {v.reference}',
           f'<p>Thank you for reporting this responsibly. Your reference is {v.reference}. We will reply within 5 '
           f'working days. Please do not share details publicly until we have fixed it.</p>')
    return v


def security_check():
    """Deployment checks for platform staff: (level, message) pairs, worst first."""
    import os
    out = []
    base = str(settings.BASE_DIR)
    if settings.DEBUG:
        out.append(('bad', 'DEBUG is on. Set DEBUG=False in production.'))
    if len(settings.SECRET_KEY) < 40 or settings.SECRET_KEY.startswith('django-insecure'):
        out.append(('bad', 'SECRET_KEY is short or a development key. Generate a long random one.'))
    if '*' in settings.ALLOWED_HOSTS:
        out.append(('warn', 'ALLOWED_HOSTS contains "*".'))
    if getattr(settings, 'ALLOW_MASTER_PASSWORDS', False):
        out.append(('bad', 'Shared master passwords are switched on (ALLOW_MASTER_PASSWORDS).'))
    if not getattr(settings, 'FIELD_ENCRYPTION_KEY', ''):
        out.append(('warn', 'FIELD_ENCRYPTION_KEY is not set; the encryption key is derived from SECRET_KEY, so '
                            'changing SECRET_KEY would make encrypted notes unreadable.'))
    if not settings.DEBUG and not getattr(settings, 'SESSION_COOKIE_SECURE', False):
        out.append(('warn', 'SESSION_COOKIE_SECURE is off.'))
    if not getattr(settings, 'TWO_FACTOR_ENFORCE', True):
        out.append(('warn', 'Two-step sign-in requirements are not enforced (TWO_FACTOR_ENFORCE=False).'))
    gi = os.path.join(base, '.gitignore')
    if os.path.exists(os.path.join(base, '.env')) and (not os.path.exists(gi) or '.env' not in open(gi).read()):
        out.append(('bad', '.env exists but is not in .gitignore.'))
    try:
        from .media import protected_fields
        left = 0
        for model, field in protected_fields():
            for name in model._base_manager.exclude(**{field: ''}).exclude(**{f'{field}__isnull': True}) \
                    .values_list(field, flat=True)[:2000]:
                if os.path.isfile(os.path.join(str(settings.MEDIA_ROOT), name)):
                    left += 1
        if left:
            out.append(('warn', f'{left} personal files are still in the public media folder. Run '
                                f'"python manage.py move_signed_media".'))
    except Exception:
        log.exception('Media check failed')
    from users.models import User
    platform = User.objects.filter(is_superuser=True, tenant__isnull=True, is_active=True)
    missing = [u.username for u in platform if not two_factor_for(u)]
    if missing:
        out.append(('warn', f'Platform accounts without two-step sign-in: {", ".join(missing[:10])}.'))
    open_vr = VulnerabilityReport.objects.filter(status__in=('NEW', 'TRIAGED')).count()
    if open_vr:
        out.append(('warn', f'{open_vr} security reports are open.'))
    from .models import PenTest
    last = PenTest.objects.order_by('-starts').first()
    if not last or last.starts < timezone.localdate() - timedelta(days=365):
        out.append(('warn', 'No penetration test in the last 12 months.'))
    if not out:
        out.append(('ok', 'No problems found.'))
    order = {'bad': 0, 'warn': 1, 'ok': 2}
    return sorted(out, key=lambda x: order[x[0]])


# ─────────────────────────────────────────────────────────────────────────────
# Nightly
# ─────────────────────────────────────────────────────────────────────────────

def nightly():
    from tenants.models import Tenant
    out = {'retention': 0, 'reminders': 0, 'cleaned': 0}
    out['cleaned'] += LoginFailure.objects.filter(at__lt=timezone.now() - timedelta(days=30)).delete()[0]
    for t in Tenant.objects.exclude(status__in=('PENDING', 'CANCELLED')):
        try:
            out['retention'] += sum(run_retention(t).values())
        except Exception:
            log.exception('Retention failed for %s', t.subdomain)
        emails = admin_emails(t)
        for b in Breach.objects.filter(tenant=t, status__in=('OPEN', 'CONTAINED'), reported_at__isnull=True,
                                       not_reported_reason=''):
            if b.hours_left > -24:
                out['reminders'] += 1
                for e in emails:
                    _email(e, f'Breach {b.reference}: decide on reporting',
                           f'<p>{escape(b.title)} — the 72-hour NDPC deadline '
                           f'{"passed" if b.hours_left < 0 else "is"} {timezone.localtime(b.deadline):%d %B %H:%M}. '
                           f'Report it or record why it does not need reporting.</p>')
        soon = timezone.localdate() + timedelta(days=5)
        for dr in DataRequest.objects.filter(tenant=t, status__in=('NEW', 'VERIFYING', 'IN_PROGRESS'), due_on__lte=soon):
            out['reminders'] += 1
            for e in emails:
                _email(e, f'Data request {dr.reference} due {dr.due_on:%d %B}',
                       f'<p>{escape(dr.requester_name)} — {escape(dr.get_kind_display().lower())}. '
                       f'{"Overdue." if dr.overdue else "Due soon."}</p>')
    return out

