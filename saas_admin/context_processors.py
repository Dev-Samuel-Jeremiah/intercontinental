"""School branding, admin notices and product tours for every page."""


def platform(request):
    tenant = getattr(request, 'tenant', None)
    user = getattr(request, 'user', None)
    if tenant is None:
        return {}
    out = {}
    try:
        from .models import Branding
        from .services import notices_for, tour_for
        out['brand'] = Branding.objects.filter(tenant=tenant).first()
        if user is not None and user.is_authenticated:
            out['platform_notices'] = notices_for(user, tenant)
            if request.method == 'GET' and not request.headers.get('x-requested-with'):
                out['tour'] = tour_for(user, request.path)
    except Exception:   # tables missing (before migrate) must never break a page
        return {}
    return out
