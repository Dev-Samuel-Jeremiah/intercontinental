"""
transport/services.py
─────────────────────
    F341 routes, stops, riders          F350 field trip bookings
    F342/F358 fees per term or month    F351 vehicle documents, licence expiry
    F343 live position from the crew's  F352 seat capacity checks
         phone while a trip runs        F354 pickup point changes
    F344/F356 on/off scans, trip        F355 incident reports
         attendance, left-on-bus check  F359 transport reports
    F345 drivers and attendants         F360 manifests
    F346/F347 maintenance and fuel
    F348 suggested stop order
    F349 delay alerts
"""
import math
import re
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import Count, Sum
from django.utils import timezone
from django.utils.html import escape

from users.models import Staff, Student

from .models import (Driver, FieldTrip, Incident, PickupChange, Rider, Route, Stop,
                     TransportBill, TransportSettings, Trip, TripScan, Vehicle, VehicleDocument)

ZERO = Decimal('0')


class TransportError(ValueError):
    pass


def settings_for(tenant):
    return TransportSettings.objects.filter(tenant=tenant).first() or TransportSettings.objects.create(tenant=tenant)


def is_manager(user, tenant):
    from users.views_profile import can_manage
    if can_manage(user):
        return True
    staff = Staff.objects.filter(user=user, tenant=tenant).first()
    return bool(staff and settings_for(tenant).managers.filter(pk=staff.pk).exists())


def crew_routes(user, tenant):
    from django.db.models import Q
    return Route.objects.filter(tenant=tenant, active=True).filter(
        Q(driver__staff__user=user) | Q(attendant__staff__user=user))


def manager_emails(tenant):
    from hr.services import admins_emails
    out = set(admins_emails(tenant))
    for s in settings_for(tenant).managers.select_related('user'):
        if s.user.email:
            out.add(s.user.email)
    return sorted(out)


def _email(to, subject, html):
    from communications.services import send_email
    return send_email(to, subject, html)


def _tell_family(student, subject, text, channel='BOTH'):
    from communications.services import send_sms
    from users.records import contacts
    if channel in ('EMAIL', 'BOTH'):
        for _n, e in contacts(student, 'EMAIL'):
            _email(e, subject, f'<p>{escape(text)}</p>')
    if channel in ('SMS', 'BOTH'):
        for _n, p in contacts(student, 'SMS'):
            send_sms(p, text)


def check_file(upload):
    from users import records
    try:
        return records.check_upload(upload)
    except records.RecordError as problem:
        raise TransportError(str(problem))


# ─────────────────────────────────────────────────────────────────────────────
# F341 / F352 — riders and seats
# ─────────────────────────────────────────────────────────────────────────────

def seats_left(route, exclude=None):
    if not route.vehicle_id:
        return None
    riders = route.riders.filter(active=True)
    if exclude:
        riders = riders.exclude(pk=exclude.pk)
    return route.vehicle.capacity - riders.count()


def enrol(student, route, stop=None, direction='BOTH', plan='TERM', notes=''):
    if stop and stop.route_id != route.pk:
        raise TransportError('That stop is not on this route.')
    if direction not in dict(Rider.DIRECTIONS) or plan not in dict(Rider.PLANS):
        raise TransportError('Choose the direction and how they pay.')
    current = Rider.objects.filter(student=student).first()
    left = seats_left(route, exclude=current if current and current.route_id == route.pk else None)
    if left is not None and left <= 0:
        raise TransportError(f'{route.name} is full ({route.vehicle.capacity} seats on {route.vehicle}).')
    rider, _ = Rider.objects.update_or_create(
        student=student, defaults={'tenant_id': student.tenant_id, 'route': route, 'stop': stop,
                                   'direction': direction, 'plan': plan, 'notes': (notes or '')[:255],
                                   'active': True})
    return rider


def leg_riders(route, leg):
    want = ('BOTH', 'AM') if leg == 'AM' else ('BOTH', 'PM')
    return Rider.objects.filter(route=route, active=True, direction__in=want) \
        .select_related('student__class_assigned', 'stop')


def manifest(route, leg):
    """[(stop or None, time, [riders])] in driving order (afternoon runs backwards)."""
    riders = list(leg_riders(route, leg))
    stops = list(route.stops.all())
    if leg == 'PM':
        stops.reverse()
    rows = []
    for s in stops:
        here = [r for r in riders if r.stop_id == s.pk]
        if here:
            rows.append((s, s.morning_time if leg == 'AM' else s.afternoon_time,
                         sorted(here, key=lambda r: r.student.full_name)))
    loose = [r for r in riders if not r.stop_id]
    if loose:
        rows.append((None, None, loose))
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# Trips: F343 position, F344/F356 scans, F349 delays
# ─────────────────────────────────────────────────────────────────────────────

def trip_for(route, leg, day=None, user=None):
    day = day or timezone.localdate()
    trip, _ = Trip.objects.get_or_create(route=route, leg=leg, date=day,
                                         defaults={'tenant_id': route.tenant_id, 'run_by': user})
    return trip


UUID_RE = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', re.I)


def resolve_code(tenant, code):
    """A student from a scanned ID-card QR (it carries the card's id) or a typed admission number."""
    code = (code or '').strip()
    if not code:
        return None
    m = UUID_RE.search(code)
    if m:
        from idcards.models import IDCard
        card = IDCard.objects.filter(tenant=tenant, card_uid=m.group(0), is_active=True, student__isnull=False) \
            .select_related('student').first()
        return card.student if card else None
    return Student.objects.filter(tenant=tenant, admission_number__iexact=code).first()


def trip_students(trip):
    if trip.route_id:
        return [r.student for r in leg_riders(trip.route, trip.leg)]
    ft = trip.field_trip
    if ft and ft.consent_form_id:
        return [r.student for r in ft.consent_form.responses.filter(answer='YES').select_related('student')]
    return []


def scan(trip, student, kind, user):
    if kind not in ('ON', 'OFF'):
        raise TransportError('Choose on or off.')
    if trip.ended_at:
        raise TransportError('This trip has ended.')
    if student not in trip_students(trip):
        raise TransportError(f'{student.full_name} is not on this bus list.')
    if kind == 'OFF' and not TripScan.objects.filter(trip=trip, student=student, kind='ON').exists():
        raise TransportError(f'{student.full_name} was not scanned on.')
    try:
        with transaction.atomic():
            s = TripScan.objects.create(tenant_id=trip.tenant_id, trip=trip, student=student, kind=kind, by=user)
    except IntegrityError:
        raise TransportError(f'{student.full_name} is already scanned {kind.lower()}.')
    if not trip.started_at:
        trip.started_at = timezone.now()
        trip.save(update_fields=['started_at'])
    cfg = settings_for(trip.tenant)
    if cfg.notify_scans and trip.route_id:
        rider = getattr(student, 'bus', None)
        where = f' at {rider.stop.name}' if rider and rider.stop_id and (
            (kind == 'ON' and trip.leg == 'AM') or (kind == 'OFF' and trip.leg == 'PM')) else ''
        verb = 'got on the school bus' if kind == 'ON' else 'got off the school bus'
        _tell_family(student, f'{student.full_name} {verb}',
                     f'{trip.tenant.name}: {student.full_name.split()[0]} {verb}{where} at '
                     f'{timezone.localtime(s.at):%-I:%M %p}.', cfg.scan_channel)
    return s


def attendance(trip):
    expected = trip_students(trip)
    on = set(trip.scans.filter(kind='ON').values_list('student_id', flat=True))
    off = set(trip.scans.filter(kind='OFF').values_list('student_id', flat=True))
    return {'expected': expected, 'on': [s for s in expected if s.pk in on],
            'missing': [s for s in expected if s.pk not in on],
            'still_on': [s for s in expected if s.pk in on and s.pk not in off]}


def end_trip(trip, user):
    """Close the trip. Anyone scanned on but never off raises an urgent alert — check the bus."""
    if trip.ended_at:
        raise TransportError('Already ended.')
    att = attendance(trip)
    trip.ended_at = timezone.now()
    trip.save(update_fields=['ended_at'])
    if att['still_on']:
        names = ', '.join(escape(s.full_name) for s in att['still_on'])
        label = trip.route.name if trip.route_id else trip.field_trip.title
        for e in manager_emails(trip.tenant):
            _email(e, f'URGENT: check the bus — {label}',
                   f'<p>The {trip.get_leg_display().lower()} trip on {label} ended with students scanned on but '
                   f'not off: <b>{names}</b>. Check the bus and confirm where each child is.</p>')
    return att


def set_delay(trip, minutes, note=''):
    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        raise TransportError('Give the delay in minutes.')
    if minutes < 0 or minutes > 300:
        raise TransportError('Give the delay in minutes (0 to 300).')
    trip.delay_minutes, trip.delay_note = minutes, (note or '')[:160]
    trip.save(update_fields=['delay_minutes', 'delay_note'])
    if minutes >= 10 and trip.route_id:
        why = f' ({note})' if note else ''
        for s in trip_students(trip):
            _tell_family(s, f'School bus running late — {trip.route.name}',
                         f'{trip.tenant.name}: the {trip.get_leg_display().lower()} bus ({trip.route.name}) is about '
                         f'{minutes} minutes late{why}. Sorry for the wait.', 'SMS')
    return trip


def record_position(trip, lat, lng):
    try:
        lat, lng = Decimal(str(lat)).quantize(Decimal('0.000001')), Decimal(str(lng)).quantize(Decimal('0.000001'))
    except Exception:
        raise TransportError('Bad position.')
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        raise TransportError('Bad position.')
    if trip.ended_at:
        raise TransportError('This trip has ended.')
    trip.last_lat, trip.last_lng, trip.last_seen = lat, lng, timezone.now()
    trip.save(update_fields=['last_lat', 'last_lng', 'last_seen'])


# ─────────────────────────────────────────────────────────────────────────────
# F342 / F358 — fees
# ─────────────────────────────────────────────────────────────────────────────

def generate_bills(tenant, plan, period):
    period = (period or '').strip()
    if plan not in ('TERM', 'MONTH') or not period:
        raise TransportError('Choose termly or monthly riders and name the period, e.g. "1st Term 2026/2027".')
    n = 0
    for r in Rider.objects.filter(tenant=tenant, active=True, plan=plan).select_related('route', 'student'):
        amount = r.route.fee_term if plan == 'TERM' else r.route.fee_month
        if amount <= 0:
            continue
        _bill, created = TransportBill.objects.get_or_create(
            student=r.student, period=period[:40],
            defaults={'tenant': tenant, 'route': r.route, 'amount': amount})
        n += 1 if created else 0
    return n


def pay_from_wallet(bill, user):
    from finance.views import _get_family_wallet_for_student
    if bill.status != 'UNPAID':
        raise TransportError('This bill is not waiting for payment.')
    wallet = _get_family_wallet_for_student(bill.student)
    if not wallet:
        raise TransportError('No family wallet is set up for this child.')
    due = bill.amount - bill.paid
    with transaction.atomic():
        try:
            txn = wallet.debit(due, f'Transport {bill.period}: {bill.student.full_name}', performed_by=user,
                               category='OTHER')
        except ValueError as problem:
            raise TransportError(str(problem))
        bill.paid, bill.status, bill.transaction = bill.amount, 'PAID', txn
        bill.save(update_fields=['paid', 'status', 'transaction'])
    return bill


def mark_bill(bill, status, note=''):
    if status not in ('PAID', 'WAIVED'):
        raise TransportError('Choose paid or waived.')
    bill.status, bill.note = status, (note or '')[:160]
    if status == 'PAID':
        bill.paid = bill.amount
    bill.save(update_fields=['status', 'note', 'paid'])


# ─────────────────────────────────────────────────────────────────────────────
# F346 / F347 — maintenance and fuel
# ─────────────────────────────────────────────────────────────────────────────

def latest_odometer(vehicle):
    vals = [x for x in (vehicle.fuel.exclude(odometer__isnull=True).values_list('odometer', flat=True).first(),
                        vehicle.maintenance.exclude(odometer__isnull=True).values_list('odometer', flat=True).first())
            if x]
    return max(vals) if vals else None


def service_due(vehicle, today=None):
    """A sentence if the next service is due within two weeks or 500 km, else None."""
    today = today or timezone.localdate()
    last = vehicle.maintenance.filter(kind='SERVICE').first() or vehicle.maintenance.first()
    if not last:
        return None
    if last.next_due_date and last.next_due_date <= today + timedelta(days=14):
        return f'Service due {last.next_due_date:%d %b %Y}'
    odo = latest_odometer(vehicle)
    if last.next_due_km and odo and odo >= last.next_due_km - 500:
        return f'Service due at {last.next_due_km:,} km (now {odo:,} km)'
    return None


def fuel_stats(vehicle, days=90):
    since = timezone.localdate() - timedelta(days=days)
    logs = list(vehicle.fuel.filter(date__gte=since).order_by('date', 'id'))
    litres = sum((f.litres for f in logs), ZERO)
    cost = sum((f.cost for f in logs), ZERO)
    odos = [f.odometer for f in logs if f.odometer]
    km = (max(odos) - min(odos)) if len(odos) >= 2 else None
    # The first fill-up's litres were burnt before the first reading, so leave them out of km per litre.
    used = litres - logs[0].litres if logs and len(odos) >= 2 else None
    return {'litres': litres, 'cost': cost, 'km': km,
            'km_per_litre': round(km / float(used), 2) if km and used else None,
            'cost_per_km': round(float(cost) / km, 2) if km else None,
            'maintenance': vehicle.maintenance.filter(date__gte=since).aggregate(s=Sum('cost'))['s'] or ZERO}


# ─────────────────────────────────────────────────────────────────────────────
# F345 / F351 — documents and licences
# ─────────────────────────────────────────────────────────────────────────────

def expiring(tenant, days=None):
    days = settings_for(tenant).reminder_days if days is None else days
    limit = timezone.localdate() + timedelta(days=days)
    docs = list(VehicleDocument.objects.filter(tenant=tenant, expires_on__isnull=False, expires_on__lte=limit,
                                               vehicle__active=True).select_related('vehicle'))
    licences = list(Driver.objects.filter(tenant=tenant, active=True, licence_expires__isnull=False,
                                          licence_expires__lte=limit))
    return docs, licences


def send_reminders(tenant):
    docs, licences = expiring(tenant)
    docs = [d for d in docs if not d.reminded_at]
    licences = [d for d in licences if not d.licence_reminded_at]
    if not (docs or licences):
        return 0
    lines = [f'<li>{escape(str(d.vehicle))}: {escape(d.get_kind_display())} — {d.expires_on:%d %b %Y}</li>'
             for d in docs]
    lines += [f'<li>{escape(d.name)} ({d.get_role_display()}): driving licence — {d.licence_expires:%d %b %Y}</li>'
              for d in licences]
    for e in manager_emails(tenant):
        _email(e, f'{tenant.name}: transport documents expiring',
               f'<p>These expire soon or have expired:</p><ul>{"".join(lines)}</ul>')
    now = timezone.now()
    VehicleDocument.objects.filter(pk__in=[d.pk for d in docs]).update(reminded_at=now)
    Driver.objects.filter(pk__in=[d.pk for d in licences]).update(licence_reminded_at=now)
    return len(docs) + len(licences)


def run_reminders():
    from tenants.middleware import clear_current_tenant, set_current_tenant
    from tenants.models import Tenant
    total = 0
    for t in Tenant.objects.filter(status__in=('ACTIVE', 'TRIAL')):
        set_current_tenant(t)
        try:
            total += send_reminders(t)
        finally:
            clear_current_tenant()
    return total


# ─────────────────────────────────────────────────────────────────────────────
# F348 — suggested stop order
# ─────────────────────────────────────────────────────────────────────────────

def _km(a, b):
    lat1, lng1, lat2, lng2 = map(math.radians, (float(a[0]), float(a[1]), float(b[0]), float(b[1])))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def _length(points):
    return sum(_km(points[i], points[i + 1]) for i in range(len(points) - 1))


def suggest_order(route):
    """Nearest-stop-first order for the morning run, ending at school. Needs map positions for every stop."""
    cfg = settings_for(route.tenant)
    stops = list(route.stops.all())
    if len(stops) < 3 or any(s.lat is None or s.lng is None for s in stops) or cfg.school_lat is None:
        raise TransportError('Give every stop and the school a map position (latitude and longitude) first.')
    school = (cfg.school_lat, cfg.school_lng)
    # Start from the stop farthest from school, then always go to the nearest unvisited stop.
    left = stops[:]
    current = max(left, key=lambda s: _km((s.lat, s.lng), school))
    order = [current]
    left.remove(current)
    while left:
        current = min(left, key=lambda s: _km((current.lat, current.lng), (s.lat, s.lng)))
        order.append(current)
        left.remove(current)
    before = _length([(s.lat, s.lng) for s in stops] + [school])
    after = _length([(s.lat, s.lng) for s in order] + [school])
    return {'order': order, 'before_km': round(before, 1), 'after_km': round(after, 1)}


def apply_order(route, order):
    for i, s in enumerate(order, 1):
        Stop.objects.filter(pk=s.pk).update(order=i)


# ─────────────────────────────────────────────────────────────────────────────
# F350 — field trips
# ─────────────────────────────────────────────────────────────────────────────

ROUTE_RUN = timedelta(minutes=90)


def _overlaps(day, a1, a2, b1, b2):
    return datetime.combine(day, a1) < datetime.combine(day, b2) and datetime.combine(day, b1) < \
        datetime.combine(day, a2)


def vehicle_clashes(vehicle, day, depart, back, exclude=None):
    out = []
    for ft in FieldTrip.objects.filter(vehicle=vehicle, date=day).exclude(pk=getattr(exclude, 'pk', None)):
        if _overlaps(day, depart, back, ft.depart, ft.back):
            out.append(f'{vehicle} is booked for "{ft.title}" {ft.depart:%-I:%M %p}–{ft.back:%-I:%M %p}.')
    if day.weekday() < 5:
        for r in Route.objects.filter(vehicle=vehicle, active=True):
            for t in (r.morning_departs, r.afternoon_departs):
                if t:
                    end = (datetime.combine(day, t) + ROUTE_RUN).time()
                    if _overlaps(day, depart, back, t, end):
                        out.append(f'{vehicle} runs {r.name} at {t:%-I:%M %p}.')
    return out


def book_field_trip(tenant, user, data):
    title, dest = (data.get('title') or '').strip(), (data.get('destination') or '').strip()
    try:
        day = datetime.strptime(data.get('date') or '', '%Y-%m-%d').date()
        depart = datetime.strptime(data.get('depart') or '', '%H:%M').time()
        back = datetime.strptime(data.get('back') or '', '%H:%M').time()
    except ValueError:
        raise TransportError('Give the date, departure and return times.')
    if not (title and dest) or back <= depart:
        raise TransportError('Give a title and destination, and a return time after departure.')
    vehicle = Vehicle.objects.filter(tenant=tenant, pk=data.get('vehicle') or 0, active=True).first()
    if not vehicle:
        raise TransportError('Choose a vehicle.')
    seats = str(data.get('seats_needed') or '0')
    seats = int(seats) if seats.isdigit() else 0
    if seats > vehicle.capacity:
        raise TransportError(f'{vehicle} has {vehicle.capacity} seats; {seats} are needed. Book another vehicle too.')
    problems = vehicle_clashes(vehicle, day, depart, back)
    if problems:
        raise TransportError(' '.join(problems))
    from communications.models import ConsentForm
    return FieldTrip.objects.create(
        tenant=tenant, title=title[:160], destination=dest[:200], date=day, depart=depart, back=back, vehicle=vehicle,
        driver=Driver.objects.filter(tenant=tenant, pk=data.get('driver') or 0).first(), seats_needed=seats,
        consent_form=ConsentForm.objects.filter(tenant=tenant, pk=data.get('consent_form') or 0).first(),
        notes=(data.get('notes') or '').strip(), booked_by=user)


# ─────────────────────────────────────────────────────────────────────────────
# F354 — pickup changes
# ─────────────────────────────────────────────────────────────────────────────

def request_change(student, user, new_stop, new_address, from_date, reason):
    rider = Rider.objects.filter(student=student, active=True).first()
    if not rider:
        raise TransportError(f'{student.full_name} does not use the school bus.')
    if new_stop and new_stop.route_id != rider.route_id:
        raise TransportError('Choose a stop on the same route, or describe the new address.')
    if not (new_stop or (new_address or '').strip()) or not (reason or '').strip():
        raise TransportError('Choose a stop (or give the new address) and say why.')
    if not from_date or from_date < timezone.localdate():
        raise TransportError('Choose the date it starts, from today on.')
    pc = PickupChange.objects.create(tenant_id=student.tenant_id, student=student, requested_by=user,
                                     new_stop=new_stop, new_address=(new_address or '').strip()[:255],
                                     from_date=from_date, reason=reason.strip()[:255])
    for e in manager_emails(student.tenant):
        _email(e, f'Pickup change: {student.full_name}',
               f'<p>{escape(student.full_name)} — from {from_date:%d %b}: '
               f'{escape(new_stop.name if new_stop else new_address)}. {escape(reason)}</p>')
    return pc


def decide_change(pc, user, approve, stop=None, note=''):
    if pc.status != 'PENDING':
        raise TransportError('Already decided.')
    if approve:
        stop = stop or pc.new_stop
        rider = Rider.objects.filter(student=pc.student, active=True).first()
        if not stop or not rider or stop.route_id != rider.route_id:
            raise TransportError('Choose a stop on the child\'s route.')
        rider.stop = stop
        rider.save(update_fields=['stop'])
    pc.status, pc.decided_by, pc.note = ('APPROVED' if approve else 'DECLINED'), user, (note or '')[:255]
    pc.save()
    if pc.requested_by and pc.requested_by.email:
        _email(pc.requested_by.email, f'Pickup change {pc.get_status_display().lower()}',
               f'<p>The pickup change for {escape(pc.student.full_name)} was {pc.get_status_display().lower()}.'
               f'{" New stop: " + escape(stop.name) if approve and stop else ""} {escape(note)}</p>')


# ─────────────────────────────────────────────────────────────────────────────
# F355 — incidents
# ─────────────────────────────────────────────────────────────────────────────

def report_incident(tenant, user, data, students, tell_parents=False):
    if data.get('kind') not in dict(Incident.KINDS) or not (data.get('description') or '').strip():
        raise TransportError('Choose what happened and describe it.')
    try:
        day = datetime.strptime(data.get('date') or '', '%Y-%m-%d').date()
    except ValueError:
        day = timezone.localdate()
    inc = Incident.objects.create(
        tenant=tenant, route=Route.objects.filter(tenant=tenant, pk=data.get('route') or 0).first(),
        vehicle=Vehicle.objects.filter(tenant=tenant, pk=data.get('vehicle') or 0).first(), date=day,
        kind=data['kind'], description=data['description'].strip(), action=(data.get('action') or '').strip(),
        reported_by=user, parents_told=bool(tell_parents and students))
    inc.students.set(students)
    for e in manager_emails(tenant):
        _email(e, f'Transport incident: {inc.get_kind_display()}',
               f'<p>{escape(inc.get_kind_display())} on {day:%d %b}'
               f'{" — " + escape(inc.route.name) if inc.route else ""}.</p><p>{escape(inc.description)}</p>')
    if tell_parents:
        for s in students:
            _tell_family(s, f'School bus: {inc.get_kind_display().lower()}',
                         f'{tenant.name}: there was an incident on the school bus today involving '
                         f'{s.full_name.split()[0]} ({inc.get_kind_display().lower()}). The school will contact '
                         f'you with details.', 'BOTH')
    return inc


# ─────────────────────────────────────────────────────────────────────────────
# F359 — reports
# ─────────────────────────────────────────────────────────────────────────────

def route_report(tenant, days=30):
    since = timezone.localdate() - timedelta(days=days)
    rows = []
    for r in Route.objects.filter(tenant=tenant).select_related('vehicle'):
        riders = r.riders.filter(active=True).count()
        trips = r.trips.filter(date__gte=since)
        n = trips.count()
        on_time = trips.filter(delay_minutes__lte=5).count()
        bills = TransportBill.objects.filter(route=r).aggregate(billed=Sum('amount'), paid=Sum('paid'))
        rows.append({'route': r, 'riders': riders, 'capacity': r.vehicle.capacity if r.vehicle_id else None,
                     'use': round(100 * riders / r.vehicle.capacity) if r.vehicle_id and r.vehicle.capacity else None,
                     'trips': n, 'on_time': round(100 * on_time / n) if n else None,
                     'scans': TripScan.objects.filter(trip__in=trips, kind='ON').count(),
                     'incidents': r.incidents.filter(date__gte=since).count(),
                     'billed': bills['billed'] or ZERO, 'paid': bills['paid'] or ZERO})
    return rows


def vehicle_report(tenant, days=90):
    return [{'vehicle': v, **fuel_stats(v, days), 'due': service_due(v),
             'docs': v.documents.filter(expires_on__lte=timezone.localdate()).count()}
            for v in Vehicle.objects.filter(tenant=tenant, active=True)]


def student_route_counts(tenant):
    return dict(Rider.objects.filter(tenant=tenant, active=True).values_list('route_id').annotate(n=Count('id')))


def today_trip(student, day=None):
    rider = Rider.objects.filter(student=student, active=True).select_related('route', 'stop').first()
    if not rider:
        return None, []
    trips = list(Trip.objects.filter(route=rider.route, date=day or timezone.localdate()))
    return rider, trips


def date_or_none(raw):
    try:
        return date.fromisoformat(raw or '')
    except ValueError:
        return None
