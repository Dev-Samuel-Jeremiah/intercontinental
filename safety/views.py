"""
safety/views.py
───────────────
The gate pickup desk, pickup log, emergency roll call, lost ID cards, the
school's safety settings, and the parent's pickup codes.
"""
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from idcards.models import IDCard
from users.models import ENROLLED_STATUSES, Attendance, Staff, Student
from users.views_profile import can_manage, can_view

from . import services as svc
from .models import LostCardReport, PickupPass, PickupRelease, RollCall, RollCallEntry


def _gate(user):
    from idcards.views import _can_operate_gate
    return user.is_authenticated and _can_operate_gate(user)


def _deny(request, who):
    messages.error(request, f'Only {who}.')
    return redirect('dashboard:router')


# ─────────────────────────────────────────────────────────────────────────────
# Pickup desk
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def pickup_desk(request):
    if not _gate(request.user):
        return _deny(request, "gate staff and administrators can release children "
                              "(ask an administrator to allow 'Can Mark Attendance')")
    if request.method == 'POST':
        student = get_object_or_404(Student, pk=request.POST.get('student_id'))
        try:
            r = svc.release(student, request.user,
                            collector=request.POST.get('collector') or '',
                            code=request.POST.get('code') or '',
                            override_reason=request.POST.get('override_reason') or '',
                            other_name=request.POST.get('other_name') or '',
                            relationship=request.POST.get('relationship') or '',
                            reason=request.POST.get('reason') or '',
                            can_override=can_manage(request.user))
            messages.success(request, f'{student.full_name} released to {r.collector_name}'
                             + (' (early).' if r.early else '.') + ' The family is being told.')
            return redirect('safety:pickup_desk')
        except svc.SafetyError as problem:
            messages.error(request, str(problem))
            return redirect(reverse('safety:pickup_desk') + f'?student={student.id}')

    q = (request.GET.get('q') or '').strip()
    results = []
    if len(q) >= 2:
        results = (Student.objects.filter(status__in=ENROLLED_STATUSES)
                   .filter(Q(full_name__icontains=q) | Q(admission_number__icontains=q))
                   .select_related('class_assigned').order_by('full_name')[:12])
    student = Student.objects.filter(pk=request.GET.get('student') or 0,
                                     status__in=ENROLLED_STATUSES).select_related('class_assigned').first()
    context = {'q': q, 'results': results, 'student': student, 'can_override': can_manage(request.user),
               'today_releases': PickupRelease.objects.filter(released_at__date=timezone.localdate())
               .select_related('student')[:12]}
    if student:
        today = timezone.localdate()
        context.update({
            'collectors': svc.collectors(student),
            'passes': svc._today_passes(student).filter(status=PickupPass.ISSUED),
            'locked': svc.code_pickups_locked(student),
            'mark': Attendance.objects.filter(student=student, date=today, subject__isnull=True).first(),
            'already': PickupRelease.objects.filter(student=student, released_at__date=today).first(),
            'early_now': svc.is_early(request.tenant),
        })
    return render(request, 'safety/pickup_desk.html', context)


@login_required
def pickup_log(request):
    if not can_view(request.user):
        return _deny(request, 'staff can see the pickup log')
    try:
        day = date.fromisoformat(request.GET.get('date') or '')
    except ValueError:
        day = timezone.localdate()
    rows = (PickupRelease.objects.filter(released_at__date=day)
            .select_related('student', 'student__class_assigned', 'released_by'))
    return render(request, 'safety/pickup_log.html', {
        'rows': rows, 'day': day, 'early': sum(1 for r in rows if r.early),
        'overrides': sum(1 for r in rows if r.checked_by == 'OVERRIDE'),
    })


# ─────────────────────────────────────────────────────────────────────────────
# Roll call
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def roll_calls(request):
    if not can_view(request.user):
        return _deny(request, 'staff can run a roll call')
    if request.method == 'POST':
        try:
            roll = svc.start_roll_call(request.tenant, request.POST.get('title'), request.POST.get('kind'),
                                       request.POST.get('assembly_point'), request.user)
            return redirect('safety:roll_call', roll_id=roll.id)
        except svc.SafetyError as problem:
            messages.error(request, str(problem))
            return redirect('safety:roll_calls')
    open_roll = RollCall.objects.filter(ended_at__isnull=True).first()
    return render(request, 'safety/roll_calls.html', {
        'open_roll': open_roll, 'history': RollCall.objects.filter(ended_at__isnull=False)[:20]})


@login_required
def roll_call(request, roll_id):
    if not can_view(request.user):
        return _deny(request, 'staff can run a roll call')
    roll = get_object_or_404(RollCall, pk=roll_id)
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action == 'mark':
                kind, pk = request.POST.get('type'), request.POST.get('id')
                student = Student.objects.filter(pk=pk).first() if kind == 'student' else None
                staff = Staff.objects.filter(pk=pk).first() if kind == 'staff' else None
                if not (student or staff):
                    raise svc.SafetyError('That person could not be found.')
                svc.mark(roll, request.user, student=student, staff=staff,
                         status=request.POST.get('status') or 'SAFE', note=request.POST.get('note') or '')
                if request.headers.get('x-requested-with') == 'fetch':
                    return JsonResponse({'ok': True, **_summary(roll)})
            elif action == 'end':
                svc.end_roll_call(roll, request.POST.get('note'))
                messages.success(request, 'Roll call ended and saved.')
        except svc.SafetyError as problem:
            if request.headers.get('x-requested-with') == 'fetch':
                return JsonResponse({'ok': False, 'error': str(problem)}, status=400)
            messages.error(request, str(problem))
        return redirect('safety:roll_call', roll_id=roll.id)
    if request.GET.get('format') == 'json':
        return JsonResponse(_summary(roll, people=True))
    status = svc.roll_status(roll)
    return render(request, 'safety/roll_call.html', {'roll': roll, **status})


def _summary(roll, people=False):
    s = svc.roll_status(roll)
    out = {'counts': s['counts'], 'total': s['total'], 'elapsed': s['elapsed'], 'open': roll.is_open}
    if people:
        out['people'] = [{k: p[k] for k in ('type', 'id', 'status')} for p in s['people']]
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Lost ID cards
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def lost_cards(request):
    if not can_view(request.user):
        return _deny(request, 'staff can manage lost ID cards')
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action == 'report':
                student = get_object_or_404(Student, pk=request.POST.get('student_id'))
                card = IDCard.objects.filter(student=student, card_type='STUDENT').first()
                if not card:
                    raise svc.SafetyError(f'{student.full_name} has no ID card on record yet.')
                svc.report_lost(card, request.user, request.POST.get('note'))
                messages.success(request, f"{student.full_name}'s card is switched off — it no longer "
                                          f"opens the gate.")
            elif action == 'reissue':
                if not can_manage(request.user):
                    return _deny(request, 'a school administrator can issue a new card')
                report = get_object_or_404(LostCardReport, pk=request.POST.get('report_id'))
                svc.reissue(report, request.user, request.POST.get('fee_mode'))
                messages.success(request, f'New card ready for {report.card.holder_name} — print it now.')
                if report.card.card_type == 'STUDENT':
                    return redirect('idcards:print_student_card', student_id=report.card.student_id)
            elif action == 'fee_paid':
                report = get_object_or_404(LostCardReport, pk=request.POST.get('report_id'), fee_status='OWED')
                report.fee_status = 'CASH'
                report.save(update_fields=['fee_status'])
                messages.success(request, 'Fee marked as paid.')
        except svc.SafetyError as problem:
            messages.error(request, str(problem))
        return redirect('safety:lost_cards')
    return render(request, 'safety/lost_cards.html', {
        'reports': LostCardReport.objects.select_related('card', 'card__student', 'card__staff',
                                                         'reported_by')[:100],
        'students': Student.objects.filter(status__in=ENROLLED_STATUSES, id_card__isnull=False)
        .order_by('full_name').only('id', 'full_name', 'admission_number'),
        'fee': svc.settings_for(request.tenant).card_replacement_fee, 'can_manage': can_manage(request.user),
    })


@login_required
def safety_settings(request):
    if not can_manage(request.user):
        return _deny(request, 'a school administrator can change these settings')
    cfg = svc.settings_for(request.tenant)
    if request.method == 'POST':
        try:
            hh, mm = (int(x) for x in (request.POST.get('school_ends_at') or '14:00').split(':')[:2])
            cfg.school_ends_at = time(hh, mm)
            cfg.pass_valid_hours = max(1, min(24, int(request.POST.get('pass_valid_hours') or 6)))
            fee = Decimal(str(request.POST.get('card_replacement_fee') or '0'))
            if fee < 0:
                raise ValueError
            cfg.card_replacement_fee = fee
        except (ValueError, TypeError, InvalidOperation):
            messages.error(request, 'Check the time, hours and fee.')
            return redirect('safety:settings')
        cfg.notify_parent_on_pickup = bool(request.POST.get('notify_parent_on_pickup'))
        cfg.phone_clock_in = bool(request.POST.get('phone_clock_in'))
        try:
            lat, lng = request.POST.get('school_latitude'), request.POST.get('school_longitude')
            cfg.school_latitude = Decimal(lat).quantize(Decimal('0.000001')) if lat else None
            cfg.school_longitude = Decimal(lng).quantize(Decimal('0.000001')) if lng else None
            if (cfg.school_latitude is not None and not -90 <= cfg.school_latitude <= 90) or \
                    (cfg.school_longitude is not None and not -180 <= cfg.school_longitude <= 180):
                raise ValueError
            cfg.clock_radius_m = max(30, min(2000, int(request.POST.get('clock_radius_m') or 150)))
        except (ValueError, InvalidOperation):
            messages.error(request, "Check the school's location and radius.")
            return redirect('safety:settings')
        if cfg.phone_clock_in and (cfg.school_latitude is None or cfg.school_longitude is None):
            messages.error(request, "Set the school's location before switching on phone clock-in.")
            return redirect('safety:settings')
        cfg.save()
        messages.success(request, 'Saved.')
        return redirect('safety:settings')
    return render(request, 'safety/settings.html', {'cfg': cfg})


# ─────────────────────────────────────────────────────────────────────────────
# Parents
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def parent_pickup(request):
    from users.views_achievements import _parent_children
    children = list(_parent_children(request))
    if not children:
        messages.error(request, 'This page is for parents.')
        return redirect('dashboard:router')
    if request.method == 'POST':
        action = request.POST.get('action')
        child = next((c for c in children if str(c.id) == request.POST.get('child_id')), None)
        try:
            if action == 'cancel':
                p = get_object_or_404(PickupPass, pk=request.POST.get('pass_id'), student__in=children)
                svc.cancel_pass(p)
                messages.success(request, 'Code cancelled.')
            elif not child:
                raise svc.SafetyError('Choose one of your children.')
            elif action == 'lost_card':
                card = IDCard.objects.filter(student=child, card_type='STUDENT').first()
                if not card:
                    raise svc.SafetyError(f'{child.full_name} has no ID card on record.')
                svc.report_lost(card, request.user, 'Reported by parent')
                messages.success(request, f"{child.full_name}'s lost card is switched off. "
                                          f"The school will issue a new one.")
            else:
                try:
                    valid_on = date.fromisoformat(request.POST.get('valid_on') or '')
                except ValueError:
                    valid_on = timezone.localdate()
                p = svc.issue_pass(child, request.POST.get('collector_name'),
                                   collector_phone=request.POST.get('collector_phone') or '',
                                   relationship=request.POST.get('relationship') or '',
                                   reason=request.POST.get('reason') or '', valid_on=valid_on,
                                   user=request.user)
                messages.success(request, f'Pickup code {p.code} for {p.collector_name}. '
                                          + ('We have texted it to them.' if p.collector_phone else
                                             'Give it to them — they show it at the gate.'))
        except svc.SafetyError as problem:
            messages.error(request, str(problem))
        return redirect('safety:parent_pickup')
    today = timezone.localdate()
    return render(request, 'parents/pickup.html', {
        'children': children,
        'passes': PickupPass.objects.filter(student__in=children, valid_on__gte=today - timedelta(days=7))
        .select_related('student')[:20],
        'releases': PickupRelease.objects.filter(student__in=children).select_related('student')[:10],
        'today': today, 'latest': today + timedelta(days=7),
        'cards': {c.id: IDCard.objects.filter(student=c, card_type='STUDENT').first() for c in children},
    })


# ─────────────────────────────────────────────────────────────────────────────
# Visitors
# ─────────────────────────────────────────────────────────────────────────────

from . import staff_attendance as sa, visitors as vis  # noqa: E402
from .models import StaffLeave, Visit  # noqa: E402


def _staff_choice(raw):
    return Staff.objects.filter(pk=raw or 0).first()


@login_required
def visitor_desk(request):
    if not can_view(request.user):
        return _deny(request, 'staff can use the visitor desk')
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action in ('walk_in', 'expect'):
                student = Student.objects.filter(pk=request.POST.get('student_id') or 0).first()
                host = _staff_choice(request.POST.get('host_id'))
                if action == 'walk_in':
                    v = vis.walk_in(request.tenant, request.POST, request.user, host=host, student=student,
                                    id_type=request.POST.get('id_type') or '',
                                    id_number=request.POST.get('id_number') or '',
                                    photo=request.FILES.get('photo'))
                    messages.success(request, f'{v.name} checked in — badge {v.badge_number}.')
                    return redirect('safety:visitor_badge', visit_id=v.id)
                try:
                    day = date.fromisoformat(request.POST.get('expected_on') or '')
                except ValueError:
                    day = timezone.localdate()
                try:
                    hh, mm = (int(x) for x in (request.POST.get('expected_time') or '').split(':')[:2])
                    at_time = time(hh, mm)
                except (ValueError, TypeError):
                    at_time = None
                v = vis.pre_register(request.tenant, request.POST, host=host or getattr(request.user, 'staff', None),
                                     student=student, expected_on=day, expected_time=at_time, user=request.user)
                messages.success(request, f'{v.name} is expected {v.expected_on:%d %b}.'
                                 + (' Their pass has been sent.' if (v.phone or v.email) else ''))
            else:
                v = get_object_or_404(Visit, pk=request.POST.get('visit_id'))
                if action == 'check_in':
                    vis.check_in(v, request.user, id_type=request.POST.get('id_type') or '',
                                 id_number=request.POST.get('id_number') or '', photo=request.FILES.get('photo'))
                    messages.success(request, f'{v.name} checked in — badge {v.badge_number}.')
                    return redirect('safety:visitor_badge', visit_id=v.id)
                if action == 'check_out':
                    vis.check_out(v, request.user)
                    messages.success(request, f'{v.name} checked out.')
                elif action == 'deny':
                    vis.turn_away(v, request.user, request.POST.get('reason') or '')
                    messages.success(request, f'{v.name} turned away.')
                elif action == 'cancel' and v.status == Visit.EXPECTED:
                    v.status = Visit.CANCELLED
                    v.save(update_fields=['status'])
                    messages.success(request, 'Visit cancelled.')
        except vis.VisitError as problem:
            messages.error(request, str(problem))
        return redirect('safety:visitor_desk')

    today = timezone.localdate()
    q = (request.GET.get('q') or '').strip()
    history = Visit.objects.select_related('host')
    if q:
        history = history.filter(Q(name__icontains=q) | Q(organisation__icontains=q) | Q(phone__icontains=q))
    return render(request, 'safety/visitors.html', {
        'expected': Visit.objects.filter(expected_on=today, status=Visit.EXPECTED).select_related('host', 'student'),
        'on_site': Visit.objects.filter(status=Visit.ON_SITE).select_related('host', 'student').order_by('checked_in_at'),
        'upcoming': Visit.objects.filter(expected_on__gt=today, status=Visit.EXPECTED).select_related('host')[:20],
        'history': history.exclude(status=Visit.EXPECTED)[:40] if q else Visit.objects.filter(
            checked_in_at__date=today, status=Visit.LEFT).select_related('host'),
        'q': q, 'purposes': Visit.PURPOSES, 'staff': Staff.objects.order_by('full_name'),
        'students': Student.objects.filter(status__in=ENROLLED_STATUSES).order_by('full_name')
        .only('id', 'full_name', 'admission_number'),
        'today': today, 'closing': svc.settings_for(request.tenant).school_ends_at,
    })


@login_required
def visitor_scan(request, token):
    """Reception scans a visitor's QR pass: show the visit and check them in or out."""
    if not can_view(request.user):
        return _deny(request, 'staff can scan visitor passes')
    v = Visit.objects.filter(token=token).first()
    if not v:
        messages.error(request, "That pass isn't for this school, or it has been cancelled.")
        return redirect('safety:visitor_desk')
    return render(request, 'safety/visitor_scan.html', {'v': v, 'today': timezone.localdate()})


@login_required
def visitor_badge(request, visit_id):
    if not can_view(request.user):
        return _deny(request, 'staff can print visitor badges')
    v = get_object_or_404(Visit.objects.select_related('host', 'student'), pk=visit_id)
    return render(request, 'safety/visitor_badge.html', {'v': v, 'school': request.tenant})


@login_required
def visitor_photo(request, visit_id):
    from django.http import FileResponse, Http404
    if not can_view(request.user):
        raise Http404
    v = get_object_or_404(Visit, pk=visit_id)
    if not v.photo:
        raise Http404
    response = FileResponse(v.photo.open('rb'), content_type='image/jpeg')
    response['Cache-Control'] = 'private, no-store'
    return response


def visit_pass(request, token):
    """Public: the pass a visitor shows at the gate. Holds only what the gate needs."""
    v = Visit.objects.filter(token=token).select_related('host').first() if getattr(request, 'tenant', None) else None
    if not v or v.status in (Visit.CANCELLED, Visit.DENIED):
        return render(request, 'safety/visit_pass.html', {'v': None, 'school': getattr(request, 'tenant', None)},
                      status=404)
    return render(request, 'safety/visit_pass.html', {'v': v, 'school': request.tenant})


def visit_qr(request, token):
    from django.http import Http404, HttpResponse
    from idcards.utils import qr_png_bytes
    if not Visit.objects.filter(token=token).exists():
        raise Http404
    url = request.build_absolute_uri(reverse('safety:visitor_scan', args=[token]))
    return HttpResponse(qr_png_bytes(url), content_type='image/png')


# ─────────────────────────────────────────────────────────────────────────────
# Staff leave and attendance
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def staff_leave(request):
    me = getattr(request.user, 'staff', None)
    manage = can_manage(request.user)
    if not (me or manage):
        return _deny(request, 'staff can request leave')
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action == 'request':
                staff = me
                if manage and request.POST.get('staff_id'):
                    staff = get_object_or_404(Staff, pk=request.POST.get('staff_id'))
                if not staff:
                    raise sa.LeaveError('Choose the staff member.')
                start = date.fromisoformat(request.POST.get('start_date') or '')
                end = date.fromisoformat(request.POST.get('end_date') or request.POST.get('start_date') or '')
                lv = sa.request_leave(staff, request.POST.get('leave_type'), start, end,
                                      request.POST.get('reason'), request.user)
                if manage and staff != me:
                    sa.review(lv, True, request.user, 'Recorded by an administrator')
                messages.success(request, 'Leave recorded.' if lv.status == 'APPROVED' else
                                 'Leave requested — an administrator will review it.')
            elif action in ('approve', 'decline'):
                if not manage:
                    return _deny(request, 'a school administrator can approve leave')
                lv = get_object_or_404(StaffLeave, pk=request.POST.get('leave_id'))
                sa.review(lv, action == 'approve', request.user, request.POST.get('note'))
                messages.success(request, f"{lv.staff.full_name}'s leave {lv.get_status_display().lower()}.")
            elif action == 'cancel':
                lv = get_object_or_404(StaffLeave, pk=request.POST.get('leave_id'))
                if not (manage or (me and lv.staff_id == me.id)):
                    return _deny(request, 'the staff member or an administrator can cancel leave')
                sa.cancel(lv)
                messages.success(request, 'Leave cancelled.')
        except (sa.LeaveError, ValueError) as problem:
            messages.error(request, str(problem) or 'Check the dates.')
        return redirect('safety:staff_leave')
    mine = StaffLeave.objects.filter(staff=me).order_by('-start_date')[:20] if me else []
    return render(request, 'safety/staff_leave.html', {
        'me': me, 'can_manage': manage, 'mine': mine, 'types': StaffLeave.TYPES,
        'pending': StaffLeave.objects.filter(status=StaffLeave.PENDING).select_related('staff') if manage else [],
        'current': StaffLeave.objects.filter(status=StaffLeave.APPROVED, start_date__lte=timezone.localdate(),
                                             end_date__gte=timezone.localdate()).select_related('staff'),
        'staff_list': Staff.objects.order_by('full_name') if manage else [],
        'taken': sa.days_taken(me, timezone.localdate().year) if me else {},
        'today': timezone.localdate(),
    })


@login_required
def staff_attendance(request):
    if not can_manage(request.user):
        return _deny(request, 'a school administrator can see staff attendance')
    import calendar as _cal
    try:
        day = date.fromisoformat(request.GET.get('date') or '')
    except ValueError:
        day = timezone.localdate()
    rows, counts, closed = sa.day_report(request.tenant, day)
    first = day.replace(day=1)
    last = day.replace(day=_cal.monthrange(day.year, day.month)[1])
    month, school_days = sa.month_summary(request.tenant, first, last)
    from users.attendance import settings_for
    cfg = settings_for(request.tenant)
    late_after = (datetime.combine(day, cfg.school_starts_at)
                  + timedelta(minutes=cfg.late_grace_minutes or 0)).time()
    today = timezone.localdate()
    return render(request, 'safety/staff_attendance.html', {
        'day': day, 'rows': rows, 'counts': counts, 'closed': closed, 'month': month,
        'school_days': school_days, 'first': first,
        'prev_day': day - timedelta(days=1), 'next_day': day + timedelta(days=1) if day < today else None,
        'is_today': day == today, 'late_after': late_after,
        'clocked_in': counts['present'] + counts['late'],
    })


# ─────────────────────────────────────────────────────────────────────────────
# Phone clock-in
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def phone_clock(request):
    from . import clock
    staff = getattr(request.user, 'staff', None)
    if not staff:
        return _deny(request, 'staff can clock in')
    if request.method == 'POST':
        try:
            log = clock.clock_by_phone(staff, request.POST.get('lat'), request.POST.get('lng'),
                                       request.POST.get('accuracy'))
            return JsonResponse({'ok': True, 'direction': log.get_direction_display(),
                                 'time': timezone.localtime(log.scanned_at).strftime('%H:%M'),
                                 'distance': log.distance_m})
        except clock.ClockError as problem:
            return JsonResponse({'ok': False, 'error': str(problem)}, status=400)
    cfg = svc.settings_for(request.tenant)
    logs = clock.today_logs(staff)
    return render(request, 'safety/phone_clock.html', {
        'cfg': cfg, 'logs': logs, 'staff': staff,
        'next_direction': 'out' if logs and logs[-1].direction == 'IN' else 'in',
    })
