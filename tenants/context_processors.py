"""
tenants/context_processors.py
Makes the school available to every template as `tenant` (and `school`).
"""


def tenant_context(request):
    tenant = getattr(request, 'tenant', None)
    return {'tenant': tenant, 'school': tenant}
