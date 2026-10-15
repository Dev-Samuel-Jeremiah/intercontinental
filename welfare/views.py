"""
welfare/views.py
────────────────
Staff pages for health (sick bay, medical records, medication, screening,
immunisations, health plans, accidents, emergency contacts), discipline
(incidents, points, detentions, suspensions, restorative meetings), care
(counselling, safeguarding, support plans), sports, resources and settings;
"My wellbeing" for students; and the parents' welfare page.
"""
from datetime import datetime, timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from users.models import ENROLLED_STATUSES, Class, Staff, Student

from . import services as svc
from .models import (Accident, Concern, CounsellingCase, Detention, DetentionSession, FitnessTest, HealthPlan,
                     Immunisation, Incident, MedicalProfile, MedicationDose, MedicationPlan, Resource,
                     RestorativeMeeting, Screening, SupportPlan, SuspensionCase, Team, TeamMember, Threshold,
                     ThresholdHit)


def _deny(request, text='You do not have access to that page.'):
    messages.error(request, text)
    return redirect('dashboard:router')


def _gate(check, text='You do not have access to that page.'):
    def deco(view):
        def wrap(request, *a, **kw):
            if not check(request.user, request.tenant):
                return _deny(request, text)
            return view(request, *a, **kw)
        wrap.__name__ = view.__name__
        return login_required(wrap)
    return deco


def _staff(user, tenant):
    return bool(svc.staff_of(user, tenant)) or getattr(user, 'is_superuser', False)


staff_only = _gate(_staff, 'This page is for staff.')
nurse_only = _gate(svc.is_nurse, 'Only the school nurse can open medical records.')
discipline_only = _gate(svc.is_discipline, 'Only the discipline team can do that.')
counsellor_only = _gate(svc.is_counsellor, 'Counselling records are for counsellors only.')
admin_only = _gate(lambda u, t: svc._is_admin(u), 'Only an administrator can do that.')


def _students(request):
    return Student.objects.filter(tenant=request.tenant, status__in=ENROLLED_STATUSES) \
        .order_by('full_name').only('id', 'full_name', 'admission_number')


def _pick(request, key='student'):
    return Student.objects.filter(tenant=request.tenant, pk=request.POST.get(key) or request.GET.get(key) or 0) \
        .select_related('class_assigned').first()


def _pick_many(request, key='students'):
    return list(Student.objects.filter(tenant=request.tenant, pk__in=request.POST.getlist(key)))


def _dt(raw):
    try:
        return timezone.make_aware(datetime.strptime(raw or '', '%Y-%m-%dT%H:%M'))
    except ValueError:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Overview
# ─────────────────────────────────────────────────────────────────────────────

@staff_only
def home(request):
    t, u = request.tenant, request.user
    from hostel.models import SickBayVisit
    ctx = {'nurse': svc.is_nurse(u, t), 'discipline': svc.is_discipline(u, t), 'lead': svc.is_lead(u, t),
           'counsellor': svc.is_counsellor(u, t), 'admin': svc._is_admin(u), 'today': timezone.localdate()}
    if ctx['nurse']:
        ctx.update(inside=SickBayVisit.objects.filter(tenant=t, discharged_at__isnull=True)
                   .select_related('student__class_assigned'),
                   meds=[p for p in svc.plans_today(t) if not p.given_today],
                   jabs=svc.immunisations_due(t)[:8])
    if ctx['discipline']:
        ctx.update(recent=Incident.objects.filter(tenant=t, date__gte=timezone.localdate() - timedelta(days=7))
                   .select_related('student').count(),
                   hits=ThresholdHit.objects.filter(tenant=t, done=False).select_related('student', 'threshold')[:10],
                   pending=SuspensionCase.objects.filter(tenant=t, status='PROPOSED').select_related('student'))
    if ctx['lead']:
        ctx['concerns'] = Concern.objects.filter(tenant=t, status='NEW').select_related('student')
    if ctx['counsellor']:
        ctx['requests'] = CounsellingCase.objects.filter(tenant=t, status='REQUESTED').select_related('student')
    ctx['detentions'] = DetentionSession.objects.filter(tenant=t, date=timezone.localdate())
    ctx['accidents'] = Accident.objects.filter(tenant=t, closed=False).count()
    ctx['plans_due'] = svc.plans_due(t)[:8]
    return render(request, 'welfare/home.html', ctx)


# ─────────────────────────────────────────────────────────────────────────────
# F381 sick bay (school-wide), F382 medical record, F384 medication
# ─────────────────────────────────────────────────────────────────────────────

@nurse_only
def sick_bay(request):
    from hostel import boarding as bd
    from hostel.models import SickBayVisit
    if request.method == 'POST':
        try:
            if request.POST.get('action') == 'admit':
                st = _pick(request)
                if not st:
                    raise svc.WelfareError('Choose the student.')
                bd.admit(st, request.user, request.POST.get('complaint'), request.POST.get('temperature'),
                         request.POST.get('treatment'), tell_parent=bool(request.POST.get('tell_parent')))
                messages.success(request, f'{st.full_name} is in the sick bay.')
            else:
                v = get_object_or_404(SickBayVisit, pk=request.POST.get('id'), tenant=request.tenant)
                bd.discharge(v, request.POST.get('outcome'), request.POST.get('treatment'))
                messages.success(request, f'{v.student.full_name} discharged.')
        except (svc.WelfareError, bd.BoardingError) as problem:
            messages.error(request, str(problem))
        return redirect('welfare:sick_bay')
    inside = list(SickBayVisit.objects.filter(tenant=request.tenant, discharged_at__isnull=True)
                  .select_related('student__class_assigned', 'student__medical'))
    return render(request, 'welfare/sick_bay.html', {
        'inside': inside, 'students': _students(request),
        'history': SickBayVisit.objects.filter(tenant=request.tenant, discharged_at__isnull=False)
        .select_related('student')[:40],
        'outcomes': [o for o in SickBayVisit.OUTCOMES if o[0]]})


@nurse_only
def medical(request, student_id):
    st = get_object_or_404(Student.objects.select_related('class_assigned'), pk=student_id, tenant=request.tenant)
    if request.method == 'POST':
        a = request.POST.get('action')
        try:
            if a == 'profile':
                svc.save_profile(st, request.POST, request.user)
            elif a == 'jab':
                svc.add_immunisation(st, request.POST, request.user, request.FILES.get('proof'), verified=True)
            elif a == 'verify':
                Immunisation.objects.filter(student=st, pk=request.POST.get('id')).update(verified=True)
            elif a == 'plan':
                svc.add_plan(st, request.POST, request.user)
            elif a == 'plan_stop':
                MedicationPlan.objects.filter(student=st, pk=request.POST.get('id')).update(active=False)
            elif a == 'screening':
                svc.save_screening(st, request.POST, request.user)
            messages.success(request, 'Saved.')
        except svc.WelfareError as problem:
            messages.error(request, str(problem))
        return redirect('welfare:medical', student_id=st.pk)
    from hostel.models import SickBayVisit
    return render(request, 'welfare/medical.html', {
        'st': st, 'p': svc.profile_for(st), 'jabs': st.immunisations.all(),
        'plans': st.medication_plans.all(), 'doses': st.medication_doses.select_related('given_by')[:20],
        'screens': st.screenings.all()[:10], 'visits': SickBayVisit.objects.filter(student=st)[:15],
        'accidents': st.accidents.all()[:10], 'card': svc.emergency_card(st),
        'blood': MedicalProfile.BLOOD, 'genotypes': MedicalProfile.GENOTYPE, 'vaccines': Immunisation.VACCINES,
        'health_plans': HealthPlan.objects.filter(tenant=request.tenant, active=True), 'today': timezone.localdate()})


@nurse_only
def immunisation_file(request, pk):
    imm = get_object_or_404(Immunisation, pk=pk, tenant=request.tenant)
    return _serve(imm.proof, 'vaccination')


def _serve(field, name):
    import os

    from django.http import FileResponse

    from users.records import CONTENT_TYPES
    if not field:
        raise Http404
    ext = os.path.splitext(field.name)[1].lower()
    try:
        return FileResponse(field.open('rb'), content_type=CONTENT_TYPES.get(ext, 'application/octet-stream'),
                            filename=f'{name}{ext}')
    except (FileNotFoundError, OSError):
        raise Http404


@nurse_only
def medication(request):
    if request.method == 'POST':
        try:
            st = _pick(request)
            if not st:
                raise svc.WelfareError('Choose the student.')
            plan = MedicationPlan.objects.filter(tenant=request.tenant, pk=request.POST.get('plan') or 0,
                                                 student=st).first()
            svc.give_dose(st, request.user, request.POST, plan)
            messages.success(request, f'Recorded for {st.full_name}.')
        except svc.WelfareError as problem:
            messages.error(request, str(problem))
        return redirect('welfare:medication')
    return render(request, 'welfare/medication.html', {
        'plans': svc.plans_today(request.tenant), 'students': _students(request),
        'doses': MedicationDose.objects.filter(tenant=request.tenant).select_related('student', 'given_by')[:40]})


@nurse_only
def screening(request):
    cls = Class.objects.filter(tenant=request.tenant, pk=request.GET.get('class') or request.POST.get('class') or 0) \
        .first()
    if request.method == 'POST':
        st = _pick(request)
        try:
            if not st:
                raise svc.WelfareError('Choose the student.')
            svc.save_screening(st, request.POST, request.user)
            messages.success(request, f'Saved for {st.full_name}.')
        except svc.WelfareError as problem:
            messages.error(request, str(problem))
        return redirect(f'{request.path}?class={cls.pk}' if cls else request.path)
    term = svc.current_term(request.tenant)
    return render(request, 'welfare/screening.html', {
        'classes': Class.objects.filter(tenant=request.tenant).order_by('name'), 'cls': cls,
        'rows': svc.screening_sheet(cls, term) if cls else [], 'term': term, 'today': timezone.localdate(),
        'referred': Screening.objects.filter(tenant=request.tenant, refer=True).select_related('student')[:15]})


@nurse_only
def immunisations(request):
    if request.method == 'POST':
        st = _pick(request)
        try:
            if not st:
                raise svc.WelfareError('Choose the student.')
            svc.add_immunisation(st, request.POST, request.user, request.FILES.get('proof'), verified=True)
            messages.success(request, 'Saved.')
        except svc.WelfareError as problem:
            messages.error(request, str(problem))
        return redirect('welfare:immunisations')
    return render(request, 'welfare/immunisations.html', {
        'due': svc.immunisations_due(request.tenant), 'students': _students(request),
        'unverified': Immunisation.objects.filter(tenant=request.tenant, verified=False).select_related('student'),
        'vaccines': Immunisation.VACCINES, 'today': timezone.localdate()})


@nurse_only
def health_plans(request):
    if request.method == 'POST':
        if request.POST.get('action') == 'toggle':
            hp = get_object_or_404(HealthPlan, pk=request.POST.get('id'), tenant=request.tenant)
            hp.active = not hp.active
            hp.save(update_fields=['active'])
        elif (request.POST.get('name') or '').strip() and (request.POST.get('provider') or '').strip():
            HealthPlan.objects.create(
                tenant=request.tenant, name=request.POST['name'].strip()[:120],
                provider=request.POST['provider'].strip()[:120], phone=(request.POST.get('phone') or '')[:30],
                hospital=(request.POST.get('hospital') or '')[:200], covers=request.POST.get('covers') or '',
                starts=svc._date(request.POST.get('starts')), ends=svc._date(request.POST.get('ends')))
        else:
            messages.error(request, 'Give the plan name and the provider.')
            return redirect('welfare:health_plans')
        messages.success(request, 'Saved.')
        return redirect('welfare:health_plans')
    plans = list(HealthPlan.objects.filter(tenant=request.tenant))
    for hp in plans:
        hp.list = svc.plan_members(hp)
    uncovered = Student.objects.filter(tenant=request.tenant, status__in=ENROLLED_STATUSES) \
        .exclude(medical__plan__isnull=False).exclude(medical__other_insurance__gt='').count()
    return render(request, 'welfare/health_plans.html', {'plans': plans, 'uncovered': uncovered,
                                                         'today': timezone.localdate()})


# ─────────────────────────────────────────────────────────────────────────────
# F393 accidents, F394 emergency contacts
# ─────────────────────────────────────────────────────────────────────────────

@staff_only
def accidents(request):
    t = request.tenant
    nurse = svc.is_nurse(request.user, t)
    if request.method == 'POST':
        try:
            if request.POST.get('op') == 'follow' and nurse:
                a = get_object_or_404(Accident, pk=request.POST.get('id'), tenant=t)
                a.follow_up = (request.POST.get('follow_up') or '').strip()
                a.closed = bool(request.POST.get('closed'))
                a.save(update_fields=['follow_up', 'closed'])
            else:
                svc.report_accident(t, request.user, request.POST, _pick(request))
            messages.success(request, 'Saved.')
        except svc.WelfareError as problem:
            messages.error(request, str(problem))
        return redirect('welfare:accidents')
    qs = Accident.objects.filter(tenant=t).select_related('student__class_assigned', 'reported_by')
    if not nurse:
        qs = qs.filter(reported_by=request.user)
    return render(request, 'welfare/accidents.html', {
        'items': qs[:60], 'nurse': nurse, 'students': _students(request), 'places': Accident.PLACES,
        'severities': Accident.SEVERITY, 'now': timezone.localtime().strftime('%Y-%m-%dT%H:%M')})


@staff_only
def emergency(request):
    t = request.tenant
    cls = Class.objects.filter(tenant=t, pk=request.GET.get('class') or 0).first()
    st = Student.objects.filter(tenant=t, pk=request.GET.get('student') or 0).select_related('class_assigned').first()
    cards = []
    if st:
        cards = [svc.emergency_card(st)]
    elif cls:
        cards = [svc.emergency_card(s) for s in Student.objects.filter(class_assigned=cls, status__in=ENROLLED_STATUSES)
                 .order_by('full_name')]
    return render(request, 'welfare/emergency.html', {
        'q': request.GET.get('q', ''), 'found': svc.find_students(t, request.GET.get('q')), 'cards': cards,
        'classes': Class.objects.filter(tenant=t).order_by('name'), 'cls': cls})


# ─────────────────────────────────────────────────────────────────────────────
# F386 incidents, F387 points, F388 detentions
# ─────────────────────────────────────────────────────────────────────────────

@staff_only
def incidents(request):
    t = request.tenant
    disc = svc.is_discipline(request.user, t)
    if request.method == 'POST':
        try:
            if request.POST.get('op') == 'close' and disc:
                Incident.objects.filter(tenant=t, pk=request.POST.get('id')).update(status='CLOSED')
            else:
                made = svc.report_incident(t, request.user, _pick_many(request), request.POST)
                messages.success(request, f'Recorded for {len(made)} student{"s" if len(made) != 1 else ""}.')
        except svc.WelfareError as problem:
            messages.error(request, str(problem))
        return redirect('welfare:incidents')
    qs = Incident.objects.filter(tenant=t).select_related('student__class_assigned', 'reported_by')
    if not disc:
        qs = qs.filter(reported_by=request.user)
    if request.GET.get('student'):
        qs = qs.filter(student_id=request.GET['student'])
    if request.GET.get('status') in ('OPEN', 'CLOSED'):
        qs = qs.filter(status=request.GET['status'])
    return render(request, 'welfare/incidents.html', {
        'items': qs[:100], 'disc': disc, 'students': _students(request), 'categories': Incident.CATEGORIES,
        'severities': Incident.SEVERITY, 'actions': Incident.ACTIONS, 'today': timezone.localdate()})


@discipline_only
def points(request):
    t = request.tenant
    if request.method == 'POST':
        a = request.POST.get('action')
        if a == 'threshold':
            pts = str(request.POST.get('points') or '')
            if (request.POST.get('name') or '').strip() and pts.isdigit() and request.POST.get('kind') in ('MERIT',
                                                                                                         'DEMERIT'):
                Threshold.objects.create(tenant=t, name=request.POST['name'].strip()[:80], kind=request.POST['kind'],
                                         points=int(pts), action=(request.POST.get('then') or '').strip()[:255],
                                         tell_parents=bool(request.POST.get('tell_parents')))
                messages.success(request, 'Threshold added. Students already past it will be picked up as points '
                                          'come in.')
            else:
                messages.error(request, 'Give a name, the kind and the number of points.')
        elif a == 'delete':
            Threshold.objects.filter(tenant=t, pk=request.POST.get('id')).delete()
        elif a == 'done':
            ThresholdHit.objects.filter(tenant=t, pk=request.POST.get('id')).update(done=True)
        elif a == 'recheck':
            n = sum(len(svc.check_thresholds(s)) for s in Student.objects.filter(tenant=t,
                                                                                status__in=ENROLLED_STATUSES))
            messages.success(request, f'{n} new threshold{"s" if n != 1 else ""} reached.')
        return redirect('welfare:points')
    return render(request, 'welfare/points.html', {
        'thresholds': Threshold.objects.filter(tenant=t), 'merits': svc.leaderboard(t, 'MERIT'),
        'demerits': svc.leaderboard(t, 'DEMERIT'),
        'hits': ThresholdHit.objects.filter(tenant=t).select_related('student__class_assigned', 'threshold')[:40]})


@staff_only
def detentions(request):
    t = request.tenant
    disc = svc.is_discipline(request.user, t)
    if request.method == 'POST':
        a = request.POST.get('action')
        try:
            if a == 'session' and disc:
                d, s, e = svc._date(request.POST.get('date')), request.POST.get('starts'), request.POST.get('ends')
                try:
                    s, e = datetime.strptime(s or '', '%H:%M').time(), datetime.strptime(e or '', '%H:%M').time()
                except ValueError:
                    raise svc.WelfareError('Give the start and end times.')
                if not d or e <= s or not (request.POST.get('room') or '').strip():
                    raise svc.WelfareError('Give the date, room and times.')
                DetentionSession.objects.create(
                    tenant=t, date=d, starts=s, ends=e, room=request.POST['room'].strip()[:60],
                    supervisor=Staff.objects.filter(tenant=t, pk=request.POST.get('supervisor') or 0).first(),
                    capacity=int(request.POST.get('capacity')) if str(request.POST.get('capacity')).isdigit() else 30)
            elif a == 'assign':
                session = get_object_or_404(DetentionSession, pk=request.POST.get('session'), tenant=t)
                for st in _pick_many(request):
                    svc.set_detention(session, st, request.POST.get('reason'), request.user)
                messages.success(request, 'Detention set; families have been told.')
                return redirect('welfare:detentions')
            elif a == 'mark' and disc:
                session = get_object_or_404(DetentionSession, pk=request.POST.get('session'), tenant=t)
                svc.mark_detentions(session, {k[2:]: v for k, v in request.POST.items() if k.startswith('d_')})
            messages.success(request, 'Saved.')
        except svc.WelfareError as problem:
            messages.error(request, str(problem))
        return redirect('welfare:detentions')
    today = timezone.localdate()
    return render(request, 'welfare/detentions.html', {
        'upcoming': DetentionSession.objects.filter(tenant=t, date__gte=today).order_by('date', 'starts')
        .prefetch_related('detentions__student'),
        'past': DetentionSession.objects.filter(tenant=t, date__lt=today).prefetch_related('detentions__student')[:10],
        'students': _students(request), 'disc': disc,
        'staff': Staff.objects.filter(tenant=t, user__is_active=True).order_by('full_name'),
        'statuses': Detention.STATUSES})


# ─────────────────────────────────────────────────────────────────────────────
# F389 suspensions, F399 restorative meetings
# ─────────────────────────────────────────────────────────────────────────────

@discipline_only
def suspensions(request):
    t = request.tenant
    if request.method == 'POST':
        a = request.POST.get('action')
        try:
            if a == 'propose':
                st = _pick(request)
                if not st:
                    raise svc.WelfareError('Choose the student.')
                svc.propose_suspension(st, request.user, request.POST)
                messages.success(request, 'Sent to the principal for approval.')
            else:
                case = get_object_or_404(SuspensionCase, pk=request.POST.get('id'), tenant=t)
                if a in ('approve', 'decline'):
                    if not svc._is_admin(request.user):
                        raise svc.WelfareError('Only an administrator can approve a suspension.')
                    svc.decide_suspension(case, request.user, a == 'approve', request.POST.get('note'),
                                          _dt(request.POST.get('meeting_at')))
                    messages.success(request, 'Approved — the letter has been emailed to the family.' if
                                     a == 'approve' else 'Declined.')
                elif a == 'return':
                    svc.record_return(case, request.user, request.POST.get('notes'))
                    messages.success(request, f'{case.student.full_name} is back on the roll.')
        except svc.WelfareError as problem:
            messages.error(request, str(problem))
        return redirect('welfare:suspensions')
    return render(request, 'welfare/suspensions.html', {
        'items': SuspensionCase.objects.filter(tenant=t).select_related('student__class_assigned', 'proposed_by')[:50],
        'students': _students(request), 'admin': svc._is_admin(request.user), 'today': timezone.localdate()})


@discipline_only
def suspension_letter(request, pk):
    case = get_object_or_404(SuspensionCase.objects.select_related('student__class_assigned'), pk=pk,
                             tenant=request.tenant)
    return render(request, 'welfare/letter.html', {'case': case, 'text': svc.letter_text(case),
                                                   'school': request.tenant.name})


@staff_only
def restorative(request):
    t = request.tenant
    disc = svc.is_discipline(request.user, t)
    if request.method == 'POST':
        try:
            if request.POST.get('op') == 'followed':
                m = get_object_or_404(RestorativeMeeting, pk=request.POST.get('id'), tenant=t)
                m.followed_up, m.follow_up_note = True, (request.POST.get('follow_up_note') or '')[:255]
                m.save(update_fields=['followed_up', 'follow_up_note'])
            else:
                svc.save_restorative(t, request.user, _pick_many(request), request.POST)
            messages.success(request, 'Saved.')
        except svc.WelfareError as problem:
            messages.error(request, str(problem))
        return redirect('welfare:restorative')
    qs = RestorativeMeeting.objects.filter(tenant=t).prefetch_related('students')
    if not disc:
        qs = qs.filter(facilitator=request.user)
    return render(request, 'welfare/restorative.html', {'items': qs[:50], 'students': _students(request),
                                                        'outcomes': RestorativeMeeting.OUTCOMES,
                                                        'today': timezone.localdate()})


# ─────────────────────────────────────────────────────────────────────────────
# F390 counselling, F391 safeguarding
# ─────────────────────────────────────────────────────────────────────────────

@counsellor_only
def counselling(request):
    t = request.tenant
    if request.method == 'POST':
        st = _pick(request)
        if st:
            case = svc.open_case(st, request.user, request.POST)
            return redirect('welfare:case', pk=case.pk)
        messages.error(request, 'Choose the student.')
        return redirect('welfare:counselling')
    show = request.GET.get('show', 'open')
    qs = CounsellingCase.objects.filter(tenant=t).select_related('student__class_assigned', 'counsellor')
    qs = qs.filter(status='CLOSED') if show == 'closed' else qs.exclude(status='CLOSED')
    return render(request, 'welfare/counselling.html', {'items': qs[:100], 'show': show,
                                                        'students': _students(request),
                                                        'reasons': CounsellingCase.REASONS})


@counsellor_only
def case(request, pk):
    c = get_object_or_404(CounsellingCase.objects.select_related('student__class_assigned'), pk=pk,
                          tenant=request.tenant)
    if request.method == 'POST':
        try:
            if request.POST.get('action') == 'close':
                svc.close_case(c)
            elif request.POST.get('action') == 'reopen':
                c.status, c.closed_at = 'OPEN', None
                c.save(update_fields=['status', 'closed_at'])
            else:
                svc.add_note(c, request.user, request.POST)
            messages.success(request, 'Saved.')
        except svc.WelfareError as problem:
            messages.error(request, str(problem))
        return redirect('welfare:case', pk=c.pk)
    return render(request, 'welfare/case.html', {'c': c, 'notes': c.notes.select_related('author'),
                                                 'today': timezone.localdate(),
                                                 'others': CounsellingCase.objects.filter(student=c.student)
                                                 .exclude(pk=c.pk)})


@staff_only
def safeguarding(request):
    t = request.tenant
    lead = svc.is_lead(request.user, t)
    if request.method == 'POST':
        st = _pick(request)
        try:
            if not st:
                raise svc.WelfareError('Choose the child.')
            svc.raise_concern(st, request.user, request.POST)
            messages.success(request, 'Thank you. The safeguarding lead has been told. Do not discuss this with '
                                      'other staff or try to investigate yourself.')
        except svc.WelfareError as problem:
            messages.error(request, str(problem))
        return redirect('welfare:safeguarding')
    return render(request, 'welfare/safeguarding.html', {
        'items': svc.concerns_for(request.user, t)[:100], 'lead': lead, 'students': _students(request),
        'categories': Concern.CATEGORIES})


@_gate(svc.is_lead, 'Only safeguarding leads can open a concern.')
def concern(request, pk):
    c = get_object_or_404(Concern.objects.select_related('student__class_assigned', 'raised_by', 'lead'), pk=pk,
                          tenant=request.tenant)
    if request.method == 'POST':
        try:
            svc.act_on_concern(c, request.user, request.POST.get('text'), request.POST.get('status'),
                               Staff.objects.filter(tenant=request.tenant, pk=request.POST.get('lead') or 0).first(),
                               request.POST.get('referred_to'))
            messages.success(request, 'Recorded.')
        except svc.WelfareError as problem:
            messages.error(request, str(problem))
        return redirect('welfare:concern', pk=c.pk)
    cfg = svc.settings_for(request.tenant)
    return render(request, 'welfare/concern.html', {
        'c': c, 'actions': c.actions.select_related('by'), 'statuses': Concern.STATUSES,
        'leads': cfg.safeguarding_leads.all(), 'card': svc.emergency_card(c.student),
        'history': Concern.objects.filter(student=c.student).exclude(pk=c.pk)})


# ─────────────────────────────────────────────────────────────────────────────
# F397 support plans
# ─────────────────────────────────────────────────────────────────────────────

def _can_edit_plan(user, tenant, plan=None):
    if svc._is_admin(user) or svc.is_nurse(user, tenant) or svc.is_counsellor(user, tenant):
        return True
    staff = svc.staff_of(user, tenant)
    return bool(plan and staff and plan.owner_id == staff.pk)


@staff_only
def support(request):
    t = request.tenant
    can = _can_edit_plan(request.user, t)
    if request.method == 'POST' and can:
        st = _pick(request)
        try:
            if not st:
                raise svc.WelfareError('Choose the student.')
            plan = svc.save_plan(st, request.user, request.POST)
            return redirect('welfare:plan', pk=plan.pk)
        except svc.WelfareError as problem:
            messages.error(request, str(problem))
        return redirect('welfare:support')
    return render(request, 'welfare/support.html', {
        'items': SupportPlan.objects.filter(tenant=t, status='ACTIVE').select_related('student__class_assigned',
                                                                                     'owner'),
        'closed': SupportPlan.objects.filter(tenant=t, status='CLOSED').select_related('student')[:20],
        'can': can, 'students': _students(request),
        'staff': Staff.objects.filter(tenant=t, user__is_active=True).order_by('full_name'),
        'today': timezone.localdate()})


@staff_only
def plan(request, pk):
    p = get_object_or_404(SupportPlan.objects.select_related('student__class_assigned', 'owner'), pk=pk,
                          tenant=request.tenant)
    can = _can_edit_plan(request.user, request.tenant, p)
    if request.method == 'POST' and can:
        try:
            if request.POST.get('action') == 'review':
                svc.review_plan(p, request.user, request.POST)
            else:
                svc.save_plan(p.student, request.user, request.POST, p)
            messages.success(request, 'Saved.')
        except svc.WelfareError as problem:
            messages.error(request, str(problem))
        return redirect('welfare:plan', pk=p.pk)
    return render(request, 'welfare/plan.html', {
        'p': p, 'can': can, 'reviews': p.reviews.select_related('by'),
        'staff': Staff.objects.filter(tenant=request.tenant, user__is_active=True).order_by('full_name'),
        'support': getattr(p.student, 'support', None), 'today': timezone.localdate()})


# ─────────────────────────────────────────────────────────────────────────────
# F396 sports and fitness
# ─────────────────────────────────────────────────────────────────────────────

@staff_only
def sports(request):
    t = request.tenant
    if request.method == 'POST':
        a = request.POST.get('action')
        try:
            if a == 'team':
                if not ((request.POST.get('name') or '').strip() and (request.POST.get('sport') or '').strip()):
                    raise svc.WelfareError('Give the team name and the sport.')
                Team.objects.create(tenant=t, name=request.POST['name'].strip()[:80],
                                    sport=request.POST['sport'].strip()[:60],
                                    season=(request.POST.get('season') or '')[:40],
                                    coach=Staff.objects.filter(tenant=t, pk=request.POST.get('coach') or 0).first())
            elif a == 'member':
                team = get_object_or_404(Team, pk=request.POST.get('team'), tenant=t)
                for st in _pick_many(request):
                    svc.add_member(team, st, request.POST.get('position'), bool(request.POST.get('captain')))
            elif a == 'remove':
                TeamMember.objects.filter(tenant=t, pk=request.POST.get('id')).delete()
            elif a == 'fitness':
                day = svc._date(request.POST.get('date'), timezone.localdate())
                n = 0
                for key, value in request.POST.items():
                    if key.startswith('v_') and str(value).strip():
                        st = Student.objects.filter(tenant=t, pk=key[2:]).first()
                        if st:
                            svc.record_fitness(st, request.POST.get('test'), value, request.user, day)
                            n += 1
                if not n:
                    st = _pick(request)
                    if not st:
                        raise svc.WelfareError('Choose the student and give the result.')
                    svc.record_fitness(st, request.POST.get('test'), request.POST.get('value'), request.user, day)
            messages.success(request, 'Saved.')
        except svc.WelfareError as problem:
            messages.error(request, str(problem))
        return redirect(request.get_full_path())
    test = request.GET.get('test') or 'SPRINT'
    cls = Class.objects.filter(tenant=t, pk=request.GET.get('class') or 0).first()
    return render(request, 'welfare/sports.html', {
        'teams': Team.objects.filter(tenant=t, active=True).select_related('coach')
        .prefetch_related('members__student__class_assigned'),
        'students': _students(request), 'tests': [(x[0], x[1]) for x in FitnessTest.TESTS], 'test': test,
        'board': svc.fitness_board(t, test), 'classes': Class.objects.filter(tenant=t).order_by('name'), 'cls': cls,
        'class_students': Student.objects.filter(class_assigned=cls, status__in=ENROLLED_STATUSES)
        .order_by('full_name') if cls else [],
        'staff': Staff.objects.filter(tenant=t, user__is_active=True).order_by('full_name'),
        'today': timezone.localdate()})


# ─────────────────────────────────────────────────────────────────────────────
# F398 resources, F395 wellbeing survey, settings
# ─────────────────────────────────────────────────────────────────────────────

@_gate(lambda u, t: svc._is_admin(u) or svc.is_counsellor(u, t), 'Only counsellors and admins edit resources.')
def resources(request):
    t = request.tenant
    svc.ensure_starters(t)
    if request.method == 'POST':
        if request.POST.get('action') == 'toggle':
            r = get_object_or_404(Resource, pk=request.POST.get('id'), tenant=t)
            r.published = not r.published
            r.save(update_fields=['published'])
        elif (request.POST.get('title') or '').strip() and (request.POST.get('body') or '').strip() and \
                request.POST.get('topic') in dict(Resource.TOPICS):
            Resource.objects.create(
                tenant=t, title=request.POST['title'].strip()[:160], topic=request.POST['topic'],
                audience=request.POST.get('audience') if request.POST.get('audience') in dict(Resource.AUDIENCES)
                else 'BOTH', body=request.POST['body'].strip(), link=(request.POST.get('link') or '').strip()[:200],
                contact=(request.POST.get('contact') or '').strip()[:160])
        else:
            messages.error(request, 'Give a title, topic and text.')
            return redirect('welfare:resources')
        messages.success(request, 'Saved.')
        return redirect('welfare:resources')
    return render(request, 'welfare/resources.html', {'items': Resource.objects.filter(tenant=t),
                                                      'topics': Resource.TOPICS, 'audiences': Resource.AUDIENCES})


@admin_only
def settings_page(request):
    t = request.tenant
    cfg = svc.settings_for(t)
    if request.method == 'POST':
        if request.POST.get('action') == 'survey':
            try:
                s = svc.start_wellbeing_survey(t, request.user, svc._date(request.POST.get('closes')))
                messages.success(request, f'"{s.title}" is open to students.')
            except svc.WelfareError as problem:
                messages.error(request, str(problem))
            return redirect('welfare:settings')
        staff = Staff.objects.filter(tenant=t)
        for field in ('nurses', 'counsellors', 'safeguarding_leads', 'discipline_team'):
            getattr(cfg, field).set(staff.filter(pk__in=request.POST.getlist(field)))
        cfg.tell_parents_incidents = bool(request.POST.get('tell_parents_incidents'))
        days = str(request.POST.get('immunisation_reminder_days') or '')
        cfg.immunisation_reminder_days = int(days) if days.isdigit() else 14
        cfg.save()
        messages.success(request, 'Saved.')
        return redirect('welfare:settings')
    from communications.models import Survey
    return render(request, 'welfare/settings.html', {
        'cfg': cfg, 'staff': Staff.objects.filter(tenant=t, user__is_active=True).order_by('full_name'),
        'sel': {f: set(getattr(cfg, f).values_list('pk', flat=True))
                for f in ('nurses', 'counsellors', 'safeguarding_leads', 'discipline_team')},
        'surveys': Survey.objects.filter(tenant=t, title__startswith='Wellbeing check')[:6]})


# ─────────────────────────────────────────────────────────────────────────────
# Students and parents
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def mine(request):
    st = Student.objects.filter(user=request.user, tenant=request.tenant).select_related('class_assigned').first()
    if not st:
        return _deny(request, 'This page is for students.')
    if request.method == 'POST':
        try:
            svc.request_help(st, request.POST.get('reason'), request.POST.get('text'))
            messages.success(request, 'Thank you for telling us. A counsellor will find a quiet time to see you.')
        except svc.WelfareError as problem:
            messages.error(request, str(problem))
        return redirect('welfare:mine')
    return render(request, 'welfare/mine.html', {
        'st': st, 'pts': svc.points(st), 'resources': svc.resources_for(request.tenant, 'STUDENTS'),
        'teams': st.teams.select_related('team'), 'bests': svc.personal_bests(st),
        'asked': CounsellingCase.objects.filter(student=st, status='REQUESTED').exists(),
        'reasons': CounsellingCase.REASONS})


@login_required
def family(request):
    from users.views_achievements import _parent_children
    kids = [k for k in _parent_children(request) if k.status in ENROLLED_STATUSES]
    if not kids:
        return _deny(request, 'This page is for parents.')
    if request.method == 'POST':
        kid = next((k for k in kids if str(k.pk) == request.POST.get('student')), None)
        if not kid:
            raise Http404
        try:
            if request.POST.get('action') == 'jab':
                svc.add_immunisation(kid, request.POST, request.user, request.FILES.get('proof'), verified=False)
                messages.success(request, 'Added — the school nurse will check it.')
            else:
                svc.save_profile(kid, request.POST, request.user, by_nurse=False)
                messages.success(request, 'Medical details saved. The school nurse has been told.')
        except svc.WelfareError as problem:
            messages.error(request, str(problem))
        return redirect('welfare:family')
    today = timezone.localdate()
    cards = [{'kid': k, 'p': svc.profile_for(k), 'jabs': k.immunisations.all(), 'report': svc.conduct_report(k),
              'detentions': Detention.objects.filter(student=k, session__date__gte=today).select_related('session'),
              'accidents': k.accidents.all()[:5], 'screen': k.screenings.first(),
              'plans': k.support_plans.filter(status='ACTIVE')} for k in kids]
    return render(request, 'welfare/family.html', {
        'page_title': 'Health & wellbeing', 'cards': cards, 'blood': MedicalProfile.BLOOD,
        'genotypes': MedicalProfile.GENOTYPE, 'vaccines': Immunisation.VACCINES,
        'resources': svc.resources_for(request.tenant, 'PARENTS'), 'today': today})
