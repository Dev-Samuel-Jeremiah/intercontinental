"""
transport/views.py
──────────────────
Transport office pages, the crew's trip page (scans, delays, position),
manifests, and the parents' bus page.
"""
import os
from datetime import datetime
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from users.models import Staff, Student

from . import services as svc
from .models import (Driver, FieldTrip, FuelLog, Incident, MaintenanceLog, PickupChange, Rider, Route, Stop,
                     TransportBill, Trip, Vehicle, VehicleDocument)


def _deny(request, text='Only the transport office can do that.'):
    messages.error(request, text)
    return redirect('dashboard:router')


def _manager_only(view):
    def wrap(request, *a, **kw):
        if not svc.is_manager(request.user, request.tenant):
            return _deny(request)
        return view(request, *a, **kw)
    wrap.__name__ = view.__name__
    return login_required(wrap)


def _date(raw):
    try:
        return datetime.strptime(raw or '', '%Y-%m-%d').date()
    except ValueError:
        return None


def _time(raw):
    try:
        return datetime.strptime(raw or '', '%H:%M').time()
    except ValueError:
        return None


def _money(raw):
    try:
        v = Decimal(str(raw or '0').replace(',', ''))
    except InvalidOperation:
        raise svc.TransportError('Amounts must be numbers.')
    if v < 0:
        raise svc.TransportError('Amounts cannot be negative.')
    return v


def _int(raw):
    raw = str(raw or '').replace(',', '')
    return int(raw) if raw.isdigit() else None


def _coord(raw):
    try:
        return Decimal(str(raw)).quantize(Decimal('0.000001')) if str(raw or '').strip() else None
    except InvalidOperation:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Office home: routes (F341) and alerts
# ─────────────────────────────────────────────────────────────────────────────

@_manager_only
def home(request):
    if request.method == 'POST':
        try:
            if not (request.POST.get('name') or '').strip():
                raise svc.TransportError('Name the route.')
            Route.objects.create(
                tenant=request.tenant, name=request.POST['name'].strip()[:80],
                vehicle=Vehicle.objects.filter(tenant=request.tenant, pk=request.POST.get('vehicle') or 0).first(),
                driver=Driver.objects.filter(tenant=request.tenant, pk=request.POST.get('driver') or 0).first(),
                attendant=Driver.objects.filter(tenant=request.tenant, pk=request.POST.get('attendant') or 0).first(),
                morning_departs=_time(request.POST.get('morning_departs')),
                afternoon_departs=_time(request.POST.get('afternoon_departs')),
                fee_term=_money(request.POST.get('fee_term')), fee_month=_money(request.POST.get('fee_month')))
            messages.success(request, 'Route added. Now add its stops.')
        except svc.TransportError as problem:
            messages.error(request, str(problem))
        return redirect('transport:home')
    counts = svc.student_route_counts(request.tenant)
    routes = list(Route.objects.filter(tenant=request.tenant).select_related('vehicle', 'driver', 'attendant'))
    for r in routes:
        r.n = counts.get(r.pk, 0)
    docs, licences = svc.expiring(request.tenant)
    return render(request, 'transport/home.html', {
        'routes': routes, 'docs': docs, 'licences': licences,
        'vehicles': Vehicle.objects.filter(tenant=request.tenant, active=True),
        'drivers': Driver.objects.filter(tenant=request.tenant, active=True, role='DRIVER'),
        'attendants': Driver.objects.filter(tenant=request.tenant, active=True, role='ATTENDANT'),
        'due': [(v, svc.service_due(v)) for v in Vehicle.objects.filter(tenant=request.tenant, active=True)
                if svc.service_due(v)],
        'changes': PickupChange.objects.filter(tenant=request.tenant, status='PENDING').count(),
        'trips': Trip.objects.filter(tenant=request.tenant, date=timezone.localdate()).select_related('route'),
        'day': timezone.localdate()})


@_manager_only
def route_detail(request, pk):
    route = get_object_or_404(Route.objects.select_related('vehicle', 'driver', 'attendant'), pk=pk,
                              tenant=request.tenant)
    if request.method == 'POST':
        a = request.POST.get('action')
        try:
            if a == 'stop':
                if not (request.POST.get('name') or '').strip():
                    raise svc.TransportError('Name the stop.')
                last = route.stops.order_by('-order').first()
                Stop.objects.create(tenant=request.tenant, route=route, name=request.POST['name'].strip()[:120],
                                    landmark=(request.POST.get('landmark') or '')[:160],
                                    order=(last.order + 1) if last else 1,
                                    morning_time=_time(request.POST.get('morning_time')),
                                    afternoon_time=_time(request.POST.get('afternoon_time')),
                                    lat=_coord(request.POST.get('lat')), lng=_coord(request.POST.get('lng')))
            elif a == 'stop_remove':
                route.stops.filter(pk=request.POST.get('id')).delete()
            elif a == 'rider':
                st = get_object_or_404(Student, pk=request.POST.get('student'), tenant=request.tenant)
                stop = route.stops.filter(pk=request.POST.get('stop') or 0).first()
                svc.enrol(st, route, stop, request.POST.get('direction') or 'BOTH', request.POST.get('plan') or 'TERM',
                          request.POST.get('notes'))
                messages.success(request, f'{st.full_name} added to {route.name}.')
            elif a == 'rider_remove':
                Rider.objects.filter(route=route, pk=request.POST.get('id')).update(active=False)
            elif a == 'optimise':
                res = svc.suggest_order(route)
                svc.apply_order(route, res['order'])
                messages.success(request, f'Stops re-ordered: about {res["before_km"]} km → {res["after_km"]} km.')
            elif a == 'edit':
                route.vehicle = Vehicle.objects.filter(tenant=request.tenant, pk=request.POST.get('vehicle') or 0).first()
                route.driver = Driver.objects.filter(tenant=request.tenant, pk=request.POST.get('driver') or 0).first()
                route.attendant = Driver.objects.filter(tenant=request.tenant,
                                                        pk=request.POST.get('attendant') or 0).first()
                route.morning_departs = _time(request.POST.get('morning_departs'))
                route.afternoon_departs = _time(request.POST.get('afternoon_departs'))
                route.fee_term, route.fee_month = _money(request.POST.get('fee_term')), _money(request.POST.get('fee_month'))
                route.active = bool(request.POST.get('active'))
                route.save()
                messages.success(request, 'Route saved.')
        except svc.TransportError as problem:
            messages.error(request, str(problem))
        return redirect('transport:route', pk=route.pk)
    from users.models import ENROLLED_STATUSES
    taken = set(Rider.objects.filter(tenant=request.tenant, active=True).values_list('student_id', flat=True))
    suggestion = None
    try:
        suggestion = svc.suggest_order(route)
    except svc.TransportError:
        pass
    return render(request, 'transport/route.html', {
        'route': route, 'stops': route.stops.all(),
        'riders': route.riders.filter(active=True).select_related('student__class_assigned', 'stop'),
        'left': svc.seats_left(route), 'suggestion': suggestion,
        'students': Student.objects.filter(tenant=request.tenant, status__in=ENROLLED_STATUSES)
        .exclude(pk__in=taken).order_by('full_name').only('id', 'full_name', 'admission_number'),
        'vehicles': Vehicle.objects.filter(tenant=request.tenant, active=True),
        'drivers': Driver.objects.filter(tenant=request.tenant, active=True),
        'directions': Rider.DIRECTIONS, 'plans': Rider.PLANS})


@login_required
def manifest(request, pk):
    route = get_object_or_404(Route.objects.select_related('vehicle', 'driver', 'attendant'), pk=pk,
                              tenant=request.tenant)
    if not (svc.is_manager(request.user, request.tenant) or route in svc.crew_routes(request.user, request.tenant)):
        return _deny(request)
    leg = 'PM' if request.GET.get('leg') == 'PM' else 'AM'
    return render(request, 'transport/manifest.html', {'route': route, 'leg': leg, 'rows': svc.manifest(route, leg),
                                                       'day': timezone.localdate()})


# ─────────────────────────────────────────────────────────────────────────────
# Fleet: vehicles, documents, maintenance, fuel (F346, F347, F351)
# ─────────────────────────────────────────────────────────────────────────────

@_manager_only
def fleet(request):
    if request.method == 'POST':
        a = request.POST.get('action')
        try:
            if a == 'vehicle':
                if not ((request.POST.get('name') or '').strip() and (request.POST.get('plate') or '').strip()):
                    raise svc.TransportError('Give the vehicle a name and plate number.')
                Vehicle.objects.create(tenant=request.tenant, name=request.POST['name'].strip()[:60],
                                       plate=request.POST['plate'].strip().upper()[:20],
                                       make_model=(request.POST.get('make_model') or '')[:80],
                                       capacity=_int(request.POST.get('capacity')) or 30)
            else:
                v = get_object_or_404(Vehicle, pk=request.POST.get('vehicle'), tenant=request.tenant)
                if a == 'doc':
                    if request.POST.get('kind') not in dict(VehicleDocument.KINDS):
                        raise svc.TransportError('Choose the document.')
                    d = VehicleDocument(tenant=request.tenant, vehicle=v, kind=request.POST['kind'],
                                        number=(request.POST.get('number') or '')[:60],
                                        expires_on=_date(request.POST.get('expires_on')))
                    if request.FILES.get('file'):
                        svc.check_file(request.FILES['file'])
                        d.file = request.FILES['file']
                    d.save()
                elif a == 'service':
                    if not (request.POST.get('description') or '').strip():
                        raise svc.TransportError('Describe the work done.')
                    MaintenanceLog.objects.create(
                        tenant=request.tenant, vehicle=v, date=_date(request.POST.get('date')) or timezone.localdate(),
                        kind=request.POST.get('kind') if request.POST.get('kind') in dict(MaintenanceLog.KINDS)
                        else 'SERVICE', description=request.POST['description'].strip()[:255],
                        cost=_money(request.POST.get('cost')), odometer=_int(request.POST.get('odometer')),
                        garage=(request.POST.get('garage') or '')[:120],
                        next_due_date=_date(request.POST.get('next_due_date')),
                        next_due_km=_int(request.POST.get('next_due_km')), recorded_by=request.user)
                elif a == 'fuel':
                    litres = _money(request.POST.get('litres'))
                    if litres <= 0:
                        raise svc.TransportError('Give the litres bought.')
                    FuelLog.objects.create(tenant=request.tenant, vehicle=v,
                                           date=_date(request.POST.get('date')) or timezone.localdate(),
                                           litres=litres, cost=_money(request.POST.get('cost')),
                                           odometer=_int(request.POST.get('odometer')),
                                           station=(request.POST.get('station') or '')[:120],
                                           recorded_by=request.user)
            messages.success(request, 'Saved.')
        except svc.TransportError as problem:
            messages.error(request, str(problem))
        return redirect('transport:fleet')
    return render(request, 'transport/fleet.html', {
        'rows': svc.vehicle_report(request.tenant), 'doc_kinds': VehicleDocument.KINDS,
        'maint_kinds': MaintenanceLog.KINDS, 'today': timezone.localdate(),
        'docs': VehicleDocument.objects.filter(tenant=request.tenant).select_related('vehicle'),
        'services': MaintenanceLog.objects.filter(tenant=request.tenant).select_related('vehicle')[:15],
        'fuels': FuelLog.objects.filter(tenant=request.tenant).select_related('vehicle')[:15]})


@_manager_only
def document_file(request, pk):
    d = get_object_or_404(VehicleDocument, pk=pk, tenant=request.tenant)
    if not d.file:
        raise Http404
    from users.records import CONTENT_TYPES
    ext = os.path.splitext(d.file.name)[1].lower()
    return FileResponse(d.file.open('rb'), content_type=CONTENT_TYPES.get(ext, 'application/octet-stream'),
                        filename=f'{d.get_kind_display()}{ext}')


# ─────────────────────────────────────────────────────────────────────────────
# F345 — drivers and attendants
# ─────────────────────────────────────────────────────────────────────────────

@_manager_only
def crew(request):
    if request.method == 'POST':
        if request.POST.get('action') == 'toggle':
            d = get_object_or_404(Driver, pk=request.POST.get('id'), tenant=request.tenant)
            d.active = not d.active
            d.save(update_fields=['active'])
        elif (request.POST.get('name') or '').strip() and (request.POST.get('phone') or '').strip():
            Driver.objects.create(
                tenant=request.tenant, name=request.POST['name'].strip()[:120],
                role='ATTENDANT' if request.POST.get('role') == 'ATTENDANT' else 'DRIVER',
                phone=request.POST['phone'].strip()[:20],
                staff=Staff.objects.filter(tenant=request.tenant, pk=request.POST.get('staff') or 0).first(),
                licence_number=(request.POST.get('licence_number') or '')[:40],
                licence_expires=_date(request.POST.get('licence_expires')),
                address=(request.POST.get('address') or '')[:255],
                next_of_kin=(request.POST.get('next_of_kin') or '')[:160])
            messages.success(request, 'Added.')
        else:
            messages.error(request, 'Give the name and phone number.')
        return redirect('transport:crew')
    return render(request, 'transport/crew.html', {
        'people': Driver.objects.filter(tenant=request.tenant).select_related('staff'),
        'staff': Staff.objects.filter(tenant=request.tenant, user__is_active=True).order_by('full_name'),
        'today': timezone.localdate()})


# ─────────────────────────────────────────────────────────────────────────────
# Crew trip page: F344 scans, F349 delays, F343 position, F356 attendance
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def my_trips(request):
    manager = svc.is_manager(request.user, request.tenant)
    routes = Route.objects.filter(tenant=request.tenant, active=True) if manager else \
        svc.crew_routes(request.user, request.tenant)
    if not routes and not manager:
        return _deny(request, 'You are not on a bus crew.')
    today = timezone.localdate()
    field = FieldTrip.objects.filter(tenant=request.tenant, date=today) if manager else FieldTrip.objects.filter(
        tenant=request.tenant, date=today, driver__staff__user=request.user)
    return render(request, 'transport/my_trips.html', {'routes': routes, 'field': field, 'today': today})


def _trip_allowed(request, trip):
    if svc.is_manager(request.user, request.tenant):
        return True
    if trip.route_id:
        return trip.route in svc.crew_routes(request.user, request.tenant)
    ft = trip.field_trip
    return bool(ft and ft.driver and ft.driver.staff and ft.driver.staff.user_id == request.user.pk)


@login_required
def run_trip(request, route_id=None, field_id=None):
    leg = request.GET.get('leg') or request.POST.get('leg') or ('PM' if timezone.localtime().hour >= 12 else 'AM')
    if route_id:
        route = get_object_or_404(Route, pk=route_id, tenant=request.tenant)
        trip = svc.trip_for(route, 'PM' if leg == 'PM' else 'AM', user=request.user)
    else:
        ft = get_object_or_404(FieldTrip, pk=field_id, tenant=request.tenant)
        trip, _ = Trip.objects.get_or_create(field_trip=ft, leg='FIELD', date=ft.date,
                                             defaults={'tenant': request.tenant, 'run_by': request.user})
    if not _trip_allowed(request, trip):
        return _deny(request, 'You are not on this bus crew.')
    if request.method == 'POST':
        a = request.POST.get('action')
        try:
            if a in ('ON', 'OFF'):
                st = svc.resolve_code(request.tenant, request.POST.get('code')) if request.POST.get('code') else \
                    Student.objects.filter(tenant=request.tenant, pk=request.POST.get('student') or 0).first()
                if not st:
                    raise svc.TransportError('Card or admission number not recognised.')
                svc.scan(trip, st, a, request.user)
                messages.success(request, f'{st.full_name} — {"on" if a == "ON" else "off"}.')
            elif a == 'delay':
                svc.set_delay(trip, request.POST.get('minutes'), request.POST.get('note'))
                messages.success(request, 'Delay saved' + (' — families have been told.'
                                                            if trip.delay_minutes >= 10 else '.'))
            elif a == 'end':
                att = svc.end_trip(trip, request.user)
                if att['still_on']:
                    messages.error(request, 'Still on the bus: ' + ', '.join(s.full_name for s in att['still_on']) +
                                   '. The transport office has been alerted.')
                else:
                    messages.success(request, 'Trip ended — everyone is accounted for.')
        except svc.TransportError as problem:
            messages.error(request, str(problem))
        return redirect(f'{request.path}?leg={leg}')
    att = svc.attendance(trip)
    on = {s.pk for s in att['on']}
    still = {s.pk for s in att['still_on']}
    return render(request, 'transport/run_trip.html', {
        'trip': trip, 'leg': leg, 'att': att,
        'rows': [(s, s.pk in on, s.pk in on and s.pk not in still) for s in att['expected']]})


@login_required
@require_POST
def position(request, trip_id):
    trip = get_object_or_404(Trip, pk=trip_id, tenant=request.tenant)
    if not _trip_allowed(request, trip):
        raise Http404
    try:
        svc.record_position(trip, request.POST.get('lat'), request.POST.get('lng'))
    except svc.TransportError as problem:
        return JsonResponse({'ok': False, 'error': str(problem)}, status=400)
    return JsonResponse({'ok': True})


# ─────────────────────────────────────────────────────────────────────────────
# F342 / F358 — fees
# ─────────────────────────────────────────────────────────────────────────────

@_manager_only
def fees(request):
    if request.method == 'POST':
        a = request.POST.get('action')
        try:
            if a == 'generate':
                n = svc.generate_bills(request.tenant, request.POST.get('plan'), request.POST.get('period'))
                messages.success(request, f'{n} new bill{"s" if n != 1 else ""} created.')
            else:
                bill = get_object_or_404(TransportBill, pk=request.POST.get('id'), tenant=request.tenant)
                if a == 'wallet':
                    svc.pay_from_wallet(bill, request.user)
                else:
                    svc.mark_bill(bill, a, request.POST.get('note'))
                messages.success(request, 'Saved.')
        except svc.TransportError as problem:
            messages.error(request, str(problem))
        return redirect(request.get_full_path())
    period = request.GET.get('period') or ''
    bills = TransportBill.objects.filter(tenant=request.tenant).select_related('student', 'route')
    if period:
        bills = bills.filter(period=period)
    periods = list(TransportBill.objects.filter(tenant=request.tenant).values_list('period', flat=True).distinct())
    return render(request, 'transport/fees.html', {'bills': bills[:300], 'periods': periods, 'period': period})


# ─────────────────────────────────────────────────────────────────────────────
# F350 field trips, F354 pickup changes, F355 incidents, F359 reports
# ─────────────────────────────────────────────────────────────────────────────

@_manager_only
def field_trips(request):
    if request.method == 'POST':
        try:
            ft = svc.book_field_trip(request.tenant, request.user, request.POST)
            messages.success(request, f'{ft.vehicle} booked for {ft.title}.')
        except svc.TransportError as problem:
            messages.error(request, str(problem))
        return redirect('transport:field_trips')
    from communications.models import ConsentForm
    return render(request, 'transport/field_trips.html', {
        'items': FieldTrip.objects.filter(tenant=request.tenant, date__gte=timezone.localdate())
        .select_related('vehicle', 'driver', 'consent_form'),
        'vehicles': Vehicle.objects.filter(tenant=request.tenant, active=True),
        'drivers': Driver.objects.filter(tenant=request.tenant, active=True, role='DRIVER'),
        'forms': ConsentForm.objects.filter(tenant=request.tenant, deadline__gte=timezone.localdate())})


@_manager_only
def changes(request):
    if request.method == 'POST':
        pc = get_object_or_404(PickupChange, pk=request.POST.get('id'), tenant=request.tenant)
        try:
            svc.decide_change(pc, request.user, request.POST.get('action') == 'approve',
                              Stop.objects.filter(tenant=request.tenant, pk=request.POST.get('stop') or 0).first(),
                              request.POST.get('note'))
            messages.success(request, 'Done.')
        except svc.TransportError as problem:
            messages.error(request, str(problem))
        return redirect('transport:changes')
    pending = list(PickupChange.objects.filter(tenant=request.tenant, status='PENDING')
                   .select_related('student', 'new_stop', 'requested_by'))
    for pc in pending:
        rider = Rider.objects.filter(student=pc.student, active=True).select_related('route', 'stop').first()
        pc.rider = rider
        pc.options = rider.route.stops.all() if rider else []
    return render(request, 'transport/changes.html', {
        'pending': pending, 'done': PickupChange.objects.filter(tenant=request.tenant).exclude(status='PENDING')
        .select_related('student')[:20]})


@login_required
def incidents(request):
    manager = svc.is_manager(request.user, request.tenant)
    crew = svc.crew_routes(request.user, request.tenant)
    if not (manager or crew.exists()):
        return _deny(request)
    if request.method == 'POST':
        if request.POST.get('op') == 'close' and manager:
            Incident.objects.filter(tenant=request.tenant, pk=request.POST.get('id')).update(closed=True)
        else:
            students = list(Student.objects.filter(tenant=request.tenant, pk__in=request.POST.getlist('students')))
            try:
                svc.report_incident(request.tenant, request.user, request.POST, students,
                                    bool(request.POST.get('tell_parents')))
                messages.success(request, 'Reported to the transport office.')
            except svc.TransportError as problem:
                messages.error(request, str(problem))
        return redirect('transport:incidents')
    routes = Route.objects.filter(tenant=request.tenant) if manager else crew
    riders = Rider.objects.filter(route__in=routes, active=True).select_related('student').order_by('student__full_name')
    return render(request, 'transport/incidents.html', {
        'items': Incident.objects.filter(tenant=request.tenant).select_related('route', 'vehicle', 'reported_by')
        .prefetch_related('students')[:40] if manager else
        Incident.objects.filter(tenant=request.tenant, reported_by=request.user)[:20],
        'routes': routes, 'vehicles': Vehicle.objects.filter(tenant=request.tenant, active=True), 'riders': riders,
        'kinds': Incident.KINDS, 'manager': manager, 'today': timezone.localdate()})


@_manager_only
def reports(request):
    if request.method == 'POST':
        cfg = svc.settings_for(request.tenant)
        cfg.notify_scans = bool(request.POST.get('notify_scans'))
        cfg.scan_channel = request.POST.get('scan_channel') if request.POST.get('scan_channel') in ('SMS', 'EMAIL',
                                                                                                   'BOTH') else 'SMS'
        cfg.reminder_days = _int(request.POST.get('reminder_days')) or 30
        cfg.school_lat, cfg.school_lng = _coord(request.POST.get('school_lat')), _coord(request.POST.get('school_lng'))
        cfg.save()
        cfg.managers.set(Staff.objects.filter(tenant=request.tenant, pk__in=request.POST.getlist('managers')))
        messages.success(request, 'Settings saved.')
        return redirect('transport:reports')
    cfg = svc.settings_for(request.tenant)
    return render(request, 'transport/reports.html', {
        'routes': svc.route_report(request.tenant), 'vehicles': svc.vehicle_report(request.tenant), 'cfg': cfg,
        'staff': Staff.objects.filter(tenant=request.tenant, user__is_active=True).order_by('full_name'),
        'mgrs': set(cfg.managers.values_list('pk', flat=True)),
        'channels': [('SMS', 'SMS'), ('EMAIL', 'Email'), ('BOTH', 'SMS and email')]})


# ─────────────────────────────────────────────────────────────────────────────
# Parents
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def family(request):
    from users.views_achievements import _parent_children
    kids = list(_parent_children(request))
    cards = []
    for k in kids:
        rider, trips = svc.today_trip(k)
        if rider:
            cards.append({'kid': k, 'rider': rider, 'trips': trips,
                          'scans': list(k.bus_scans.filter(trip__date=timezone.localdate()).select_related('trip')),
                          'bills': TransportBill.objects.filter(student=k).exclude(status='WAIVED')[:6],
                          'stops': rider.route.stops.all(),
                          'changes': PickupChange.objects.filter(student=k)[:3]})
    if not cards:
        return _deny(request, 'None of your children use the school bus.')
    if request.method == 'POST':
        a = request.POST.get('action')
        card = next((c for c in cards if str(c['kid'].pk) == request.POST.get('student')), None)
        try:
            if a == 'pay' and card:
                bill = get_object_or_404(TransportBill, pk=request.POST.get('id'), student=card['kid'])
                svc.pay_from_wallet(bill, request.user)
                messages.success(request, 'Paid from your family wallet.')
            elif a == 'change' and card:
                stop = Stop.objects.filter(tenant=request.tenant, pk=request.POST.get('stop') or 0).first()
                svc.request_change(card['kid'], request.user, stop, request.POST.get('new_address'),
                                   _date(request.POST.get('from_date')), request.POST.get('reason'))
                messages.success(request, 'Sent to the transport office.')
        except svc.TransportError as problem:
            messages.error(request, str(problem))
        return redirect('transport:family')
    return render(request, 'transport/family.html', {'page_title': 'School bus', 'cards': cards,
                                                     'today': timezone.localdate()})


@login_required
def family_position(request, trip_id):
    """The bus's last known position, for a parent whose child is on the route."""
    from users.views_achievements import _parent_children
    trip = get_object_or_404(Trip, pk=trip_id, tenant=request.tenant)
    kids = list(_parent_children(request))
    if not trip.route_id or not Rider.objects.filter(route=trip.route, student__in=kids, active=True).exists():
        raise Http404
    return JsonResponse({'lat': float(trip.last_lat) if trip.last_lat is not None else None,
                         'lng': float(trip.last_lng) if trip.last_lng is not None else None,
                         'seen': timezone.localtime(trip.last_seen).strftime('%H:%M') if trip.last_seen else None,
                         'ended': bool(trip.ended_at), 'delay': trip.delay_minutes})

