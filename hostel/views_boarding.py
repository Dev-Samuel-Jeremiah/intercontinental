"""
hostel/views_boarding.py
────────────────────────
Boarding life pages for hostel staff, and the parents' Boarding page.
"""
from datetime import datetime, timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from users.models import Student

from . import boarding as bd
from .models import (Bed, BoarderProfile, BoardingReport, ExeatRequest, Inspection, RollCall, RollCallMark,
                     Room, RoomChangeRequest, SickBayVisit, VisitBooking, VisitingDay)
from .views import _is_hostel_staff


def _deny(request, text='Hostel staff only.'):
    messages.error(request, text)
    return redirect('dashboard:router')


def _staff_only(view):
    def wrap(request, *a, **kw):
        if not _is_hostel_staff(request.user):
            return _deny(request)
        return view(request, *a, **kw)
    wrap.__name__ = view.__name__
    return login_required(wrap)


def _date(raw):
    try:
        return datetime.strptime(raw or '', '%Y-%m-%d').date()
    except ValueError:
        return None


def _dt(raw):
    try:
        return timezone.make_aware(datetime.strptime(raw or '', '%Y-%m-%dT%H:%M'))
    except ValueError:
        return None


def _hostel(request):
    hs = bd.hostels(request.tenant)
    h = hs.filter(pk=request.GET.get('hostel') or request.POST.get('hostel') or 0).first() or hs.first()
    return hs, h


def _term(request):
    from teaching.services import current_term
    return current_term(request.tenant)


# ─────────────────────────────────────────────────────────────────────────────
# F321 — bed map
# ─────────────────────────────────────────────────────────────────────────────

@_staff_only
def bed_map(request):
    from .services import assign_bed
    hs, h = _hostel(request)
    if request.method == 'POST':
        bed = get_object_or_404(Bed, pk=request.POST.get('bed'), tenant=request.tenant)
        boarder = get_object_or_404(BoarderProfile, pk=request.POST.get('boarder'), tenant=request.tenant)
        try:
            assign_bed(boarder, bed, performed_by=request.user)
            messages.success(request, f'{boarder.student.full_name} is now in {bed.room} bed {bed.bed_number}.')
        except ValueError as problem:
            messages.error(request, str(problem))
        return redirect(f'{request.path}?hostel={bed.room.hostel_id}')
    return render(request, 'hostel/boarding/bed_map.html', {
        'hostels': hs, 'h': h, 'floors': bd.bed_map(h) if h else [], 'waiting': bd.unbedded(request.tenant)})


# ─────────────────────────────────────────────────────────────────────────────
# F322 / F327 / F336 — roll calls
# ─────────────────────────────────────────────────────────────────────────────

@_staff_only
def roll_calls(request):
    hs, h = _hostel(request)
    kind = request.GET.get('kind') or request.POST.get('kind') or 'NIGHT'
    if kind not in dict(RollCall.KINDS):
        kind = 'NIGHT'
    day = _date(request.GET.get('date') or request.POST.get('date')) or timezone.localdate()
    if not h:
        return _deny(request, 'Add a hostel first.')
    rc = bd.roll_call(h, kind, day, request.user)
    if request.method == 'POST':
        statuses = {k[2:]: v for k, v in request.POST.items() if k.startswith('s_')}
        absent = bd.save_roll(rc, statuses, request.user, request.POST.get('note'))
        messages.success(request, 'Saved.' + (f' {len(absent)} absent — the warden and administrators have been '
                                             'emailed.' if absent and kind in ('NIGHT', 'LIGHTS') else ''))
        return redirect(f'{request.path}?hostel={h.pk}&kind={kind}&date={day:%Y-%m-%d}')
    return render(request, 'hostel/boarding/roll_call.html', {
        'hostels': hs, 'h': h, 'rc': rc, 'kind': kind, 'day': day, 'kinds': RollCall.KINDS,
        'marks': rc.marks.select_related('boarder__student__class_assigned', 'boarder__bed__room')
        .order_by('boarder__bed__room__room_number', 'boarder__student__full_name'),
        'statuses': RollCallMark.STATUSES, 'summary': bd.roll_summary(rc),
        'recent': RollCall.objects.filter(hostel=h).order_by('-date', 'kind')[:10],
        'late': bd.late_returns(request.tenant)})


# ─────────────────────────────────────────────────────────────────────────────
# F323 — exeats
# ─────────────────────────────────────────────────────────────────────────────

@_staff_only
def exeats(request):
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action == 'new':
                b = get_object_or_404(BoarderProfile, pk=request.POST.get('boarder'), tenant=request.tenant)
                bd.request_exeat(b, request.user, _dt(request.POST.get('leave_at')), _dt(request.POST.get('return_by')),
                                 request.POST.get('reason'), request.POST.get('pickup_person'),
                                 request.POST.get('pickup_phone'), by_parent=False)
                messages.success(request, 'Sent to the family to approve.')
            else:
                r = get_object_or_404(ExeatRequest, pk=request.POST.get('id'), tenant=request.tenant)
                if action in ('approve', 'decline'):
                    bd.school_decide(r, request.user, action == 'approve', request.POST.get('note'))
                elif action == 'out':
                    bd.sign_out(r, request.user)
                elif action == 'in':
                    bd.sign_in(r, request.user)
        except bd.BoardingError as problem:
            messages.error(request, str(problem))
        return redirect('hostel:exeats')
    qs = ExeatRequest.objects.filter(tenant=request.tenant).select_related('boarder__student', 'requested_by')
    now = timezone.now()
    out = list(qs.filter(status='OUT'))
    for r in out:
        r.overdue = r.return_by < now
    return render(request, 'hostel/boarding/exeats.html', {
        'waiting': qs.filter(status='SCHOOL'), 'parent': qs.filter(status='PARENT'),
        'approved': qs.filter(status='APPROVED').order_by('leave_at'), 'out': out,
        'recent': qs.filter(status__in=('RETURNED', 'DECLINED', 'CANCELLED'))[:20],
        'boarders': BoarderProfile.objects.filter(tenant=request.tenant, status='ACTIVE', bed__isnull=False)
        .select_related('student').order_by('student__full_name'), 'late': bd.late_returns(request.tenant)})


# ─────────────────────────────────────────────────────────────────────────────
# F328 — visiting days
# ─────────────────────────────────────────────────────────────────────────────

@_staff_only
def visiting(request):
    if request.method == 'POST':
        a = request.POST.get('action')
        if a == 'day':
            day, start, end = _date(request.POST.get('date')), request.POST.get('start'), request.POST.get('end')
            try:
                st, en = datetime.strptime(start, '%H:%M').time(), datetime.strptime(end, '%H:%M').time()
            except (TypeError, ValueError):
                st = en = None
            if not (day and st and en and en > st):
                messages.error(request, 'Give the date and a start and end time.')
            else:
                mins, per = str(request.POST.get('slot_minutes') or '60'), str(request.POST.get('families') or '20')
                VisitingDay.objects.create(tenant=request.tenant, date=day, start=st, end=en,
                                           slot_minutes=int(mins) if mins.isdigit() and int(mins) >= 15 else 60,
                                           families_per_slot=int(per) if per.isdigit() and int(per) else 20,
                                           notes=request.POST.get('notes') or '')
                messages.success(request, 'Visiting day published. Families can book on the portal.')
        elif a == 'arrived':
            b = get_object_or_404(VisitBooking, pk=request.POST.get('id'), tenant=request.tenant)
            b.arrived_at = None if b.arrived_at else timezone.now()
            b.save(update_fields=['arrived_at'])
        elif a == 'toggle':
            d = get_object_or_404(VisitingDay, pk=request.POST.get('id'), tenant=request.tenant)
            d.open = not d.open
            d.save(update_fields=['open'])
        return redirect(request.get_full_path())
    days = list(VisitingDay.objects.filter(tenant=request.tenant, date__gte=timezone.localdate() - timedelta(days=7)))
    current = next((d for d in days if str(d.pk) == request.GET.get('day')), days[0] if days else None)
    return render(request, 'hostel/boarding/visiting.html', {
        'days': days, 'current': current,
        'bookings': current.bookings.select_related('boarder__student', 'parent') if current else [],
        'slots': bd.slots(current) if current else []})


# ─────────────────────────────────────────────────────────────────────────────
# F329 — inspections
# ─────────────────────────────────────────────────────────────────────────────

@_staff_only
def inspections(request):
    hs, h = _hostel(request)
    if request.method == 'POST':
        room = get_object_or_404(Room, pk=request.POST.get('room'), tenant=request.tenant)
        try:
            i = bd.inspect(room, request.user, request.POST, request.POST.get('note'))
            messages.success(request, f'{room}: {i.total}%')
        except bd.BoardingError as problem:
            messages.error(request, str(problem))
        return redirect(f'{request.path}?hostel={room.hostel_id}')
    return render(request, 'hostel/boarding/inspections.html', {
        'hostels': hs, 'h': h, 'rooms': Room.objects.filter(hostel=h, is_active=True) if h else [],
        'criteria': list(enumerate(bd.CRITERIA)), 'league': bd.room_league(h) if h else [],
        'recent': Inspection.objects.filter(room__hostel=h).select_related('room', 'inspector')[:20] if h else [],
        'scale': [1, 2, 3, 4, 5]})


# ─────────────────────────────────────────────────────────────────────────────
# F330 — sick bay
# ─────────────────────────────────────────────────────────────────────────────

@_staff_only
def sick_bay(request):
    if request.method == 'POST':
        a = request.POST.get('action')
        try:
            if a == 'admit':
                st = get_object_or_404(Student, pk=request.POST.get('student'), tenant=request.tenant)
                bd.admit(st, request.user, request.POST.get('complaint'), request.POST.get('temperature'),
                         request.POST.get('treatment'), tell_parent=bool(request.POST.get('tell_parent')))
                messages.success(request, f'{st.full_name} admitted.')
            else:
                v = get_object_or_404(SickBayVisit, pk=request.POST.get('id'), tenant=request.tenant)
                bd.discharge(v, request.POST.get('outcome'), request.POST.get('treatment'))
                messages.success(request, f'{v.student.full_name} discharged.')
        except bd.BoardingError as problem:
            messages.error(request, str(problem))
        return redirect('hostel:sick_bay')
    from users.models import ENROLLED_STATUSES
    return render(request, 'hostel/boarding/sick_bay.html', {
        'inside': SickBayVisit.objects.filter(tenant=request.tenant, discharged_at__isnull=True)
        .select_related('student__class_assigned'),
        'history': SickBayVisit.objects.filter(tenant=request.tenant, discharged_at__isnull=False)
        .select_related('student')[:30],
        'students': Student.objects.filter(tenant=request.tenant, status__in=ENROLLED_STATUSES)
        .order_by('full_name').only('id', 'full_name', 'admission_number'),
        'outcomes': [o for o in SickBayVisit.OUTCOMES if o[0]]})


# ─────────────────────────────────────────────────────────────────────────────
# F338 — room changes
# ─────────────────────────────────────────────────────────────────────────────

@_staff_only
def room_changes(request):
    if request.method == 'POST':
        r = get_object_or_404(RoomChangeRequest, pk=request.POST.get('id'), tenant=request.tenant)
        approve = request.POST.get('action') == 'approve'
        bed = Bed.objects.filter(tenant=request.tenant, pk=request.POST.get('bed') or 0).first()
        try:
            bd.decide_room_change(r, request.user, approve, bed, request.POST.get('note'))
            messages.success(request, 'Done.')
        except bd.BoardingError as problem:
            messages.error(request, str(problem))
        return redirect('hostel:room_changes')
    return render(request, 'hostel/boarding/room_changes.html', {
        'pending': RoomChangeRequest.objects.filter(tenant=request.tenant, status='PENDING')
        .select_related('boarder__student', 'boarder__bed__room', 'preferred_room', 'requested_by'),
        'done': RoomChangeRequest.objects.filter(tenant=request.tenant).exclude(status='PENDING')
        .select_related('boarder__student')[:20], 'beds': bd.free_beds(request.tenant)})


# ─────────────────────────────────────────────────────────────────────────────
# F337 — housemaster reports
# ─────────────────────────────────────────────────────────────────────────────

@_staff_only
def reports(request):
    hs, h = _hostel(request)
    term = _term(request)
    if not term:
        return _deny(request, 'Set up the academic session and term first.')
    done = {r.boarder_id: r for r in BoardingReport.objects.filter(term=term)} if h else {}
    rows = [{'b': b, 'r': done.get(b.pk)} for b in bd.boarders_in(h)] if h else []
    return render(request, 'hostel/boarding/reports.html', {'hostels': hs, 'h': h, 'term': term, 'rows': rows})


@_staff_only
def report_edit(request, boarder_id):
    b = get_object_or_404(BoarderProfile.objects.select_related('student', 'bed__room__hostel'), pk=boarder_id,
                          tenant=request.tenant)
    term = _term(request)
    if not term:
        return _deny(request, 'Set up the academic session and term first.')
    rep = BoardingReport.objects.filter(boarder=b, term=term).first()
    if request.method == 'POST':
        try:
            bd.save_report(b, term, request.user, request.POST)
            messages.success(request, 'Report saved.')
            from django.urls import reverse
            return redirect(reverse('hostel:boarding_reports') + (f'?hostel={b.bed.room.hostel_id}' if b.bed_id else ''))
        except bd.BoardingError as problem:
            messages.error(request, str(problem))
    return render(request, 'hostel/boarding/report_edit.html', {
        'b': b, 'term': term, 'rep': rep, 'facts': bd.term_facts(b, term), 'ratings': BoardingReport.RATINGS})


# ─────────────────────────────────────────────────────────────────────────────
# F333 / F334 / F340 — kitchen list and night sheet
# ─────────────────────────────────────────────────────────────────────────────

@_staff_only
def kitchen(request):
    hs, h = _hostel(request)
    return render(request, 'hostel/boarding/kitchen.html', {
        'hostels': hs, 'h': h, 'diets': bd.diet_list(h) if h else [], 'menu': bd.week_menu(h) if h else []})


@_staff_only
def night_sheet(request):
    hs, h = _hostel(request)
    return render(request, 'hostel/boarding/night_sheet.html', {
        'hostels': hs, 'h': h, 'rows': bd.night_sheet(h) if h else [], 'now': timezone.localtime()})


# ─────────────────────────────────────────────────────────────────────────────
# Parents: Boarding page
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def family_boarding(request):
    from users.views_achievements import _parent_children
    kids = list(_parent_children(request))
    boarders = list(BoarderProfile.objects.filter(student__in=kids, student_type__in=('BOARDER', 'WEEKLY'))
                    .exclude(status='CHECKED_OUT').select_related('student', 'bed__room__hostel'))
    if not boarders:
        return _deny(request, 'None of your children board at the school.')
    if request.method == 'POST':
        a = request.POST.get('action')
        b = next((x for x in boarders if str(x.pk) == request.POST.get('boarder')), None)
        try:
            if a == 'exeat' and b:
                bd.request_exeat(b, request.user, _dt(request.POST.get('leave_at')), _dt(request.POST.get('return_by')),
                                 request.POST.get('reason'), request.POST.get('pickup_person'),
                                 request.POST.get('pickup_phone'), by_parent=True)
                messages.success(request, 'Sent to the housemaster. You will get an email when it is decided.')
            elif a in ('approve', 'decline'):
                r = get_object_or_404(ExeatRequest, pk=request.POST.get('id'), boarder__in=boarders)
                bd.parent_approve(r, request.user, a == 'approve')
                messages.success(request, 'Thank you.')
            elif a == 'cancel':
                r = get_object_or_404(ExeatRequest, pk=request.POST.get('id'), boarder__in=boarders,
                                      status__in=('PARENT', 'SCHOOL', 'APPROVED'))
                r.status = ExeatRequest.CANCELLED
                r.save(update_fields=['status'])
            elif a == 'visit' and b:
                day = get_object_or_404(VisitingDay, pk=request.POST.get('day'), tenant=request.tenant)
                slot = datetime.strptime(request.POST.get('slot') or '00:00', '%H:%M').time()
                bd.book_visit(day, b, request.user, slot, request.POST.get('visitors'), request.POST.get('names'))
                messages.success(request, 'Visit booked. Please bring ID.')
            elif a == 'unvisit':
                VisitBooking.objects.filter(pk=request.POST.get('id'), boarder__in=boarders).delete()
            elif a == 'room' and b:
                bd.request_room_change(b, request.user, request.POST.get('reason'))
                messages.success(request, 'Sent to the housemaster.')
            elif a == 'diet' and b:
                if request.POST.get('diet') in dict(BoarderProfile.DIETS):
                    b.diet, b.diet_note = request.POST['diet'], (request.POST.get('diet_note') or '')[:255]
                    b.save(update_fields=['diet', 'diet_note'])
                    messages.success(request, 'Dietary needs saved — the kitchen will see them.')
        except (bd.BoardingError, ValueError) as problem:
            messages.error(request, str(problem))
        return redirect('hostel:family_boarding')
    cards = []
    for b in boarders:
        h = bd.hostel_of(b)
        cards.append({
            'b': b, 'hostel': h, 'menu': bd.week_menu(h) if h else [],
            'exeats': ExeatRequest.objects.filter(boarder=b)[:8],
            'bookings': VisitBooking.objects.filter(boarder=b, day__date__gte=timezone.localdate())
            .select_related('day'),
            'reports': BoardingReport.objects.filter(boarder=b, published=True).select_related('term'),
            'rooms': RoomChangeRequest.objects.filter(boarder=b)[:3]})
    days = [(d, bd.slots(d)) for d in VisitingDay.objects.filter(tenant=request.tenant, open=True,
                                                                  date__gte=timezone.localdate())]
    return render(request, 'hostel/boarding/family.html', {'page_title': 'Boarding', 'cards': cards, 'days': days,
                                                           'diets': BoarderProfile.DIETS})
