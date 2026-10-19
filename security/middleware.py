"""
security/middleware.py
──────────────────────
LoginThrottleMiddleware   F446  counts failed sign-ins on every login page and
                                pauses an account (or an IP address) after too
                                many; works for any login view because it looks
                                at whether the request ended signed in.
SecurityGateMiddleware    F442  holds a signed-in user at the two-step page
                                until the code is entered, or at the set-up
                                page when the school requires it;
                          F447  sends staff with an expired password to change it;
                          F456  keeps the session list fresh, signs out idle
                                staff, and sets the device cookie (F445).
SecurityHeadersMiddleware F457  Content-Security-Policy, Referrer-Policy and
                                Permissions-Policy on every response.
"""
import time

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import logout
from django.shortcuts import redirect, render

from . import services as svc

OPEN_PREFIXES = ('/static/', '/m/', '/favicon', '/security/2fa/', '/logout/', '/admin/logout/',
                 '/support/impersonate/', '/hooks/', '/.well-known/', '/tls-ask/')


class LoginThrottleMiddleware:

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        field = svc.LOGIN_FIELDS.get(request.path) if request.method == 'POST' else None
        if not field:
            return self.get_response(request)
        identity = request.POST.get(field, '')
        minutes = svc.lock_minutes_left(request, identity)
        if minutes:
            resp = render(request, 'security/locked.html', {'minutes': minutes}, status=429)
            resp['Retry-After'] = str(minutes * 60)
            return resp
        response = self.get_response(request)
        user = getattr(request, 'user', None)
        if user is not None and user.is_authenticated:
            svc.clear_failures(request, identity)
        elif response.status_code < 500:
            svc.record_failure(request, identity)
        return response


class SecurityGateMiddleware:

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, 'user', None)
        if user is not None and user.is_authenticated and not request.path.startswith(OPEN_PREFIXES):
            stop = self.check(request, user)
            if stop is not None:
                return stop
        response = self.get_response(request)
        token = getattr(request, '_sec_device_token', None)
        if token:
            response.set_cookie(svc.DEVICE_COOKIE, token, max_age=60 * 60 * 24 * 365 * 2, httponly=True,
                                samesite='Lax', secure=request.is_secure())
        return response

    def check(self, request, user):
        session = request.session
        impersonating = bool(session.get('impersonation_id'))
        now = time.time()
        if session.get('sec_2fa') == 'pending' and not impersonating:
            return redirect(f'/security/2fa/verify/?next={request.get_full_path()}')
        if getattr(settings, 'TWO_FACTOR_ENFORCE', True) and not impersonating and session.get('sec_2fa') != 'ok' \
                and session.get('sec_2fa_free', 0) < now:
            if svc.required_2fa(user) and not svc.two_factor_for(user):
                if not request.path.startswith('/security/'):
                    messages.info(request, 'Your school requires two-step sign-in for your account. '
                                           'Set it up to continue.')
                return redirect('/security/2fa/setup/?required=1')
            session['sec_2fa_free'] = now + 600          # check again in 10 minutes
        if not impersonating and not getattr(user, 'is_student', False) and not getattr(user, 'is_parent', False):
            seen = session.get('sec_seen') or 0
            idle = session.get('sec_idle')
            if idle is None or session.get('sec_pw_ok', 0) < now:
                cfg = svc.settings_for(getattr(request, 'tenant', None) or getattr(user, 'tenant', None))
                idle = session['sec_idle'] = cfg.idle_minutes
                if request.path != '/change-password/' and not request.path.startswith('/security/') \
                        and svc.password_expired(user):
                    messages.info(request, 'Your password has expired. Choose a new one to continue.')
                    return redirect('/change-password/')
                session['sec_pw_ok'] = now + 3600
            if idle and seen and now - seen > idle * 60:
                logout(request)
                messages.info(request, 'You were signed out after a period of inactivity.')
                return redirect('/')
            if now - seen > 60:
                session['sec_seen'] = now
        svc.touch(request)
        return None


CSP_SOURCES = {
    'default-src': ["'self'"],
    'script-src': ["'self'", "'unsafe-inline'", 'https://cdn.jsdelivr.net', 'https://unpkg.com', 'https://code.jquery.com',
                   'https://cdnjs.cloudflare.com', 'https://cdn.datatables.net', 'https://connect.facebook.net',
                   'https://js.paystack.co', 'https://cdn.tailwindcss.com'],
    'style-src': ["'self'", "'unsafe-inline'", 'https://fonts.googleapis.com', 'https://cdn.jsdelivr.net',
                  'https://unpkg.com', 'https://cdnjs.cloudflare.com', 'https://cdn.datatables.net'],
    'font-src': ["'self'", 'data:', 'https://fonts.gstatic.com', 'https://cdn.jsdelivr.net', 'https://unpkg.com',
                 'https://cdnjs.cloudflare.com'],
    'img-src': ["'self'", 'data:', 'blob:', 'https:'],
    'media-src': ["'self'", 'blob:'],
    'connect-src': ["'self'", 'https://*.facebook.com', 'https://api.paystack.co'],
    'frame-src': ["'self'", 'https://checkout.paystack.com', 'https://www.youtube.com', 'https://www.youtube-nocookie.com',
                  'https://player.vimeo.com'],
    'worker-src': ["'self'", 'blob:'],
    'object-src': ["'none'"],
    'base-uri': ["'self'"],
}


def build_csp():
    sources = {k: list(v) for k, v in CSP_SOURCES.items()}
    for directive, extra in (getattr(settings, 'CSP_EXTRA', None) or {}).items():
        sources.setdefault(directive, ["'self'"]).extend(extra if isinstance(extra, (list, tuple)) else [extra])
    ancestors = "'none'" if getattr(settings, 'X_FRAME_OPTIONS', 'DENY') == 'DENY' else "'self'"
    parts = [f'{k} {" ".join(dict.fromkeys(v))}' for k, v in sources.items()]
    parts.append(f'frame-ancestors {ancestors}')
    if getattr(settings, 'CSP_REPORT_URI', ''):
        parts.append(f'report-uri {settings.CSP_REPORT_URI}')
    return '; '.join(parts)


class PlatformHeartbeatMiddleware:
    """Now and then (at most every 10 minutes per worker), make sure today's backup has happened."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        try:
            from .platform import maybe_auto_backup
            maybe_auto_backup()
        except Exception:
            import logging
            logging.getLogger(__name__).exception('Backup heartbeat failed')
        return response


class SecurityHeadersMiddleware:

    def __init__(self, get_response):
        self.get_response = get_response
        self.csp = None

    def __call__(self, request):
        response = self.get_response(request)
        if not getattr(settings, 'CSP_ENABLED', True):
            return response
        if self.csp is None:
            self.csp = build_csp()
        header = 'Content-Security-Policy-Report-Only' if getattr(settings, 'CSP_REPORT_ONLY', False) \
            else 'Content-Security-Policy'
        if header not in response and 'Content-Security-Policy' not in response:
            response[header] = self.csp
        response.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
        response.setdefault('Permissions-Policy',
                            'camera=(self), microphone=(self), geolocation=(self), payment=(), usb=(), '
                            'interest-cohort=()')
        response.setdefault('Cross-Origin-Opener-Policy', 'same-origin-allow-popups')
        return response
