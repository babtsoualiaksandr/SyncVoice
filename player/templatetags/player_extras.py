from django import template

register = template.Library()


@register.filter
def hms(seconds) -> str:
    """Seconds as 5:12 or 1:02:03; empty for nothing."""
    if seconds in (None, ''):
        return ''
    total = int(round(float(seconds)))
    h, rest = divmod(total, 3600)
    m, s = divmod(rest, 60)
    return f'{h}:{m:02}:{s:02}' if h else f'{m}:{s:02}'
