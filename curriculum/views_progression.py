"""
curriculum/views_progression.py
───────────────────────────────
Subject streams, end-of-session promotion and graduation certificates.
"""
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from academics.models import AcademicSession
from academics.utils import get_active_session
from users.models import ENROLLED_STATUSES, Class, Student, Subject
from users.views_profile import can_manage, can_view

from . import progression as prog
from .models import ClassProgression, PromotionDecision, PromotionPolicy, Stream, StudentStream


def _deny(request, who):
    messages.error(request, f'Only {who}.')
    return redirect('dashboard:router')


def _int(raw, default=0, lo=0, hi=100):
    try:
        return max(lo, min(hi, int(raw)))
    except (TypeError, ValueError):
        return default


def _dec(raw, default):
    try:
        return max(Decimal(0), min(Decimal(100), Decimal(str(raw))))
    except (InvalidOperation, TypeError, ValueError):
        return default


# ─────────────────────────────────────────────────────────────────────────────
# Streams
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def streams(request):
    if not can_view(request.user):
        return _deny(request, 'staff can see subject streams')
    manage = can_manage(request.user)
    if request.method == 'POST':
        if not manage:
            return _deny(request, 'administrators can change streams')
        action = request.POST.get('action')
        if action == 'delete':
            get_object_or_404(Stream, pk=request.POST.get('id')).delete()
            messages.success(request, 'Stream removed. Students keep their current subjects.')
            return redirect('curriculum:streams')
        if action == 'assign':
            return _assign(request)
        stream = (get_object_or_404(Stream, pk=request.POST.get('id')) if request.POST.get('id')
                  else Stream(tenant=request.tenant))
        name = (request.POST.get('name') or '').strip()[:60]
        if not name:
            messages.error(request, 'Give the stream a name.')
            return redirect('curriculum:streams')
        if Stream.objects.filter(tenant=request.tenant, name__iexact=name).exclude(pk=stream.pk).exists():
            messages.error(request, f'There is already a {name} stream.')
            return redirect('curriculum:streams')
        stream.name = name
        stream.levels = (request.POST.get('levels') or '').strip()[:120]
        stream.description = (request.POST.get('description') or '').strip()[:255]
        stream.min_electives = _int(request.POST.get('min_electives'), 1, 0, 20)
        stream.max_electives = _int(request.POST.get('max_electives'), 3, 0, 20)
        stream.min_subjects = _int(request.POST.get('min_subjects'), 8, 1, 20)
        stream.max_subjects = _int(request.POST.get('max_subjects'), 9, 1, 20)
        stream.save()
        compulsory = set(request.POST.getlist('compulsory'))
        stream.compulsory.set(Subject.objects.filter(pk__in=compulsory))
        stream.electives.set(Subject.objects.filter(pk__in=set(request.POST.getlist('electives')) - compulsory))
        problems = prog.check_stream(stream)
        if problems:
            messages.warning(request, f'{stream.name} saved, but: ' + ' '.join(problems))
        else:
            messages.success(request, f'{stream.name} saved.')
        return redirect('curriculum:streams')

    all_streams = list(Stream.objects.filter(tenant=request.tenant).prefetch_related('compulsory', 'electives'))
    for s in all_streams:
        s.problems = prog.check_stream(s)
    classes = Class.objects.filter(tenant=request.tenant).order_by('name')
    school_class = classes.filter(pk=request.GET.get('class')).first() if request.GET.get('class') else None
    roster = []
    if school_class:
        choices = {c.student_id: c for c in StudentStream.objects.filter(tenant=request.tenant,
                                                                         student__class_assigned=school_class)
                   .prefetch_related('electives')}
        for st in Student.objects.filter(tenant=request.tenant, class_assigned=school_class,
                                         status__in=ENROLLED_STATUSES).order_by('full_name'):
            c = choices.get(st.id)
            roster.append({'student': st, 'stream_id': c.stream_id if c else None,
                           'electives': {e.id for e in c.electives.all()} if c else set()})
    return render(request, 'curriculum/streams.html', {
        'streams': all_streams, 'subjects': Subject.objects.filter(tenant=request.tenant).order_by('name'),
        'classes': classes, 'school_class': school_class, 'roster': roster, 'can_manage': manage,
        'class_streams': [s for s in all_streams if school_class and s.applies_to(school_class)],
    })


def _assign(request):
    school_class = get_object_or_404(Class, pk=request.POST.get('class_id'))
    streams_by_id = {str(s.id): s for s in Stream.objects.filter(tenant=request.tenant)}
    saved, problems, kept = 0, [], 0
    for st in Student.objects.filter(tenant=request.tenant, class_assigned=school_class,
                                     status__in=ENROLLED_STATUSES):
        stream = streams_by_id.get(request.POST.get(f'stream_{st.id}') or '')
        if not stream:
            continue
        electives = request.POST.getlist(f'electives_{st.id}')
        current = StudentStream.objects.filter(tenant=request.tenant, student=st).first()
        if current and current.stream_id == stream.id and \
                {str(e) for e in current.electives.values_list('id', flat=True)} == set(electives):
            continue
        try:
            _, k = prog.choose_stream(st, stream, electives, request.user)
            saved, kept = saved + 1, kept + k
        except prog.ProgressionError as problem:
            problems.append(str(problem))
    if saved:
        messages.success(request, f'Subjects set for {saved} student{"s" if saved != 1 else ""}.'
                         + (f' {kept} result{"s" if kept != 1 else ""} for dropped subjects already had marks '
                            'and were left as they are — check them in Student Subjects.' if kept else ''))
    for p in problems[:8]:
        messages.error(request, p)
    if not saved and not problems:
        messages.info(request, 'Nothing changed.')
    return redirect(f"{reverse('curriculum:streams')}?class={school_class.id}")


# ─────────────────────────────────────────────────────────────────────────────
# Promotion
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def promotion(request):
    if not can_manage(request.user):
        return _deny(request, 'administrators run end-of-session promotion')
    session = (AcademicSession.objects.filter(pk=request.GET.get('session')).first()
               if request.GET.get('session') else get_active_session())
    policy = prog.policy_for(request.tenant)
    classes = list(Class.objects.filter(tenant=request.tenant).order_by('name'))
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'policy':
            policy.pass_average = _dec(request.POST.get('pass_average'), policy.pass_average)
            policy.conditional_band = _dec(request.POST.get('conditional_band'), policy.conditional_band)
            policy.subject_pass_mark = _dec(request.POST.get('subject_pass_mark'), policy.subject_pass_mark)
            policy.min_subjects_passed = _int(request.POST.get('min_subjects_passed'), 0, 0, 20)
            policy.min_attendance = _int(request.POST.get('min_attendance'), 0, 0, 100)
            policy.basis = 'LAST' if request.POST.get('basis') == 'LAST' else 'SESSION'
            policy.save()
            policy.must_pass.set(Subject.objects.filter(pk__in=request.POST.getlist('must_pass')))
            messages.success(request, 'Promotion rules saved. Recalculate a class to apply them.')
        elif action == 'map':
            by_id = {str(c.id): c for c in classes}
            for c in classes:
                choice = request.POST.get(f'next_{c.id}', '')
                ClassProgression.objects.update_or_create(
                    tenant=request.tenant, from_class=c,
                    defaults={'graduates': choice == 'GRADUATE', 'to_class': by_id.get(choice)})
            messages.success(request, 'Next classes saved.')
        return redirect(f"{reverse('curriculum:promotion')}?session={session.id if session else ''}")
    map_rows = prog.progression_map(request.tenant)
    summary = {}
    if session:
        for d in PromotionDecision.objects.filter(tenant=request.tenant, session=session):
            row = summary.setdefault(d.from_class_id, {'total': 0, 'applied': 0, 'repeat': 0})
            row['total'] += 1
            row['applied'] += bool(d.applied_at)
            row['repeat'] += d.outcome == 'REPEAT'
    for r in map_rows:
        r['summary'] = summary.get(r['school_class'].id)
        r['enrolled'] = Student.objects.filter(tenant=request.tenant, class_assigned=r['school_class'],
                                               status__in=ENROLLED_STATUSES).count()
    return render(request, 'curriculum/promotion.html', {
        'session': session, 'sessions': AcademicSession.objects.filter(tenant=request.tenant).order_by('-start_date'),
        'policy': policy, 'must_pass': set(policy.must_pass.values_list('id', flat=True)) if policy.pk else set(),
        'subjects': Subject.objects.filter(tenant=request.tenant).order_by('name'),
        'map_rows': map_rows, 'classes': classes,
    })


@transaction.atomic
def _save_overrides(request, decisions):
    for d in decisions:
        outcome = request.POST.get(f'outcome_{d.id}')
        reason = request.POST.get(f'reason_{d.id}', '')
        if not d.applied_at and outcome and (outcome != d.outcome or reason != d.override_reason):
            prog.override(d, outcome, reason, request.user)


@login_required
def promotion_class(request, class_id):
    if not can_manage(request.user):
        return _deny(request, 'administrators run end-of-session promotion')
    school_class = get_object_or_404(Class, pk=class_id)
    session = (AcademicSession.objects.filter(pk=request.GET.get('session') or request.POST.get('session')).first()
               or get_active_session())
    if not session:
        messages.error(request, 'Set up the academic session first.')
        return redirect('curriculum:promotion')
    back = f"{reverse('curriculum:promotion_class', args=[school_class.id])}?session={session.id}"
    if request.method == 'POST':
        action = request.POST.get('action')
        decisions = list(PromotionDecision.objects.filter(tenant=request.tenant, session=session,
                                                          from_class=school_class).select_related('student'))
        try:
            if action == 'recalculate':
                prog.prepare(school_class, session, request.user)
                messages.success(request, 'Recalculated from the latest results and rules.')
            elif action == 'save':
                _save_overrides(request, decisions)
                messages.success(request, 'Decisions saved.')
            elif action == 'apply':
                _save_overrides(request, decisions)
                pending = [d for d in decisions if not d.applied_at]
                counts = prog.apply(pending, request.user, notify=bool(request.POST.get('notify')))
                labels = dict(PromotionDecision.OUTCOMES)
                messages.success(request, 'Done: ' + ', '.join(f'{n} {labels[k].lower()}' for k, n in counts.items())
                                 if counts else 'Nothing left to apply.')
        except prog.ProgressionError as problem:
            messages.error(request, str(problem))
        return redirect(back)
    decisions = list(PromotionDecision.objects.filter(tenant=request.tenant, session=session, from_class=school_class)
                     .select_related('student', 'to_class', 'decided_by'))
    if not decisions:
        decisions = prog.prepare(school_class, session, request.user)
    to_class, graduates = prog.next_for(school_class)
    counts = {k: sum(1 for d in decisions if d.outcome == k) for k, _ in PromotionDecision.OUTCOMES}
    return render(request, 'curriculum/promotion_class.html', {
        'school_class': school_class, 'session': session, 'decisions': decisions, 'outcomes': PromotionDecision.OUTCOMES,
        'to_class': to_class, 'graduates': graduates, 'counts': counts,
        'pending': sum(1 for d in decisions if not d.applied_at), 'policy': prog.policy_for(request.tenant),
    })


# ─────────────────────────────────────────────────────────────────────────────
# Graduation certificates
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def graduation_certificates(request):
    if not can_manage(request.user):
        return _deny(request, 'administrators print graduation certificates')
    graduates = Student.objects.filter(tenant=request.tenant, status='Graduated').order_by('-left_on', 'full_name')
    years = sorted({s.left_on.year for s in graduates if s.left_on}, reverse=True)
    year = request.GET.get('year') or (str(years[0]) if years else '')
    if year.isdigit():
        graduates = graduates.filter(left_on__year=int(year))
    ids = request.GET.getlist('id')
    if ids:
        graduates = graduates.filter(pk__in=ids)
    certs = [prog.certificate(s) for s in graduates]
    return render(request, 'curriculum/graduation_certificates.html', {
        'certs': certs, 'years': years, 'year': year, 'school': request.tenant,
    })


def verify_graduation(request, reference):
    """Public: anyone holding a certificate can check it is genuine."""
    cert = prog.verify_certificate(reference, request.tenant) if getattr(request, 'tenant', None) else None
    return render(request, 'curriculum/graduation_verify.html', {'cert': cert, 'school': getattr(request, 'tenant', None)},
                  status=200 if cert else 404)


def graduation_qr(request, reference):
    from idcards.utils import qr_png_bytes
    url = request.build_absolute_uri(reverse('curriculum:verify_graduation', args=[reference]))
    return HttpResponse(qr_png_bytes(url), content_type='image/png')
