from django import template

register = template.Library()

@register.filter
def get_item(dictionary, key):
    """Allow dict access by variable key in templates: {{ mydict|get_item:key }}"""
    if isinstance(dictionary, dict):
        return dictionary.get(key)
    return None


@register.filter
def time12(value):
    """
    Convert a time object or string to 12-hour format with AM/PM.
    Usage: {{ slot.start_time|time12 }}
    """
    if value is None:
        return ''
    try:
        # Works with datetime.time objects
        hour = value.hour
        minute = value.minute
    except AttributeError:
        return str(value)
    period = 'AM' if hour < 12 else 'PM'
    hour12 = hour % 12 or 12
    return f"{hour12}:{minute:02d} {period}"
