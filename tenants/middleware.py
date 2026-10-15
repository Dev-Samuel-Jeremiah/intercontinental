"""
tenants/middleware.py
─────────────────────
TenantMiddleware — attaches the school to every request.

This is a dedicated, single-school installation. Every request is served
by the school portal (lms_project/tenant_urls.py) and is bound to the one
school configured by SCHOOL_SLUG in settings. The tenant layer is kept
underneath so every record stays tied to the school, but there is no
public SaaS site, no subscription and nothing that can lock the school out.

Resolution order:
  1. A school whose address (subdomain or custom_domain) matches the host —
     kept so the test suite, which builds several schools, keeps working.
  2. Otherwise the school named by settings.SCHOOL_SLUG.
  3. Otherwise the only school in the database.
If no school exists yet, a short page explains how to run `setup_school`.
"""

import threading
from django.conf import settings
from django.core.cache import cache
from django.shortcuts import render

_thread_local = threading.local()

TENANT_CACHE_TIMEOUT = 60  # seconds


def get_current_tenant():
    """Return the tenant bound to the current thread (usable in signals/models)."""
    return getattr(_thread_local, 'tenant', None)


def set_current_tenant(tenant):
    _thread_local.tenant = tenant


def clear_current_tenant():
    _thread_local.tenant = None


def get_school():
    """The school this installation runs, or None before setup_school has run."""
    from tenants.models import Tenant  # avoid circular import
    pk = cache.get('tenant:school')
    if pk:
        tenant = Tenant.objects.filter(pk=pk).first()
        if tenant:
            return tenant
    slug = getattr(settings, 'SCHOOL_SLUG', '')
    tenant = (Tenant.objects.filter(subdomain=slug).first() if slug else None) \
        or Tenant.objects.order_by('created_at').first()
    if tenant:
        cache.set('tenant:school', str(tenant.pk), TENANT_CACHE_TIMEOUT)
    return tenant


_ALLOWED_WHILE_FORCED = ('/change-password/', '/logout/', '/static/', '/media/')


class TenantMiddleware:
    """
    Must be placed AFTER SessionMiddleware and AuthenticationMiddleware
    in MIDDLEWARE settings so request.user is available.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        tenant = self._resolve_tenant(request)
        request.tenant = tenant
        set_current_tenant(tenant)
        request.urlconf = 'lms_project.tenant_urls'

        if tenant is None:
            if not request.path.startswith(('/admin/', '/static/', '/media/')):
                response = render(request, 'tenants/not_set_up.html', status=503)
                clear_current_tenant()
                return response
        else:
            # ── Force password change for auto-generated accounts ────────
            if (request.user.is_authenticated
                    and getattr(request.user, 'must_change_password', False)
                    and not request.path.startswith(_ALLOWED_WHILE_FORCED)):
                from django.shortcuts import redirect
                clear_current_tenant()
                return redirect('users:change_password')
            request.session['tenant_id'] = str(tenant.id)

        response = self.get_response(request)
        clear_current_tenant()
        return response

    def _resolve_tenant(self, request):
        host = request.get_host().split(':')[0].lower()
        return self._lookup_host(host) or get_school()

    @staticmethod
    def _lookup_host(host):
        from tenants.models import Tenant  # avoid circular import
        cache_key = f'tenant:host:{host}'
        cached = cache.get(cache_key)
        if cached == '__NONE__':
            return None
        if cached:
            t = Tenant.objects.filter(pk=cached).first()
            if t:
                return t
        label = host.split('.')[0]
        tenant = (Tenant.objects.filter(custom_domain=host).first()
                  or (Tenant.objects.filter(subdomain=label).first() if '.' in host else None))
        cache.set(cache_key, str(tenant.pk) if tenant else '__NONE__', TENANT_CACHE_TIMEOUT)
        return tenant


# ─────────────────────────────────────────────────────────────────────────────
# Feature gates — kept so existing imports keep working. Every module is
# included in this installation, so they never block anything.
# ─────────────────────────────────────────────────────────────────────────────

def feature_required(feature_code):
    def decorator(view_func):
        return view_func
    return decorator


class FeatureRequiredMixin:
    required_feature = None
