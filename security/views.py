"""
security/views.py
─────────────────
Personal pages for everyone signed in (two-step sign-in, sessions and
devices), the school's Security & Privacy pages for administrators, the
parents' privacy page, the public privacy notice and request form, signed
media and security.txt.
"""
from datetime import timedelta

from django.conf import settings as django_settings
from django.contrib import messages
from django.contrib.auth import logout
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme

from . import services as svc
from . import totp
from .models import (AuditEvent, Breach, Consent, DataPurpose, DataRequest, KnownDevice, LoginFailure, Role,
                     RoleAssignment, TwoFactor, UserSession)


def _deny(request, text='Only an administrator can open that page.'):
    messages.error(request, text)
    return redirect('dashboard:router')


def admin_only(view):
    def wrap(request, *a, **kw):
        if getattr(request, 'tenant', None) is None:
            raise Http404
        if not svc.is_admin(request.user, request.tenant):
            return _deny(request)
        return view(request, *a, **kw)
    wrap.__name__ = view.__name__
    return login_required(wrap)


def _safe_next(request, default='/'):
    nxt = request.POST.get('next') or request.GET.get('next') or ''
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}) and \
            not nxt.startswith('/security/2fa/'):
        return nxt
    return default


def _home(request):
    return '/dashboard/'


def _base(request):
    return 'dashboard/base.html' if getattr(request, 'tenant', None) else 'security/_plain_base.html'


# ─────────────────────────────────────────────────────────────────────────────
# Personal: two-step sign-in, sessions, devices
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def me(request):
    if request.method == 'POST':
        op = request.POST.get('op')
        if op in ('disable', 'codes'):
            if not request.user.check_password(request.POST.get('password', '')):
                messages.error(request, 'Your password is not right.')
                return redirect('security:me')
            if op == 'disable':
                if svc.required_2fa(request.user):
                    messages.error(request, 'Your school requires two-step sign-in for your account, so it cannot be '
                                            'switched off. You can set it up again with a different method.')
                    return redirect('security:me')
                svc.disable_2fa(request.user, request.user, request)
                request.session.pop('sec_2fa', None)
                messages.success(request, 'Two-step sign-in is off.')
            else:
                tf = svc.two_factor_for(request.user)
                if tf:
                    request.session['sec_backup'] = svc.new_backup_codes(tf)
                    return redirect('security:backup_codes')
        elif op == 'switch':
            if not request.user.check_password(request.POST.get('password', '')):
                messages.error(request, 'Your password is not right.')
                return redirect('security:me')
            TwoFactor.objects.filter(user=request.user).update(confirmed_at=None)
            request.session['sec_2fa'] = 'ok'
            return redirect('security:setup')
        return redirect('security:me')
    tf = TwoFactor.objects.filter(user=request.user).first()
    return render(request, 'security/me.html', {
        'base': _base(request), 'tf': tf if tf and tf.active else None, 'required': svc.required_2fa(request.user),
        'devices': KnownDevice.objects.filter(user=request.user)[:10],
        'sessions': svc.active_sessions(user=request.user), 'current': request.session.session_key,
        'pw_changed': svc.password_changed_on(request.user), 'cfg': svc.settings_for(getattr(request, 'tenant', None)),
        'backup_left': len(tf.backup_codes) if tf and tf.active else 0,
        'recent': AuditEvent.objects.filter(user=request.user, category='AUTH')[:10]})


def _send(request, tf, sent_note=None):
    """Send a code and tell the user honestly whether it went."""
    if svc.send_code(request, tf):
        if sent_note:
            messages.info(request, sent_note)
        return
    where = 'email' if tf.method == 'EMAIL' else 'text message'
    fallback = 'use one of your backup codes' if tf.active else 'choose another method, such as an authenticator app'
    messages.error(request, f'We could not send the {where} just now. Try "Send another code" in a minute, '
                            f'or {fallback}.')


@login_required
def setup(request):
    if request.session.get('sec_2fa') == 'pending':
        return redirect('security:verify')
    if svc.two_factor_for(request.user):
        return redirect('security:me')
    tf = TwoFactor.objects.filter(user=request.user, confirmed_at__isnull=True).first()
    if request.method == 'POST':
        op = request.POST.get('op')
        try:
            if op == 'start':
                tf = svc.start_setup(request.user, request.POST.get('method'), request.POST.get('phone'))
                if tf.method != 'TOTP':
                    _send(request, tf, 'We sent you a 6-digit code.' if tf.method == 'EMAIL'
                          else 'We texted you a 6-digit code.')
            elif op == 'resend' and tf and tf.method != 'TOTP':
                _send(request, tf, 'A new code is on its way.')
            elif op == 'confirm' and tf:
                request.session['sec_backup'] = svc.confirm_setup(request, tf, request.POST.get('code'))
                return redirect('security:backup_codes')
            elif op == 'restart':
                TwoFactor.objects.filter(user=request.user, confirmed_at__isnull=True).delete()
                tf = None
        except svc.SecurityError as problem:
            messages.error(request, str(problem))
        return redirect(request.get_full_path())
    qr = uri = None
    if tf and tf.method == 'TOTP':
        issuer = getattr(getattr(request, 'tenant', None), 'name', None) or 'School Portal'
        uri = totp.uri(tf.secret, request.user.username, issuer)
        qr = svc.qr_svg(uri)
    return render(request, 'security/setup.html', {
        'base': _base(request), 'tf': tf, 'qr': qr, 'uri': uri, 'required': request.GET.get('required'),
        'methods': TwoFactor.METHODS, 'secret_spaced': ' '.join(tf.secret[i:i + 4] for i in range(0, len(tf.secret), 4))
        if tf else ''})


@login_required
def backup_codes(request):
    codes = request.session.pop('sec_backup', None)
    if not codes:
        return redirect('security:me')
    return render(request, 'security/backup_codes.html', {'base': _base(request), 'codes': codes, 'home': _home(request)})


@login_required
def verify(request):
    tf = svc.two_factor_for(request.user)
    if request.session.get('sec_2fa') != 'pending' or not tf:
        request.session.pop('sec_2fa', None)          # never leave a session waiting for a code that can't come
        return redirect(_safe_next(request, _home(request)))
    if request.method == 'POST':
        if request.POST.get('op') == 'send':
            _send(request, tf, 'We sent you a new code.')
            return redirect(request.get_full_path())
        if svc.check_code(request, tf, request.POST.get('code')):
            request.session['sec_2fa'] = 'ok'
            request.session.pop('sec_2fa_tries', None)
            svc.log_event('LOGIN', f'{request.user.username} passed two-step sign-in', request=request)
            if len(tf.backup_codes or []) <= 2:
                messages.warning(request, f'You have {len(tf.backup_codes or [])} backup codes left. Make new ones on '
                                          f'Sign-in & security.')
            return redirect(_safe_next(request, _home(request)))
        tries = request.session.get('sec_2fa_tries', 0) + 1
        request.session['sec_2fa_tries'] = tries
        svc.log_event('2FA_FAILED', f'Wrong two-step code for {request.user.username} ({tries})', request=request)
        if tries >= 5:
            logout(request)
            messages.error(request, 'Too many wrong codes. Sign in again.')
            return redirect('/')
        messages.error(request, 'That code is not right.')
        return redirect(request.get_full_path())
    if tf.method != 'TOTP' and not request.session.get('sec_code'):
        _send(request, tf)
    return render(request, 'security/verify.html', {'tf': tf, 'next': request.GET.get('next', '')})


@login_required
def sessions(request):
    if request.method == 'POST':
        op = request.POST.get('op')
        if op == 'others':
            n = svc.revoke_others(request.user, request.session.session_key, request)
            messages.success(request, f'Signed out of {n} other session{"s" if n != 1 else ""}.')
        elif op == 'one':
            us = UserSession.objects.filter(pk=request.POST.get('id'), user=request.user).first()
            if us and us.session_key != request.session.session_key:
                svc.revoke_session(us, request.user, request)
                messages.success(request, 'That session has been signed out.')
        elif op == 'forget':
            KnownDevice.objects.filter(pk=request.POST.get('id'), user=request.user).delete()
            messages.success(request, 'Device forgotten. You will get an alert if it signs in again.')
        return redirect('security:sessions')
    return render(request, 'security/sessions.html', {
        'base': _base(request), 'sessions': svc.active_sessions(user=request.user),
        'current': request.session.session_key, 'devices': KnownDevice.objects.filter(user=request.user),
        'label': svc.device_label})


# ─────────────────────────────────────────────────────────────────────────────
# School: overview, settings, people
# ─────────────────────────────────────────────────────────────────────────────

@admin_only
def overview(request):
    return render(request, 'security/overview.html', {'o': svc.overview(request.tenant)})


@admin_only
def settings_page(request):
    cfg = svc.settings_for(request.tenant)
    if request.method == 'POST':
        def num(name, lo, hi):
            try:
                return min(max(int(request.POST.get(name) or 0), lo), hi)
            except ValueError:
                return getattr(cfg, name)
        before = {f: getattr(cfg, f) for f in ('require_2fa', 'min_length', 'require_mix', 'require_symbol',
                                               'expiry_days', 'history', 'lockout_attempts', 'lockout_minutes',
                                               'alert_new_device', 'idle_minutes', 'notify_admins_lockout')}
        if request.POST.get('require_2fa') in dict(cfg.TWO_FACTOR):
            cfg.require_2fa = request.POST['require_2fa']
        cfg.min_length = num('min_length', 8, 64)
        cfg.require_mix = bool(request.POST.get('require_mix'))
        cfg.require_symbol = bool(request.POST.get('require_symbol'))
        cfg.expiry_days = num('expiry_days', 0, 730)
        cfg.history = num('history', 0, 24)
        cfg.lockout_attempts = num('lockout_attempts', 3, 20)
        cfg.lockout_minutes = num('lockout_minutes', 5, 1440)
        cfg.alert_new_device = bool(request.POST.get('alert_new_device'))
        cfg.idle_minutes = num('idle_minutes', 0, 480)
        if 0 < cfg.idle_minutes < 5:
            cfg.idle_minutes = 5
        cfg.notify_admins_lockout = bool(request.POST.get('notify_admins_lockout'))
        cfg.save()
        changes = {k: [v, getattr(cfg, k)] for k, v in before.items() if v != getattr(cfg, k)}
        if changes:
            svc.log_event('SETTINGS', 'Security settings changed: ' + ', '.join(k.replace('_', ' ') for k in changes),
                          request=request, changes=changes)
        request.session.pop('sec_2fa_free', None)
        messages.success(request, 'Security settings saved.')
        return redirect('security:settings')
    return render(request, 'security/settings.html', {'cfg': cfg})


@admin_only
def people(request):
    from users.models import Staff, User
    t = request.tenant
    if request.method == 'POST':
        op = request.POST.get('op')
        if op == 'reset_2fa':
            u = get_object_or_404(User, pk=request.POST.get('user'), tenant=t)
            if u == request.user:
                messages.error(request, 'Turn off your own two-step sign-in from Sign-in & security.')
            elif svc.disable_2fa(u, request.user, request):
                messages.success(request, f'Two-step sign-in reset for {u.username}. They set it up again at their '
                                          f'next sign-in.')
        elif op == 'revoke':
            us = get_object_or_404(UserSession, pk=request.POST.get('id'), tenant=t)
            if us.session_key != request.session.session_key:
                svc.revoke_session(us, request.user, request, 'Signed out by an administrator')
                messages.success(request, f'{us.user.username} has been signed out of that device.')
        elif op == 'unlock':
            ident = (request.POST.get('identity') or '').lower()
            LoginFailure.objects.filter(host=request.get_host().split(':')[0].lower(), identity=ident).delete()
            svc.log_event('SETTINGS', f'Sign-in lock cleared for "{ident}"', request=request)
            messages.success(request, f'"{ident}" can sign in again.')
        return redirect('security:people')
    users = User.objects.filter(tenant=t, is_active=True).filter(is_staff_user=True) | \
        User.objects.filter(tenant=t, is_active=True, is_superuser=True)
    users = users.distinct().order_by('username')
    tfs = {tf.user_id: tf for tf in TwoFactor.objects.filter(user__in=users, confirmed_at__isnull=False)}
    names = dict(Staff.objects.filter(tenant=t).values_list('user_id', 'full_name'))
    roles = {a.staff.user_id: a.role.name for a in RoleAssignment.objects.filter(tenant=t).select_related('staff', 'role')}
    rows = [{'u': u, 'name': names.get(u.pk) or u.get_full_name() or u.username, 'tf': tfs.get(u.pk),
             'admin': svc.is_admin(u, t), 'role': roles.get(u.pk)} for u in users]
    host = request.get_host().split(':')[0].lower()
    window = timezone.now() - timedelta(minutes=svc.settings_for(t).lockout_minutes)
    from django.db.models import Count, Max
    locks = LoginFailure.objects.filter(host=host, at__gte=window).values('identity') \
        .annotate(n=Count('id'), last=Max('at')).order_by('-last')
    return render(request, 'security/people.html', {
        'rows': rows, 'sessions': svc.active_sessions(tenant=t), 'current': request.session.session_key,
        'locks': locks, 'limit': svc.settings_for(t).lockout_attempts, 'label': svc.device_label})


@admin_only
def audit(request):
    from users.models import User
    from saas_admin.services import date_or
    f = {'q': request.GET.get('q', '').strip(), 'category': request.GET.get('category', ''),
         'user_id': request.GET.get('user', ''), 'since': date_or(request.GET.get('since')),
         'until': date_or(request.GET.get('until'))}
    rows = svc.audit_feed(request.tenant, limit=5000 if request.GET.get('csv') else 300, **f)
    if request.GET.get('csv'):
        svc.log_event('EXPORT', f'Audit trail downloaded ({len(rows)} rows)', request=request)
        resp = HttpResponse(svc.feed_csv(rows), content_type='text/csv')
        resp['Content-Disposition'] = f'attachment; filename="audit-{request.tenant.subdomain}-{timezone.localdate()}.csv"'
        return resp
    people = User.objects.filter(tenant=request.tenant).filter(is_staff_user=True).order_by('username') | \
        User.objects.filter(tenant=request.tenant, is_superuser=True)
    return render(request, 'security/audit.html', {'rows': rows, 'f': f, 'categories': AuditEvent.CATEGORIES,
                                                   'people': people.distinct().order_by('username'),
                                                   'qs': request.GET.urlencode()})


# ─────────────────────────────────────────────────────────────────────────────
# School: roles (F443)
# ─────────────────────────────────────────────────────────────────────────────

@admin_only
def roles(request):
    from users.models import Staff
    t = request.tenant
    if request.method == 'POST':
        op = request.POST.get('op')
        try:
            if op == 'starter':
                n = svc.create_starter_roles(t, request.user)
                messages.success(request, f'{n} starter roles added — adjust them as you like.' if n
                                 else 'The starter roles are already here.')
            elif op == 'save':
                role = Role.objects.filter(tenant=t, pk=request.POST.get('id') or 0).first()
                role = svc.save_role(t, request.POST, request.user, role)
                messages.success(request, f'Role "{role.name}" saved.')
                return redirect(f'{request.path}?edit={role.pk}')
            elif op == 'assign':
                role = get_object_or_404(Role, tenant=t, pk=request.POST.get('id'))
                staff = list(Staff.objects.filter(tenant=t, pk__in=request.POST.getlist('staff')))
                n = svc.assign_role(role, staff, request.user)
                messages.success(request, f'{n} staff now have the role "{role.name}".')
                return redirect(f'{request.path}?edit={role.pk}')
            elif op == 'unassign':
                s = get_object_or_404(Staff, tenant=t, pk=request.POST.get('staff'))
                svc.unassign_role(s, request.user)
                messages.success(request, f'{s.full_name} no longer follows a role.')
            elif op == 'delete':
                role = get_object_or_404(Role, tenant=t, pk=request.POST.get('id'))
                svc.log_event('ROLE', f'Role "{role.name}" deleted', request=request)
                role.delete()
                messages.success(request, 'Role deleted. Staff keep the switches they had.')
        except svc.SecurityError as problem:
            messages.error(request, str(problem))
        return redirect(request.path)
    edit = Role.objects.filter(tenant=t, pk=request.GET.get('edit') or 0).first()
    all_roles = list(Role.objects.filter(tenant=t))
    counts = {r.pk: r.assignments.count() for r in all_roles}
    for r in all_roles:
        r.n = counts[r.pk]
    return render(request, 'security/roles.html', {
        'roles': all_roles, 'edit': edit, 'groups': svc.permission_groups(),
        'staff': Staff.objects.filter(tenant=t).exclude(role='ADMIN').select_related('custom_role__role')
        .order_by('full_name'), 'base_roles': [r for r in Staff.ROLE_CHOICES if r[0] != 'ADMIN'],
        'members': edit.assignments.select_related('staff') if edit else []})


# ─────────────────────────────────────────────────────────────────────────────
# School: privacy notice, purposes, consent (F448, F449)
# ─────────────────────────────────────────────────────────────────────────────

@admin_only
def privacy(request):
    t = request.tenant
    n = svc.notice_for(t)
    svc.ensure_purposes(t)
    if request.method == 'POST':
        op = request.POST.get('op')
        if op == 'notice':
            for f in ('controller', 'dpo_name', 'dpo_email', 'dpo_phone', 'dpo_address', 'dpco'):
                setattr(n, f, (request.POST.get(f) or '').strip()[:255])
            n.notice = (request.POST.get('notice') or '').strip() or svc.default_notice(t)
            n.published = bool(request.POST.get('published'))
            n.updated_by = request.user
            n.save()
            svc.log_event('PRIVACY', 'Privacy notice and Data Protection Officer details updated', request=request,
                          category='PRIVACY')
            messages.success(request, 'Privacy notice saved.')
        elif op == 'purpose':
            p = DataPurpose.objects.filter(tenant=t, pk=request.POST.get('id') or 0).first() or DataPurpose(tenant=t)
            name = (request.POST.get('name') or '').strip()
            if not name:
                messages.error(request, 'Give the purpose a name.')
                return redirect('security:privacy')
            p.name, p.description = name[:120], (request.POST.get('description') or '')[:500]
            p.data_used = (request.POST.get('data_used') or '')[:255]
            if request.POST.get('lawful_basis') in dict(DataPurpose.BASES):
                p.lawful_basis = request.POST['lawful_basis']
            p.active = bool(request.POST.get('active'))
            p.save()
            svc.log_event('PRIVACY', f'Purpose "{p.name}" saved ({p.get_lawful_basis_display().lower()})',
                          request=request, category='PRIVACY')
            messages.success(request, 'Purpose saved.')
        elif op == 'reset_notice':
            n.notice = svc.default_notice(t)
            n.save(update_fields=['notice', 'updated_at'])
            messages.success(request, 'The standard notice text is back. Review it and save.')
        return redirect('security:privacy')
    return render(request, 'security/privacy.html', {
        'n': n, 'purposes': DataPurpose.objects.filter(tenant=t), 'bases': DataPurpose.BASES,
        'edit': DataPurpose.objects.filter(tenant=t, pk=request.GET.get('purpose') or 0).first(),
        'public_url': f'{t.portal_url}/privacy/'})


@admin_only
def consents(request):
    from users.models import ENROLLED_STATUSES, Class, Student
    t = request.tenant
    purposes = list(svc.ensure_purposes(t).filter(active=True, lawful_basis='CONSENT'))
    if request.method == 'POST':
        st = get_object_or_404(Student, tenant=t, pk=request.POST.get('student'))
        p = get_object_or_404(DataPurpose, tenant=t, pk=request.POST.get('purpose'))
        try:
            svc.set_consent(st, p, request.POST.get('granted') == '1', request.user,
                            given_by=request.POST.get('given_by') or '', relationship=request.POST.get('relationship') or '',
                            method=request.POST.get('method') or 'PAPER', request=request)
            messages.success(request, f'Recorded for {st.full_name}.')
        except svc.SecurityError as problem:
            messages.error(request, str(problem))
        return redirect(f'{request.path}?class={request.POST.get("class", "")}')
    classes = Class.objects.filter(tenant=t).order_by('name')
    cls = classes.filter(pk=request.GET.get('class') or 0).first()
    rows = []
    if cls:
        students = Student.objects.filter(tenant=t, class_assigned=cls, status__in=ENROLLED_STATUSES).order_by('full_name')
        have = {(c.student_id, c.purpose_id): c for c in Consent.objects.filter(student__in=students)}
        rows = [{'s': s, 'cells': [(p, have.get((s.pk, p.pk))) for p in purposes]} for s in students]
    return render(request, 'security/consents.html', {
        'purposes': purposes, 'classes': classes, 'cls': cls, 'rows': rows, 'coverage': svc.consent_coverage(t),
        'methods': Consent.METHODS})


# ─────────────────────────────────────────────────────────────────────────────
# School: data subject requests (F451)
# ─────────────────────────────────────────────────────────────────────────────

@admin_only
def requests_list(request):
    from users.models import Student
    t = request.tenant
    if request.method == 'POST':
        st = Student.objects.filter(tenant=t, pk=request.POST.get('student') or 0).first()
        try:
            dr = svc.submit_request(t, request.POST, st, request.user, request)
            if request.POST.get('identity_checked'):
                dr.identity_checked = True
                dr.save(update_fields=['identity_checked'])
            messages.success(request, f'Request {dr.reference} recorded. Reply by {dr.due_on:%d %B %Y}.')
            return redirect('security:request', pk=dr.pk)
        except svc.SecurityError as problem:
            messages.error(request, str(problem))
        return redirect('security:requests')
    qs = DataRequest.objects.filter(tenant=t).select_related('student')
    status = request.GET.get('status', 'open')
    if status == 'open':
        qs = qs.exclude(status__in=('DONE', 'REFUSED'))
    elif status in dict(DataRequest.STATUSES):
        qs = qs.filter(status=status)
    return render(request, 'security/requests.html', {
        'rows': qs[:200], 'status': status, 'kinds': DataRequest.KINDS, 'statuses': DataRequest.STATUSES,
        'students': Student.objects.filter(tenant=t).order_by('full_name').only('id', 'full_name', 'admission_number')})


@admin_only
def request_detail(request, pk):
    from users.models import Student
    t = request.tenant
    dr = get_object_or_404(DataRequest, tenant=t, pk=pk)
    if request.method == 'POST':
        op = request.POST.get('op')
        try:
            if op == 'verify':
                dr.identity_checked, dr.status = True, 'IN_PROGRESS' if dr.status in ('NEW', 'VERIFYING') else dr.status
                dr.handled_by = request.user
                dr.save()
                svc.add_note(dr, request.user, f'Identity confirmed: {request.POST.get("how") or "checked"}')
            elif op == 'student':
                dr.student = get_object_or_404(Student, tenant=t, pk=request.POST.get('student'))
                dr.save(update_fields=['student'])
            elif op == 'status':
                if request.POST.get('status') in ('NEW', 'VERIFYING', 'IN_PROGRESS'):
                    dr.status = request.POST['status']
                    dr.save(update_fields=['status'])
            elif op == 'note':
                svc.add_note(dr, request.user, request.POST.get('text'))
            elif op == 'export':
                svc.build_request_export(dr, request.user, request)
                messages.success(request, 'The data export is ready. It is sent with your reply.')
            elif op == 'anonymise':
                if not dr.student or request.POST.get('confirm', '').strip().upper() != dr.student.admission_number.upper():
                    raise svc.SecurityError('Type the admission number to confirm.')
                if not dr.identity_checked:
                    raise svc.SecurityError("Confirm the requester's identity first.")
                svc.anonymise_student(dr.student, request.user, request)
                svc.add_note(dr, request.user, 'Personal details anonymised; anonymous results and payments kept as '
                                               'the law requires.')
                messages.success(request, 'The record has been anonymised.')
            elif op == 'close':
                svc.close_request(dr, request.user, request.POST.get('status'), request.POST.get('response'), request)
                messages.success(request, 'Reply sent and request closed.')
        except svc.SecurityError as problem:
            messages.error(request, str(problem))
        return redirect('security:request', pk=dr.pk)
    return render(request, 'security/request.html', {
        'dr': dr, 'students': Student.objects.filter(tenant=t).order_by('full_name').only('id', 'full_name',
                                                                                       'admission_number')})


@login_required
def request_file(request, pk):
    dr = get_object_or_404(DataRequest, tenant=request.tenant, pk=pk)
    allowed = svc.is_admin(request.user, request.tenant) or (
        dr.status == 'DONE' and dr.submitted_by_id == request.user.pk)
    if not allowed or not dr.export:
        raise Http404
    svc.log_event('EXPORT', f'{dr.reference}: export downloaded', request=request, obj=dr)
    from django.http import FileResponse
    return FileResponse(dr.export.open('rb'), as_attachment=True, filename=f'{dr.reference}.zip')


# ─────────────────────────────────────────────────────────────────────────────
# School: breaches (F452)
# ─────────────────────────────────────────────────────────────────────────────

@admin_only
def breaches(request):
    if request.method == 'POST':
        try:
            b = svc.report_breach(request.tenant, request.POST, request.user, request)
            messages.warning(request, f'{b.reference} recorded. Administrators and the DPO have been told.')
            return redirect('security:breach', pk=b.pk)
        except svc.SecurityError as problem:
            messages.error(request, str(problem))
        return redirect('security:breaches')
    return render(request, 'security/breaches.html', {
        'rows': Breach.objects.filter(tenant=request.tenant), 'severities': Breach.SEVERITY,
        'now': timezone.localtime().strftime('%Y-%m-%dT%H:%M')})


@admin_only
def breach_detail(request, pk):
    b = get_object_or_404(Breach, tenant=request.tenant, pk=pk)
    if request.method == 'POST':
        try:
            svc.update_breach(b, request.POST.get('op'), request.user, request.POST, request)
            messages.success(request, 'Saved.')
        except svc.SecurityError as problem:
            messages.error(request, str(problem))
        return redirect('security:breach', pk=b.pk)
    return render(request, 'security/breach.html', {'b': b, 'letter': svc.ndpc_letter(b), 'notice': svc.people_notice(b),
                                                    'now': timezone.localtime().strftime('%Y-%m-%dT%H:%M')})


# ─────────────────────────────────────────────────────────────────────────────
# School: retention (F450) and compliance report (F458)
# ─────────────────────────────────────────────────────────────────────────────

@admin_only
def retention(request):
    rules = svc.rules_for(request.tenant)
    if request.method == 'POST':
        op = request.POST.get('op')
        try:
            if op == 'save':
                for r in rules:
                    svc.save_rule(r, request.POST.get(f'on_{r.kind}'), request.POST.get(f'days_{r.kind}') or r.days,
                                  request.user)
                messages.success(request, 'Retention rules saved. They run every night.')
            elif op == 'run':
                out = svc.run_retention(request.tenant)
                messages.success(request, f'Done — {sum(out.values())} records removed or anonymised.')
        except svc.SecurityError as problem:
            messages.error(request, str(problem))
        return redirect('security:retention')
    for r in rules:
        r.preview = svc.run_rule(r, dry=True)
    return render(request, 'security/retention.html', {'rules': rules})


@admin_only
def compliance(request):
    try:
        year = int(request.GET.get('year') or timezone.localdate().year)
    except ValueError:
        year = timezone.localdate().year
    rep = svc.compliance(request.tenant, year)
    return render(request, 'security/compliance.html', {
        'r': rep, 'years': range(timezone.localdate().year, timezone.localdate().year - 4, -1),
        'generated': timezone.localtime()})


# ─────────────────────────────────────────────────────────────────────────────
# Parents (F448, F451)
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def family(request):
    from users.models import ENROLLED_STATUSES
    from users.views_achievements import _parent_children
    t = request.tenant
    kids = [k for k in _parent_children(request) if k.status in ENROLLED_STATUSES]
    if not kids:
        return _deny(request, 'This page is for parents.')
    purposes = list(svc.ensure_purposes(t).filter(active=True))
    if request.method == 'POST':
        kid = next((k for k in kids if str(k.pk) == request.POST.get('student')), None)
        if not kid:
            raise Http404
        try:
            if request.POST.get('op') == 'consent':
                p = next((p for p in purposes if str(p.pk) == request.POST.get('purpose') and p.needs_consent), None)
                if not p:
                    raise Http404
                svc.set_consent(kid, p, request.POST.get('granted') == '1', request.user,
                                given_by=kid.parent_name or request.user.get_full_name(), relationship='Parent',
                                method='PORTAL', request=request)
                messages.success(request, 'Thank you — your choice is saved.')
            else:
                data = request.POST.copy()
                data['requester_name'] = data.get('requester_name') or kid.parent_name or 'Parent'
                data['requester_email'] = data.get('requester_email') or kid.parent_email or ''
                data['requester_phone'] = data.get('requester_phone') or kid.parent_phone or ''
                data['relationship'] = 'Parent'
                dr = svc.submit_request(t, data, kid, request.user, request)
                messages.success(request, f'Request {dr.reference} sent. The school will reply by '
                                          f'{dr.due_on:%d %B %Y}.')
        except svc.SecurityError as problem:
            messages.error(request, str(problem))
        return redirect('security:family')
    have = {(c.student_id, c.purpose_id): c for c in Consent.objects.filter(student__in=kids)}
    cards = [{'kid': k, 'rows': [(p, have.get((k.pk, p.pk))) for p in purposes]} for k in kids]
    return render(request, 'security/family.html', {
        'page_title': 'Privacy & consent', 'cards': cards, 'kinds': DataRequest.KINDS,
        'mine': DataRequest.objects.filter(tenant=t, student__in=kids).order_by('-created_at')[:20],
        'n': svc.notice_for(t)})


# ─────────────────────────────────────────────────────────────────────────────
# Public
# ─────────────────────────────────────────────────────────────────────────────

def public_notice(request):
    t = getattr(request, 'tenant', None)
    if t is None:
        raise Http404
    n = svc.notice_for(t)
    if not n.published:
        raise Http404
    from saas_admin.services import render_body
    return render(request, 'security/public_notice.html', {
        'page_title': 'Privacy notice', 'n': n, 'body': render_body(n.notice),
        'purposes': svc.ensure_purposes(t).filter(active=True),
        'rules': [r for r in svc.rules_for(t) if r.enabled]})


def public_request(request):
    t = getattr(request, 'tenant', None)
    if t is None:
        raise Http404
    done = None
    if request.method == 'POST':
        if request.POST.get('website'):                     # honeypot for bots
            raise Http404
        ip = svc.client_ip(request)
        recent = AuditEvent.objects.filter(tenant=t, action='REQUEST', ip=ip,
                                           at__gte=timezone.now() - timedelta(hours=1)).count() if ip else 0
        if recent >= 5:
            messages.error(request, 'Too many requests from this connection. Please try again later.')
        else:
            data = request.POST.copy()
            data['details'] = (f"Student named: {data.get('student_name', '').strip()} "
                               f"(admission no. {data.get('admission_number', '').strip() or 'not given'})\n\n"
                               f"{data.get('details', '')}")
            try:
                done = svc.submit_request(t, data, None, None, request)
            except svc.SecurityError as problem:
                messages.error(request, str(problem))
    return render(request, 'security/public_request.html', {'page_title': 'Data protection request',
                                                            'kinds': DataRequest.KINDS, 'done': done,
                                                            'n': svc.notice_for(t), 'form': request.POST})


def disclosure(request):
    done = None
    if request.method == 'POST':
        if request.POST.get('website'):
            raise Http404
        try:
            done = svc.report_vulnerability(request.POST)
        except svc.SecurityError as problem:
            messages.error(request, str(problem))
    from django.conf import settings
    from .models import VulnerabilityReport
    return render(request, 'security/disclosure.html', {
        'done': done, 'severities': VulnerabilityReport.SEVERITY, 'form': request.POST,
        'root': getattr(settings, 'ROOT_DOMAIN', 'localhost')})


def security_txt(request):
    from django.conf import settings
    from saas_admin.services import platform_emails
    root = getattr(settings, 'ROOT_DOMAIN', 'localhost')
    scheme = 'http' if root in ('localhost', '127.0.0.1') else 'https'
    contact = getattr(settings, 'SECURITY_CONTACT_EMAIL', '') or (platform_emails() or [''])[0]
    expires = (timezone.now() + timedelta(days=365)).strftime('%Y-%m-%dT00:00:00Z')
    site = getattr(settings, 'SITE_URL', '') or f'{scheme}://{root}'
    lines = [f'Contact: {site}/security/disclosure/']
    if contact:
        lines.insert(0, f'Contact: mailto:{contact}')
    lines += [f'Expires: {expires}', f'Policy: {site}/security/disclosure/', 'Preferred-Languages: en']
    return HttpResponse('\n'.join(lines) + '\n', content_type='text/plain')


# ─────────────────────────────────────────────────────────────────────────────
# Platform: backups and system health
# ─────────────────────────────────────────────────────────────────────────────

@admin_only
def platform_page(request):
    from . import platform
    if request.method == 'POST' and request.POST.get('op') == 'backup':
        try:
            platform.start_manual(request.user)
            messages.success(request, 'Backup started — it takes a few seconds to a minute. Refresh to see it below.')
        except platform.PlatformError as problem:
            messages.error(request, str(problem))
        return redirect('security:platform')
    checks = platform.health(request.tenant)
    return render(request, 'security/platform.html', {
        'checks': checks, 'counts': platform.summary(checks), 'backups': platform.backups(),
        'last': platform.last_good(), 'fresh': not platform.backup_due(), 'running': platform.running(), 'keep_days': django_settings.BACKUP_KEEP_DAYS,
        'can_download': request.user.is_superuser, 'auto': getattr(django_settings, 'AUTO_BACKUP', True),
        'offsite': bool(getattr(django_settings, 'BACKUP_OFFSITE_DIR', '') or getattr(django_settings, 'BACKUP_OFFSITE_COMMAND', '')),
    })


@admin_only
def backup_download(request, name):
    """The whole database — superusers only, and every download is in the audit trail."""
    from django.http import FileResponse
    from . import platform
    if not request.user.is_superuser:
        return _deny(request)
    try:
        path = platform.backup_path(name)
    except platform.PlatformError as problem:
        messages.error(request, str(problem))
        return redirect('security:platform')
    svc.log_event('EXPORT', f'Backup file downloaded: {name}', request=request)
    return FileResponse(open(path, 'rb'), as_attachment=True, filename=name, content_type='application/gzip')


# ─────────────────────────────────────────────────────────────────────────────
# Password help (no email on the account)
# ─────────────────────────────────────────────────────────────────────────────

def password_help_ask(request):
    """Public, students and parents only: ask the school office to reset a password.
    The answer is the same whether or not the account exists."""
    from . import password_help as ph
    from .models import PasswordHelpRequest
    t = getattr(request, 'tenant', None)
    if t is None:
        raise Http404
    sent = False
    if request.method == 'POST':
        if request.POST.get('website'):                     # honeypot for bots
            raise Http404
        try:
            ph.ask(t, request.POST, request)
            sent = True
        except ph.PasswordHelpError as problem:
            messages.error(request, str(problem))
    kind = request.POST.get('kind') or request.GET.get('who', '').upper()
    return render(request, 'security/password_help_ask.html', {
        'tenant': t, 'sent': sent, 'kinds': ph.PUBLIC_KINDS,
        'kind': kind if kind in dict(ph.PUBLIC_KINDS) else 'STUDENT',
        'form': {} if sent else request.POST})


@admin_only
def password_help(request):
    """Requests from people with no email, and resets done at the office."""
    from . import password_help as ph
    from .models import PasswordHelpRequest
    t = request.tenant
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action == 'decline':
                req = get_object_or_404(PasswordHelpRequest._default_manager, pk=request.POST.get('pk'), tenant=t)
                ph.decline(req, request.user, request)
                messages.success(request, f'Request from {req.name} declined.')
                return redirect('security:password_help')
            if not request.POST.get('identity_checked'):
                raise ph.PasswordHelpError('Tick the box to confirm you have checked who they are first.')
            if action == 'desk':
                req, password = ph.at_desk(t, request.POST, request.user, request)
            else:
                req = get_object_or_404(PasswordHelpRequest._default_manager, pk=request.POST.get('pk'), tenant=t)
                password = ph.reset(req, request.user, request)
            request.session['password_help_shown'] = {'pk': req.pk, 'password': password}
        except ph.PasswordHelpError as problem:
            messages.error(request, str(problem))
        return redirect('security:password_help')

    shown = request.session.pop('password_help_shown', None)          # shown once, never stored anywhere else
    if shown:
        shown['req'] = PasswordHelpRequest._default_manager.filter(pk=shown['pk'], tenant=t).first()
        shown['label'] = ph.account_label(shown['req']) if shown['req'] else ''
    status = request.GET.get('status', 'OPEN')
    rows = PasswordHelpRequest._default_manager.filter(tenant=t).select_related('user', 'student', 'handled_by')
    if status in dict(PasswordHelpRequest.STATUSES):
        rows = rows.filter(status=status)
    rows = list(rows[:200])
    for r in rows:
        r.account = ph.account_label(r)
        r.phone_on_file = ph.phone_on_file(r)
        r.allowed = ph.can_reset(request.user, r)
    return render(request, 'security/password_help.html', {
        'rows': rows, 'status': status, 'statuses': PasswordHelpRequest.STATUSES, 'kinds': PasswordHelpRequest.KINDS,
        'shown': shown, 'waiting': PasswordHelpRequest._default_manager.filter(tenant=t, status='OPEN').count()})


# ─────────────────────────────────────────────────────────────────────────────
# What each staff role sees (sidebar and dashboard)
# ─────────────────────────────────────────────────────────────────────────────

@admin_only
def menu_access(request):
    """Module permissions: what each staff role, or one person, sees in the sidebar and on the dashboard,
    and (for a person) their permission switches."""
    from dashboard import access, menu
    from users.models import Staff
    from .models import MenuRule
    t = request.tenant
    roles = dict(menu.ROLES)
    people = list(Staff.objects.filter(tenant=t, role__in=roles).select_related('user').order_by('role', 'full_name'))
    person = None
    if request.POST.get('staff') or request.GET.get('staff'):
        person = get_object_or_404(Staff, pk=request.POST.get('staff') or request.GET.get('staff'), tenant=t,
                                   role__in=roles)
    role = person.role if person else (request.POST.get('role') or request.GET.get('role') or 'TEACHER')
    if role not in roles:
        role = 'TEACHER'
    who = person.full_name if person else roles[role]
    sections = menu.sections_for(role)
    dash = menu.DASHBOARD_OF[role]
    cards = menu.CARDS[dash]
    every = {i['name'] for g in sections for i in g['items']} | {f'{dash}:{k}' for k, _ in cards}
    back = f"{request.path}?staff={person.pk}" if person else f"{request.path}?role={role}"

    if request.method == 'POST':
        action = request.POST.get('action', 'save')
        before = menu.saved_hidden(t, role, person)
        if action == 'reset':
            if person:
                MenuRule._default_manager.filter(tenant=t, staff=person).delete()
                messages.success(request, f'{who} now follows the {roles[role].lower()} settings.')
            else:
                MenuRule._default_manager.filter(tenant=t, role=role, staff__isnull=True).delete()
                messages.success(request, f'{who} are back to the recommended menu.')
        else:
            shown = set(request.POST.getlist('show'))
            after = set(menu.save(t, role, every - shown, request.user, staff=person))
            note = f'{who} now see{"s" if person else ""} {len(every - after)} of {len(every)} links and dashboard cards.'
            if person:
                switches = {name: name in request.POST.getlist('switch') for name in access.SWITCH_LABEL}
                changed = [access.SWITCH_LABEL[k] for k, v in switches.items() if getattr(person, k) != v]
                if changed:
                    for k, v in switches.items():
                        setattr(person, k, v)
                    person.save(update_fields=list(switches))
                    svc.log_event('PERMISSIONS', f'Permission switches changed for {who}: {", ".join(changed)}',
                                  request=request, obj=person)
                    note += f' Switches changed: {", ".join(changed)}.'
            messages.success(request, 'Saved. ' + note)
        after = menu.saved_hidden(t, role, person)
        if before != after:
            svc.log_event('PERMISSIONS', f'Sidebar and dashboard changed for {who}', request=request,
                          changes={'hidden': [sorted(before), sorted(after)]})
        if person:
            access.forget(person.user, t)
        return redirect(back)

    hidden = menu.saved_hidden(t, role, person)
    closed = access.closed_to(person.user, t, person) if person else set()
    for g in sections:
        for i in g['items']:
            i['on'] = i['name'] not in hidden
            i['needs'] = access.needs(i['name'])
            i['blocked'] = person is not None and i['on'] and i['name'] in closed
        g['shown'] = sum(1 for i in g['items'] if i['on'])
    card_rows = [{'key': f'{dash}:{k}', 'label': label, 'on': f'{dash}:{k}' not in hidden} for k, label in cards]
    own = {r.staff_id for r in MenuRule._default_manager.filter(tenant=t, staff__isnull=False)}
    return render(request, 'security/menu_access.html', {
        'roles': menu.ROLES, 'role': role, 'role_label': roles[role], 'who': who, 'person': person,
        'people': people, 'own_settings': [p for p in people if p.pk in own],
        'person_has_own': bool(person and person.pk in own),
        'sections': sections, 'cards': card_rows,
        'dash_label': {'teacher': 'Teacher dashboard', 'admin': 'Office dashboard',
                       'proprietor': 'Proprietor dashboard'}[dash],
        'customised': MenuRule._default_manager.filter(tenant=t, role=role, staff__isnull=True).exists(),
        'staff_count': sum(1 for p in people if p.role == role),
        'switches': [(group, [(name, label, getattr(person, name, False)) for name, label in rows])
                     for group, rows in access.SWITCHES] if person else [],
    })
