from django import template

register = template.Library()

@register.filter
def get_item(dictionary, key):
    """
    Safely get a value from a dictionary by key.
    Returns None if dictionary is not a dict or key does not exist.
    """
    if isinstance(dictionary, dict):
        return dictionary.get(key)
    return None


@register.filter
def dict_get(dictionary, key, default=""):
    """
    Safely get a value from a dictionary by key.
    Returns the specified default if dictionary is not a dict or key is missing.
    """
    if isinstance(dictionary, dict):
        return dictionary.get(key, default)
    return default
