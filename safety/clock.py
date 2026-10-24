"""
safety/clock.py
───────────────
Staff clocking in from their own phone — allowed only when the phone
reports a position on school grounds.

The phone's position comes from the browser and can be faked by a
determined person; this stops casual clocking in from home, not fraud.
A reading less accurate than the allowed radius is refused rather than
trusted, so a vague position can't clock anyone in.
"""
import math
from decimal import Decimal, InvalidOperation

from django.utils import timezone

from idcards.models import StaffClockLog
from idcards.services import _next_direction
from safety.services import settings_for

MAX_ACCURACY_M = 100


class ClockError(ValueError):
    pass


def distance_m(lat1, lon1, lat2, lon2):
    """Great-circle distance in metres."""
    r = 6371000
    p1, p2 = math.radians(float(lat1)), math.radians(float(lat2))
    dp, dl = p2 - p1, math.radians(float(lon2) - float(lon1))
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return int(round(2 * r * math.asin(math.sqrt(a))))


def _coord(raw, lo, hi):
    try:
        v = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        raise ClockError('Your phone did not send a position.')
    if not (lo <= v <= hi):
        raise ClockError('Your phone sent an impossible position.')
    return v.quantize(Decimal('0.000001'))


def clock_by_phone(staff, lat, lon, accuracy):
    cfg = settings_for(staff.tenant)
    if not cfg.phone_clock_in:
        raise ClockError('Phone clock-in is not switched on for this school.')
    if cfg.school_latitude is None or cfg.school_longitude is None:
        raise ClockError("The school's location has not been set yet.")
    lat, lon = _coord(lat, -90, 90), _coord(lon, -180, 180)
    try:
        accuracy = float(accuracy)
    except (TypeError, ValueError):
        accuracy = 9999
    if accuracy > max(MAX_ACCURACY_M, cfg.clock_radius_m):
        raise ClockError(f'Your position is only accurate to about {int(accuracy)} m. Step outside or '
                         f'turn on GPS and try again.')
    dist = distance_m(lat, lon, cfg.school_latitude, cfg.school_longitude)
    if dist > cfg.clock_radius_m:
        raise ClockError(f'You are about {dist} m from school — you need to be within '
                         f'{cfg.clock_radius_m} m to clock in.')
    last = (StaffClockLog.objects.filter(tenant_id=staff.tenant_id, staff=staff,
                                         scanned_at__date=timezone.localdate()).order_by('scanned_at').last())
    from safety.staff_attendance import ClockClosed, check_clock_in
    try:
        check_clock_in(staff, _next_direction(last))
    except ClockClosed as closed:
        raise ClockError(str(closed))
    return StaffClockLog.objects.create(tenant_id=staff.tenant_id, staff=staff, direction=_next_direction(last),
                                        method='PHONE', latitude=lat, longitude=lon, distance_m=dist)


def today_logs(staff):
    return list(StaffClockLog.objects.filter(tenant_id=staff.tenant_id, staff=staff,
                                             scanned_at__date=timezone.localdate()).order_by('scanned_at'))
