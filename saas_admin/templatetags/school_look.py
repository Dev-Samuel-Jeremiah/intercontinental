"""Template tags for public pages: the school's banner picture and crest, wherever a page is rendered."""
from django import template
from django.templatetags.static import static

register = template.Library()


@register.simple_tag(takes_context=True)
def school_banner(context):
    """The home page's banner picture (Branding → Website home page), or the built-in one."""
    from saas_admin.services import landing_content
    request = context.get('request')
    tenant = context.get('tenant') or getattr(request, 'tenant', None)
    return landing_content(tenant)['hero_image']


@register.simple_tag(takes_context=True)
def school_crest(context):
    """The school's uploaded logo, or the built-in crest."""
    tenant = context.get('tenant')
    return tenant.logo.url if tenant is not None and tenant.logo else static('images/school-crest.png')
