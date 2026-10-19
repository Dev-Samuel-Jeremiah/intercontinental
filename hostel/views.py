"""hostel/views.py — Hostel Management Views"""

from users.models import ENROLLED_STATUSES
from decimal import Decimal, InvalidOperation
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.db.models import Q, Count, Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from academics.models import AcademicSession
from academics.utils import get_active_session, get_active_term
from users.models import Student

from hostel.models import (
    Bed, BoarderProfile, CheckInOut, Floor,
    HostelFeeStructure, HostelNotice, HostelTermBilling,
    Hostel, IncidentReport, MealAttendance, MealPlan, Room, VisitorLog,
)
from hostel.services import (
    assign_bed, generate_hostel_bills, get_hostel_summary,
    grant_exeat, pay_hostel_bill, record_return, unassign_bed,
)


# ─────────────────────────────────────────────────────────────────────────────
# Permission
# ─────────────────────────────────────────────────────────────────────────────

def _is_hostel_staff(user):
    """Administrators, Account Officers, hostel wardens, and anyone given the boarding switches on the Roles page."""
    from security.access import allowed
    if allowed(user, 'can_access_hostel', 'can_manage_boarders'):
        return True
    if not getattr(user, 'is_authenticated', False):
        return False
    from .models import Hostel
    return Hostel.objects.filter(warden=user).exists()


def _hostel_required(fn):
    def wrap(request, *a, **kw):
        if not request.user.is_authenticated or not _is_hostel_staff(request.user):
            if request.user.is_authenticated:
                from security.access import refused
                refused(request, 'can_access_hostel')
            return HttpResponse("Hostel staff access only.", status=403)
        return fn(request, *a, **kw)
    wrap.__name__ = fn.__name__
    return wrap


# ─────────────────────────────────────────────────────────────────────────────
# DASHBOARD
# ─────────────────────────────────────────────────────────────────────────────

@login_required
@_hostel_required
def dashboard(request):
    summary         = get_hostel_summary()
    hostels         = Hostel.objects.filter(is_active=True).prefetch_related('rooms')
    recent_checkins = CheckInOut.objects.select_related(
        'boarder__student', 'authorized_by'
    ).order_by('-datetime')[:10]
    notices         = HostelNotice.objects.filter(is_active=True).order_by('-created_at')[:5]
    open_incidents_qs = IncidentReport.objects.filter(
        status__in=['OPEN', 'UNDER_REVIEW']
    ).select_related('boarder__student').order_by('-incident_date')[:5]
    pending_visitors_qs = VisitorLog.objects.filter(
        status='PENDING'
    ).select_related('boarder__student').order_by('-created_at')[:5]
    overdue_exeats_qs = CheckInOut.objects.filter(
        movement_type='EXEAT',
        actual_return__isnull=True,
        expected_return__lt=timezone.now(),
    ).select_related('boarder__student')[:10]

    return render(request, 'hostel/dashboard.html', {
        **summary,
        'hostels':             hostels,
        'recent_checkins':     recent_checkins,
        'notices':             notices,
        'open_incidents_qs':   open_incidents_qs,
        'pending_visitors_qs': pending_visitors_qs,
        'overdue_exeats_qs':   overdue_exeats_qs,
    })


# ─────────────────────────────────────────────────────────────────────────────
# HOSTEL LIST & DETAIL
# ─────────────────────────────────────────────────────────────────────────────

@login_required
@_hostel_required
def hostel_list(request):
    hostels = Hostel.objects.filter(is_active=True).prefetch_related('rooms__beds')
    return render(request, 'hostel/hostel_list.html', {'hostels': hostels})


@login_required
@_hostel_required
def hostel_detail(request, hostel_id):
    hostel  = get_object_or_404(Hostel, id=hostel_id)
    rooms   = hostel.rooms.filter(is_active=True).prefetch_related('beds').order_by('room_number')
    boarders = BoarderProfile.objects.filter(
        bed__room__hostel=hostel, status='ACTIVE'
    ).select_related('student', 'bed__room')
    notices = HostelNotice.objects.filter(
        Q(hostel=hostel) | Q(hostel__isnull=True), is_active=True
    ).order_by('-created_at')[:5]

    return render(request, 'hostel/hostel_detail.html', {
        'hostel': hostel, 'rooms': rooms,
        'boarders': boarders, 'notices': notices,
    })


@login_required
@_hostel_required
def hostel_create_edit(request, hostel_id=None):
    hostel = get_object_or_404(Hostel, id=hostel_id) if hostel_id else None
    if request.method == 'POST':
        name        = request.POST.get('name', '').strip()
        gender      = request.POST.get('gender', 'MALE')
        description = request.POST.get('description', '').strip()
        warden_id   = request.POST.get('warden') or None
        # Only accept a warden from this school
        if warden_id and not get_user_model().objects.filter(
                pk=warden_id, tenant=request.tenant).exists():
            warden_id = None
        address     = request.POST.get('address', '').strip()

        dup = Hostel.objects.filter(name__iexact=name)
        if hostel:
            dup = dup.exclude(pk=hostel.pk)
        if dup.exists():
            messages.error(request, f'A hostel named "{name}" already exists.')
            return redirect(request.path)

        if hostel:
            hostel.name = name; hostel.gender = gender
            hostel.description = description; hostel.warden_id = warden_id
            hostel.address = address; hostel.save()
            messages.success(request, f'Hostel "{name}" updated.')
        else:
            hostel = Hostel.objects.create(
                name=name, gender=gender, description=description,
                warden_id=warden_id, address=address,
            )
            messages.success(request, f'Hostel "{name}" created.')
        return redirect('hostel:hostel_detail', hostel_id=hostel.id)

    from django.contrib.auth import get_user_model
    User = get_user_model()
    staff_users = User.objects.filter(tenant=request.tenant, is_staff_user=True).order_by('username')
    return render(request, 'hostel/hostel_form.html', {
        'hostel': hostel, 'staff_users': staff_users,
    })



# ─────────────────────────────────────────────────────────────────────────────
# FLOOR MANAGEMENT  (add / delete floors for a hostel)
# ─────────────────────────────────────────────────────────────────────────────

@login_required
@_hostel_required
@require_POST
def manage_floors(request, hostel_id):
    """Add or delete a floor for a hostel."""
    hostel = get_object_or_404(Hostel, id=hostel_id)
    action = request.POST.get('action', 'add')

    if action == 'add':
        name         = request.POST.get('floor_name', '').strip()
        floor_number = request.POST.get('floor_number', '0')
        try:
            floor_number = int(floor_number)
        except ValueError:
            floor_number = 0
        if name:
            Floor.objects.get_or_create(
                hostel=hostel, floor_number=floor_number,
                defaults={'name': name}
            )
            messages.success(request, f'Floor "{name}" added to {hostel.name}.')
        else:
            messages.error(request, 'Floor name is required.')

    elif action == 'delete':
        floor_id = request.POST.get('floor_id')
        Floor.objects.filter(id=floor_id, hostel=hostel).delete()
        messages.success(request, 'Floor deleted.')

    return redirect('hostel:hostel_detail', hostel_id=hostel.id)


# ─────────────────────────────────────────────────────────────────────────────
# ROOM MANAGEMENT
# ─────────────────────────────────────────────────────────────────────────────

@login_required
@_hostel_required
def room_create_edit(request, hostel_id, room_id=None):
    hostel = get_object_or_404(Hostel, id=hostel_id)
    room   = get_object_or_404(Room, id=room_id, hostel=hostel) if room_id else None

    if request.method == 'POST':
        room_number = request.POST.get('room_number', '').strip()
        room_type   = request.POST.get('room_type', 'DORMITORY')
        capacity    = int(request.POST.get('capacity', 4))
        floor_id    = request.POST.get('floor') or None
        has_ac      = request.POST.get('has_ac') == 'on'
        has_bath    = request.POST.get('has_bathroom') == 'on'
        description = request.POST.get('description', '').strip()

        if room:
            room.room_number = room_number; room.room_type = room_type
            room.capacity    = capacity;    room.floor_id  = floor_id
            room.has_ac      = has_ac;      room.has_bathroom = has_bath
            room.description = description; room.save()
            messages.success(request, f'Room {room_number} updated.')
        else:
            room = Room.objects.create(
                hostel=hostel, room_number=room_number, room_type=room_type,
                capacity=capacity, floor_id=floor_id, has_ac=has_ac,
                has_bathroom=has_bath, description=description,
            )
            # Auto-create beds
            for i in range(1, capacity + 1):
                Bed.objects.create(room=room, bed_number=f'B{i:02d}')
            messages.success(request, f'Room {room_number} created with {capacity} beds.')
        return redirect('hostel:hostel_detail', hostel_id=hostel.id)

    floors = hostel.floors.all()
    return render(request, 'hostel/room_form.html', {
        'hostel': hostel, 'room': room, 'floors': floors,
    })


# ─────────────────────────────────────────────────────────────────────────────
# BOARDER MANAGEMENT
# ─────────────────────────────────────────────────────────────────────────────

@login_required
@_hostel_required
def boarder_list(request):
    q          = request.GET.get('q', '')
    status_f   = request.GET.get('status', 'ACTIVE')
    type_f     = request.GET.get('type', '')
    hostel_f   = request.GET.get('hostel', '')

    boarders = BoarderProfile.objects.select_related(
        'student__class_assigned', 'bed__room__hostel'
    ).order_by('student__full_name')

    if q:
        boarders = boarders.filter(
            Q(student__full_name__icontains=q) |
            Q(student__admission_number__icontains=q)
        )
    if status_f:
        boarders = boarders.filter(status=status_f)
    if type_f:
        boarders = boarders.filter(student_type=type_f)
    if hostel_f:
        boarders = boarders.filter(bed__room__hostel_id=hostel_f)

    hostels = Hostel.objects.filter(is_active=True)
    return render(request, 'hostel/boarder_list.html', {
        'boarders': boarders, 'hostels': hostels,
        'q': q, 'status_f': status_f, 'type_f': type_f, 'hostel_f': hostel_f,
    })


@login_required
@_hostel_required
def boarder_profile(request, student_id):
    student  = get_object_or_404(Student, id=student_id)
    profile, _ = BoarderProfile.objects.get_or_create(
        student=student,
        defaults={'student_type': 'DAY'}
    )
    movements = profile.movements.all()[:20]
    billings  = profile.term_billings.all()
    incidents = profile.incidents.all()
    visitors  = profile.visitors.all()[:10]
    active_session = get_active_session()
    active_term    = get_active_term()
    current_billing = None
    if active_session and active_term:
        current_billing = billings.filter(
            session=active_session, term=active_term
        ).first()
    return render(request, 'hostel/boarder_profile.html', {
        'student':         student,
        'profile':         profile,
        'movements':       movements,
        'billings':        billings,
        'incidents':       incidents,
        'visitors':        visitors,
        'current_billing': current_billing,
        'active_session':  active_session,
        'active_term':     active_term,
    })


@login_required
@_hostel_required
@require_POST
def boarder_update(request, student_id):
    student  = get_object_or_404(Student, id=student_id)
    profile, _ = BoarderProfile.objects.get_or_create(student=student)
    profile.student_type       = request.POST.get('student_type', 'DAY')
    profile.emergency_name     = request.POST.get('emergency_name', '').strip()
    profile.emergency_phone    = request.POST.get('emergency_phone', '').strip()
    profile.emergency_rel      = request.POST.get('emergency_rel', '').strip()
    profile.medical_conditions = request.POST.get('medical_conditions', '').strip()
    profile.doctor_name        = request.POST.get('doctor_name', '').strip()
    profile.doctor_phone       = request.POST.get('doctor_phone', '').strip()
    profile.blood_group        = request.POST.get('blood_group', '').strip()
    profile.note               = request.POST.get('note', '').strip()

    session_id = request.POST.get('session_id') or None
    if session_id:
        profile.session_id = session_id
    profile.save()
    messages.success(request, f'{student.full_name} profile updated.')
    return redirect('hostel:boarder_profile', student_id=student.id)


# ─────────────────────────────────────────────────────────────────────────────
# BED ASSIGNMENT
# ─────────────────────────────────────────────────────────────────────────────

@login_required
@_hostel_required
def assign_bed_view(request):
    """Show available beds and assign a boarder."""
    if request.method == 'POST':
        student_id = request.POST.get('student_id')
        bed_id     = request.POST.get('bed_id')
        student    = get_object_or_404(Student, id=student_id)
        bed        = get_object_or_404(Bed, id=bed_id)
        profile, _ = BoarderProfile.objects.get_or_create(student=student)
        if profile.student_type == 'DAY':
            profile.student_type = 'BOARDER'
            profile.save(update_fields=['student_type'])
        try:
            assign_bed(profile, bed, performed_by=request.user)
            messages.success(request,
                f'✓ {student.full_name} assigned to '
                f'Room {bed.room.room_number}, Bed {bed.bed_number} '
                f'({bed.room.hostel.name}).')
        except ValueError as e:
            messages.error(request, str(e))
        return redirect('hostel:boarder_profile', student_id=student.id)

    students     = Student.objects.filter(status__in=ENROLLED_STATUSES).order_by('full_name')
    hostels      = Hostel.objects.filter(is_active=True).prefetch_related(
        'rooms__beds'
    )
    # Available beds grouped by hostel→room
    available_beds = Bed.objects.filter(
        status='AVAILABLE', room__is_active=True
    ).select_related('room__hostel', 'room__floor').order_by(
        'room__hostel__name', 'room__room_number', 'bed_number'
    )
    return render(request, 'hostel/assign_bed.html', {
        'students': students, 'hostels': hostels,
        'available_beds': available_beds,
    })


@login_required
@_hostel_required
@require_POST
def checkout_boarder(request, student_id):
    student = get_object_or_404(Student, id=student_id)
    profile = get_object_or_404(BoarderProfile, student=student)
    reason  = request.POST.get('reason', '').strip()
    try:
        unassign_bed(profile, reason=reason, performed_by=request.user)
        messages.success(request, f'{student.full_name} checked out.')
    except Exception as e:
        messages.error(request, str(e))
    return redirect('hostel:boarder_profile', student_id=student.id)


# ─────────────────────────────────────────────────────────────────────────────
# EXEAT / LEAVE
# ─────────────────────────────────────────────────────────────────────────────

@login_required
@_hostel_required
@require_POST
def grant_exeat_view(request, student_id):
    student  = get_object_or_404(Student, id=student_id)
    profile  = get_object_or_404(BoarderProfile, student=student)
    from datetime import datetime
    expected_str = request.POST.get('expected_return', '')
    try:
        expected = datetime.fromisoformat(expected_str)
    except ValueError:
        messages.error(request, 'Invalid return date/time.')
        return redirect('hostel:boarder_profile', student_id=student.id)
    reason  = request.POST.get('reason', '').strip()
    consent = request.POST.get('parent_consent') == 'on'
    grant_exeat(profile, expected, reason,
                authorized_by=request.user, parent_consent=consent)
    messages.success(request,
        f'Exeat granted to {student.full_name}. '
        f'Expected return: {expected:%d %b %Y %H:%M}.')
    return redirect('hostel:boarder_profile', student_id=student.id)


@login_required
@_hostel_required
@require_POST
def return_from_exeat(request, movement_id):
    movement = get_object_or_404(CheckInOut, id=movement_id, movement_type='EXEAT')
    record_return(movement.boarder, movement, performed_by=request.user)
    messages.success(request,
        f'{movement.boarder.student.full_name} recorded as returned.')
    return redirect('hostel:boarder_profile',
                    student_id=movement.boarder.student.id)


# ─────────────────────────────────────────────────────────────────────────────
# BILLING
# ─────────────────────────────────────────────────────────────────────────────

@login_required
@_hostel_required
def billing_overview(request):
    from academics.models import Term as T
    session_id = request.GET.get('session')
    term_id    = request.GET.get('term')
    active_session = get_active_session()
    active_term    = get_active_term()
    session = AcademicSession.objects.filter(id=session_id).first() if session_id else active_session
    term    = T.objects.filter(id=term_id).first() if term_id else active_term

    billings = HostelTermBilling.objects.filter(
        session=session, term=term
    ).select_related(
        'boarder__student__class_assigned',
        'boarder__bed__room__hostel'
    ).order_by('boarder__student__full_name') if session and term else []

    agg = (HostelTermBilling.objects.filter(session=session, term=term)
           .aggregate(expected=Sum('total_fee'), paid=Sum('amount_paid'))
           if session and term else {'expected': 0, 'paid': 0})

    sessions = AcademicSession.objects.all().order_by('-name')
    terms    = T.objects.all().order_by('-id')
    fee_structures = HostelFeeStructure.objects.filter(is_active=True).select_related('hostel')
    hostels  = Hostel.objects.filter(is_active=True)

    return render(request, 'hostel/billing.html', {
        'billings':       billings,
        'sel_session':    session,
        'sel_term':       term,
        'sessions':       sessions,
        'terms':          terms,
        'fee_structures': fee_structures,
        'hostels':        hostels,
        'total_expected': agg['expected'] or 0,
        'total_paid':     agg['paid']     or 0,
        'outstanding':    (agg['expected'] or 0) - (agg['paid'] or 0),
    })


@login_required
@_hostel_required
@require_POST
def generate_bills(request):
    from academics.models import Term as T
    term_id    = request.POST.get('term_id')
    session_id = request.POST.get('session_id')
    term    = get_object_or_404(T, id=term_id)
    session = get_object_or_404(AcademicSession, id=session_id)
    result  = generate_hostel_bills(term, session, performed_by=request.user)
    messages.success(request,
        f"Bills generated: {result['created']} new, "
        f"{result['skipped']} already existed.")
    return redirect(f'/hostel/billing/?session={session.id}&term={term.id}')


@login_required
@_hostel_required
@require_POST
def pay_bill_view(request, billing_id):
    billing = get_object_or_404(HostelTermBilling, id=billing_id)
    try:
        amount = Decimal(str(request.POST.get('amount', '0')))
    except InvalidOperation:
        messages.error(request, 'Invalid amount.')
        return redirect('hostel:billing')
    try:
        pay_hostel_bill(billing, amount, performed_by=request.user)
        messages.success(request,
            f'₦{amount:,.2f} paid for {billing.boarder.student.full_name}.')
    except ValueError as e:
        messages.error(request, str(e))
    return redirect('hostel:boarder_profile',
                    student_id=billing.boarder.student.id)


@login_required
@_hostel_required
@require_POST
def save_fee_structure(request):
    from academics.models import Term as T
    hostel_id  = request.POST.get('hostel') or None
    term_id    = request.POST.get('term')   or None
    session_id = request.POST.get('session') or None
    try:
        boarding = Decimal(request.POST.get('boarding_fee', '0'))
        meal     = Decimal(request.POST.get('meal_fee',     '0'))
        laundry  = Decimal(request.POST.get('laundry_fee',  '0'))
        other    = Decimal(request.POST.get('other_fee',    '0'))
    except InvalidOperation:
        messages.error(request, 'Invalid fee amount.')
        return redirect('hostel:billing')

    obj, created = HostelFeeStructure.objects.update_or_create(
        hostel_id=hostel_id, term_id=term_id, session_id=session_id,
        defaults=dict(boarding_fee=boarding, meal_fee=meal,
                      laundry_fee=laundry, other_fee=other, is_active=True,
                      description=request.POST.get('description', '').strip()),
    )
    messages.success(request,
        f'Fee structure {"created" if created else "updated"}.')
    return redirect('hostel:billing')


# ─────────────────────────────────────────────────────────────────────────────
# VISITOR LOG
# ─────────────────────────────────────────────────────────────────────────────

@login_required
@_hostel_required
def visitor_log(request):
    status_f = request.GET.get('status', '')
    visitors = VisitorLog.objects.select_related(
        'boarder__student', 'approved_by'
    ).order_by('-created_at')
    if status_f:
        visitors = visitors.filter(status=status_f)
    return render(request, 'hostel/visitors.html', {
        'visitors': visitors, 'status_f': status_f,
    })


@login_required
@_hostel_required
@require_POST
def log_visitor(request):
    boarder_id    = request.POST.get('boarder_id')
    profile = get_object_or_404(BoarderProfile, id=boarder_id)
    VisitorLog.objects.create(
        boarder=profile,
        visitor_name=request.POST.get('visitor_name', '').strip(),
        visitor_phone=request.POST.get('visitor_phone', '').strip(),
        relationship=request.POST.get('relationship', 'PARENT'),
        purpose=request.POST.get('purpose', '').strip(),
        id_type=request.POST.get('id_type', '').strip(),
        id_number=request.POST.get('id_number', '').strip(),
        visit_date=request.POST.get('visit_date') or timezone.now().date(),
    )
    messages.success(request, 'Visitor logged.')
    return redirect('hostel:visitors')


@login_required
@_hostel_required
@require_POST
def approve_visitor(request, visitor_id):
    v = get_object_or_404(VisitorLog, id=visitor_id)
    action = request.POST.get('action')
    if action == 'approve':
        v.status = 'APPROVED'; v.approved_by = request.user; v.save()
        messages.success(request, f'Visit approved for {v.visitor_name}.')
    elif action == 'deny':
        v.status = 'DENIED'; v.approved_by = request.user; v.save()
        messages.warning(request, f'Visit denied for {v.visitor_name}.')
    elif action == 'complete':
        v.status = 'COMPLETED'; v.save()
        messages.success(request, 'Visit marked complete.')
    return redirect('hostel:visitors')


# ─────────────────────────────────────────────────────────────────────────────
# INCIDENTS
# ─────────────────────────────────────────────────────────────────────────────

@login_required
@_hostel_required
def incidents(request):
    status_f   = request.GET.get('status', '')
    severity_f = request.GET.get('severity', '')
    qs = IncidentReport.objects.select_related(
        'boarder__student', 'reported_by'
    ).order_by('-incident_date')
    if status_f:   qs = qs.filter(status=status_f)
    if severity_f: qs = qs.filter(severity=severity_f)
    return render(request, 'hostel/incidents.html', {
        'incidents': qs, 'status_f': status_f, 'severity_f': severity_f,
    })


@login_required
@_hostel_required
@require_POST
def log_incident(request):
    boarder_id = request.POST.get('boarder_id')
    profile = get_object_or_404(BoarderProfile, id=boarder_id)
    IncidentReport.objects.create(
        boarder=profile,
        incident_type=request.POST.get('incident_type', 'OTHER'),
        severity=request.POST.get('severity', 'LOW'),
        title=request.POST.get('title', '').strip(),
        description=request.POST.get('description', '').strip(),
        location=request.POST.get('location', '').strip(),
        reported_by=request.user,
    )
    messages.success(request, 'Incident report logged.')
    return redirect('hostel:incidents')


@login_required
@_hostel_required
@require_POST
def resolve_incident(request, incident_id):
    inc = get_object_or_404(IncidentReport, id=incident_id)
    inc.status        = 'RESOLVED'
    inc.action_taken  = request.POST.get('action_taken', '').strip()
    inc.resolved_by   = request.user
    inc.resolved_at   = timezone.now()
    inc.save()
    messages.success(request, f'Incident "{inc.title}" resolved.')
    return redirect('hostel:incidents')


# ─────────────────────────────────────────────────────────────────────────────
# NOTICES
# ─────────────────────────────────────────────────────────────────────────────

@login_required
@_hostel_required
def notices(request):
    if request.method == 'POST':
        hostel_id = request.POST.get('hostel') or None
        HostelNotice.objects.create(
            hostel_id=hostel_id,
            title=request.POST.get('title', '').strip(),
            content=request.POST.get('content', '').strip(),
            priority=request.POST.get('priority', 'NORMAL'),
            expires_on=request.POST.get('expires_on') or None,
            posted_by=request.user,
        )
        messages.success(request, 'Notice posted.')
        return redirect('hostel:notices')

    all_notices = HostelNotice.objects.select_related('hostel').all()
    hostels     = Hostel.objects.filter(is_active=True)
    return render(request, 'hostel/notices.html', {
        'all_notices': all_notices, 'hostels': hostels,
    })


@login_required
@_hostel_required
@require_POST
def delete_notice(request, notice_id):
    HostelNotice.objects.filter(id=notice_id).update(is_active=False)
    messages.success(request, 'Notice removed.')
    return redirect('hostel:notices')


# ─────────────────────────────────────────────────────────────────────────────
# MEAL PLANS
# ─────────────────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────────────────────
# ADVANCED ANALYTICS — Hostel Reports & Stats
# ─────────────────────────────────────────────────────────────────────────────

@login_required
@_hostel_required
def analytics(request):
    """Advanced hostel analytics — occupancy trends, payment rates, incidents."""
    from django.db.models import Count, Q, Sum
    from academics.utils import get_active_session, get_active_term

    # Occupancy per hostel
    hostels = Hostel.objects.filter(is_active=True).prefetch_related('rooms__beds')
    hostel_stats = []
    for h in hostels:
        total = h.total_beds
        occ   = h.occupied_beds
        hostel_stats.append({
            'hostel':       h,
            'total':        total,
            'occupied':     occ,
            'available':    total - occ,
            'rate':         h.occupancy_rate,
        })

    # Boarder type breakdown
    type_breakdown = (
        BoarderProfile.objects.filter(status__in=['ACTIVE','EXEAT'])
        .values('student_type')
        .annotate(count=Count('id'))
    )

    # Billing collection rate by hostel
    billing_stats = []
    active_session = get_active_session()
    active_term    = get_active_term()
    if active_session and active_term:
        for h in hostels:
            agg = HostelTermBilling.objects.filter(
                boarder__bed__room__hostel=h,
                session=active_session, term=active_term,
            ).aggregate(expected=Sum('total_fee'), paid=Sum('amount_paid'))
            exp  = agg['expected'] or 0
            paid = agg['paid']     or 0
            billing_stats.append({
                'hostel':    h,
                'expected':  exp,
                'paid':      paid,
                'rate':      round((paid / exp) * 100, 1) if exp > 0 else 0,
            })

    # Incident severity breakdown (last 90 days)
    from django.utils import timezone as tz
    from datetime import timedelta
    ninety_days_ago = tz.now() - timedelta(days=90)
    incident_breakdown = (
        IncidentReport.objects
        .filter(incident_date__gte=ninety_days_ago)
        .values('severity')
        .annotate(count=Count('id'))
        .order_by('-count')
    )

    # Top overdue boarders (unpaid fees)
    unpaid_billings = HostelTermBilling.objects.filter(
        status__in=['UNPAID','PARTIAL'],
        session=active_session, term=active_term,
    ).select_related(
        'boarder__student', 'boarder__bed__room__hostel'
    ).order_by('-total_fee') if active_session and active_term else []

    # Movement counts last 30 days
    thirty_ago = tz.now() - timedelta(days=30)
    movement_counts = (
        CheckInOut.objects
        .filter(datetime__gte=thirty_ago)
        .values('movement_type')
        .annotate(count=Count('id'))
    )

    return render(request, 'hostel/analytics.html', {
        'hostel_stats':        hostel_stats,
        'type_breakdown':      list(type_breakdown),
        'billing_stats':       billing_stats,
        'incident_breakdown':  list(incident_breakdown),
        'unpaid_billings':     unpaid_billings[:20],
        'movement_counts':     list(movement_counts),
        'sel_session':         active_session,
        'sel_term':            active_term,
    })


@login_required
@_hostel_required  
def payment_reminders(request):
    """Show all boarders with unpaid/partial hostel bills for current term."""
    from academics.utils import get_active_session, get_active_term
    from django.db.models import Q

    active_session = get_active_session()
    active_term    = get_active_term()

    unpaid = HostelTermBilling.objects.filter(
        status__in=['UNPAID', 'PARTIAL'],
        session=active_session, term=active_term,
    ).select_related(
        'boarder__student__class_assigned',
        'boarder__bed__room__hostel',
    ).order_by('boarder__student__full_name') if active_session and active_term else []

    return render(request, 'hostel/payment_reminders.html', {
        'unpaid':       unpaid,
        'sel_session':  active_session,
        'sel_term':     active_term,
        'total_outstanding': sum(b.balance_due for b in unpaid),
    })


@login_required
@_hostel_required
def meal_plans(request):
    if request.method == 'POST':
        hostel_id = request.POST.get('hostel_id')
        try:
            from datetime import time as dt_time
            t_str = request.POST.get('time', '07:00')
            h, m  = t_str.split(':')
            meal_time = dt_time(int(h), int(m))
        except Exception:
            messages.error(request, 'Invalid time format.')
            return redirect('hostel:meal_plans')
        MealPlan.objects.update_or_create(
            hostel_id=hostel_id,
            meal_type=request.POST.get('meal_type', 'BREAKFAST'),
            day_of_week=int(request.POST.get('day_of_week', 0)),
            defaults=dict(
                menu=request.POST.get('menu', '').strip(),
                time=meal_time,
            ),
        )
        messages.success(request, 'Meal plan saved.')
        return redirect('hostel:meal_plans')

    hostels = Hostel.objects.filter(is_active=True)
    plans   = MealPlan.objects.select_related('hostel').filter(is_active=True)
    return render(request, 'hostel/meal_plans.html', {
        'plans': plans, 'hostels': hostels,
    })
