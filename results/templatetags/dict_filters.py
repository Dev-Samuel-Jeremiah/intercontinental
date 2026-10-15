# classroom/templatetags/dict_filters.py
from django import template

register = template.Library()

@register.filter
def get_item(dictionary, key):
    if isinstance(dictionary, dict):
        return dictionary.get(key)
    return None

@register.filter
def dict_get(dictionary, key, default=""):
    if isinstance(dictionary, dict):
        return dictionary.get(key, default)
    return default
