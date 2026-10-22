"""
lms_project/error_views.py
──────────────────────────
Branded error pages for the whole platform (public site and every school
portal). Wired up as handler400/403/404/500 in both root URLconfs and as
CSRF_FAILURE_VIEW.

Only used when DEBUG=False — in development Django shows its technical pages.

The 500 handler must never fail itself: it renders without the request
context processors (which hit the database) and falls back to plain HTML.
"""
import logging
import uuid

from django.http import HttpResponse, JsonResponse
from django.template.loader import render_to_string
from django.urls import NoReverseMatch, reverse

logger = logging.getLogger('django.request')

ERRORS = {
    400: {
        'title': 'Something about that request wasn’t right',
        'message': 'The page couldn’t understand the request. Please go back and try again.',
        'icon': 'bi-exclamation-diamond', 'tone': 'amber', 'label': 'Bad request',
    },
    403: {
        'title': 'You don’t have access to this page',
        'message': 'Your account doesn’t have permission to view this page. If you think you should, please contact your school administrator.',
        'icon': 'bi-shield-lock', 'tone': 'amber', 'label': 'Access denied',
    },
    404: {
        'title': 'We couldn’t find that page',
        'message': 'The link may be broken, or the page may have been moved or removed.',
        'icon': 'bi-compass', 'tone': 'gold', 'label': 'Page not found',
    },
    500: {
        'title': 'Sorry, that didn’t work',
        'message': 'The portal is still running: only this one action failed. Go back and try again. If it keeps happening, tell the ICT office the reference below so they can fix it.',
        'icon': 'bi-tools', 'tone': 'red', 'label': 'Server error',
    },
    'csrf': {
        'title': 'Your session expired',
        'message': 'For your security, the form couldn’t be submitted because the page was open for too long or your session changed. Go back, refresh the page and try again.',
        'icon': 'bi-arrow-repeat', 'tone': 'amber', 'label': 'Session expired',
    },
}


def _wants_json(request):
    accept = request.headers.get('Accept', '')
    return (request.headers.get('x-requested-with') == 'XMLHttpRequest'
            or ('application/json' in accept and 'text/html' not in accept)
            or '/api/' in request.path)


def _safe_reverse(name):
    try:
        return reverse(name)
    except NoReverseMatch:
        return None


def _home_action(request):
    """Where 'go home' should lead for this visitor."""
    tenant = getattr(request, 'tenant', None)
    user = getattr(request, 'user', None)
    if tenant is not None:
        if user is not None and user.is_authenticated:
            if getattr(user, 'is_parent', False):
                return _safe_reverse('users:parent_dashboard') or '/', 'Go to my dashboard'
            return _safe_reverse('dashboard:router') or '/', 'Go to my dashboard'
        return _safe_reverse('users:staff_login') or '/', 'Go to sign in'
    return '/', 'Go to homepage'


def _render(request, key, status, ref=None, safe=False):
    info = ERRORS[key]
    if _wants_json(request):
        body = {'ok': False, 'status': status, 'error': info['label'], 'message': info['message']}
        if ref:
            body['reference'] = ref
        return JsonResponse(body, status=status)

    tenant = getattr(request, 'tenant', None)
    home_url, home_label = _home_action(request)
    context = {
        'code': status, 'ref': ref, 'home_url': home_url, 'home_label': home_label,
        'school_name': getattr(tenant, 'name', '') or '',
        'school_initials': (getattr(tenant, 'name', '') or 'SP')[:2].upper(),
        'tenant': tenant,
        'signed_in': bool(getattr(getattr(request, 'user', None), 'is_authenticated', False)),
        'path': request.path,
        **info,
    }
    try:
        # safe=True: no request → no context processors → no database access.
        html = render_to_string('errors/error.html', context, request=None if safe else request)
    except Exception:  # never let the error page itself fail
        html = (f'<!doctype html><title>{info["label"]}</title>'
                f'<body style="font-family:system-ui;background:#081a3a;color:#f2f5fb;display:grid;'
                f'place-items:center;min-height:100vh;margin:0;text-align:center">'
                f'<div><h1>{info["title"]}</h1><p>{info["message"]}</p>'
                f'{f"<p>Reference: {ref}</p>" if ref else ""}<p><a style="color:#ffe580" href="/">Go home</a></p></div>')
    return HttpResponse(html, status=status)


def bad_request(request, exception=None):
    return _render(request, 400, 400)


def permission_denied(request, exception=None):
    return _render(request, 403, 403)


def page_not_found(request, exception=None):
    return _render(request, 404, 404)


def server_error(request):
    ref = uuid.uuid4().hex[:8].upper()
    tenant = getattr(request, 'tenant', None)
    logger.error('Server error ref=%s path=%s school=%s', ref, request.path,
                 getattr(tenant, 'subdomain', '-'), exc_info=True)
    return _render(request, 500, 500, ref=ref, safe=True)


def csrf_failure(request, reason=''):
    return _render(request, 'csrf', 403)
