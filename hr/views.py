"""
hr/views.py
───────────
Staff directory, each staff member's HR file, onboarding, contracts,
appraisals, training, disciplinary cases, exits, recruitment, the public
careers page and the staff member's own "My HR" page.
"""
import os
from datetime import datetime
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from users.models import Staff
from users.views_profile import can_manage, can_view

from . import services as svc
from .models import (Appraisal, AppraisalCycle, Contract, DisciplinaryCase, ExitCase, JobApplication,
                     OnboardingStep, OnboardingTask, StaffRecord, TrainingRecord, Vacancy)


def _deny(request, text='Only administrators can manage HR.'):
    messages.error(request, text)
    return redirect('dashboard:router')


def _pk(raw):
    raw = str(raw or '')
    return int(raw) if raw.isdigit() else 0


def _date(raw):
    try:
        return datetime.strptime(raw or '', '%Y-%m-%d').date()
    except ValueError:
        return None


def _me(request):
    return request.user.staff_profiles.filter(tenant=request.tenant).first()


def _serve(field):
    if not field:
        raise Http404
    try:
        handle = field.open('rb')
    except (FileNotFoundError, OSError):
        raise Http404
    from users.records import CONTENT_TYPES
    ext = os.path.splitext(field.name)[1].lower()
    return FileResponse(handle, content_type=CONTENT_TYPES.get(ext, 'application/octet-stream'),
                        filename=f'document{ext}')


# ─────────────────────────────────────────────────────────────────────────────
# F237 — directory
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def directory(request):
    if not can_view(request.user):
        return _deny(request, 'The staff directory is for staff.')
    from curriculum.models import Department
    staff = Staff.objects.filter(tenant=request.tenant, user__is_active=True).select_related('user').order_by('full_name')
    q = (request.GET.get('q') or '').strip()
    if q:
        staff = staff.filter(Q(full_name__icontains=q) | Q(record__job_title__icontains=q) |
                             Q(record__phone__icontains=q) | Q(record__extension__icontains=q))
    dept = _pk(request.GET.get('department'))
    if dept:
        staff = staff.filter(record__department_id=dept)
    records = {r.staff_id: r for r in StaffRecord.objects.filter(staff__in=staff).select_related('department')}
    people = [{'staff': s, 'record': records.get(s.pk)} for s in staff if not (records.get(s.pk) and records[s.pk].left_on)]
    return render(request, 'hr/directory.html', {
        'people': people, 'q': q, 'department': dept, 'is_admin': can_manage(request.user),
        'departments': Department.objects.filter(tenant=request.tenant)})


@login_required
def photo(request, staff_id):
    if not can_view(request.user):
        raise Http404
    rec = get_object_or_404(StaffRecord, staff_id=staff_id, tenant=request.tenant)
    return _serve(rec.photo)


@login_required
def document(request, kind, pk):
    """Private HR documents: administrators, or the staff member the document belongs to."""
    models_ = {'onboarding': (OnboardingStep, 'file'), 'contract': (Contract, 'file'),
               'training': (TrainingRecord, 'certificate'), 'cv': (JobApplication, 'cv')}
    if kind not in models_:
        raise Http404
    model, field = models_[kind]
    obj = get_object_or_404(model, pk=pk, tenant=request.tenant)
    me = _me(request)
    own = kind != 'cv' and me and obj.staff_id == me.pk
    if not (can_manage(request.user) or own):
        raise Http404
    return _serve(getattr(obj, field))


# ─────────────────────────────────────────────────────────────────────────────
# A staff member's HR file
# ─────────────────────────────────────────────────────────────────────────────

RECORD_TEXT = ['job_title', 'phone', 'extension', 'emergency_name', 'emergency_phone', 'address']


@login_required
def staff_file(request, staff_id):
    if not can_manage(request.user):
        return _deny(request)
    from curriculum.models import Department
    staff = get_object_or_404(Staff.objects.select_related('user'), pk=staff_id, tenant=request.tenant)
    rec = svc.record_for(staff)
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action == 'record':
                for f in RECORD_TEXT:
                    setattr(rec, f, (request.POST.get(f) or '').strip()[:255])
                rec.department = Department.objects.filter(tenant=request.tenant,
                                                           pk=_pk(request.POST.get('department'))).first()
                rec.date_joined = _date(request.POST.get('date_joined'))
                rec.date_of_birth = _date(request.POST.get('date_of_birth'))
                if request.FILES.get('photo'):
                    svc.check_file(request.FILES['photo'])
                    if request.FILES['photo'].name.lower().endswith('.pdf'):
                        raise svc.HRError('The photo must be a JPG, PNG or WEBP image.')
                    rec.photo = request.FILES['photo']
                rec.save()
                messages.success(request, 'Saved.')
            elif action == 'tick':
                step = get_object_or_404(OnboardingStep, pk=_pk(request.POST.get('step')), staff=staff)
                svc.tick(step, request.user, request.FILES.get('file'), request.POST.get('note'),
                         done=request.POST.get('done') != '0')
            elif action == 'contract':
                svc.add_contract(staff, request.POST.get('kind'), _date(request.POST.get('start')),
                                 _date(request.POST.get('end')), request.user, request.FILES.get('file'),
                                 request.POST.get('note'))
                messages.success(request, 'Contract added.')
            elif action == 'training':
                svc.add_training(staff, request.POST, request.user, request.FILES.get('certificate'))
                messages.success(request, 'Training recorded.')
            elif action == 'issue':
                svc.issue(staff, request.POST.get('kind'), request.POST.get('allegation'),
                          _date(request.POST.get('respond_by')), request.user)
                messages.success(request, 'Issued. The staff member has been emailed to reply on the portal.')
            elif action == 'exit':
                case = svc.open_exit(staff, request.POST.get('kind'), _date(request.POST.get('notice_date')),
                                     _date(request.POST.get('last_day')), request.POST.get('reason'), request.user)
                return redirect('hr:exit', pk=case.pk)
        except svc.HRError as problem:
            messages.error(request, str(problem))
        return redirect('hr:staff_file', staff_id=staff.pk)
    steps = svc.checklist(staff)
    return render(request, 'hr/staff_file.html', {
        'staff': staff, 'rec': rec, 'steps': steps, 'done': sum(1 for s in steps if s.done),
        'contracts': staff.contracts.all(), 'current': svc.current_contract(staff),
        'training': staff.training.all(), 'cases': staff.disciplinary.all(), 'exits': staff.exits.all(),
        'appraisals': staff.appraisals.select_related('cycle'), 'departments': Department.objects.filter(tenant=request.tenant),
        'contract_kinds': Contract.KINDS, 'case_kinds': DisciplinaryCase.KINDS, 'exit_kinds': ExitCase.KINDS,
        'today': timezone.localdate()})


# ─────────────────────────────────────────────────────────────────────────────
# Onboarding and contracts overviews
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def people(request):
    """HR home: everyone with onboarding progress, current contract and alerts."""
    if not can_manage(request.user):
        return _deny(request)
    staff = list(Staff.objects.filter(tenant=request.tenant).select_related('user').order_by('full_name'))
    progress = svc.onboarding_progress(staff)
    contracts = {s.pk: svc.current_contract(s) for s in staff}
    rows = [{'staff': s, 'progress': progress.get(s.pk, (0, 0)), 'contract': contracts[s.pk]} for s in staff]
    return render(request, 'hr/people.html', {
        'rows': rows, 'expiring': svc.expiring(request.tenant, 60),
        'open_cases': DisciplinaryCase.objects.filter(tenant=request.tenant, closed_at__isnull=True).select_related('staff'),
        'open_exits': ExitCase.objects.filter(tenant=request.tenant, completed_at__isnull=True).select_related('staff'),
        'new_applications': JobApplication.objects.filter(tenant=request.tenant, status='NEW').count()})


@login_required
def onboarding_tasks(request):
    if not can_manage(request.user):
        return _deny(request)
    svc.ensure_tasks(request.tenant)
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'add':
            title = (request.POST.get('title') or '').strip()[:120]
            if title:
                last = OnboardingTask.objects.filter(tenant=request.tenant).order_by('-order').first()
                OnboardingTask.objects.create(tenant=request.tenant, title=title, order=(last.order + 1) if last else 0,
                                              needs_file=bool(request.POST.get('needs_file')))
                messages.success(request, 'Added to the checklist.')
        else:
            t = get_object_or_404(OnboardingTask, pk=_pk(request.POST.get('id')), tenant=request.tenant)
            t.active = not t.active
            t.save(update_fields=['active'])
            messages.success(request, 'Checklist updated.')
        return redirect('hr:onboarding_tasks')
    return render(request, 'hr/onboarding_tasks.html', {
        'tasks': OnboardingTask.objects.filter(tenant=request.tenant)})


# ─────────────────────────────────────────────────────────────────────────────
# F229 — appraisals
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def appraisals(request):
    me = _me(request)
    admin = can_manage(request.user)
    if not (admin or me):
        return _deny(request, 'Appraisals are for staff.')
    if request.method == 'POST' and admin:
        try:
            cycle = svc.start_cycle(request.tenant, (request.POST.get('title') or '').strip(),
                                    _date(request.POST.get('opens')), _date(request.POST.get('closes')),
                                    request.POST.get('criteria'), request.user)
            messages.success(request, f'{cycle.title} started for {cycle.appraisals.count()} staff.')
            return redirect('hr:cycle', pk=cycle.pk)
        except svc.HRError as problem:
            messages.error(request, str(problem))
        return redirect('hr:appraisals')
    to_review = Appraisal.objects.filter(tenant=request.tenant, reviewer=me, status=Appraisal.REVIEW) \
        .select_related('staff', 'cycle') if me else []
    return render(request, 'hr/appraisals.html', {
        'cycles': AppraisalCycle.objects.filter(tenant=request.tenant) if admin else [],
        'mine': Appraisal.objects.filter(staff=me).select_related('cycle') if me else [],
        'to_review': to_review, 'admin': admin,
        'default_criteria': '\n'.join(f'{n}: {w}' for n, w in svc.DEFAULT_CRITERIA)})


@login_required
def cycle(request, pk):
    if not can_manage(request.user):
        return _deny(request)
    c = get_object_or_404(AppraisalCycle, pk=pk, tenant=request.tenant)
    if request.method == 'POST':
        c.open = not c.open
        c.save(update_fields=['open'])
        return redirect('hr:cycle', pk=c.pk)
    items = c.appraisals.select_related('staff', 'reviewer')
    return render(request, 'hr/cycle.html', {'c': c, 'items': items,
                                             'done': sum(1 for a in items if a.status == Appraisal.DONE)})


@login_required
def appraisal(request, pk):
    a = get_object_or_404(Appraisal.objects.select_related('cycle', 'staff', 'reviewer'), pk=pk, tenant=request.tenant)
    me = _me(request)
    own = me and a.staff_id == me.pk
    reviewer = svc.may_review(request.user, a)
    if not (own or reviewer):
        return _deny(request, 'Only the staff member, their reviewer and administrators can open this.')
    if request.method == 'POST':
        try:
            if request.POST.get('action') == 'self' and own:
                svc.self_review(a, request.POST)
                messages.success(request, 'Self-review sent to your reviewer.')
            elif request.POST.get('action') == 'review' and reviewer:
                svc.review(a, request.user, request.POST)
                messages.success(request, f'Appraisal complete: {a.overall} out of 5.')
        except svc.HRError as problem:
            messages.error(request, str(problem))
        return redirect('hr:appraisal', pk=a.pk)
    rows = [{'i': i, 'name': c['name'], 'weight': c['weight'], 'self': a.self_scores.get(c['name']),
             'score': a.scores.get(c['name'])} for i, c in enumerate(a.cycle.criteria)]
    return render(request, 'hr/appraisal.html', {
        'a': a, 'rows': rows, 'own': own, 'reviewer': reviewer, 'facts': svc.indicators(a.staff, a.cycle),
        'scale': [1, 2, 3, 4, 5]})


# ─────────────────────────────────────────────────────────────────────────────
# F236 — disciplinary case
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def case(request, pk):
    c = get_object_or_404(DisciplinaryCase.objects.select_related('staff', 'issued_by'), pk=pk, tenant=request.tenant)
    me = _me(request)
    own = me and c.staff_id == me.pk
    admin = can_manage(request.user)
    if not (own or admin):
        raise Http404
    if request.method == 'POST':
        try:
            if request.POST.get('action') == 'respond' and own:
                svc.respond(c, me, request.POST.get('response'))
                messages.success(request, 'Your reply has been sent.')
            elif request.POST.get('action') == 'close' and admin:
                svc.close_case(c, request.POST.get('outcome'), request.POST.get('outcome_note'))
                messages.success(request, 'Case closed.')
        except svc.HRError as problem:
            messages.error(request, str(problem))
        return redirect('hr:case', pk=c.pk)
    return render(request, 'hr/case.html', {'c': c, 'own': own, 'admin': admin, 'outcomes': DisciplinaryCase.OUTCOMES})


# ─────────────────────────────────────────────────────────────────────────────
# F240 — exits
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def exit_case(request, pk):
    if not can_manage(request.user):
        return _deny(request)
    e = get_object_or_404(ExitCase.objects.select_related('staff__user'), pk=pk, tenant=request.tenant)
    if request.method == 'POST' and not e.completed_at:
        action = request.POST.get('action')
        try:
            if action == 'handover':
                ticked = set(request.POST.getlist('done'))
                e.handover = [{'item': h['item'], 'done': str(i) in ticked} for i, h in enumerate(e.handover)]
                extra = (request.POST.get('new_item') or '').strip()[:120]
                if extra:
                    e.handover.append({'item': extra, 'done': False})
                try:
                    e.other_amounts = Decimal(str(request.POST.get('other_amounts') or '0').replace(',', ''))
                except InvalidOperation:
                    raise svc.HRError('Other amounts must be a number (use minus for money owed by them).')
                e.save(update_fields=['handover', 'other_amounts'])
                messages.success(request, 'Saved.')
            elif action == 'complete':
                svc.complete_exit(e, request.user, disable_login=bool(request.POST.get('disable_login')))
                messages.success(request, f'{e.staff.full_name} has been taken off payroll and the exit is complete.')
        except svc.HRError as problem:
            messages.error(request, str(problem))
        return redirect('hr:exit', pk=e.pk)
    return render(request, 'hr/exit.html', {'e': e, 'money': e.settlement if e.completed_at else svc.settlement(e)})


# ─────────────────────────────────────────────────────────────────────────────
# F231 — recruitment
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def vacancies(request):
    if not can_manage(request.user):
        return _deny(request)
    if request.method == 'POST':
        title, desc, closing = (request.POST.get('title') or '').strip(), (request.POST.get('description') or '').strip(), \
            _date(request.POST.get('closing_date'))
        if not (title and desc and closing):
            messages.error(request, 'Give the job title, description and closing date.')
        else:
            v = Vacancy.objects.create(tenant=request.tenant, title=title[:160], description=desc, closing_date=closing,
                                       department=(request.POST.get('department') or '').strip()[:80],
                                       requirements=(request.POST.get('requirements') or '').strip(),
                                       created_by=request.user)
            messages.success(request, 'Vacancy published on your careers page.')
            return redirect('hr:vacancy', pk=v.pk)
        return redirect('hr:vacancies')
    items = list(Vacancy.objects.filter(tenant=request.tenant))
    for v in items:
        v.count = v.applications.count()
        v.new = v.applications.filter(status='NEW').count()
    return render(request, 'hr/vacancies.html', {'items': items, 'today': timezone.localdate(),
                                                 'careers_url': request.build_absolute_uri('/hr/careers/')})


@login_required
def vacancy(request, pk):
    if not can_manage(request.user):
        return _deny(request)
    v = get_object_or_404(Vacancy, pk=pk, tenant=request.tenant)
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'toggle':
            v.published = not v.published
            v.save(update_fields=['published'])
        else:
            app = get_object_or_404(JobApplication, pk=_pk(request.POST.get('id')), vacancy=v)
            try:
                if action == 'interview':
                    when = None
                    try:
                        when = timezone.make_aware(datetime.strptime(request.POST.get('when') or '', '%Y-%m-%dT%H:%M'))
                    except ValueError:
                        pass
                    svc.book_interview(app, when, request.POST.get('venue'))
                    messages.success(request, f'Interview booked; {app.full_name} has been emailed.')
                elif request.POST.get('status') in dict(JobApplication.STATUSES):
                    app.status = request.POST['status']
                    app.notes = (request.POST.get('notes') or app.notes).strip()[:4000]
                    app.save(update_fields=['status', 'notes'])
                    messages.success(request, f'{app.full_name}: {app.get_status_display()}.')
            except svc.HRError as problem:
                messages.error(request, str(problem))
        return redirect('hr:vacancy', pk=v.pk)
    return render(request, 'hr/vacancy.html', {'v': v, 'apps': v.applications.all(),
                                               'statuses': JobApplication.STATUSES})


def careers(request):
    """Public list of open jobs (no sign-in)."""
    if not getattr(request, 'tenant', None):
        raise Http404
    jobs = Vacancy.objects.filter(tenant=request.tenant, published=True, closing_date__gte=timezone.localdate())
    return render(request, 'hr/careers.html', {'jobs': jobs, 'school': request.tenant})


def careers_apply(request, pk):
    if not getattr(request, 'tenant', None):
        raise Http404
    v = get_object_or_404(Vacancy, pk=pk, tenant=request.tenant, published=True)
    done = False
    if request.method == 'POST':
        try:
            svc.apply(v, request.POST, request.FILES.get('cv'))
            done = True
        except svc.HRError as problem:
            messages.error(request, str(problem))
    return render(request, 'hr/careers_apply.html', {'v': v, 'school': request.tenant, 'done': done,
                                                      'open': v.closing_date >= timezone.localdate()})


# ─────────────────────────────────────────────────────────────────────────────
# My HR (any staff member)
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def my_hr(request):
    me = _me(request)
    if not me:
        return _deny(request, 'This page is for staff.')
    rec = svc.record_for(me)
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action == 'contact':
                for f in ('phone', 'extension', 'emergency_name', 'emergency_phone', 'address'):
                    setattr(rec, f, (request.POST.get(f) or '').strip()[:255])
                if 'date_of_birth' in request.POST:
                    rec.date_of_birth = _date(request.POST.get('date_of_birth'))
                rec.save()
                messages.success(request, 'Your contact details are saved.')
            elif action == 'training':
                svc.add_training(me, request.POST, request.user, request.FILES.get('certificate'))
                messages.success(request, 'Training added to your record.')
        except svc.HRError as problem:
            messages.error(request, str(problem))
        return redirect('hr:my_hr')
    steps = svc.checklist(me)
    return render(request, 'hr/my_hr.html', {
        'me': me, 'rec': rec, 'contract': svc.current_contract(me), 'contracts': me.contracts.all(),
        'appraisals': me.appraisals.select_related('cycle'), 'cases': me.disciplinary.all(),
        'training': me.training.all(), 'steps': steps, 'done': sum(1 for s in steps if s.done),
        'to_review': Appraisal.objects.filter(reviewer=me, status=Appraisal.REVIEW).select_related('staff', 'cycle')})
