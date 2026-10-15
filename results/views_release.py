"""
results/views_release.py
────────────────────────
Subject-sheet review and class approval, release rules (fees, PINs,
notifications), PIN batches, and the public report-card check.
"""
import csv

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from academics.models import Term
from academics.utils import get_active_term
from users.models import Class, Subject
from users.views_profile import can_manage, can_view

from . import release
from .models import PinBatch, ResultPin, ResultSheetSettings, SubjectSheet


def _deny(request, who):
    messages.error(request, f'Only {who}.')
    return redirect('dashboard:router')


@login_required
def sheet_action(request):
    """Submit a subject sheet for review, or approve / send it back."""
    if request.method != 'POST' or not can_view(request.user):
        return redirect('dashboard:router')
    school_class = get_object_or_404(Class, pk=request.POST.get('class'))
    subject = get_object_or_404(Subject, pk=request.POST.get('subject'))
    term = get_object_or_404(Term, pk=request.POST.get('term'))
    sheet = release.sheet_for(school_class, subject, term)
    action = request.POST.get('action')
    try:
        if action == 'submit':
            from .views import _can_enter_scores
            if not _can_enter_scores(request.user):
                raise release.ReleaseError('You are not allowed to enter scores.')
            release.submit_sheet(sheet, request.user)
            messages.success(request, f'{subject.name} for {school_class.name} sent for review.')
        elif action in ('approve', 'return'):
            release.review_sheet(sheet, request.user, action == 'approve', request.POST.get('comment'))
            messages.success(request, 'Approved.' if action == 'approve' else 'Sent back to the subject teacher.')
    except release.ReleaseError as problem:
        messages.error(request, str(problem))
    nxt = request.POST.get('next') or ''
    if nxt.startswith('/'):
        return redirect(nxt)
    return redirect(f"{reverse('results:manual_score_entry')}?class={school_class.id}&subject={subject.id}&term={term.id}")


@login_required
def approvals(request):
    if not can_view(request.user):
        return _deny(request, 'staff can see result approvals')
    from curriculum.services import hod_subject_ids
    staff = getattr(request.user, 'staff', None)
    manage = can_manage(request.user)
    heads = hod_subject_ids(staff)
    if not (manage or heads):
        return _deny(request, 'heads of department and administrators review results')
    terms = Term.objects.filter(tenant=request.tenant).select_related('session').order_by('-start_date')
    term = terms.filter(pk=request.GET.get('term')).first() if request.GET.get('term') else (get_active_term() or terms.first())
    if request.method == 'POST' and manage:
        school_class = get_object_or_404(Class, pk=request.POST.get('class'))
        try:
            if request.POST.get('action') == 'approve_class':
                release.approve_class(school_class, term, request.user, request.POST.get('comment'))
                messages.success(request, f'{school_class.name} approved — its results can now be published.')
            elif request.POST.get('action') == 'withdraw':
                release.withdraw_approval(school_class, term)
                messages.success(request, f'Approval for {school_class.name} withdrawn.')
        except release.ReleaseError as problem:
            messages.error(request, str(problem))
        return redirect(f"{reverse('results:approvals')}?term={term.id}")
    boards = []
    if term:
        for c in Class.objects.filter(tenant=request.tenant).order_by('name'):
            rows, approval = release.class_status(c, term)
            if not manage:
                rows = [r for r in rows if r['subject'].id in heads]
            if not rows:
                continue
            counts = {'approved': sum(1 for r in rows if r['sheet'] and r['sheet'].status == 'APPROVED'),
                      'waiting': sum(1 for r in rows if r['sheet'] and r['sheet'].status == 'SUBMITTED'),
                      'total': sum(1 for r in rows if r['rows'])}
            boards.append({'school_class': c, 'rows': rows, 'approval': approval, 'counts': counts})
    return render(request, 'results/approvals.html', {
        'terms': terms, 'term': term, 'boards': boards, 'can_manage': manage,
        'enabled': release.settings_for(request.tenant).require_approval,
    })


@login_required
def release_settings(request):
    if not can_manage(request.user):
        return _deny(request, 'administrators can change how results are released')
    cfg, _ = ResultSheetSettings.objects.get_or_create(tenant=request.tenant)
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action == 'rules':
                cfg.require_approval = bool(request.POST.get('require_approval'))
                cfg.fee_gate = bool(request.POST.get('fee_gate'))
                pct = int(request.POST.get('fee_gate_percent') or 100)
                if not 1 <= pct <= 100:
                    raise release.ReleaseError('The fees threshold is a percentage from 1 to 100.')
                cfg.fee_gate_percent = pct
                cfg.require_pin = bool(request.POST.get('require_pin'))
                cfg.notify_on_publish = bool(request.POST.get('notify_on_publish'))
                cfg.save()
                messages.success(request, 'Release rules saved.')
            elif action == 'generate':
                batch = release.generate_pins(request.tenant, request.POST.get('name'), request.POST.get('count') or 0,
                                              request.POST.get('price') or 0, request.POST.get('max_uses') or 5,
                                              request.user)
                messages.success(request, f'{batch.pins.count()} PINs created in "{batch.name}". Download or print them below.')
            elif action == 'disable':
                pin = get_object_or_404(ResultPin, pk=request.POST.get('pin_id'))
                pin.disabled = not pin.disabled
                pin.save(update_fields=['disabled'])
                messages.success(request, f"PIN {pin.serial} {'disabled' if pin.disabled else 're-enabled'}.")
        except (release.ReleaseError, ValueError) as problem:
            messages.error(request, str(problem) or 'Check the numbers and try again.')
        return redirect('results:release')
    batches = PinBatch.objects.filter(tenant=request.tenant).annotate(
        total=Count('pins'), used=Count('pins', filter=Q(pins__student__isnull=False)))
    q = (request.GET.get('q') or '').strip()
    found = (ResultPin.objects.filter(tenant=request.tenant).filter(Q(serial__icontains=q) | Q(pin=q) |
                                                                     Q(student__full_name__icontains=q))
             .select_related('student', 'term')[:30] if q else [])
    return render(request, 'results/release.html', {'cfg': cfg, 'batches': batches, 'q': q, 'found': found})


@login_required
def pin_batch(request, pk):
    if not can_manage(request.user):
        return _deny(request, 'administrators can see PINs')
    batch = get_object_or_404(PinBatch, pk=pk)
    pins = batch.pins.select_related('student', 'term')
    if request.GET.get('format') == 'csv':
        resp = HttpResponse(content_type='text/csv')
        resp['Content-Disposition'] = f'attachment; filename="result-pins-{batch.id}.csv"'
        w = csv.writer(resp)
        w.writerow(['Serial', 'PIN', 'Uses allowed', 'Used by', 'Term', 'Uses so far'])
        for p in pins:
            w.writerow([p.serial, p.pin, p.max_uses, p.student.full_name if p.student else '',
                        p.term.name if p.term else '', p.uses])
        return resp
    unused_only = request.GET.get('unused') == '1'
    if unused_only:
        pins = pins.filter(student__isnull=True)
    return render(request, 'results/pin_cards.html', {'batch': batch, 'pins': pins, 'school': request.tenant,
                                                      'portal': request.build_absolute_uri(reverse('users:parent_login')),
                                                      'unused_only': unused_only})


def verify_report(request, ref):
    """Public: check a printed report card against the school's published results."""
    data = release.verify(ref, request.tenant) if getattr(request, 'tenant', None) else None
    return render(request, 'results/verify.html', {'data': data, 'school': getattr(request, 'tenant', None)},
                  status=200 if data else 404)


def verify_qr(request, ref):
    from idcards.utils import qr_png_bytes
    url = request.build_absolute_uri(reverse('results:verify', args=[ref]))
    return HttpResponse(qr_png_bytes(url), content_type='image/png')
