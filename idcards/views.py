from django.http import JsonResponse
from django.conf import settings
from users.models import ENROLLED_STATUSES
import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import HttpResponse, Http404
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.utils import timezone

from tenants.middleware import get_current_tenant
from users.models import Student, Staff, Class

from .models import IDCard, GateScanLog, StaffClockLog, ReceptionStation
from .services import record_gate_scan, record_staff_clock, get_or_create_student_card, get_or_create_staff_card
from .utils import qr_png_bytes, placeholder_png_bytes

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────────────
# permission helpers
# ────────────────────────────────────────────────────────────────────────────

def _is_admin(user):
    staff = user.staff
    return user.is_superuser or (staff is not None and staff.role == 'ADMIN')


def _can_manage_students(user):
    staff = user.staff
    return user.is_superuser or (staff is not None and (staff.role == 'ADMIN' or staff.can_manage_students))


def _can_operate_gate(user):
    staff = user.staff
    return user.is_superuser or (staff is not None and (staff.role == 'ADMIN' or staff.can_mark_attendance))


# ────────────────────────────────────────────────────────────────────────────
# card lookups / auto-provisioning
# ────────────────────────────────────────────────────────────────────────────

def _card_for_student(student):
    return get_or_create_student_card(student)


def _card_for_staff(staff):
    return get_or_create_staff_card(staff)


# ────────────────────────────────────────────────────────────────────────────
# QR image endpoint — embedded via <img> in the card / poster templates
# ────────────────────────────────────────────────────────────────────────────

@login_required
def qr_image(request, uid):
    card = get_object_or_404(IDCard, card_uid=uid)
    scan_url = request.build_absolute_uri(reverse('idcards:gate_scan', kwargs={'uid': card.card_uid}))
    try:
        png = qr_png_bytes(scan_url)
    except Exception:
        logger.exception("QR generation failed for card %s", uid)
        png = placeholder_png_bytes()
    return HttpResponse(png, content_type='image/png')


@login_required
def reception_qr_image(request, uid):
    station = get_object_or_404(ReceptionStation, station_uid=uid)
    scan_url = request.build_absolute_uri(reverse('idcards:reception_clock', kwargs={'uid': station.station_uid}))
    try:
        png = qr_png_bytes(scan_url, box_size=14)
    except Exception:
        logger.exception("QR generation failed for reception station %s", uid)
        png = placeholder_png_bytes()
    return HttpResponse(png, content_type='image/png')


# ────────────────────────────────────────────────────────────────────────────
# ID card display / print
# ────────────────────────────────────────────────────────────────────────────

@login_required
def my_id_card(request):
    user = request.user
    student = user.student
    staff = user.staff
    if student is not None:
        card = _card_for_student(student)
        return render(request, 'idcards/student_card.html',
                      {'card': card, 'student': student, 'tenant': get_current_tenant()})
    if staff is not None:
        card = _card_for_staff(staff)
        return render(request, 'idcards/staff_card.html',
                      {'card': card, 'staff': staff, 'tenant': get_current_tenant()})
    messages.error(request, "No ID card profile found for your account.")
    return redirect('dashboard:router')


@login_required
def print_student_card(request, student_id):
    if not _can_manage_students(request.user):
        messages.error(request, "You don't have permission to view student ID cards.")
        return redirect('dashboard:router')
    student = get_object_or_404(Student, id=student_id)
    card = _card_for_student(student)
    return render(request, 'idcards/student_card.html',
                  {'card': card, 'student': student, 'tenant': get_current_tenant(), 'is_admin_view': True})


@login_required
def print_staff_card(request, staff_id):
    if not _is_admin(request.user):
        messages.error(request, "You don't have permission to view staff ID cards.")
        return redirect('dashboard:router')
    staff = get_object_or_404(Staff, id=staff_id)
    card = _card_for_staff(staff)
    return render(request, 'idcards/staff_card.html',
                  {'card': card, 'staff': staff, 'tenant': get_current_tenant(), 'is_admin_view': True})


@login_required
def bulk_print_cards(request):
    if not _can_manage_students(request.user):
        messages.error(request, "You don't have permission to bulk-print ID cards.")
        return redirect('dashboard:router')

    classes = Class.objects.all().order_by('name')
    selected_class_id = request.GET.get('class_id')
    students = []
    if selected_class_id:
        students = Student.objects.filter(class_assigned_id=selected_class_id, status__in=ENROLLED_STATUSES).order_by('full_name')

    cards = [_card_for_student(s) for s in students]
    return render(request, 'idcards/bulk_print.html', {
        'classes': classes,
        'selected_class_id': selected_class_id,
        'cards': cards,
        'tenant': get_current_tenant(),
    })


@login_required
def id_card_directory(request):
    """Admin directory: search/filter every student or staff member and jump
    straight to their printable ID card."""
    if not _can_manage_students(request.user):
        messages.error(request, "You don't have permission to view the ID card directory.")
        return redirect('dashboard:router')

    tab = request.GET.get('tab', 'students')
    q = request.GET.get('q', '').strip()

    students_page = None
    staff_page = None
    classes = Class.objects.all().order_by('name')
    selected_class_id = request.GET.get('class_id', '')
    selected_role = request.GET.get('role', '')

    if tab == 'staff':
        if not _is_admin(request.user):
            messages.error(request, "You don't have permission to view the staff directory.")
            return redirect('idcards:id_card_directory')
        staff_qs = Staff.objects.select_related('user').all().order_by('full_name')
        if q:
            staff_qs = staff_qs.filter(full_name__icontains=q)
        if selected_role:
            staff_qs = staff_qs.filter(role=selected_role)
        staff_page = Paginator(staff_qs, 25).get_page(request.GET.get('page'))
    else:
        tab = 'students'
        student_qs = Student.objects.select_related('class_assigned').filter(status__in=ENROLLED_STATUSES).order_by('full_name')
        if q:
            student_qs = student_qs.filter(
                Q(full_name__icontains=q) | Q(admission_number__icontains=q)
            )
        if selected_class_id:
            student_qs = student_qs.filter(class_assigned_id=selected_class_id)
        students_page = Paginator(student_qs, 25).get_page(request.GET.get('page'))

    return render(request, 'idcards/directory.html', {
        'tab': tab,
        'q': q,
        'classes': classes,
        'selected_class_id': selected_class_id,
        'selected_role': selected_role,
        'role_choices': Staff.ROLE_CHOICES,
        'students_page': students_page,
        'staff_page': staff_page,
        'is_full_admin': _is_admin(request.user),
    })


@login_required
def reissue_card(request, uid):
    if not _can_manage_students(request.user):
        messages.error(request, "You don't have permission to reissue ID cards.")
        return redirect('dashboard:router')
    card = get_object_or_404(IDCard, card_uid=uid)
    card.reissue()
    messages.success(request, f"A new QR code has been issued for {card.holder_name}. The old card is now invalid.")
    if card.card_type == 'STUDENT':
        return redirect('idcards:print_student_card', student_id=card.student_id)
    return redirect('idcards:print_staff_card', staff_id=card.staff_id)


# ────────────────────────────────────────────────────────────────────────────
# gate scan — a staff member (with a scanner / phone) scans a student's card
# ────────────────────────────────────────────────────────────────────────────

@login_required
def gate_scan(request, uid):
    if request.GET.get('format') == 'json':
        problem = _gate_problem(request, uid)
        if problem:
            return JsonResponse({'ok': False, 'message': problem})
    if not _can_operate_gate(request.user):
        return render(request, 'idcards/scan_error.html',
                       {'message': "Your account isn't authorised to operate the gate scanner. "
                                   "Ask an administrator to enable 'Can Mark Attendance' for your staff profile."})

    try:
        card = IDCard.objects.get(card_uid=uid, card_type='STUDENT')
    except IDCard.DoesNotExist:
        return render(request, 'idcards/scan_error.html',
                       {'message': "This QR code doesn't match any student ID card on file."})

    if not card.is_active:
        return render(request, 'idcards/scan_error.html',
                       {'message': f"This ID card has been deactivated. Please issue a new card for {card.holder_name}."})

    log = record_gate_scan(card, operator_user=request.user)
    if request.GET.get('format') == 'json':       # the in-portal scanner keeps scanning and shows this
        return JsonResponse(_scan_json(log, card.holder_name, 'Gate'))
    return render(request, 'idcards/gate_scan_result.html', {'log': log, 'card': card})


def _gate_problem(request, uid):
    if not _can_operate_gate(request.user):
        return "Your account can't record gate scans. An administrator can switch on 'Can Mark Attendance' for you."
    card = IDCard.objects.filter(card_uid=uid, card_type='STUDENT').first()
    if card is None:
        return "This code doesn't match any student ID card."
    if not card.is_active:
        return f'This card has been deactivated. Issue a new card for {card.holder_name}.'
    return ''


@login_required
def gate_scan_log(request):
    if not _can_operate_gate(request.user):
        messages.error(request, "You don't have permission to view the gate scan log.")
        return redirect('dashboard:router')

    logs = GateScanLog.objects.select_related('student', 'student__class_assigned').all()

    class_id = request.GET.get('class_id')
    date_str = request.GET.get('date')
    direction = request.GET.get('direction')

    if class_id:
        logs = logs.filter(student__class_assigned_id=class_id)
    if date_str:
        logs = logs.filter(scanned_at__date=date_str)
    else:
        logs = logs.filter(scanned_at__date=timezone.localdate())
    if direction in ('IN', 'OUT'):
        logs = logs.filter(direction=direction)

    paginator = Paginator(logs, 50)
    page_obj = paginator.get_page(request.GET.get('page'))

    return render(request, 'idcards/gate_scan_log.html', {
        'page_obj': page_obj,
        'classes': Class.objects.all().order_by('name'),
        'selected_class_id': class_id,
        'selected_date': date_str or timezone.localdate().isoformat(),
        'selected_direction': direction or '',
    })


# ────────────────────────────────────────────────────────────────────────────
# staff reception clock in/out — one shared QR, scanned by each staff member
# ────────────────────────────────────────────────────────────────────────────

@login_required
def reception_clock(request, uid):
    as_json = request.GET.get('format') == 'json'           # the in-portal scanner: always answer with a reason
    station = ReceptionStation.objects.filter(station_uid=uid, is_active=True).first()
    if station is None:
        if as_json:
            return JsonResponse({'ok': False, 'message': 'This clock-in code is old or switched off. '
                                                         'Print the current Clock-in QR poster.'})
        raise Http404
    staff = request.user.staff
    if staff is None:
        if request.user.is_superuser:
            message = ("The main administrator account has no staff record, so it can't clock in or out. "
                       "Sign in with your own staff account to clock in.")
        else:
            message = "Only staff can clock in or out. Students and parents should not scan this code."
        if as_json:
            return JsonResponse({'ok': False, 'message': message})
        return render(request, 'idcards/scan_error.html', {'message': message})
    from safety.staff_attendance import ClockClosed
    try:
        log = record_staff_clock(staff, station=station)
    except ClockClosed as closed:
        if request.GET.get('format') == 'json':
            return JsonResponse({'ok': False, 'message': str(closed)})
        return render(request, 'idcards/scan_error.html', {'message': str(closed)})
    if request.GET.get('format') == 'json':
        return JsonResponse(_scan_json(log, staff.full_name, 'Staff clock'))
    return render(request, 'idcards/reception_clock_result.html', {'log': log, 'staff': staff})


def _scan_json(log, name, kind):
    when = timezone.localtime(log.scanned_at)
    word = {'IN': 'in', 'OUT': 'out'}.get(log.direction, log.direction.lower())
    repeat = bool(getattr(log, 'repeat', False))
    return {'ok': True, 'kind': kind, 'name': name, 'direction': log.direction, 'time': when.strftime('%H:%M'),
            'repeat': repeat,
            'message': (f'{name} was already scanned {word} at {when:%H:%M}' if repeat
                        else f'{name} — {word} at {when:%H:%M}')}


@login_required
def scanner(request):
    """The camera scanner opened from the top bar: student ID cards at the gate, the reception code to clock
    in or out, and any other code from this portal (e.g. a report card's verification code)."""
    if request.user.staff is None and not request.user.is_superuser:
        messages.error(request, 'The scanner is for staff.')
        return redirect('dashboard:router')
    return render(request, 'idcards/scanner.html', {'can_gate': _can_operate_gate(request.user),
                                                    'site_url': settings.SITE_URL})


@login_required
def reception_qr_page(request):
    if not _is_admin(request.user):
        messages.error(request, "You don't have permission to view the reception QR code.")
        return redirect('dashboard:router')
    tenant = get_current_tenant()
    station = ReceptionStation.get_or_create_default(tenant)
    return render(request, 'idcards/reception_qr_page.html', {'station': station, 'tenant': tenant})


@login_required
def staff_attendance_log(request):
    if not _is_admin(request.user):
        messages.error(request, "You don't have permission to view staff attendance.")
        return redirect('dashboard:router')

    logs = StaffClockLog.objects.select_related('staff').all()
    date_str = request.GET.get('date')
    if date_str:
        logs = logs.filter(scanned_at__date=date_str)
    else:
        logs = logs.filter(scanned_at__date=timezone.localdate())

    paginator = Paginator(logs, 50)
    page_obj = paginator.get_page(request.GET.get('page'))

    return render(request, 'idcards/staff_attendance_log.html', {
        'page_obj': page_obj,
        'selected_date': date_str or timezone.localdate().isoformat(),
    })
