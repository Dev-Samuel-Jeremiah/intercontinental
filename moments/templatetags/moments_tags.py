from django import template
from django.utils import timezone

register = template.Library()


def _parts(when):
    secs = max(0, int((timezone.now() - when).total_seconds()))
    for size, short, long in ((604800, 'w', 'week'), (86400, 'd', 'day'), (3600, 'h', 'hour'), (60, 'm', 'minute')):
        if secs >= size:
            return secs // size, short, long
    return 0, '', ''


@register.filter
def ago(when):
    """"3 hours ago", "Just now", or the date for anything older than four weeks."""
    if not when:
        return ''
    n, _, unit = _parts(when)
    if not n:
        return 'Just now'
    if unit == 'week' and n > 4:
        return timezone.localtime(when).strftime('%-d %B %Y' if when.year != timezone.now().year else '%-d %B')
    return f'{n} {unit}{"s" if n != 1 else ""} ago'


@register.filter
def short_ago(when):
    """"3h", "2d", "now"."""
    if not when:
        return ''
    n, short, _ = _parts(when)
    return f'{n}{short}' if n else 'now'
