"""
schoolai/views.py
─────────────────
School intelligence pages: the dashboard (memory, maturity, models, risk,
forecast, learning log), students the models are worried about, each model's
history, the knowledge base, questions waiting to be taught, and "Ask the
school" for everyone.
"""
import json
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from users.models import Class, Staff

from . import knowledge, memory, training
from .models import AskedQuestion, KnowledgeItem, LearningRun, ModelVersion, Prediction, Snapshot


def _is_admin(user, tenant):
    return user.is_authenticated and (user.is_superuser or Staff.objects.filter(tenant=tenant, user=user, role='ADMIN').exists())


def _form_class_ids(user, tenant):
    return list(Class.objects.filter(tenant=tenant, form_teacher__user=user).values_list('pk', flat=True))


def admin_only(view):
    def wrapped(request, *args, **kwargs):
        if not _is_admin(request.user, request.tenant):
            messages.error(request, 'Only school administrators can open the school AI.')
            return redirect('schoolai:ask')
        return view(request, *args, **kwargs)
    wrapped.__name__, wrapped.__doc__ = view.__name__, view.__doc__
    return login_required(wrapped)


# ─────────────────────────────────────────────────────────────────────────────
# Dashboard
# ─────────────────────────────────────────────────────────────────────────────

@admin_only
def dashboard(request):
    t = request.tenant
    if request.method == 'POST' and request.POST.get('op') == 'learn':
        from lms_project.tasks import run_in_background
        if LearningRun.objects.filter(tenant=t, finished_at__isnull=True,
                                      started_at__gte=timezone.now() - timedelta(hours=1)).exists():
            messages.info(request, 'The school AI is already learning — refresh in a minute.')
        else:
            run_in_background(training.learn, t, 'MANUAL', request.user, tenant=t)
            messages.success(request, 'Learning started. It usually takes under a minute — refresh to see the results.')
        return redirect('schoolai:dashboard')
    models = {k: training.active(t, k) for k, _ in ModelVersion.KINDS}
    risk = (Prediction.objects.filter(tenant=t, kind='FAIL_RISK').select_related('student__class_assigned')
            .order_by('-value')[:8])
    avg = {p.student_id: p for p in Prediction.objects.filter(tenant=t, kind='NEXT_AVG', student__in=[r.student_id for r in risk])}
    for r in risk:
        r.expected = avg.get(r.student_id)
    bands = {b: Prediction.objects.filter(tenant=t, kind='FAIL_RISK', band=b).count() for b in ('HIGH', 'MEDIUM', 'LOW')}
    history = {k: list(ModelVersion.objects.filter(tenant=t, kind=k).order_by('version')
                       .values('version', 'status', 'metrics', 'trained_at', 'samples'))[-12:]
               for k in ('NEXT_AVG', 'FAIL_RISK')}
    snaps = list(Snapshot.objects.filter(tenant=t).order_by('-date')[:90])[::-1]
    return render(request, 'schoolai/dashboard.html', {
        'm': training.maturity(t), 'models': models, 'risk': risk, 'bands': bands,
        'runs': LearningRun.objects.filter(tenant=t)[:6], 'last_run': LearningRun.objects.filter(tenant=t).first(),
        'chart': json.dumps({
            'avg': [{'v': h['version'], 'mae': (h['metrics'].get('validation') or {}).get('mae'),
                     'base': (h['metrics'].get('validation') or {}).get('baseline_mae')} for h in history['NEXT_AVG']],
            'risk': [{'v': h['version'], 'auc': (h['metrics'].get('validation') or {}).get('auc')} for h in history['FAIL_RISK']],
            'enrol': (models['ENROLMENT'].params.get('history') if models['ENROLMENT'] else []),
            'forecast': (models['ENROLMENT'].metrics if models['ENROLMENT'] else None),
            'snaps': [{'d': s.date.isoformat(), 'att': s.metrics.get('attendance_7d'), 'risk': s.metrics.get('at_risk')}
                      for s in snaps],
        }),
        'waiting': AskedQuestion.objects.filter(tenant=t, status='UNSURE').count(),
        'disliked': AskedQuestion.objects.filter(tenant=t, status='ANSWERED', rating=-1).count(),
    })


@admin_only
def model_detail(request, kind):
    if kind not in dict(ModelVersion.KINDS):
        return redirect('schoolai:dashboard')
    versions = ModelVersion.objects.filter(tenant=request.tenant, kind=kind)
    current = versions.filter(status='ACTIVE').first()
    labels = {k: v[1] for k, v in memory.FEATURE_INFO.items()}
    importance = [(labels.get(k, k), round(100 * v)) for k, v in ((current.metrics.get('importance') or {}).items()
                                                                  if current else [])]
    return render(request, 'schoolai/model.html', {'kind': kind, 'label': dict(ModelVersion.KINDS)[kind],
                                                   'versions': versions[:40], 'current': current,
                                                   'importance': importance})


# ─────────────────────────────────────────────────────────────────────────────
# Students
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def students(request):
    t = request.tenant
    admin = _is_admin(request.user, t)
    form_classes = _form_class_ids(request.user, t)
    if not admin and not form_classes:
        messages.error(request, 'Predictions are for administrators and form teachers.')
        return redirect('schoolai:ask')
    qs = Prediction.objects.filter(tenant=t, kind='FAIL_RISK').select_related('student__class_assigned', 'term', 'model')
    if not admin:
        qs = qs.filter(student__class_assigned_id__in=form_classes)
    classes = Class.objects.filter(tenant=t, pk__in=qs.values('student__class_assigned')).order_by('name')
    band = request.GET.get('band') if request.GET.get('band') in ('HIGH', 'MEDIUM', 'LOW') else ''
    if band:
        qs = qs.filter(band=band)
    if request.GET.get('class', '').isdigit():
        qs = qs.filter(student__class_assigned_id=request.GET['class'])
    page = Paginator(qs.order_by('-value'), 40).get_page(request.GET.get('page'))
    avg = {p.student_id: p for p in Prediction.objects.filter(tenant=t, kind='NEXT_AVG',
                                                               student__in=[p.student_id for p in page])}
    for p in page:
        p.expected = avg.get(p.student_id)
    return render(request, 'schoolai/students.html', {'page': page, 'classes': classes, 'band': band,
                                                      'class_id': request.GET.get('class', ''), 'admin': admin,
                                                      'maturity': training.maturity(t)})


# ─────────────────────────────────────────────────────────────────────────────
# Knowledge and questions
# ─────────────────────────────────────────────────────────────────────────────

@admin_only
def knowledge_list(request):
    t = request.tenant
    if request.method == 'POST':
        op = request.POST.get('op')
        if op == 'sync':
            messages.success(request, 'Knowledge refreshed: ' + knowledge.sync(t) + '.')
        elif op in ('save', 'delete', 'toggle'):
            item = get_object_or_404(KnowledgeItem, pk=request.POST.get('id'), tenant=t) if request.POST.get('id') else None
            if op == 'delete' and item:
                if item.source in ('MANUAL', 'ANSWER'):
                    item.delete()
                    messages.success(request, 'Removed.')
                else:
                    messages.error(request, 'Automatic knowledge comes from school content — switch it off instead.')
            elif op == 'toggle' and item:
                item.active = not item.active
                item.save(update_fields=['active', 'updated_at'])
            else:
                title, body = (request.POST.get('title') or '').strip(), (request.POST.get('body') or '').strip()
                if not title or not body:
                    messages.error(request, 'Give it a title and the information.')
                else:
                    item = item or KnowledgeItem(tenant=t, source='MANUAL', created_by=request.user)
                    item.title, item.body = title[:200], body
                    item.audience = request.POST.get('audience') if request.POST.get('audience') in dict(KnowledgeItem.AUDIENCES) else 'ALL'
                    item.keywords = (request.POST.get('keywords') or '').strip()[:255]
                    item.save()
                    messages.success(request, f'"{item.title}" saved — the school AI knows it now.')
        from django.core.cache import cache
        cache.delete(knowledge._index_key(t))
        return redirect(request.get_full_path())
    qs = KnowledgeItem.objects.filter(tenant=t)
    src = request.GET.get('source', '')
    if src in dict(KnowledgeItem.SOURCES):
        qs = qs.filter(source=src)
    q = (request.GET.get('q') or '').strip()
    if q:
        qs = qs.filter(title__icontains=q) | qs.filter(body__icontains=q)
    page = Paginator(qs.order_by('-active', '-updated_at'), 30).get_page(request.GET.get('page'))
    edit = KnowledgeItem.objects.filter(tenant=t, pk=request.GET.get('edit')).first() if request.GET.get('edit') else None
    return render(request, 'schoolai/knowledge.html', {'page': page, 'sources': KnowledgeItem.SOURCES, 'source': src, 'q': q,
                                                       'audiences': KnowledgeItem.AUDIENCES, 'edit': edit})


@admin_only
def questions(request):
    t = request.tenant
    if request.method == 'POST':
        question = get_object_or_404(AskedQuestion, pk=request.POST.get('id'), tenant=t)
        if request.POST.get('op') == 'dismiss':
            question.status, question.resolved_by, question.resolved_at = 'RESOLVED', request.user, timezone.now()
            question.save(update_fields=['status', 'resolved_by', 'resolved_at'])
            messages.success(request, 'Dismissed.')
        else:
            try:
                item = knowledge.teach(question, request.user, request.POST.get('answer'), request.POST.get('title'),
                                       request.POST.get('audience'), request.POST.get('keywords') or '')
                messages.success(request, f'Taught. "{item.title}" is now part of the school\'s knowledge, and the asker has been told.')
            except knowledge.KnowledgeError as problem:
                messages.error(request, str(problem))
        return redirect('schoolai:questions')
    waiting = AskedQuestion.objects.filter(tenant=t, status='UNSURE').select_related('user')[:100]
    disliked = AskedQuestion.objects.filter(tenant=t, status='ANSWERED', rating=-1).select_related('user')[:50]
    recent = AskedQuestion.objects.filter(tenant=t).select_related('user')[:30]
    return render(request, 'schoolai/questions.html', {'waiting': waiting, 'disliked': disliked, 'recent': recent,
                                                       'audiences': KnowledgeItem.AUDIENCES})


@login_required
def ask(request):
    t = request.tenant
    audience = knowledge.audience_of(request.user, t)
    if request.method == 'POST':
        try:
            data = json.loads(request.body or b'{}') if request.content_type == 'application/json' else request.POST
            q, items = knowledge.ask(t, request.user, data.get('question'))
        except knowledge.KnowledgeError as problem:
            return JsonResponse({'ok': False, 'error': str(problem)}, status=400)
        return JsonResponse({'ok': True, 'id': q.pk, 'answer': q.answer, 'status': q.status, 'ai': q.used_ai,
                             'sources': [{'title': i.title, 'source': i.get_source_display()} for i in items[:3]]})
    mine = AskedQuestion.objects.filter(tenant=t, user=request.user)[:20]
    return render(request, 'schoolai/ask.html', {'mine': mine, 'suggestions': knowledge.suggestions(t, audience),
                                                 'admin': _is_admin(request.user, t),
                                                 'ai': bool(__import__('assistant.providers', fromlist=['available']).available())})


@login_required
@require_POST
def rate(request, pk):
    q = get_object_or_404(AskedQuestion, pk=pk, tenant=request.tenant)
    try:
        data = json.loads(request.body or b'{}')
        knowledge.rate(q, request.user, bool(data.get('helpful')))
    except (knowledge.KnowledgeError, ValueError) as problem:
        return JsonResponse({'ok': False, 'error': str(problem)}, status=400)
    return JsonResponse({'ok': True})
