from django import template

from results.models import AssessmentScheme

register = template.Library()


@register.simple_tag(takes_context=True)
def ca_scheme(context):
    """{% ca_scheme as ca %} — the school's assessment columns, labels and maximums."""
    request = context.get('request')
    tenant = getattr(request, 'tenant', None)
    if tenant is None:
        from tenants.middleware import get_current_tenant
        tenant = get_current_tenant()
    cache = getattr(request, '_ca_scheme', None) if request is not None else None
    if cache is None:
        cache = AssessmentScheme.for_tenant(tenant)
        if request is not None:
            request._ca_scheme = cache
    return cache


@register.filter
def score(result, field):
    """{{ tr|score:'ca1_score' }}"""
    return getattr(result, field, 0) or 0


@register.filter
def ca_total(result, scheme):
    return sum(getattr(result, c['field'], 0) or 0 for c in scheme.ca_columns)
