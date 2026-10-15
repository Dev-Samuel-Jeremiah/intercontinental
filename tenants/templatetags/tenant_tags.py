"""
tenants/templatetags/tenant_tags.py
─────────────────────────────────────
Template tags for feature-gating in HTML templates.

Usage:
  {% load tenant_tags %}

  {% if_feature 'finance' %}
    <a href="{% url 'finance:dashboard' %}">Finance</a>
  {% endif_feature %}

  {{ 'finance'|has_feature:request }}
"""

from django import template
from django.utils.html import format_html

register = template.Library()


@register.simple_tag(takes_context=True)
def has_feature(context, feature_code):
    """Returns True if current tenant has the feature."""
    tenant = context.get('tenant') or getattr(context.get('request'), 'tenant', None)
    if not tenant:
        return False
    return tenant.has_feature(feature_code)


@register.inclusion_tag('tenants/partials/upgrade_prompt.html', takes_context=True)
def upgrade_prompt(context, feature_code, label=''):
    tenant = context.get('tenant')
    plan = tenant.plan if tenant else None
    return {
        'feature_code': feature_code,
        'feature_label': label or feature_code.replace('_', ' ').title(),
        'tenant': tenant,
        'plan': plan,
    }


class FeatureBlockNode(template.Node):
    """Node for {% if_feature 'code' %} ... {% endif_feature %}"""

    def __init__(self, feature_code, nodelist_true, nodelist_false):
        self.feature_code = feature_code
        self.nodelist_true = nodelist_true
        self.nodelist_false = nodelist_false

    def render(self, context):
        feature = self.feature_code.resolve(context)
        tenant = context.get('tenant') or getattr(
            context.get('request'), 'tenant', None)

        if tenant and tenant.has_feature(feature):
            return self.nodelist_true.render(context)
        return self.nodelist_false.render(context)


@register.tag('if_feature')
def if_feature_tag(parser, token):
    """
    {% if_feature 'finance' %}
      ... shown if plan includes finance ...
    {% else_feature %}
      ... optional fallback ...
    {% endif_feature %}
    """
    bits = token.split_contents()
    if len(bits) != 2:
        raise template.TemplateSyntaxError(
            f"'{bits[0]}' tag requires exactly one argument.")

    feature_code = parser.compile_filter(bits[1])
    nodelist_true = parser.parse(('else_feature', 'endif_feature'))
    token = parser.next_token()

    if token.contents == 'else_feature':
        nodelist_false = parser.parse(('endif_feature',))
        parser.delete_first_token()
    else:
        nodelist_false = template.NodeList()

    return FeatureBlockNode(feature_code, nodelist_true, nodelist_false)


@register.filter
def plan_limit(tenant, resource):
    """{{ tenant|plan_limit:'students' }} → 200"""
    if not tenant or not tenant.plan:
        return '—'
    val = getattr(tenant.plan, f'max_{resource}', 0)
    return 'Unlimited' if val == 0 else val
