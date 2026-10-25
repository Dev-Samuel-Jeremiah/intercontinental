"""
family/views.py
───────────────
Parent pages (the family hub and everything off it), staff pages to run
them, and password-free sign-in.
"""
from datetime import datetime

from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from users.models import Staff, Student
from users.views_profile import can_manage, can_see_fees, can_view

from . import services as svc
from .models import (DAYS, MODES, Appointment, Availability, Opportunity, PtaExecutive, PtaLevy, PtaMeeting,
                     SatisfactionResponse, Ticket)


def _deny(request, text='Only administrators can do that.'):
    messages.error(request, text)
    return redirect('dashboard:router')


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


def _kids(request):
    return svc.children_of(request.user, request.tenant)


def _parent_only(view):
    def wrap(request, *a, **kw):
        kids = _kids(request)
        if not kids:
            return _deny(request, 'This page is for parents.')
        request.kids = kids
        return view(request, *a, **kw)
    wrap.__name__ = view.__name__
    return login_required(wrap)


def _me(request):
    return Staff.objects.filter(user=request.user, tenant=request.tenant).first()


def _ctx(request, title, **kw):
    return {'page_title': title, 'kids': request.kids, **kw}


# ─────────────────────────────────────────────────────────────────────────────
# Parent: hub
# ─────────────────────────────────────────────────────────────────────────────

@_parent_only
def hub(request):
    from communications.models import ConsentForm
    from communications.engage import consent_open, consent_students
    kids = request.kids
    first = kids[0]
    if request.method == 'POST':
        try:
            svc.record_score(request.tenant, request.user, first, request.POST.get('score'), request.POST.get('comment'))
            messages.success(request, 'Thank you for telling us.')
        except svc.FamilyError as problem:
            messages.error(request, str(problem))
        return redirect('family:hub')
    consents_waiting = 0
    for f in ConsentForm.objects.filter(tenant=request.tenant, closed=False, deadline__gte=timezone.localdate()):
        replied = set(f.responses.values_list('student_id', flat=True))
        ids = set(consent_students(f).values_list('pk', flat=True))
        consents_waiting += sum(1 for k in kids if k.pk in ids and k.pk not in replied and consent_open(f))
    return render(request, 'family/hub.html', _ctx(
        request, 'Family hub', today=svc.digest(kids, timezone.localdate()),
        appointments=Appointment.objects.filter(parent=request.user, status='BOOKED',
                                                date__gte=timezone.localdate()).select_related('staff'),
        consents_waiting=consents_waiting, score_due=svc.score_due(request.tenant, first),
        open_tickets=Ticket.objects.filter(tenant=request.tenant, parent=request.user)
        .exclude(status__in=('RESOLVED', 'CLOSED')).count(),
        scale=range(11)))


@_parent_only
def digest_view(request):
    day = _date(request.GET.get('day')) or timezone.localdate()
    return render(request, 'family/digest.html', _ctx(request, 'Today at school', d=svc.digest(request.kids, day)))


# ─────────────────────────────────────────────────────────────────────────────
# F265 — fee statement (parents for their children, bursary for anyone)
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def statement(request, student_id):
    student = get_object_or_404(Student.objects.select_related('class_assigned'), pk=student_id, tenant=request.tenant)
    kids = _kids(request)
    if not (student in kids or can_see_fees(request.user)):
        raise Http404
    st = svc.statement(student)
    if request.GET.get('pdf'):
        resp = HttpResponse(svc.statement_pdf(request.tenant, st), content_type='application/pdf')
        resp['Content-Disposition'] = f'attachment; filename="Fee_statement_{student.admission_number.replace("/", "-")}.pdf"'
        return resp
    template = 'family/statement.html' if student in kids else 'family/statement_staff.html'
    return render(request, template, {'page_title': 'Fee statement', 'st': st, 'kids': kids})


# ─────────────────────────────────────────────────────────────────────────────
# F276 — teachers
# ─────────────────────────────────────────────────────────────────────────────

@_parent_only
def teachers(request):
    from communications.connect import office_hours_text
    rows = []
    for k in request.kids:
        people = []
        for staff, subjects, form in svc.teachers_of(k):
            from hr.models import StaffRecord
            rec = StaffRecord.objects.filter(staff=staff).first()
            people.append({'staff': staff, 'subjects': subjects, 'form': form, 'email': staff.user.email,
                           'extension': getattr(rec, 'extension', '') if rec else '',
                           'bookable': staff.availability.exists()})
        rows.append((k, people))
    return render(request, 'family/teachers.html', _ctx(request, "My children's teachers", rows=rows,
                                                        hours=office_hours_text(request.tenant)))


# ─────────────────────────────────────────────────────────────────────────────
# F266 — appointments
# ─────────────────────────────────────────────────────────────────────────────

@_parent_only
def book(request):
    kids = request.kids
    child = next((k for k in kids if str(k.pk) == (request.POST.get('student') or request.GET.get('student'))), kids[0])
    staff = Staff.objects.filter(tenant=request.tenant, pk=request.POST.get('staff') or request.GET.get('staff') or 0) \
        .first()
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action == 'cancel':
                appt = get_object_or_404(Appointment, pk=request.POST.get('id'), parent=request.user)
                svc.cancel(appt, request.user, request.POST.get('note'))
                messages.success(request, 'Appointment cancelled.')
            else:
                if not staff:
                    raise svc.FamilyError('Choose who to meet.')
                day_raw, _, start_raw = (request.POST.get('slot') or '').partition('|')
                day, start = _date(request.POST.get('date') or day_raw), _time(request.POST.get('start') or start_raw)
                if not (day and start):
                    raise svc.FamilyError('Choose a time.')
                appt = svc.book(request.user, child, staff, day, start, request.POST.get('mode'),
                                request.POST.get('reason'))
                messages.success(request, f'Booked with {staff.full_name} on {appt.date:%A %d %B} at '
                                          f'{appt.start:%-I:%M %p}. A confirmation email is on its way.')
        except svc.FamilyError as problem:
            messages.error(request, str(problem))
        return redirect('family:book')
    options = svc.meetable(child)
    if staff and staff not in options:
        staff = None
    return render(request, 'family/book.html', _ctx(
        request, 'Book an appointment', child=child, options=options, staff=staff,
        slots=svc.upcoming(staff) if staff else [], modes=MODES,
        mine=Appointment.objects.filter(parent=request.user, date__gte=timezone.localdate())
        .select_related('staff', 'student')))


@login_required
def staff_appointments(request):
    me = _me(request)
    if not me:
        return _deny(request, 'This page is for staff.')
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action == 'add':
                mins = str(request.POST.get('slot_minutes') or '')
                svc.set_availability(me, request.POST.get('day'), _time(request.POST.get('start')),
                                     _time(request.POST.get('end')), int(mins) if mins.isdigit() else 0,
                                     request.POST.get('location'))
                messages.success(request, 'Added.')
            elif action == 'remove':
                Availability.objects.filter(staff=me, pk=request.POST.get('id')).delete()
            elif action in ('cancel', 'done'):
                appt = get_object_or_404(Appointment, pk=request.POST.get('id'), staff=me)
                if action == 'cancel':
                    svc.cancel(appt, request.user, request.POST.get('note'))
                else:
                    appt.status = Appointment.DONE
                    appt.save(update_fields=['status'])
        except svc.FamilyError as problem:
            messages.error(request, str(problem))
        return redirect('family:staff_appointments')
    return render(request, 'family/staff_appointments.html', {
        'times': Availability.objects.filter(staff=me), 'days': DAYS,
        'upcoming': Appointment.objects.filter(staff=me, date__gte=timezone.localdate(), status='BOOKED')
        .select_related('student', 'parent'),
        'past': Appointment.objects.filter(staff=me, date__lt=timezone.localdate()).select_related('student')[:20]})


# ─────────────────────────────────────────────────────────────────────────────
# F267 — PTA
# ─────────────────────────────────────────────────────────────────────────────

@_parent_only
def pta(request):
    first = request.kids[0]
    if request.method == 'POST':
        levy = get_object_or_404(PtaLevy, pk=request.POST.get('levy'), tenant=request.tenant)
        try:
            svc.pay_levy_from_wallet(levy, first, request.user)
            messages.success(request, f'Paid ₦{levy.amount:,.2f} from your family wallet. Thank you!')
        except svc.FamilyError as problem:
            messages.error(request, str(problem))
        return redirect('family:pta')
    key = svc.family_key(first)
    levies = [(lv, svc.levy_status(lv, key)) for lv in PtaLevy.objects.filter(tenant=request.tenant, active=True)]
    return render(request, 'family/pta.html', _ctx(
        request, 'PTA', levies=levies, wallet=svc.family_wallet_balance(first),
        execs=PtaExecutive.objects.filter(tenant=request.tenant),
        coming=PtaMeeting.objects.filter(tenant=request.tenant, date__gte=timezone.localdate()).order_by('date'),
        past=PtaMeeting.objects.filter(tenant=request.tenant, date__lt=timezone.localdate(), minutes_published=True)))


@login_required
def pta_manage(request):
    if not can_manage(request.user) and not can_see_fees(request.user):
        return _deny(request)
    if request.method == 'POST':
        a = request.POST.get('action')
        try:
            if a == 'exec':
                if (request.POST.get('name') or '').strip() and (request.POST.get('role') or '').strip():
                    PtaExecutive.objects.create(tenant=request.tenant, name=request.POST['name'].strip()[:120],
                                                role=request.POST['role'].strip()[:80],
                                                phone=(request.POST.get('phone') or '')[:20],
                                                email=(request.POST.get('email') or '')[:254])
            elif a == 'exec_remove':
                PtaExecutive.objects.filter(tenant=request.tenant, pk=request.POST.get('id')).delete()
            elif a == 'meeting':
                day = _date(request.POST.get('date'))
                if not (day and (request.POST.get('title') or '').strip()):
                    raise svc.FamilyError('Give the meeting a title and date.')
                PtaMeeting.objects.create(tenant=request.tenant, title=request.POST['title'].strip()[:160], date=day,
                                          time=_time(request.POST.get('time')) or datetime(2000, 1, 1, 10).time(),
                                          venue=(request.POST.get('venue') or '')[:160],
                                          agenda=request.POST.get('agenda') or '')
            elif a == 'minutes':
                m = get_object_or_404(PtaMeeting, pk=request.POST.get('id'), tenant=request.tenant)
                m.minutes = request.POST.get('minutes') or ''
                m.minutes_published = bool(request.POST.get('publish'))
                m.save(update_fields=['minutes', 'minutes_published'])
            elif a == 'levy':
                from decimal import Decimal, InvalidOperation
                try:
                    amount = Decimal(str(request.POST.get('amount') or '').replace(',', ''))
                except InvalidOperation:
                    raise svc.FamilyError('Give the amount as a number.')
                if amount <= 0 or not (request.POST.get('title') or '').strip():
                    raise svc.FamilyError('Give the levy a title and an amount.')
                PtaLevy.objects.create(tenant=request.tenant, title=request.POST['title'].strip()[:160], amount=amount,
                                       due_date=_date(request.POST.get('due_date')))
            messages.success(request, 'Saved.')
        except svc.FamilyError as problem:
            messages.error(request, str(problem))
        return redirect('family:pta_manage')
    return render(request, 'family/pta_manage.html', {
        'execs': PtaExecutive.objects.filter(tenant=request.tenant),
        'meetings': PtaMeeting.objects.filter(tenant=request.tenant),
        'levies': [(lv, svc.levy_report(lv)) for lv in PtaLevy.objects.filter(tenant=request.tenant)]})


@login_required
def pta_levy(request, pk):
    if not can_manage(request.user) and not can_see_fees(request.user):
        return _deny(request)
    levy = get_object_or_404(PtaLevy, pk=pk, tenant=request.tenant)
    if request.method == 'POST':
        if request.POST.get('action') == 'toggle':
            levy.active = not levy.active
            levy.save(update_fields=['active'])
        else:
            student = get_object_or_404(Student, pk=request.POST.get('student'), tenant=request.tenant)
            try:
                svc.record_levy(levy, student, request.POST.get('method'), request.user)
                messages.success(request, f'Recorded for {student.full_name}\'s family.')
            except svc.FamilyError as problem:
                messages.error(request, str(problem))
        return redirect('family:pta_levy', pk=levy.pk)
    rep = svc.levy_report(levy)
    if request.GET.get('export') in ('csv', 'xlsx'):
        from analytics.export import respond
        rows = [[', '.join(c.full_name for c in r['children']), r['children'][0].parent_name or '',
                 r['payment'].get_method_display() if r['payment'] else 'Not paid',
                 float(r['payment'].amount) if r['payment'] else 0] for r in rep['rows']]
        return respond(request.GET['export'], levy.title, ['Children', 'Parent', 'Paid by', 'Amount'], rows,
                       request.tenant.name)
    return render(request, 'family/pta_levy.html', {'levy': levy, 'rep': rep})


# ─────────────────────────────────────────────────────────────────────────────
# F268 — volunteering
# ─────────────────────────────────────────────────────────────────────────────

@_parent_only
def volunteer(request):
    if request.method == 'POST':
        opp = get_object_or_404(Opportunity, pk=request.POST.get('id'), tenant=request.tenant)
        try:
            if request.POST.get('action') == 'withdraw':
                opp.signups.filter(user=request.user).delete()
                messages.success(request, 'You have been taken off the list.')
            else:
                svc.volunteer(opp, request.user, request.POST.get('name'), request.POST.get('phone'),
                              request.POST.get('note'))
                messages.success(request, f'Thank you for volunteering for {opp.title}!')
        except svc.FamilyError as problem:
            messages.error(request, str(problem))
        return redirect('family:volunteer')
    opps = list(Opportunity.objects.filter(tenant=request.tenant, cancelled=False, date__gte=timezone.localdate()))
    for o in opps:
        o.left = svc.places_left(o)
        o.joined = o.signups.filter(user=request.user).exists()
    first = request.kids[0]
    return render(request, 'family/volunteer.html', _ctx(
        request, 'Volunteer', opps=opps, default_name=request.user.get_full_name() or first.parent_name or '',
        default_phone=first.parent_phone or ''))


@login_required
def volunteer_manage(request):
    if not can_view(request.user):
        return _deny(request, 'This page is for staff.')
    if request.method == 'POST':
        if request.POST.get('action') == 'cancel':
            o = get_object_or_404(Opportunity, pk=request.POST.get('id'), tenant=request.tenant)
            o.cancelled = not o.cancelled
            o.save(update_fields=['cancelled'])
        else:
            day = _date(request.POST.get('date'))
            title = (request.POST.get('title') or '').strip()
            if not (day and title):
                messages.error(request, 'Give a title and date.')
            else:
                places = str(request.POST.get('places') or '')
                Opportunity.objects.create(tenant=request.tenant, title=title[:160], date=day,
                                           details=request.POST.get('details') or '',
                                           start_time=_time(request.POST.get('start_time')),
                                           end_time=_time(request.POST.get('end_time')),
                                           places=int(places) if places.isdigit() and int(places) > 0 else None,
                                           created_by=request.user)
                messages.success(request, 'Posted for parents.')
        return redirect('family:volunteer_manage')
    opps = list(Opportunity.objects.filter(tenant=request.tenant).prefetch_related('signups'))
    return render(request, 'family/volunteer_manage.html', {'opps': opps, 'today': timezone.localdate()})


# ─────────────────────────────────────────────────────────────────────────────
# F269 — feedback and complaints
# ─────────────────────────────────────────────────────────────────────────────

@_parent_only
def feedback(request):
    if request.method == 'POST':
        child = next((k for k in request.kids if str(k.pk) == request.POST.get('student')), None)
        try:
            t = svc.open_ticket(request.tenant, request.user, child, request.POST.get('kind'),
                                request.POST.get('category'), request.POST.get('subject'), request.POST.get('body'))
            messages.success(request, f'Thank you — your reference is {t.reference}. We will reply here.')
            return redirect('family:feedback_detail', pk=t.pk)
        except svc.FamilyError as problem:
            messages.error(request, str(problem))
        return redirect('family:feedback')
    return render(request, 'family/feedback.html', _ctx(
        request, 'Feedback and complaints', kinds=Ticket.KINDS, categories=Ticket.CATEGORIES,
        tickets=Ticket.objects.filter(tenant=request.tenant, parent=request.user),
        days=svc.settings_for(request.tenant).complaint_days))


@_parent_only
def feedback_detail(request, pk):
    t = get_object_or_404(Ticket, pk=pk, tenant=request.tenant, parent=request.user)
    if request.method == 'POST':
        try:
            if request.POST.get('action') == 'rate':
                svc.rate(t, request.user, request.POST.get('stars'), request.POST.get('comment'))
                messages.success(request, 'Thank you for rating our response.')
            else:
                svc.ticket_reply(t, request.user, request.POST.get('body'))
        except svc.FamilyError as problem:
            messages.error(request, str(problem))
        return redirect('family:feedback_detail', pk=t.pk)
    return render(request, 'family/feedback_detail.html', _ctx(
        request, t.reference, t=t, msgs=t.messages.filter(internal=False).select_related('author')))


@login_required
def tickets(request):
    if not can_view(request.user):
        return _deny(request, 'This page is for staff.')
    admin = can_manage(request.user)
    me = _me(request)
    qs = Ticket.objects.filter(tenant=request.tenant).select_related('student', 'assigned_to', 'parent')
    if not admin:
        qs = qs.filter(assigned_to=me)
    show = request.GET.get('show', 'open')
    if show == 'open':
        qs = qs.exclude(status__in=('RESOLVED', 'CLOSED'))
    days = svc.settings_for(request.tenant).complaint_days
    items = list(qs[:200])
    for t in items:
        t.late = svc.overdue(t, days)
    rated = [t.rating for t in Ticket.objects.filter(tenant=request.tenant, rating__isnull=False)]
    return render(request, 'family/tickets.html', {
        'items': items, 'show': show, 'days': days, 'admin': admin,
        'avg_rating': round(sum(rated) / len(rated), 1) if rated else None,
        'late_count': sum(1 for t in items if t.late)})


@login_required
def ticket_detail(request, pk):
    if not can_view(request.user):
        return _deny(request, 'This page is for staff.')
    t = get_object_or_404(Ticket.objects.select_related('student', 'parent', 'assigned_to'), pk=pk,
                          tenant=request.tenant)
    admin = can_manage(request.user)
    me = _me(request)
    if not (admin or (me and t.assigned_to_id == me.pk)):
        return _deny(request, 'This complaint is not assigned to you.')
    if request.method == 'POST':
        if request.POST.get('action') == 'assign' and admin:
            t.assigned_to = Staff.objects.filter(tenant=request.tenant, pk=request.POST.get('staff') or 0).first()
            t.save(update_fields=['assigned_to', 'updated_at'])
            if t.assigned_to and t.assigned_to.user.email:
                svc._email(t.assigned_to.user.email, f'Assigned to you: {t.reference} {t.subject}',
                           f'<p>{t.get_kind_display()} about {t.get_category_display()}: {t.subject}</p>')
        else:
            svc.ticket_reply(t, request.user, request.POST.get('body'), internal=bool(request.POST.get('internal')),
                             status=request.POST.get('status') or None)
        return redirect('family:ticket_detail', pk=t.pk)
    return render(request, 'family/ticket_detail.html', {
        't': t, 'msgs': t.messages.select_related('author'), 'statuses': Ticket.STATUSES, 'admin': admin,
        'staff': Staff.objects.filter(tenant=request.tenant, user__is_active=True).order_by('full_name'),
        'late': svc.overdue(t, svc.settings_for(request.tenant).complaint_days)})


# ─────────────────────────────────────────────────────────────────────────────
# Settings and satisfaction dashboard (admins)
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def manage(request):
    if not can_manage(request.user):
        return _deny(request)
    cfg = svc.settings_for(request.tenant)
    if request.method == 'POST':
        cfg.digest_enabled = bool(request.POST.get('digest_enabled'))
        cfg.satisfaction_enabled = bool(request.POST.get('satisfaction_enabled'))
        cfg.magic_login = bool(request.POST.get('magic_login'))
        days = str(request.POST.get('complaint_days') or '')
        cfg.complaint_days = min(60, max(1, int(days))) if days.isdigit() else cfg.complaint_days
        cfg.save()
        messages.success(request, 'Saved.')
        return redirect('family:manage')
    return render(request, 'family/manage.html', {
        'cfg': cfg, 'rep': svc.nps_report(request.tenant),
        'responses': SatisfactionResponse.objects.filter(tenant=request.tenant).count()})


# ─────────────────────────────────────────────────────────────────────────────
# F277 — password-free sign-in (no login needed)
# ─────────────────────────────────────────────────────────────────────────────

def magic_request(request):
    if not getattr(request, 'tenant', None):
        raise Http404
    if request.user.is_authenticated and getattr(request.user, 'is_parent', False):
        return redirect('users:parent_dashboard')
    sent = None
    contact = ''
    if request.method == 'POST':
        contact = (request.POST.get('contact') or '').strip()
        try:
            svc.request_login(request.tenant, contact)
            # The same answer whether or not the contact is on file, so nobody can test numbers.
            sent = 'EMAIL' if '@' in contact else 'SMS'
        except svc.FamilyError as problem:
            messages.error(request, str(problem))
    return render(request, 'family/magic_login.html', {'page_title': 'Sign in', 'sent': sent, 'contact': contact})


def magic_code(request):
    if not getattr(request, 'tenant', None) or request.method != 'POST':
        raise Http404
    try:
        user = svc.use_code(request.tenant, request.POST.get('contact'), request.POST.get('code'))
    except svc.FamilyError as problem:
        messages.error(request, str(problem))
        return render(request, 'family/magic_login.html', {'page_title': 'Sign in', 'sent': 'SMS',
                                                           'contact': request.POST.get('contact', '')})
    login(request, user, backend='django.contrib.auth.backends.ModelBackend')
    return redirect('users:parent_dashboard')


def magic_link(request, token):
    if not getattr(request, 'tenant', None):
        raise Http404
    try:
        user = svc.use_link(request.tenant, token)
    except svc.FamilyError as problem:
        messages.error(request, str(problem))
        return redirect('family:magic_request')
    login(request, user, backend='django.contrib.auth.backends.ModelBackend')
    return redirect('users:parent_dashboard')
