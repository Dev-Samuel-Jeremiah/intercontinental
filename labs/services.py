"""
labs/services.py
────────────────
The rules for labs and practical rooms.

Who may do what: administrators, a lab's head and its technicians manage the
lab (equipment, chemicals, inspections, charges). Any member of staff can
book a lab, plan and run practicals, record breakages and report incidents.
Teachers mark the practicals they run; administrators can mark any.
"""
import re
from collections import defaultdict
from datetime import datetime, time, timedelta
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Count, Q, Sum
from django.utils import timezone

from users.models import ENROLLED_STATUSES, Class, Staff, Student, Subject

from .models import (Breakage, Equipment, EquipmentMove, Lab, LabBooking, Practical, PracticalChemical,
                     PracticalRecord, PracticalSession, SafetyCheck, SafetyIncident)

ZERO = Decimal('0.00')
AT_RISK_ATTENDANCE = 75      # % of practicals attended
AT_RISK_SCORE = 50           # % average mark
CHECKLIST = [
    'Fire extinguisher present, charged and in date',
    'First aid kit complete and in date',
    'Eyewash station / clean water available',
    'Gas taps and supply off and not leaking',
    'Electrical sockets, cables and plugs safe',
    'Fume cupboard / ventilation working',
    'Chemicals labelled, in date and stored by hazard',
    'Safety goggles, gloves and lab coats available',
    'Emergency exits and gangways clear',
    'Broken glass and chemical waste disposed of safely',
    'Safety rules displayed',
]


class LabError(ValueError):
    pass


# ─────────────────────────────────────────────────────────────────────────────
# Who
# ─────────────────────────────────────────────────────────────────────────────

def staff_for(user, tenant):
    return Staff.objects.filter(tenant=tenant, user=user).first() if user.is_authenticated else None


def is_staff(user, tenant):
    return bool(user.is_authenticated and (user.is_superuser or staff_for(user, tenant)))


def is_admin(user, tenant):
    if not user.is_authenticated:
        return False
    return user.is_superuser or Staff.objects.filter(tenant=tenant, user=user, role='ADMIN').exists()


def can_manage(user, lab):
    if is_admin(user, lab.tenant):
        return True
    me = staff_for(user, lab.tenant)
    return bool(me and (lab.in_charge_id == me.id or lab.technicians.filter(pk=me.pk).exists()))


def can_mark(user, session):
    if is_admin(user, session.tenant):
        return True
    me = staff_for(user, session.tenant)
    return bool(me and (session.teacher_id == me.id or (session.lab and can_manage(user, session.lab))))


def active_term(tenant):
    from academics.models import Term
    return Term.objects.filter(tenant=tenant, is_active=True).select_related('session').first()


def _int(value, default=0, low=0, high=10 ** 6, name='Number'):
    try:
        n = int(str('' if value is None else value).strip() or default)
    except (TypeError, ValueError):
        raise LabError(f'{name} must be a whole number.')
    if not low <= n <= high:
        raise LabError(f'{name} must be between {low} and {high}.')
    return n


def _money(value, name='Amount'):
    try:
        d = Decimal(str(value or '0').replace(',', '').replace('₦', '').strip() or '0')
    except InvalidOperation:
        raise LabError(f'{name} must be a number.')
    if d < 0:
        raise LabError(f'{name} cannot be negative.')
    return d.quantize(Decimal('0.01'))


def _date(value, name='Date'):
    if hasattr(value, 'year'):
        return value
    try:
        return datetime.strptime(str(value), '%Y-%m-%d').date()
    except (TypeError, ValueError):
        raise LabError(f'Choose a {name.lower()}.')


def _time(value, name='Time'):
    if isinstance(value, time):
        return value
    try:
        return datetime.strptime(str(value)[:5], '%H:%M').time()
    except (TypeError, ValueError):
        raise LabError(f'Choose a {name.lower()}.')


# ─────────────────────────────────────────────────────────────────────────────
# Labs
# ─────────────────────────────────────────────────────────────────────────────

ROOM_KIND = {'ICT': 'ICT'}           # everything else is a timetable "Science lab" room
SPACE_KIND = {'ICT': 'ICT'}


@transaction.atomic
def save_lab(tenant, data, lab=None):
    from facilities.models import Space
    from timetable.models import TeachingRoom
    name = (data.get('name') or '').strip()[:80]
    if not name:
        raise LabError('Give the lab a name.')
    if Lab.objects.filter(tenant=tenant, name__iexact=name).exclude(pk=getattr(lab, 'pk', None)).exists():
        raise LabError(f'There is already a lab called {name}.')
    lab = lab or Lab(tenant=tenant)
    old_name = lab.name
    lab.name = name
    lab.kind = data.get('kind') if data.get('kind') in dict(Lab.KINDS) else 'OTHER'
    for field in ('building', 'safety_rules', 'first_aid_kit', 'fire_extinguisher', 'emergency_shutoff', 'eyewash'):
        setattr(lab, field, (data.get(field) or '').strip())
    lab.capacity = _int(data.get('capacity'), 30, 1, 500, 'Capacity')
    lab.workstations = _int(data.get('workstations'), 0, 0, 500, 'Work stations')
    lab.inspect_every_days = _int(data.get('inspect_every_days'), 30, 7, 365, 'Inspection interval')
    lab.in_charge = Staff.objects.filter(tenant=tenant, pk=data.get('in_charge')).first() if data.get('in_charge') else None
    lab.active = data.get('active', 'on') in ('on', True, '1', 'true')
    # Keep the timetable room (bookings, clashes) and the facilities space (chemicals, repairs) in step
    room = lab.room or TeachingRoom.objects.filter(tenant=tenant, name__iexact=old_name or name).first()
    if room is None:
        room = TeachingRoom(tenant=tenant)
    room.name, room.kind = name, ROOM_KIND.get(lab.kind, 'LAB')
    room.capacity, room.active = lab.capacity, lab.active
    room.save()
    space = lab.space or Space.objects.filter(tenant=tenant, name__iexact=old_name or name, kind__in=('LAB', 'ICT')).first()
    if space is None:
        space = Space(tenant=tenant)
    space.name, space.kind, space.building = name, SPACE_KIND.get(lab.kind, 'LAB'), lab.building[:80]
    space.capacity, space.teaching_room, space.active = lab.capacity, room, lab.active
    space.save()
    lab.room, lab.space = room, space
    lab.save()
    lab.subjects.set(Subject.objects.filter(tenant=tenant, pk__in=[i for i in data.getlist('subjects') if i.isdigit()])
                     if hasattr(data, 'getlist') else [])
    lab.technicians.set(Staff.objects.filter(tenant=tenant, pk__in=[i for i in data.getlist('technicians') if i.isdigit()])
                        if hasattr(data, 'getlist') else [])
    return lab


def inspection(lab):
    """{'last': SafetyCheck|None, 'due': date, 'overdue': bool}."""
    last = lab.checks.first()
    due = (last.date if last else lab.created_at.date()) + timedelta(days=lab.inspect_every_days)
    if last is None:
        due = timezone.localdate()
    return {'last': last, 'due': due, 'overdue': due < timezone.localdate() or last is None,
            'failed': bool(last and not last.passed)}


def chemicals(lab):
    from facilities.models import Chemical
    if not lab.space_id:
        return Chemical.objects.none()
    return Chemical.objects.filter(tenant=lab.tenant, space=lab.space, active=True)


def status(lab):
    """Everything the lab card shows."""
    today = timezone.localdate()
    eq = list(lab.equipment.filter(active=True))
    chems = list(chemicals(lab))
    soon = today + timedelta(days=60)
    low = [e for e in eq if e.low]
    service_due = [e for e in eq if e.service_due and e.service_due <= today]
    chem_low = [c for c in chems if c.reorder_level and c.quantity <= c.reorder_level]
    chem_expiring = [c for c in chems if c.expires_on and c.expires_on <= soon]
    return {
        'equip_alerts': len(low) + len(service_due), 'chem_alerts': len(chem_low) + len(chem_expiring),
        'today': list(bookings(lab, today, today)),
        'next': LabBooking.objects.filter(lab=lab, booking__cancelled=False, booking__date__gt=today)
                  .select_related('booking', 'school_class', 'subject').first(),
        'value': sum((e.value for e in eq), ZERO),
        'items': sum(e.quantity for e in eq),
        'broken': sum(e.broken for e in eq),
        'low': low, 'service_due': service_due, 'chem_low': chem_low, 'chem_expiring': chem_expiring,
        'chem_count': len(chems),
        'incidents': lab.incidents.filter(closed=False).count(),
        'inspection': inspection(lab),
        'unpaid': lab.breakages.filter(status='UNPAID').aggregate(s=Sum('charge'))['s'] or ZERO,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Bookings (through the timetable, so they never clash with lessons)
# ─────────────────────────────────────────────────────────────────────────────

def bookings(lab, start, end):
    return (LabBooking.objects.filter(lab=lab, booking__cancelled=False, booking__date__gte=start, booking__date__lte=end)
            .select_related('booking', 'booking__booked_by', 'school_class', 'subject', 'practical'))


def book(lab, user, data):
    from timetable import scheduling
    if not lab.room_id:
        save_lab(lab.tenant, _lab_data(lab), lab)
        lab.refresh_from_db()
    day = _date(data.get('date'))
    if day < timezone.localdate():
        raise LabError('Choose today or a later date.')
    start, end = _time(data.get('start_time'), 'Start time'), _time(data.get('end_time'), 'End time')
    school_class = Class.objects.filter(tenant=lab.tenant, pk=data.get('school_class')).first() if data.get('school_class') else None
    subject = Subject.objects.filter(tenant=lab.tenant, pk=data.get('subject')).first() if data.get('subject') else None
    practical = Practical.objects.filter(tenant=lab.tenant, pk=data.get('practical')).first() if data.get('practical') else None
    if practical and not subject:
        subject = practical.subject
    students = _int(data.get('students'), 0, 0, 1000, 'Number of students') or None
    if students and students > lab.capacity:
        raise LabError(f'{lab.name} holds {lab.capacity} students; split the class into groups.')
    purpose = (data.get('purpose') or '').strip() or ' · '.join(
        x for x in ((school_class.name if school_class else ''), (subject.name if subject else ''),
                    (practical.title if practical else 'Practical')) if x)
    try:
        created, skipped = scheduling.book_room(lab.room, day, start, end, purpose, user,
                                                _int(data.get('repeat_weeks'), 1, 1, 15, 'Weeks'))
    except scheduling.SchedulingError as problem:
        raise LabError(str(problem))
    made = [LabBooking.objects.create(tenant=lab.tenant, lab=lab, booking=b, school_class=school_class, subject=subject,
                                      practical=practical, students=students,
                                      prep_notes=(data.get('prep_notes') or '').strip())
            for b in created]
    if made and lab.technicians.exists():
        from notifications import services as notes
        for tech in lab.technicians.select_related('user'):
            if tech.user_id != user.pk:
                notes.to_user(tech.user, 'lab', f'{lab.name} booked: {purpose}',
                              body=f'{day:%a %d %b}, {start:%H:%M}–{end:%H:%M}' + (f' · {len(made)} weeks' if len(made) > 1 else '')
                                   + (f'. Prepare: {made[0].prep_notes[:120]}' if made[0].prep_notes else ''),
                              url=f'/labs/{lab.pk}/?week={day:%Y-%m-%d}', tenant=lab.tenant, admin_feed=False)
    return made, skipped


def _lab_data(lab):
    from django.http import QueryDict
    q = QueryDict(mutable=True)
    q.update({'name': lab.name, 'kind': lab.kind, 'building': lab.building, 'capacity': lab.capacity,
              'workstations': lab.workstations, 'inspect_every_days': lab.inspect_every_days,
              'in_charge': lab.in_charge_id or '', 'active': 'on' if lab.active else ''})
    for f in ('safety_rules', 'first_aid_kit', 'fire_extinguisher', 'emergency_shutoff', 'eyewash'):
        q[f] = getattr(lab, f)
    q.setlist('subjects', [str(i) for i in lab.subjects.values_list('pk', flat=True)])
    q.setlist('technicians', [str(i) for i in lab.technicians.values_list('pk', flat=True)])
    return q


def cancel(lab_booking, user):
    b = lab_booking.booking
    if not (can_manage(user, lab_booking.lab) or b.booked_by_id == user.pk):
        raise LabError('Only the person who booked it, or the lab staff, can cancel this booking.')
    b.cancelled = True
    b.save(update_fields=['cancelled'])


def week(lab, start):
    """Monday–Saturday of the week containing `start`: timetabled lessons and bookings for this lab, in time order."""
    from timetable.lessons import DAY_CODES, published_timetable
    from timetable.models import TimetableSlot
    monday = start - timedelta(days=start.weekday())
    days = [monday + timedelta(days=i) for i in range(6)]
    tt = published_timetable(lab.tenant) if lab.room_id else None
    lessons = defaultdict(list)
    if tt:
        for e in (TimetableSlot.objects.filter(timetable=tt, room=lab.room)
                  .select_related('school_class', 'subject', 'time_slot', 'teacher')):
            lessons[e.day].append(e)
    by_day = defaultdict(list)
    for lb in bookings(lab, days[0], days[-1]):
        by_day[lb.booking.date].append(lb)
    out = []
    for d in days:
        items = [{'start': e.time_slot.start_time, 'end': e.time_slot.end_time, 'kind': 'lesson',
                  'title': f'{e.school_class.name} · {e.subject.name}', 'who': getattr(e.teacher, 'full_name', '')}
                 for e in lessons.get(DAY_CODES[d.weekday()], [])]
        items += [{'start': lb.booking.start_time, 'end': lb.booking.end_time, 'kind': 'booking', 'title': lb.booking.purpose,
                   'who': _name(lb.booking.booked_by), 'lb': lb} for lb in by_day.get(d, [])]
        out.append({'date': d, 'today': d == timezone.localdate(), 'items': sorted(items, key=lambda i: i['start'])})
    return out


def _name(user):
    if not user:
        return ''
    s = Staff.objects.filter(user=user).first()
    return s.full_name if s else (user.get_full_name() or user.username)


def mark_prepared(lab_booking, user):
    if not can_manage(user, lab_booking.lab):
        raise LabError('Only lab staff can mark a booking as set up.')
    lab_booking.prepared, lab_booking.prepared_by = not lab_booking.prepared, user
    lab_booking.save(update_fields=['prepared', 'prepared_by'])


# ─────────────────────────────────────────────────────────────────────────────
# Equipment
# ─────────────────────────────────────────────────────────────────────────────

def save_equipment(lab, user, data, eq=None):
    if not can_manage(user, lab):
        raise LabError('Only the lab head, technicians or administrators can change equipment.')
    name = (data.get('name') or '').strip()[:120]
    if not name:
        raise LabError('Name the equipment.')
    new = eq is None
    eq = eq or Equipment(tenant=lab.tenant, lab=lab)
    eq.name = name
    eq.category = data.get('category') if data.get('category') in dict(Equipment.CATEGORIES) else 'OTHER'
    if new:
        eq.quantity = _int(data.get('quantity'), 1, 0, 100000, 'Quantity')
    eq.min_quantity = _int(data.get('min_quantity'), 0, 0, 100000, 'Minimum quantity')
    eq.unit_cost = _money(data.get('unit_cost'), 'Cost')
    eq.location = (data.get('location') or '').strip()[:120]
    eq.serial = (data.get('serial') or '').strip()[:80]
    eq.service_every_days = _int(data.get('service_every_days'), 0, 0, 3650, 'Service interval')
    eq.last_serviced = _date(data.get('last_serviced')) if data.get('last_serviced') else eq.last_serviced
    eq.notes = (data.get('notes') or '').strip()[:255]
    eq.save()
    if new and eq.quantity:
        EquipmentMove.objects.create(tenant=lab.tenant, equipment=eq, kind='ADD', quantity=eq.quantity,
                                     note='Added to the register', by=user)
    return eq


@transaction.atomic
def move(eq, user, kind, quantity, note=''):
    eq = Equipment.objects.select_for_update().get(pk=eq.pk)
    if not can_manage(user, eq.lab) and kind != 'BREAK':
        raise LabError('Only the lab head, technicians or administrators can change equipment.')
    qty = _int(quantity, 0, 0, 100000, 'Quantity')
    if kind not in dict(EquipmentMove.KINDS):
        raise LabError('Choose what happened.')
    if kind in ('ADD', 'BREAK', 'REPAIR', 'WRITEOFF') and qty < 1:
        raise LabError('Enter how many.')
    if kind == 'ADD':
        eq.quantity += qty
    elif kind == 'BREAK':
        if qty > eq.quantity:
            raise LabError(f'Only {eq.quantity} working {eq.name} are recorded.')
        eq.quantity -= qty
        eq.broken += qty
    elif kind == 'REPAIR':
        if qty > eq.broken:
            raise LabError(f'Only {eq.broken} {eq.name} are recorded as broken.')
        eq.broken -= qty
        eq.quantity += qty
    elif kind == 'WRITEOFF':
        if qty > eq.broken + eq.quantity:
            raise LabError(f'Only {eq.broken + eq.quantity} {eq.name} are recorded.')
        from_broken = min(qty, eq.broken)
        eq.broken -= from_broken
        eq.quantity -= qty - from_broken
    elif kind == 'SERVICE':
        eq.last_serviced = timezone.localdate()
        qty = 0
    elif kind == 'COUNT':
        qty, eq.quantity = qty - eq.quantity, qty
    eq.save()
    return EquipmentMove.objects.create(tenant=eq.tenant, equipment=eq, kind=kind, quantity=qty,
                                        note=(note or '').strip()[:200], by=user)


# ─────────────────────────────────────────────────────────────────────────────
# Breakages and charges
# ─────────────────────────────────────────────────────────────────────────────

@transaction.atomic
def report_breakage(lab, user, data):
    eq = Equipment.objects.filter(lab=lab, pk=data.get('equipment')).first() if data.get('equipment') else None
    item = (data.get('item') or '').strip()[:120] or (eq.name if eq else '')
    if not item:
        raise LabError('Say what broke.')
    qty = _int(data.get('quantity'), 1, 1, 10000, 'Quantity')
    student = Student.objects.filter(tenant=lab.tenant, pk=data.get('student')).first() if data.get('student') else None
    session = PracticalSession.objects.filter(tenant=lab.tenant, pk=data.get('session')).first() if data.get('session') else None
    cause = data.get('cause') if data.get('cause') in dict(Breakage.CAUSES) else 'UNKNOWN'
    cost = (eq.unit_cost * qty) if eq and eq.unit_cost else _money(data.get('cost'), 'Cost')
    charge = _money(data.get('charge'), 'Charge') if data.get('charge') not in (None, '') else ZERO
    if charge and not student:
        raise LabError('Choose the student to charge, or leave the charge at 0.')
    if charge and not can_manage(user, lab):
        raise LabError('Only lab staff or administrators can charge a family for a breakage.')
    if eq:
        move(eq, user, 'BREAK', min(qty, eq.quantity) or qty, f'Breakage: {item}') if eq.quantity else None
    b = Breakage.objects.create(tenant=lab.tenant, lab=lab, equipment=eq, item=item, quantity=qty, student=student,
                                session=session, cause=cause, description=(data.get('description') or '').strip()[:300],
                                date=_date(data.get('date')) if data.get('date') else timezone.localdate(),
                                cost=cost, charge=charge, status='UNPAID' if charge else 'NONE', reported_by=user)
    if charge:
        from notifications import services as notes
        notes.to_family(student, 'payment', f'Lab breakage charge: ₦{charge:,.2f}',
                        body=f'{student.full_name} broke {qty} × {item} in the {lab.name} on {b.date:%d %b}. '
                             'It can be paid from the family wallet on the parent portal.',
                        url='/labs/my/', level='warning', admin_feed=False)
    return b


def pay_from_wallet(b, user):
    from finance.views import _get_family_wallet_for_student
    if b.status != 'UNPAID':
        raise LabError('This charge is not waiting for payment.')
    wallet = _get_family_wallet_for_student(b.student)
    if not wallet:
        raise LabError('No family wallet is set up for this child.')
    with transaction.atomic():
        try:
            txn = wallet.debit(b.charge, f'Lab breakage: {b.student.full_name} — {b.quantity} × {b.item} ({b.lab.name})'[:300],
                               performed_by=user, category='OTHER')
        except ValueError as problem:
            raise LabError(str(problem))
        b.status, b.transaction, b.settled_by = 'PAID', txn, user
        b.save(update_fields=['status', 'transaction', 'settled_by'])
    return b


def settle(b, user, status):
    if not can_manage(user, b.lab):
        raise LabError('Only lab staff or administrators can settle charges.')
    if status not in ('PAID', 'WAIVED') or b.status != 'UNPAID':
        raise LabError('Only an unpaid charge can be marked paid or waived.')
    b.status, b.settled_by = status, user
    b.save(update_fields=['status', 'settled_by'])
    return b


# ─────────────────────────────────────────────────────────────────────────────
# Safety
# ─────────────────────────────────────────────────────────────────────────────

def record_check(lab, user, data):
    if not can_manage(user, lab):
        raise LabError('Only the lab head, technicians or administrators record inspections.')
    items = [{'item': text, 'ok': data.get(f'ok_{i}') == 'on', 'note': (data.get(f'note_{i}') or '').strip()[:160]}
             for i, text in enumerate(CHECKLIST)]
    check = SafetyCheck.objects.create(tenant=lab.tenant, lab=lab, items=items, passed=all(i['ok'] for i in items),
                                       notes=(data.get('notes') or '').strip(), by=user,
                                       date=_date(data.get('date')) if data.get('date') else timezone.localdate())
    if not check.passed:
        from notifications import services as notes
        failed = [i['item'] for i in items if not i['ok']]
        notes.to_admins(lab.tenant, 'lab', f'{lab.name} failed its safety inspection',
                        body='; '.join(failed)[:300], url=f'/labs/{lab.pk}/?tab=safety', level='warning')
    return check


@transaction.atomic
def report_incident(lab, user, data, student_ids=()):
    kind = data.get('kind')
    if kind not in dict(SafetyIncident.KINDS):
        raise LabError('Choose what kind of incident it was.')
    description = (data.get('description') or '').strip()
    if not description:
        raise LabError('Describe what happened.')
    severity = data.get('severity') if data.get('severity') in dict(SafetyIncident.SEVERITIES) else 'MINOR'
    when = timezone.now()
    if data.get('happened_at'):
        try:
            when = timezone.make_aware(datetime.strptime(data.get('happened_at'), '%Y-%m-%dT%H:%M'))
        except ValueError:
            raise LabError('Enter when it happened.')
    inc = SafetyIncident.objects.create(
        tenant=lab.tenant, lab=lab, kind=kind, severity=severity, happened_at=when, description=description,
        first_aid=(data.get('first_aid') or '').strip(), action=(data.get('action') or '').strip(), reported_by=user,
        session=PracticalSession.objects.filter(tenant=lab.tenant, pk=data.get('session')).first() if data.get('session') else None)
    students = list(Student.objects.filter(tenant=lab.tenant, pk__in=[i for i in student_ids if str(i).isdigit()]))
    inc.students.set(students)
    from notifications import services as notes
    level = {'MINOR': 'info', 'MODERATE': 'warning', 'SERIOUS': 'danger'}[severity]
    notes.to_admins(lab.tenant, 'lab', f'Lab incident in {lab.name}: {inc.get_kind_display()}',
                    body=f'{inc.get_severity_display()}. {description[:200]}', url=f'/labs/incidents/{inc.pk}/', level=level)
    if kind != 'NEARMISS':
        for st in students:
            notes.to_family(st, 'lab', f'{st.full_name.split(" ")[0]} was involved in a lab incident',
                            body=f'{inc.get_kind_display()} in the {lab.name} on {timezone.localtime(when):%d %b at %H:%M}. '
                                 f'{inc.get_severity_display()}.' + (f' First aid: {inc.first_aid[:120]}' if inc.first_aid else '')
                                 + ' The school will contact you if anything more is needed.',
                            level=level, admin_feed=False)
        if students:
            inc.families_told = True
            inc.save(update_fields=['families_told'])
    return inc


def close_incident(inc, user, action):
    if not can_manage(user, inc.lab):
        raise LabError('Only lab staff or administrators can close an incident.')
    action = (action or '').strip()
    if not action and not inc.action:
        raise LabError('Record what has been done so it does not happen again.')
    if action:
        inc.action = action
    inc.closed = True
    inc.save(update_fields=['action', 'closed'])
    return inc


# ─────────────────────────────────────────────────────────────────────────────
# Practicals
# ─────────────────────────────────────────────────────────────────────────────

RUBRIC_LINE = re.compile(r'^\s*(.+?)\s*[|:=–-]\s*(\d{1,3})\s*$')


def parse_rubric(text):
    """"Observations | 5" per line → [{'criterion', 'max'}]."""
    out = []
    for line in (text or '').splitlines():
        if not line.strip():
            continue
        m = RUBRIC_LINE.match(line)
        if not m:
            raise LabError(f'Write each marking line as "What is marked | marks", e.g. "Observations | 5" — not "{line.strip()[:40]}".')
        out.append({'criterion': m.group(1)[:60], 'max': int(m.group(2))})
    return out


@transaction.atomic
def save_practical(tenant, user, data, practical=None):
    from facilities.models import Chemical
    title = (data.get('title') or '').strip()[:160]
    subject = Subject.objects.filter(tenant=tenant, pk=data.get('subject')).first()
    if not title or not subject:
        raise LabError('Give the practical a title and a subject.')
    p = practical or Practical(tenant=tenant, created_by=user)
    p.title, p.subject = title, subject
    p.lab = Lab.objects.filter(tenant=tenant, pk=data.get('lab')).first() if data.get('lab') else None
    p.level = (data.get('level') or '').strip()[:40]
    p.kind = data.get('kind') if data.get('kind') in dict(Practical.KINDS) else 'CLASS'
    for f in ('aim', 'apparatus', 'procedure', 'hazards'):
        setattr(p, f, (data.get(f) or '').strip())
    p.rubric = parse_rubric(data.get('rubric'))
    p.max_score = sum(r['max'] for r in p.rubric) if p.rubric else _int(data.get('max_score'), 20, 1, 100, 'Total marks')
    if not p.max_score:
        raise LabError('The practical needs at least one mark.')
    p.duration_minutes = _int(data.get('duration_minutes'), 80, 10, 480, 'Duration')
    p.save()
    if hasattr(data, 'getlist'):
        p.chemicals.all().delete()
        for chem_id, qty in zip(data.getlist('chemical'), data.getlist('chemical_qty')):
            if not chem_id:
                continue
            chem = Chemical.objects.filter(tenant=tenant, pk=chem_id).first()
            q = _money(qty, 'Chemical quantity')
            if chem and q > 0:
                PracticalChemical.objects.update_or_create(tenant=tenant, practical=p, chemical=chem,
                                                           defaults={'per_group': q})
    return p


@transaction.atomic
def plan_session(practical, user, data):
    tenant = practical.tenant
    school_class = Class.objects.filter(tenant=tenant, pk=data.get('school_class')).first()
    if school_class is None:
        raise LabError('Choose the class.')
    lab = Lab.objects.filter(tenant=tenant, pk=data.get('lab')).first() if data.get('lab') else practical.lab
    teacher = staff_for(user, tenant)
    if data.get('teacher'):
        teacher = Staff.objects.filter(tenant=tenant, pk=data.get('teacher')).first() or teacher
    term = active_term(tenant)
    s = PracticalSession.objects.create(
        tenant=tenant, practical=practical, school_class=school_class, lab=lab, teacher=teacher,
        date=_date(data.get('date')) if data.get('date') else timezone.localdate(),
        groups=_int(data.get('groups'), 1, 1, 100, 'Groups'), session=term.session if term else None, term=term,
        created_by=user)
    if lab and data.get('start_time') and data.get('end_time'):
        made, skipped = book(lab, user, {'date': s.date.isoformat(), 'start_time': data.get('start_time'),
                                         'end_time': data.get('end_time'), 'school_class': school_class.pk,
                                         'subject': practical.subject_id, 'practical': practical.pk,
                                         'prep_notes': data.get('prep_notes') or practical.apparatus})
        if skipped:
            raise LabError(f'{lab.name} is not free then: ' + ' '.join(skipped[0][1]))
        s.booking = made[0]
        s.save(update_fields=['booking'])
    students = Student.objects.filter(tenant=tenant, class_assigned=school_class, status__in=ENROLLED_STATUSES)
    PracticalRecord.objects.bulk_create([PracticalRecord(tenant=tenant, session=s, student=st) for st in students])
    return s


def roster(session):
    """The session's records, adding any student who has joined the class since it was planned."""
    have = set(session.records.values_list('student_id', flat=True))
    missing = Student.objects.filter(tenant=session.tenant, class_assigned=session.school_class,
                                     status__in=ENROLLED_STATUSES).exclude(pk__in=have)
    PracticalRecord.objects.bulk_create([PracticalRecord(tenant=session.tenant, session=session, student=st)
                                         for st in missing])
    return list(session.records.select_related('student').order_by('student__full_name'))


@transaction.atomic
def save_register(session, user, data):
    if not can_mark(user, session):
        raise LabError('Only the teacher running this practical, lab staff or administrators can mark it.')
    if session.status == PracticalSession.CANCELLED:
        raise LabError('This practical was cancelled.')
    p = session.practical
    errors = []
    for r in roster(session):
        key = f'r{r.pk}'
        att = data.get(f'{key}_att', r.attendance)
        r.attendance = att if att in dict(PracticalRecord.STATUSES) else r.attendance
        r.bench = (data.get(f'{key}_bench') or '').strip()[:10]
        r.remark = (data.get(f'{key}_remark') or '').strip()[:160]
        if not r.attended:
            r.scores, r.score = [], None
        elif p.rubric:
            marks = []
            for i, crit in enumerate(p.rubric):
                raw = (data.get(f'{key}_c{i}') or '').strip()
                if raw == '':
                    marks.append(None)
                    continue
                try:
                    v = float(raw)
                except ValueError:
                    errors.append(f'{r.student.full_name}: "{raw}" is not a mark.')
                    marks.append(None)
                    continue
                if not 0 <= v <= crit['max']:
                    errors.append(f'{r.student.full_name}: {crit["criterion"]} is out of {crit["max"]}.')
                    v = None
                marks.append(v)
            r.scores = marks
            r.score = sum(m for m in marks if m is not None) if any(m is not None for m in marks) else None
        else:
            raw = (data.get(f'{key}_score') or '').strip()
            if raw == '':
                r.score = None
            else:
                try:
                    v = float(raw)
                except ValueError:
                    v = None
                    errors.append(f'{r.student.full_name}: "{raw}" is not a mark.')
                if v is not None and not 0 <= v <= p.max_score:
                    errors.append(f'{r.student.full_name}: the mark is out of {p.max_score}.')
                    v = None
                r.score = v
        r.save()
    session.safety_briefing = data.get('safety_briefing') == 'on'
    session.notes = (data.get('notes') or '').strip()
    fields = ['safety_briefing', 'notes']
    if data.get('finish') and not errors:
        session.status = PracticalSession.DONE
        fields.append('status')
    session.save(update_fields=fields)
    if errors:
        raise LabError(' '.join(errors[:6]) + (f' …and {len(errors) - 6} more.' if len(errors) > 6 else ''))
    return session


def use_chemicals(session, user):
    """Deduct what the practical uses (per group × groups) from the chemicals register."""
    from facilities import services as fac
    if session.chemicals_used:
        raise LabError('The chemicals for this practical have already been deducted.')
    if not can_mark(user, session):
        raise LabError('Only the teacher running this practical, lab staff or administrators can do this.')
    needs = list(session.practical.chemicals.select_related('chemical'))
    if not needs:
        raise LabError('This practical has no chemicals listed.')
    short = [f'{n.chemical.name} (need {n.per_group * session.groups:g} {n.chemical.unit}, have {n.chemical.quantity:g})'
             for n in needs if n.per_group * session.groups > n.chemical.quantity]
    if short:
        raise LabError('Not enough in stock: ' + '; '.join(short) + '.')
    purpose = f'{session.practical.title} — {session.school_class.name}'[:160]
    with transaction.atomic():
        for n in needs:
            fac.chemical_move(n.chemical, user, 'USE', n.per_group * session.groups, purpose, session.date)
        session.chemicals_used = True
        session.save(update_fields=['chemicals_used'])
    return needs


def cancel_session(session, user):
    if not can_mark(user, session):
        raise LabError('Only the teacher running this practical, lab staff or administrators can cancel it.')
    session.status = PracticalSession.CANCELLED
    session.save(update_fields=['status'])
    if session.booking_id:
        b = session.booking.booking
        b.cancelled = True
        b.save(update_fields=['cancelled'])


# ─────────────────────────────────────────────────────────────────────────────
# WAEC/NECO readiness and the result sheet
# ─────────────────────────────────────────────────────────────────────────────

def readiness(school_class, subject, term=None):
    """Per student: practicals done, attended, missed, average mark — and who is at risk for the external practical."""
    sessions = PracticalSession.objects.filter(tenant=school_class.tenant, school_class=school_class,
                                               practical__subject=subject, status=PracticalSession.DONE)
    if term:
        sessions = sessions.filter(term=term)
    sessions = list(sessions.select_related('practical').order_by('date'))
    records = defaultdict(dict)
    for r in PracticalRecord.objects.filter(session__in=sessions):
        records[r.student_id][r.session_id] = r
    rows = []
    for st in Student.objects.filter(tenant=school_class.tenant, class_assigned=school_class,
                                     status__in=ENROLLED_STATUSES).order_by('full_name'):
        mine = records.get(st.id, {})
        counted = [s for s in sessions if mine.get(s.id) is None or mine[s.id].attendance != PracticalRecord.EXCUSED]
        attended = [s for s in counted if mine.get(s.id) and mine[s.id].attended]
        marks = [100 * mine[s.id].score / s.practical.max_score for s in attended
                 if mine[s.id].score is not None and s.practical.max_score]
        att_pct = round(100 * len(attended) / len(counted)) if counted else None
        avg = round(sum(marks) / len(marks)) if marks else None
        # For the result sheet: unexcused absences count as 0
        ca = [(100 * mine[s.id].score / s.practical.max_score) if (mine.get(s.id) and mine[s.id].attended
                                                                   and mine[s.id].score is not None) else 0
              for s in counted]
        rows.append({'student': st, 'done': len(counted), 'attended': len(attended), 'attendance': att_pct,
                     'average': avg, 'ca_percent': round(sum(ca) / len(ca), 1) if ca else None,
                     'missed': [s for s in counted if s not in attended],
                     'at_risk': bool(counted) and ((att_pct or 0) < AT_RISK_ATTENDANCE
                                                   or (avg is not None and avg < AT_RISK_SCORE))})
    return {'sessions': sessions, 'rows': rows, 'at_risk': sum(1 for r in rows if r['at_risk'])}


def send_to_results(school_class, subject, term, column, user):
    """Put each student's practical average (unexcused absences as 0), scaled to a CA column's marks, on the result sheet."""
    from results.locks import ScoresLocked, check_open
    from results.models import AssessmentScheme, TermResult
    from results.release import may_edit_scores
    if term is None:
        raise LabError('There is no active term.')
    scheme = AssessmentScheme.for_tenant(school_class.tenant)
    col = next((c for c in scheme.ca_columns if c['key'] == column), None)
    if col is None:
        raise LabError('Choose one of the result sheet\'s CA columns.')
    try:
        check_open(term)
    except ScoresLocked as problem:
        raise LabError(str(problem))
    ok, why = may_edit_scores(user, school_class, subject, term)
    if not ok:
        raise LabError(why)
    data = readiness(school_class, subject, term)
    if not data['sessions']:
        raise LabError(f'No {subject.name} practicals are marked as done for {school_class.name} this term.')
    n = 0
    with transaction.atomic():
        for row in data['rows']:
            if row['ca_percent'] is None:
                continue
            tr, _ = TermResult.objects.get_or_create(
                tenant=school_class.tenant, student=row['student'], subject=subject, term=term, session=term.session,
                defaults={'class_assigned': school_class})
            if tr.is_not_offering:
                continue
            setattr(tr, col['field'], round(row['ca_percent'] * col['max'] / 100, 1))
            tr.save()
            n += 1
    return n, col


def student_record(student):
    """A student's practicals, grouped by subject, newest first."""
    out = defaultdict(list)
    for r in (PracticalRecord.objects.filter(student=student, session__status=PracticalSession.DONE)
              .select_related('session__practical__subject', 'session__lab').order_by('-session__date')):
        out[r.session.practical.subject.name].append(r)
    return dict(sorted(out.items()))


def overview(tenant):
    """Numbers for the top of the labs page."""
    term = active_term(tenant)
    sessions = PracticalSession.objects.filter(tenant=tenant, status=PracticalSession.DONE)
    if term:
        sessions = sessions.filter(term=term)
    recs = PracticalRecord.objects.filter(session__in=sessions)
    total = recs.exclude(attendance=PracticalRecord.EXCUSED).count()
    present = recs.filter(attendance__in=(PracticalRecord.PRESENT, PracticalRecord.LATE)).count()
    since = term.start_date if term and getattr(term, 'start_date', None) else timezone.localdate() - timedelta(days=90)
    breaks = Breakage.objects.filter(tenant=tenant, date__gte=since)
    return {'term': term, 'practicals': sessions.count(),
            'attendance': round(100 * present / total) if total else None,
            'breakage_cost': breaks.aggregate(s=Sum('cost'))['s'] or ZERO, 'breakages': breaks.count(),
            'open_incidents': SafetyIncident.objects.filter(tenant=tenant, closed=False).count(),
            'upcoming': PracticalSession.objects.filter(tenant=tenant, status=PracticalSession.PLANNED,
                                                        date__gte=timezone.localdate()).count()}
