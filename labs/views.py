"""
labs/views.py
─────────────
Pages for labs and practical rooms: the overview, each lab (schedule,
equipment, chemicals, breakages, safety), incidents, the practicals library,
practical registers and marks, WAEC/NECO readiness, and a student's (or
family's) practical record.
"""
from datetime import timedelta
from types import SimpleNamespace

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from facilities.services import FacilityError
from users.models import Class, Staff, Student, Subject

from . import services as svc
from .models import (Breakage, Equipment, EquipmentMove, Lab, LabBooking, Practical, PracticalRecord,
                     PracticalSession, SafetyIncident)


def _staff_only(view):
    def wrapped(request, *args, **kwargs):
        if not svc.is_staff(request.user, request.tenant):
            messages.error(request, 'Labs and practicals are for staff. Students and parents: see My practicals.')
            return redirect('labs:my') if (getattr(request.user, 'student', None) or getattr(request.user, 'is_parent', False)) \
                else redirect('dashboard:router')
        return view(request, *args, **kwargs)
    wrapped.__name__, wrapped.__doc__ = view.__name__, view.__doc__
    return login_required(wrapped)


def _back(request, default):
    nxt = request.POST.get('next') or ''
    return redirect(nxt if nxt.startswith('/') and not nxt.startswith('//') else default)


# ─────────────────────────────────────────────────────────────────────────────
# Overview and labs
# ─────────────────────────────────────────────────────────────────────────────

@_staff_only
def home(request):
    tenant = request.tenant
    labs = list(Lab.objects.filter(tenant=tenant).prefetch_related('subjects').select_related('in_charge'))
    for lab in labs:
        lab.st = svc.status(lab)
    upcoming = (PracticalSession.objects.filter(tenant=tenant, status=PracticalSession.PLANNED,
                                                date__gte=timezone.localdate() - timedelta(days=7))
                .select_related('practical__subject', 'school_class', 'lab', 'teacher').order_by('date')[:10])
    return render(request, 'labs/home.html', {
        'labs': labs, 'ov': svc.overview(tenant), 'upcoming': upcoming, 'admin': svc.is_admin(request.user, tenant),
        'today': timezone.localdate(),
        'incidents': SafetyIncident.objects.filter(tenant=tenant, closed=False).select_related('lab')[:6],
    })


@_staff_only
def lab_form(request, pk=None):
    lab = get_object_or_404(Lab, pk=pk, tenant=request.tenant) if pk else None
    if lab and not svc.can_manage(request.user, lab) or (not lab and not svc.is_admin(request.user, request.tenant)):
        messages.error(request, 'Only administrators (or the lab head) can set up labs.')
        return redirect('labs:home')
    if request.method == 'POST':
        try:
            lab = svc.save_lab(request.tenant, request.POST, lab)
            messages.success(request, f'{lab.name} saved. It can now be booked from its page and in the timetable.')
            return redirect('labs:lab', lab.pk)
        except svc.LabError as problem:
            messages.error(request, str(problem))
    return render(request, 'labs/lab_form.html', {
        'lab': lab, 'kinds': Lab.KINDS, 'subjects': Subject.objects.filter(tenant=request.tenant).order_by('name'),
        'staff': Staff.objects.filter(tenant=request.tenant).order_by('full_name'),
        'chosen_subjects': set(lab.subjects.values_list('pk', flat=True)) if lab else set(),
        'chosen_techs': set(lab.technicians.values_list('pk', flat=True)) if lab else set(),
    })


@_staff_only
def lab(request, pk):
    tenant = request.tenant
    lab = get_object_or_404(Lab.objects.select_related('in_charge', 'room', 'space'), pk=pk, tenant=tenant)
    if request.method == 'POST':
        return _lab_action(request, lab)
    tab = request.GET.get('tab') or 'schedule'
    try:
        start = svc._date(request.GET.get('week')) if request.GET.get('week') else timezone.localdate()
    except svc.LabError:
        start = timezone.localdate()
    monday = start - timedelta(days=start.weekday())
    equipment = list(lab.equipment.filter(active=True))
    from facilities.models import HAZARDS
    chems = list(svc.chemicals(lab))
    hazard_names = dict(HAZARDS)
    for c in chems:
        c.hazard_labels = [hazard_names.get(h, h) for h in (c.hazards or [])]
    return render(request, 'labs/lab.html', {
        'lab': lab, 'tab': tab, 'st': svc.status(lab), 'manage': svc.can_manage(request.user, lab),
        'week': svc.week(lab, start), 'monday': monday, 'prev_week': monday - timedelta(days=7),
        'next_week': monday + timedelta(days=7), 'equipment': equipment, 'categories': Equipment.CATEGORIES,
        'moves': EquipmentMove.KINDS, 'chemicals': chems, 'hazards': HAZARDS,
        'breakages': lab.breakages.select_related('student', 'equipment')[:60], 'causes': Breakage.CAUSES,
        'checks': lab.checks.all()[:12], 'checklist': list(enumerate(svc.CHECKLIST)),
        'incidents': lab.incidents.all()[:30], 'incident_kinds': SafetyIncident.KINDS,
        'severities': SafetyIncident.SEVERITIES,
        'classes': Class.objects.filter(tenant=tenant).order_by('name'),
        'subjects': Subject.objects.filter(tenant=tenant).order_by('name'),
        'practicals': Practical.objects.filter(tenant=tenant, active=True).select_related('subject'),
        'students': Student.objects.filter(tenant=tenant).select_related('class_assigned').order_by('full_name'),
        'today': timezone.localdate(),
        'recent_sessions': lab.sessions.select_related('practical', 'school_class').order_by('-date')[:20],
    })


def _lab_action(request, lab):
    p, action, user = request.POST, request.POST.get('action'), request.user
    back = f"{reverse('labs:lab', args=[lab.pk])}?tab={p.get('tab') or 'schedule'}"
    try:
        if action == 'book':
            made, skipped = svc.book(lab, user, p)
            if made:
                messages.success(request, f'Booked {lab.name} for {len(made)} session{"s" if len(made) != 1 else ""}.')
            for day, reasons in skipped:
                messages.warning(request, f'{day:%a %d %b} not booked — {" ".join(reasons)}')
            if made:
                back = f"{reverse('labs:lab', args=[lab.pk])}?week={made[0].booking.date:%Y-%m-%d}"
        elif action == 'cancel_booking':
            svc.cancel(get_object_or_404(LabBooking, pk=p.get('booking'), lab=lab), user)
            messages.success(request, 'Booking cancelled.')
        elif action == 'prepared':
            svc.mark_prepared(get_object_or_404(LabBooking, pk=p.get('booking'), lab=lab), user)
        elif action == 'equipment':
            eq = get_object_or_404(Equipment, pk=p.get('equipment'), lab=lab) if p.get('equipment') else None
            eq = svc.save_equipment(lab, user, p, eq)
            messages.success(request, f'{eq.name} saved.')
        elif action == 'move':
            eq = get_object_or_404(Equipment, pk=p.get('equipment'), lab=lab)
            m = svc.move(eq, user, p.get('kind'), p.get('quantity') or 0, p.get('note'))
            messages.success(request, f'{eq.name}: {m.get_kind_display().lower()}.')
        elif action == 'breakage':
            b = svc.report_breakage(lab, user, p)
            messages.success(request, f'Breakage recorded: {b.quantity} × {b.item}'
                                      + (f' — ₦{b.charge:,.2f} charged to {b.student.full_name}\'s family.' if b.charge else '.'))
        elif action == 'settle':
            b = svc.settle(get_object_or_404(Breakage, pk=p.get('breakage'), lab=lab), user, p.get('status'))
            messages.success(request, f'Charge marked {b.get_status_display().lower()}.')
        elif action == 'check':
            c = svc.record_check(lab, user, p)
            (messages.success if c.passed else messages.warning)(
                request, 'Inspection passed.' if c.passed else f'Inspection recorded — {len(c.failed_items)} item(s) need attention. '
                                                               'Administrators have been told.')
        elif action == 'incident':
            inc = svc.report_incident(lab, user, p, p.getlist('students'))
            messages.success(request, 'Incident reported to the administrators'
                                      + (' and the families of the students involved.' if inc.families_told else '.'))
            back = reverse('labs:incident', args=[inc.pk])
        elif action == 'chemical':
            from facilities import services as fac
            from facilities.models import Chemical
            if not svc.can_manage(user, lab):
                raise svc.LabError('Only lab staff can change chemical stock.')
            if p.get('chemical'):
                chem = get_object_or_404(Chemical, pk=p.get('chemical'), tenant=lab.tenant, space=lab.space)
                fac.chemical_move(chem, user, p.get('kind'), p.get('quantity'), p.get('purpose'))
                messages.success(request, f'{chem.name} updated: {chem.quantity:g} → see the register below.')
            else:
                _add_chemical(lab, p)
                messages.success(request, 'Chemical added to the register.')
        else:
            raise svc.LabError('Unknown action.')
    except (svc.LabError, FacilityError) as problem:
        messages.error(request, str(problem))
    return redirect(back)


def _add_chemical(lab, p):
    from facilities.models import HAZARDS, Chemical
    name = (p.get('name') or '').strip()[:120]
    if not name:
        raise svc.LabError('Name the chemical.')
    qty = svc._money(p.get('quantity'), 'Quantity')
    reorder = svc._money(p.get('reorder_level'), 'Reorder level')
    Chemical.objects.create(
        tenant=lab.tenant, name=name, formula=(p.get('formula') or '').strip()[:60], quantity=qty,
        unit=(p.get('unit') or 'g').strip()[:10], reorder_level=reorder, space=lab.space,
        storage=(p.get('storage') or '').strip()[:160],
        hazards=[h for h in p.getlist('hazards') if h in dict(HAZARDS)],
        expires_on=svc._date(p.get('expires_on')) if p.get('expires_on') else None,
        first_aid=(p.get('first_aid') or '').strip()[:255])


@_staff_only
def incident(request, pk):
    inc = get_object_or_404(SafetyIncident.objects.select_related('lab', 'session__practical'), pk=pk, tenant=request.tenant)
    if request.method == 'POST':
        try:
            svc.close_incident(inc, request.user, request.POST.get('action'))
            messages.success(request, 'Incident closed.')
        except svc.LabError as problem:
            messages.error(request, str(problem))
        return redirect('labs:incident', inc.pk)
    return render(request, 'labs/incident.html', {'inc': inc, 'manage': svc.can_manage(request.user, inc.lab),
                                                  'reporter': svc._name(inc.reported_by)})


# ─────────────────────────────────────────────────────────────────────────────
# Practicals
# ─────────────────────────────────────────────────────────────────────────────

@_staff_only
def practicals(request):
    tenant = request.tenant
    qs = Practical.objects.filter(tenant=tenant, active=True).select_related('subject', 'lab')
    subject = request.GET.get('subject') or ''
    if subject.isdigit():
        qs = qs.filter(subject_id=subject)
    from django.db.models import Count, Q
    qs = qs.annotate(done=Count('sessions', filter=Q(sessions__status=PracticalSession.DONE)))
    return render(request, 'labs/practicals.html', {
        'practicals': qs, 'subjects': Subject.objects.filter(tenant=tenant).order_by('name'), 'subject': subject})


@_staff_only
def practical_form(request, pk=None):
    tenant = request.tenant
    p = get_object_or_404(Practical, pk=pk, tenant=tenant) if pk else None
    if request.method == 'POST':
        try:
            p = svc.save_practical(tenant, request.user, request.POST, p)
            messages.success(request, f'"{p.title}" saved.')
            return redirect('labs:practical', p.pk)
        except svc.LabError as problem:
            messages.error(request, str(problem))
    from facilities.models import Chemical
    rows = [(c.chemical_id, c.per_group) for c in p.chemicals.all()] if p else []
    rows += [('', '')] * max(3, 6 - len(rows))
    return render(request, 'labs/practical_form.html', {
        'p': p, 'kinds': Practical.KINDS, 'subjects': Subject.objects.filter(tenant=tenant).order_by('name'),
        'labs': Lab.objects.filter(tenant=tenant, active=True),
        'all_chemicals': Chemical.objects.filter(tenant=tenant, active=True).order_by('name'), 'chem_rows': rows,
        'rubric_text': '\n'.join(f"{r['criterion']} | {r['max']}" for r in (p.rubric if p else [])) or
                       'Setting up apparatus | 4\nObservations and readings | 6\nRecording and calculations | 6\nConclusion and precautions | 4',
    })


@_staff_only
def practical(request, pk):
    tenant = request.tenant
    p = get_object_or_404(Practical.objects.select_related('subject', 'lab'), pk=pk, tenant=tenant)
    if request.method == 'POST':
        try:
            s = svc.plan_session(p, request.user, request.POST)
            messages.success(request, f'{s.school_class.name} is down for this practical on {s.date:%a %d %b}'
                                      + (f', with {s.lab.name} booked.' if s.booking_id else '.'))
            return redirect('labs:session', s.pk)
        except svc.LabError as problem:
            messages.error(request, str(problem))
    return render(request, 'labs/practical.html', {
        'p': p, 'sessions': p.sessions.select_related('school_class', 'lab', 'teacher').order_by('-date'),
        'classes': Class.objects.filter(tenant=tenant).order_by('name'),
        'labs': Lab.objects.filter(tenant=tenant, active=True),
        'chemicals': p.chemicals.select_related('chemical'), 'today': timezone.localdate(),
        'print': request.GET.get('print') == '1'})


@_staff_only
def session(request, pk):
    s = get_object_or_404(PracticalSession.objects.select_related('practical__subject', 'school_class', 'lab', 'teacher',
                                                                  'booking__booking'), pk=pk, tenant=request.tenant)
    if request.method == 'POST':
        action = request.POST.get('action') or 'save'
        try:
            if action == 'chemicals':
                used = svc.use_chemicals(s, request.user)
                messages.success(request, 'Deducted from stock: ' + ', '.join(
                    f'{n.chemical.name} {n.per_group * s.groups:g} {n.chemical.unit}' for n in used) + '.')
            elif action == 'cancel':
                svc.cancel_session(s, request.user)
                messages.success(request, 'Practical cancelled' + (' and the lab booking released.' if s.booking_id else '.'))
            else:
                svc.save_register(s, request.user, request.POST)
                messages.success(request, 'Practical marked as done.' if request.POST.get('finish') else 'Register and marks saved.')
        except svc.LabError as problem:
            messages.error(request, str(problem))
        return redirect('labs:session', s.pk)
    records = svc.roster(s)
    rubric = s.practical.rubric
    for r in records:
        r.marks = [(r.scores[i] if i < len(r.scores or []) else None) for i in range(len(rubric))]
    chems = list(s.practical.chemicals.select_related('chemical'))
    for c in chems:
        c.needed = c.per_group * s.groups
    present = sum(1 for r in records if r.attended)
    scored = [r.score for r in records if r.score is not None]
    return render(request, 'labs/session.html', {
        's': s, 'records': records, 'rubric': rubric, 'can_mark': svc.can_mark(request.user, s),
        'statuses': PracticalRecord.STATUSES, 'chemicals': chems,
        'present': present, 'average': round(sum(scored) / len(scored), 1) if scored else None,
        'breakages': s.breakages.all(), 'incidents': s.incidents.all()})


@_staff_only
def readiness(request):
    tenant = request.tenant
    classes = Class.objects.filter(tenant=tenant).order_by('name')
    subjects = Subject.objects.filter(tenant=tenant, pk__in=Practical.objects.filter(tenant=tenant)
                                      .values('subject')).order_by('name')
    school_class = classes.filter(pk=request.GET.get('class') or request.POST.get('class')).first()
    subject = subjects.filter(pk=request.GET.get('subject') or request.POST.get('subject')).first()
    term = svc.active_term(tenant)
    from results.models import AssessmentScheme
    columns = AssessmentScheme.for_tenant(tenant).ca_columns
    if request.method == 'POST' and school_class and subject:
        try:
            n, col = svc.send_to_results(school_class, subject, term, request.POST.get('column'), request.user)
            messages.success(request, f'{n} practical score{"s" if n != 1 else ""} put in the "{col["label"]}" column '
                                      f'(out of {col["max"]}) for {subject.name}, {school_class.name}.')
        except svc.LabError as problem:
            messages.error(request, str(problem))
        return redirect(f"{reverse('labs:readiness')}?class={school_class.pk}&subject={subject.pk}")
    data = svc.readiness(school_class, subject, term if request.GET.get('all') != '1' else None) \
        if school_class and subject else None
    return render(request, 'labs/readiness.html', {
        'classes': classes, 'subjects': subjects, 'school_class': school_class, 'subject': subject, 'term': term,
        'data': data, 'columns': columns, 'all_terms': request.GET.get('all') == '1',
        'risk_att': svc.AT_RISK_ATTENDANCE, 'risk_score': svc.AT_RISK_SCORE})


# ─────────────────────────────────────────────────────────────────────────────
# Students and families
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def my(request):
    """A student's practical record and lab charges — or, for a parent, each child's."""
    children = []
    student = Student.objects.filter(tenant=request.tenant, user=request.user).first()
    if student:
        children = [student]
    elif getattr(request.user, 'is_parent', False):
        from finance.views import _get_all_children
        children = list(_get_all_children(SimpleNamespace(user=request.user)).filter(tenant=request.tenant))
    if not children:
        if svc.is_staff(request.user, request.tenant):
            return redirect('labs:home')
        messages.error(request, 'This page is for students and parents.')
        return redirect('dashboard:router')
    if request.method == 'POST':
        b = get_object_or_404(Breakage, pk=request.POST.get('breakage'), student__in=children)
        try:
            svc.pay_from_wallet(b, request.user)
            messages.success(request, f'₦{b.charge:,.2f} paid from the family wallet.')
        except svc.LabError as problem:
            messages.error(request, str(problem))
        return redirect('labs:my')
    rows = []
    for child in children:
        recs = svc.student_record(child)
        all_recs = [r for v in recs.values() for r in v]
        attended = [r for r in all_recs if r.attended]
        rows.append({'student': child, 'subjects': recs, 'total': len(all_recs), 'attended': len(attended),
                     'charges': child.lab_breakages.exclude(status='NONE').select_related('lab')})
    return render(request, 'labs/my.html', {'rows': rows, 'parent': not student})
