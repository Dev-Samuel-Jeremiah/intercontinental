"""
hostel/boarding.py
──────────────────
Boarding life on top of the hostel module:

    F321  bed map                    F329  room inspections
    F322  dormitory roll call        F330  sick bay
    F323  exeat requests (parents)   F334  dietary needs (kitchen list)
    F327  prep attendance            F336  lights-out checks, late returns
    F328  visiting-day booking       F337  housemaster reports
    F333  meal menu for families     F338  room change requests
    F340  night emergency sheet
"""
from datetime import datetime, timedelta
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import Avg, Count, Q
from django.utils import timezone
from django.utils.html import escape

from .models import (Bed, BoarderProfile, BoardingReport, CheckInOut, ExeatRequest, Hostel, Inspection, MealPlan,
                     RollCall, RollCallMark, Room, RoomChangeRequest, SickBayVisit, VisitBooking, VisitingDay)

LIVING = ('ACTIVE', 'EXEAT')


class BoardingError(ValueError):
    pass


def _email(to, subject, html):
    from communications.services import send_email
    return send_email(to, subject, html)


def _sms(to, text):
    from communications.services import send_sms
    return send_sms(to, text)


def _family(student):
    from users.records import contacts
    return [e for _n, e in contacts(student, 'EMAIL')], [p for _n, p in contacts(student, 'SMS')]


def _tell_family(student, subject, html, sms=None):
    emails, phones = _family(student)
    for e in emails:
        _email(e, subject, html)
    if sms:
        for p in phones:
            _sms(p, sms)


def _managers(hostel):
    """Emails of the hostel's warden and the school administrators."""
    from hr.services import admins_emails
    out = set(admins_emails(hostel.tenant))
    if hostel.warden_id and hostel.warden.email:
        out.add(hostel.warden.email)
    return sorted(out)


def boarders_in(hostel):
    return list(BoarderProfile.objects.filter(bed__room__hostel=hostel, status__in=LIVING)
                .select_related('student__class_assigned', 'bed__room').order_by('bed__room__room_number',
                                                                                  'student__full_name'))


def hostel_of(boarder):
    return boarder.bed.room.hostel if boarder.bed_id else None


# ─────────────────────────────────────────────────────────────────────────────
# F321 — bed map
# ─────────────────────────────────────────────────────────────────────────────

def bed_map(hostel):
    """[(floor name, [(room, [(bed, boarder|None)])])]"""
    occupants = {b.bed_id: b for b in BoarderProfile.objects.filter(bed__room__hostel=hostel, status__in=LIVING)
                 .select_related('student__class_assigned')}
    floors = {}
    for room in Room.objects.filter(hostel=hostel, is_active=True).select_related('floor').prefetch_related('beds') \
            .order_by('floor__floor_number', 'room_number'):
        key = room.floor.name if room.floor_id else 'Rooms'
        floors.setdefault(key, []).append((room, [(bed, occupants.get(bed.pk)) for bed in room.beds.all()]))
    return list(floors.items())


def unbedded(tenant):
    return BoarderProfile.objects.filter(tenant=tenant, bed__isnull=True, student_type__in=('BOARDER', 'WEEKLY')) \
        .exclude(status='CHECKED_OUT').select_related('student__class_assigned').order_by('student__full_name')


# ─────────────────────────────────────────────────────────────────────────────
# F322 / F327 / F336 — roll calls, prep and lights-out
# ─────────────────────────────────────────────────────────────────────────────

def in_sick_bay(student_ids):
    return set(SickBayVisit.objects.filter(student_id__in=student_ids, discharged_at__isnull=True)
               .values_list('student_id', flat=True))


def roll_call(hostel, kind, day, user):
    """The roll call for a hostel, kind and day, created with sensible starting marks."""
    rc, created = RollCall.objects.get_or_create(hostel=hostel, kind=kind, date=day,
                                                 defaults={'tenant_id': hostel.tenant_id, 'taken_by': user})
    boarders = boarders_in(hostel)
    sick = in_sick_bay([b.student_id for b in boarders])
    have = set(rc.marks.values_list('boarder_id', flat=True))
    for b in boarders:
        if b.pk not in have:
            status = 'EXEAT' if b.status == 'EXEAT' else 'SICK' if b.student_id in sick else 'PRESENT'
            RollCallMark.objects.create(tenant_id=hostel.tenant_id, roll_call=rc, boarder=b, status=status)
    return rc


def save_roll(rc, statuses, user, note=''):
    """statuses: {boarder_id: status}. Absences at night/lights-out alert the warden and admins."""
    valid = dict(RollCallMark.STATUSES)
    absent = []
    with transaction.atomic():
        for mark in rc.marks.select_related('boarder__student'):
            new = statuses.get(str(mark.boarder_id)) or statuses.get(mark.boarder_id)
            if new in valid and new != mark.status:
                mark.status = new
                mark.save(update_fields=['status'])
            if mark.status == 'ABSENT':
                absent.append(mark.boarder.student)
        rc.taken_by, rc.note = user, (note or '')[:255]
        rc.save(update_fields=['taken_by', 'note'])
    if absent and rc.kind in ('NIGHT', 'LIGHTS'):
        names = ', '.join(escape(s.full_name) for s in absent)
        for e in _managers(rc.hostel):
            _email(e, f'URGENT: {len(absent)} missing at {rc.get_kind_display().lower()} — {rc.hostel.name}',
                   f'<p>Not present at the {rc.get_kind_display().lower()} on {rc.date:%A %d %B}: <b>{names}</b>.</p>'
                   f'<p>Please check at once and contact the families if needed.</p>')
    return absent


def roll_summary(rc):
    counts = dict(rc.marks.values_list('status').annotate(n=Count('id')))
    return {k: counts.get(k, 0) for k, _ in RollCallMark.STATUSES}


def late_returns(tenant, days=30):
    """Exeats that came back after the agreed time (or are overdue now)."""
    since = timezone.now() - timedelta(days=days)
    out = []
    for m in CheckInOut.objects.filter(tenant=tenant, movement_type='EXEAT', datetime__gte=since,
                                       expected_return__isnull=False).select_related('boarder__student'):
        back = m.actual_return or (None if timezone.now() <= m.expected_return else timezone.now())
        if back and back > m.expected_return:
            late = back - m.expected_return
            out.append({'movement': m, 'student': m.boarder.student, 'late_by': late,
                        'hours': round(late.total_seconds() / 3600, 1), 'still_out': m.actual_return is None})
    return sorted(out, key=lambda r: -r['late_by'])


# ─────────────────────────────────────────────────────────────────────────────
# F323 — exeat requests
# ─────────────────────────────────────────────────────────────────────────────

def _boarder_hostel_staff(boarder):
    h = hostel_of(boarder)
    return _managers(h) if h else []


def request_exeat(boarder, user, leave_at, return_by, reason, pickup_person, pickup_phone='', by_parent=True):
    if not (leave_at and return_by) or return_by <= leave_at:
        raise BoardingError('Give when the child leaves and when they come back (after leaving).')
    if leave_at < timezone.now() - timedelta(hours=1):
        raise BoardingError('The leaving time has already passed.')
    if not (reason or '').strip() or not (pickup_person or '').strip():
        raise BoardingError('Say why, and who will collect the child.')
    if ExeatRequest.objects.filter(boarder=boarder, status__in=('PARENT', 'SCHOOL', 'APPROVED', 'OUT')).exists():
        raise BoardingError('There is already an exeat in progress for this child.')
    r = ExeatRequest.objects.create(
        tenant_id=boarder.tenant_id, boarder=boarder, leave_at=leave_at, return_by=return_by, reason=reason[:255],
        pickup_person=pickup_person[:160], pickup_phone=(pickup_phone or '')[:20], requested_by=user,
        status=ExeatRequest.PENDING_SCHOOL if by_parent else ExeatRequest.PENDING_PARENT,
        parent_approved_at=timezone.now() if by_parent else None, parent_approved_by=user if by_parent else None)
    st = boarder.student
    if by_parent:
        for e in _boarder_hostel_staff(boarder):
            _email(e, f'Exeat request: {st.full_name}',
                   f'<p>The family of {escape(st.full_name)} asked for an exeat from '
                   f'{timezone.localtime(leave_at):%a %d %b %H:%M} to {timezone.localtime(return_by):%a %d %b %H:%M}.'
                   f'</p><p>Reason: {escape(reason)}. Collected by {escape(pickup_person)}.</p>')
    else:
        _tell_family(st, f'Please approve an exeat for {st.full_name}',
                     f'<p>The school has arranged an exeat for <b>{escape(st.full_name)}</b> from '
                     f'{timezone.localtime(leave_at):%a %d %b %H:%M} to {timezone.localtime(return_by):%a %d %b %H:%M}'
                     f' ({escape(reason)}). Please approve it under Boarding on the parent portal.</p>')
    return r


def parent_approve(req, user, approve=True):
    if req.status != ExeatRequest.PENDING_PARENT:
        raise BoardingError('This request is not waiting for you.')
    if approve:
        req.status, req.parent_approved_at, req.parent_approved_by = ExeatRequest.PENDING_SCHOOL, timezone.now(), user
    else:
        req.status = ExeatRequest.DECLINED
    req.save()


def school_decide(req, user, approve, note=''):
    if req.status != ExeatRequest.PENDING_SCHOOL:
        raise BoardingError('This request is not waiting for the school.')
    req.status = ExeatRequest.APPROVED if approve else ExeatRequest.DECLINED
    req.school_decided_by, req.school_note = user, (note or '')[:255]
    req.save()
    st = req.boarder.student
    word = 'approved' if approve else 'not approved'
    _tell_family(st, f'Exeat {word}: {st.full_name}',
                 f'<p>The exeat for {escape(st.full_name)} ({timezone.localtime(req.leave_at):%a %d %b %H:%M}) was '
                 f'<b>{word}</b>.{" " + escape(note) if note else ""}</p>'
                 f'{"<p>Please bring ID when you collect your child.</p>" if approve else ""}')


def sign_out(req, user):
    from .services import grant_exeat
    if req.status != ExeatRequest.APPROVED:
        raise BoardingError('Only an approved exeat can be signed out.')
    req.movement = grant_exeat(req.boarder, req.return_by, req.reason, authorized_by=user, parent_consent=True)
    req.status = ExeatRequest.OUT
    req.save(update_fields=['movement', 'status'])
    st = req.boarder.student
    _tell_family(st, f'{st.full_name} has left school', f'<p>{escape(st.full_name)} left with '
                 f'{escape(req.pickup_person)} at {timezone.localtime():%H:%M}. Due back by '
                 f'{timezone.localtime(req.return_by):%a %d %b %H:%M}.</p>',
                 sms=f'{st.tenant.name}: {st.full_name} signed out with {req.pickup_person}. '
                     f'Due back {timezone.localtime(req.return_by):%a %d %b %H:%M}.')


def sign_in(req, user):
    from .services import record_return
    if req.status != ExeatRequest.OUT or not req.movement:
        raise BoardingError('This child is not signed out.')
    record_return(req.boarder, req.movement, performed_by=user)
    req.status = ExeatRequest.RETURNED
    req.save(update_fields=['status'])
    st = req.boarder.student
    _tell_family(st, f'{st.full_name} is back in school',
                 f'<p>{escape(st.full_name)} signed back in at {timezone.localtime():%H:%M}.</p>')


# ─────────────────────────────────────────────────────────────────────────────
# F328 — visiting days
# ─────────────────────────────────────────────────────────────────────────────

def slots(day):
    out, t = [], datetime.combine(day.date, day.start)
    stop = datetime.combine(day.date, day.end)
    taken = dict(day.bookings.values_list('slot').annotate(n=Count('id')))
    while t + timedelta(minutes=day.slot_minutes) <= stop:
        n = taken.get(t.time(), 0)
        out.append({'time': t.time(), 'left': max(0, day.families_per_slot - n)})
        t += timedelta(minutes=day.slot_minutes)
    return out


def book_visit(day, boarder, user, slot, visitors, names):
    if not day.open or day.date < timezone.localdate():
        raise BoardingError('Bookings for this visiting day are closed.')
    if not (names or '').strip():
        raise BoardingError('Give the names of the visitors.')
    try:
        visitors = max(1, min(6, int(visitors)))
    except (TypeError, ValueError):
        visitors = 1
    with transaction.atomic():
        VisitingDay.objects.select_for_update().get(pk=day.pk)
        match = next((s for s in slots(day) if s['time'] == slot), None)
        if not match:
            raise BoardingError('Choose one of the times.')
        if not match['left']:
            raise BoardingError('That time is full. Please choose another.')
        try:
            return VisitBooking.objects.create(tenant_id=day.tenant_id, day=day, boarder=boarder, parent=user,
                                               slot=slot, visitors=visitors, names=names.strip()[:255])
        except IntegrityError:
            raise BoardingError('You have already booked for this child. Cancel it to change the time.')


# ─────────────────────────────────────────────────────────────────────────────
# F329 — inspections
# ─────────────────────────────────────────────────────────────────────────────

CRITERIA = ['Cleanliness', 'Beds made', 'Lockers tidy', 'Floor swept', 'Safety (no hazards)']


def inspect(room, user, data, note=''):
    scores = {}
    for i, c in enumerate(CRITERIA):
        raw = str(data.get(f's{i}') or '')
        if raw not in ('1', '2', '3', '4', '5'):
            raise BoardingError(f'Score "{c}" from 1 to 5.')
        scores[c] = int(raw)
    total = Decimal(sum(scores.values()) * 100) / (5 * len(CRITERIA))
    return Inspection.objects.create(tenant_id=room.tenant_id, room=room, inspector=user, scores=scores,
                                     total=total.quantize(Decimal('0.1')), note=(note or '')[:255])


def room_league(hostel, days=90):
    since = timezone.localdate() - timedelta(days=days)
    return list(Room.objects.filter(hostel=hostel, is_active=True).annotate(
        avg=Avg('inspections__total', filter=Q(inspections__date__gte=since)),
        n=Count('inspections', filter=Q(inspections__date__gte=since))).filter(n__gt=0).order_by('-avg'))


# ─────────────────────────────────────────────────────────────────────────────
# F330 — sick bay
# ─────────────────────────────────────────────────────────────────────────────

def admit(student, user, complaint, temperature=None, treatment='', tell_parent=True):
    if not (complaint or '').strip():
        raise BoardingError('Say what the problem is.')
    if SickBayVisit.objects.filter(student=student, discharged_at__isnull=True).exists():
        raise BoardingError(f'{student.full_name} is already in the sick bay.')
    try:
        temp = Decimal(str(temperature)) if temperature not in (None, '') else None
    except Exception:
        raise BoardingError('Give the temperature as a number, e.g. 37.8.')
    v = SickBayVisit.objects.create(tenant_id=student.tenant_id, student=student, complaint=complaint.strip()[:255],
                                    temperature=temp, treatment=(treatment or '').strip(), nurse=user,
                                    parent_told=tell_parent)
    if tell_parent:
        _tell_family(student, f'{student.full_name} is in the sick bay',
                     f'<p>{escape(student.full_name)} came to the school sick bay at {timezone.localtime():%H:%M} '
                     f'({escape(complaint)}). The nurse is looking after them and will contact you if needed.</p>',
                     sms=f'{student.tenant.name}: {student.full_name} is in the sick bay ({complaint[:60]}). '
                         f'The nurse will call you if needed.')
    return v


def discharge(visit, outcome, treatment=''):
    if visit.discharged_at:
        raise BoardingError('Already discharged.')
    if outcome not in ('DORM', 'HOME', 'HOSPITAL'):
        raise BoardingError('Choose where the student went.')
    visit.outcome, visit.discharged_at = outcome, timezone.now()
    if treatment:
        visit.treatment = (visit.treatment + '\n' + treatment).strip()
    visit.save()
    if outcome in ('HOME', 'HOSPITAL'):
        st = visit.student
        _tell_family(st, f'{st.full_name}: {visit.get_outcome_display().lower()}',
                     f'<p>From the sick bay, {escape(st.full_name)} was {escape(visit.get_outcome_display().lower())}'
                     f'. Please contact the school for details.</p>')


# ─────────────────────────────────────────────────────────────────────────────
# F338 — room change requests
# ─────────────────────────────────────────────────────────────────────────────

def request_room_change(boarder, user, reason, preferred_room=None):
    if not (reason or '').strip():
        raise BoardingError('Say why a room change is needed.')
    if RoomChangeRequest.objects.filter(boarder=boarder, status='PENDING').exists():
        raise BoardingError('There is already a request waiting.')
    r = RoomChangeRequest.objects.create(tenant_id=boarder.tenant_id, boarder=boarder, requested_by=user,
                                         reason=reason.strip()[:2000], preferred_room=preferred_room)
    for e in _boarder_hostel_staff(boarder):
        _email(e, f'Room change request: {boarder.student.full_name}', f'<p>{escape(reason)}</p>')
    return r


def decide_room_change(req, user, approve, bed=None, note=''):
    from .services import assign_bed
    if req.status != 'PENDING':
        raise BoardingError('Already decided.')
    if approve:
        if not bed:
            raise BoardingError('Choose the new bed.')
        try:
            assign_bed(req.boarder, bed, performed_by=user)
        except ValueError as problem:
            raise BoardingError(str(problem))
    req.status = 'APPROVED' if approve else 'DECLINED'
    req.decided_by, req.note = user, (note or '')[:255]
    req.save()
    if req.requested_by and req.requested_by.email:
        _email(req.requested_by.email, f'Room change {req.get_status_display().lower()}',
               f'<p>The request for {escape(req.boarder.student.full_name)} was {req.get_status_display().lower()}.'
               f'{" New room: " + escape(str(bed.room)) if approve and bed else ""} {escape(note)}</p>')


# ─────────────────────────────────────────────────────────────────────────────
# F337 — housemaster reports
# ─────────────────────────────────────────────────────────────────────────────

def term_facts(boarder, term):
    start = term.start_date or (timezone.localdate() - timedelta(days=90))
    end = term.end_date or timezone.localdate()
    marks = RollCallMark.objects.filter(boarder=boarder, roll_call__date__range=(start, end))
    room = boarder.bed.room if boarder.bed_id else None
    return {
        'absent': marks.filter(status='ABSENT').count(), 'late': marks.filter(status='LATE').count(),
        'calls': marks.count(), 'sick': SickBayVisit.objects.filter(student=boarder.student,
                                                                 admitted_at__date__range=(start, end)).count(),
        'exeats': ExeatRequest.objects.filter(boarder=boarder, leave_at__date__range=(start, end),
                                              status__in=('OUT', 'RETURNED')).count(),
        'room_score': room.inspections.filter(date__range=(start, end)).aggregate(a=Avg('total'))['a'] if room else None,
    }


def save_report(boarder, term, user, data):
    vals = {}
    for f in ('conduct', 'neatness', 'relationships'):
        raw = str(data.get(f) or '')
        if raw not in ('1', '2', '3', '4', '5'):
            raise BoardingError('Rate each area from 1 to 5.')
        vals[f] = int(raw)
    rep, _ = BoardingReport.objects.update_or_create(
        boarder=boarder, term=term,
        defaults={'tenant_id': boarder.tenant_id, **vals, 'comment': (data.get('comment') or '').strip(),
                  'written_by': user, 'published': bool(data.get('published'))})
    return rep


# ─────────────────────────────────────────────────────────────────────────────
# F333 / F334 / F340 — menu, diets, night sheet
# ─────────────────────────────────────────────────────────────────────────────

def week_menu(hostel):
    rows = {}
    for m in MealPlan.objects.filter(hostel=hostel, is_active=True).order_by('day_of_week', 'time'):
        rows.setdefault(m.get_day_of_week_display(), []).append(m)
    return list(rows.items())


def diet_list(hostel):
    return [b for b in boarders_in(hostel) if b.diet != 'NONE' or b.diet_note]


def night_sheet(hostel):
    from users.models import Guardian
    rows = []
    for b in boarders_in(hostel):
        st = b.student
        guardians = list(Guardian.objects.filter(student=st, emergency_contact=True)) or \
            list(Guardian.objects.filter(student=st)[:2])
        rows.append({'b': b, 'student': st, 'guardians': guardians})
    return rows


def hostels(tenant):
    return Hostel.objects.filter(tenant=tenant, is_active=True).order_by('name')


def free_beds(tenant):
    return Bed.objects.filter(tenant=tenant, status='AVAILABLE', room__is_active=True) \
        .select_related('room__hostel').order_by('room__hostel__name', 'room__room_number', 'bed_number')
