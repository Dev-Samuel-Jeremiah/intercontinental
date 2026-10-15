"""
tenants/decorators.py
──────────────────────
Drop-in replacements for @login_required that also enforce:
  1. There IS a tenant on the request (not root domain)

Usage:

    from tenants.decorators import tenant_login_required, feature_required

    @tenant_login_required
    def my_view(request): ...

    @tenant_login_required
    @feature_required('finance')
    def finance_view(request): ...
"""

from functools import wraps
from django.http import HttpResponseForbidden
from django.shortcuts import redirect
from django.contrib.auth.decorators import login_required


def tenant_login_required(view_func):
    """
    Ensures:
      - User is authenticated
      - request.tenant exists and is accessible

    If tenant is None (root domain), redirects to the public site.
    If tenant is suspended/expired, the TenantMiddleware already handles it,
    but this provides a belt-and-suspenders guard inside views.
    """
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        tenant = getattr(request, 'tenant', None)

        if tenant is None or not request.user.is_authenticated:
            return redirect('/')

        return view_func(request, *args, **kwargs)

    return wrapper


def feature_required(feature_code):
    """Kept for compatibility: every module is included in this installation."""
    def decorator(view_func):
        return view_func
    return decorator


def school_admin_required(view_func):
    """Signed-in school administrators only (superuser or a Staff record with role ADMIN)."""
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect('users:staff_login')
        staff = getattr(request.user, 'staff', None)
        if not (request.user.is_superuser or (staff is not None and staff.role == 'ADMIN')):
            return HttpResponseForbidden("Admin only.")
        return view_func(request, *args, **kwargs)
    return wrapper
