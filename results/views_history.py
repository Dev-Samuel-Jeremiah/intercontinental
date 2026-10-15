"""
results/views_history.py
────────────────────────
Whole-class report card downloads, progress charts, families' requests for
a result to be checked, and transcripts.
"""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from academics.models import Term
from academics.utils import get_active_term
from users.models import Class, Student, Subject
from users.views_profile import can_manage, can_view

from . import history
from .models import ReportBundle, ResultQuery, TermResult


def _deny(request, who):
    messages.error(request, f'Only {who}.')
    return redirect('dashboard:router')


def _sees_all_results(user):
    from .views import _is_admin_or_super
    staff = getattr(user, 'staff', None)
    return _is_admin_or_super(user) or getattr(staff, 'can_view_all_results', False)


def _viewer(request, student):
    """'staff', 'family' or None — who is looking at this student's results."""
    if can_view(request.user):
        return 'staff'
    if getattr(request.user, 'student', None) and request.user.student.id == student.id:
        return 'family'
    if getattr(request.user, 'is_parent', False):
        from users.views_achievements import _parent_children
        if _parent_children(request).filter(pk=student.pk).exists():
            return 'family'
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Whole-class downloads
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def bundles(request):
    if not _sees_all_results(request.user):
        return _deny(request, 'administrators and staff who can view all results')
    terms = Term.objects.filter(tenant=request.tenant).select_related('session').order_by('-start_date')
    if request.method == 'POST':
        try:
            history.request_bundle(get_object_or_404(Class, pk=request.POST.get('class')),
                                   get_object_or_404(Term, pk=request.POST.get('term')), request.user,
                                   published_only=bool(request.POST.get('published_only')))
            messages.success(request, "Preparing the report cards. This page updates when they're ready.")
        except history.HistoryError as problem:
            messages.error(request, str(problem))
        return redirect('results:bundles')
    items = ReportBundle.objects.filter(tenant=request.tenant).select_related('school_class', 'term', 'created_by')[:30]
    return render(request, 'results/bundles.html', {
        'items': items, 'classes': Class.objects.filter(tenant=request.tenant).order_by('name'), 'terms': terms,
        'active_term': get_active_term(),
        'busy': any(b.status in (ReportBundle.PENDING, ReportBundle.RUNNING) for b in items),
    })


@login_required
def bundle_download(request, pk):
    if not _sees_all_results(request.user):
        raise Http404
    b = get_object_or_404(ReportBundle, pk=pk, status=ReportBundle.DONE)
    if not b.file:
        raise Http404
    try:
        handle = b.file.open('rb')
    except (FileNotFoundError, OSError):
        raise Http404
    return FileResponse(handle, as_attachment=True, filename=b.file.name.rsplit('/', 1)[-1].split('-', 1)[-1],
                        content_type='application/zip')


# ─────────────────────────────────────────────────────────────────────────────
# Progress
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def progress(request, student_id):
    student = get_object_or_404(Student.objects.select_related('class_assigned'), pk=student_id)
    who = _viewer(request, student)
    if not who:
        return _deny(request, 'the family and staff can see this progress')
    data = history.progress(student, published_only=(who == 'family'))
    for s in data['subjects']:
        s['points'] = history.chart_points(s['series'], 260, 70, 6)
    avg_series = [t['average'] for t in data['terms']]
    return render(request, 'results/progress.html' if who == 'staff' else 'results/progress_family.html', {
        'student': student, 'data': data, 'avg_points': history.chart_points(avg_series),
        'avg_series': avg_series, 'school': request.tenant, 'who': who,
        'x_labels': history.label_positions(data['terms']),
    })


# ─────────────────────────────────────────────────────────────────────────────
# Requests to check a result
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def query_new(request, student_id, term_id):
    student = get_object_or_404(Student, pk=student_id)
    term = get_object_or_404(Term.objects.select_related('session'), pk=term_id)
    if _viewer(request, student) != 'family':
        return _deny(request, "the student's family can ask for a result to be checked")
    published = (TermResult.objects.filter(student=student, term=term, published=True, is_not_offering=False)
                 .select_related('subject').order_by('subject__name'))
    if request.method == 'POST':
        try:
            subject = get_object_or_404(Subject, pk=request.POST.get('subject'))
            history.raise_query(student, term, subject, request.user, request.POST.get('message'))
            messages.success(request, f'Sent. The {subject.name} teacher will check and reply.')
        except history.HistoryError as problem:
            messages.error(request, str(problem))
        return redirect('results:query_new', student.id, term.id)
    return render(request, 'results/query_family.html', {
        'student': student, 'term': term, 'published': published, 'school': request.tenant,
        'queries': ResultQuery.objects.filter(student=student).select_related('subject', 'term')[:20],
        'back': reverse('results:result_detail', args=[student.id, term.session_id, term.id])
        if getattr(request.user, 'is_parent', False) else reverse('results:student_result_detail', args=[term.session_id, term.id]),
    })


@login_required
def queries(request):
    if not can_view(request.user):
        return _deny(request, 'staff can answer result questions')
    qs = ResultQuery.objects.filter(tenant=request.tenant).select_related(
        'student', 'student__class_assigned', 'subject', 'term', 'responded_by')
    mine = [q for q in qs if history.may_answer(request.user, q)]
    if request.method == 'POST':
        q = get_object_or_404(ResultQuery, pk=request.POST.get('id'))
        try:
            history.answer(q, request.user, request.POST.get('response'), request.POST.get('corrected') == '1')
            messages.success(request, 'Reply sent to the family.')
        except history.HistoryError as problem:
            messages.error(request, str(problem))
        return redirect('results:queries')
    for q in mine:
        tr = TermResult.objects.filter(student=q.student, term=q.term, subject=q.subject).first()
        q.current = tr.total_score if tr else None
    return render(request, 'results/queries.html', {
        'open': [q for q in mine if q.status == ResultQuery.OPEN],
        'done': [q for q in mine if q.status != ResultQuery.OPEN][:30],
    })


# ─────────────────────────────────────────────────────────────────────────────
# Transcripts
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def transcript(request, student_id):
    if not can_manage(request.user):
        return _deny(request, 'administrators issue transcripts')
    student = get_object_or_404(Student.objects.select_related('class_assigned'), pk=student_id)
    data = history.transcript(student)
    ref = history.transcript_reference(student) if data['sessions'] else ''
    return render(request, 'results/transcript.html', {'t': data, 'ref': ref, 'school': request.tenant,
                                                       'purpose': (request.GET.get('purpose') or '')[:120]})


def verify_transcript(request, ref):
    data = history.verify_transcript(ref, request.tenant) if getattr(request, 'tenant', None) else None
    return render(request, 'results/transcript_verify.html', {'t': data, 'school': getattr(request, 'tenant', None)},
                  status=200 if data else 404)


def transcript_qr(request, ref):
    from idcards.utils import qr_png_bytes
    return HttpResponse(qr_png_bytes(request.build_absolute_uri(reverse('results:verify_transcript', args=[ref]))),
                        content_type='image/png')
