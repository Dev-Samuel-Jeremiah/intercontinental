"""
cbt/views_live.py
─────────────────
During and after a paper: integrity events from the candidate's browser, the
invigilator's live console, answer review, and printing a paper version.
"""
import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import engine
from .models import Exam, ExamAttempt, ExamPart
from .permissions import can_manage_exams


def _may_invigilate(user, exam):
    staff = getattr(user, 'staff', None)
    return can_manage_exams(user) or bool(staff and exam.created_by_id == staff.id)


@login_required
@require_POST
def integrity_event(request):
    student = getattr(request.user, 'student', None)
    try:
        data = json.loads(request.body)
        attempt = ExamAttempt.objects.select_related('exam_part__exam').get(
            pk=data.get('attempt_id'), student=student, completed=False)
    except (ValueError, TypeError, ExamAttempt.DoesNotExist):
        return JsonResponse({'ok': False}, status=404)
    if not attempt.exam_part.exam.lockdown:
        return JsonResponse({'ok': True, 'logged': False})
    event = engine.record_event(attempt, data.get('kind'), data.get('seconds') or 0)
    count = attempt.integrity_events.count()
    return JsonResponse({'ok': True, 'logged': bool(event), 'count': count})


@login_required
def invigilate(request, exam_id):
    exam = get_object_or_404(Exam.objects.prefetch_related('classes'), pk=exam_id)
    if not _may_invigilate(request.user, exam):
        messages.error(request, 'Only exam managers and the teacher who set this exam can invigilate it.')
        return redirect('dashboard:router')
    part = exam.parts.filter(pk=request.GET.get('part')).first() or exam.parts.filter(part_type='CBT').first() \
        or exam.parts.first()
    if not part:
        messages.error(request, 'Add a CBT section to this exam first.')
        return redirect('cbt:admin_exam_list')
    if request.method == 'POST':
        attempt = get_object_or_404(ExamAttempt, pk=request.POST.get('attempt_id'), exam_part=part)
        try:
            if request.POST.get('action') == 'extend':
                engine.extend(attempt, request.POST.get('minutes') or 0, request.user)
                messages.success(request, f'{attempt.student.full_name} has {request.POST.get("minutes")} more minutes.')
            elif request.POST.get('action') == 'submit':
                engine.finish(attempt)
                messages.success(request, f"{attempt.student.full_name}'s paper submitted with their saved answers.")
        except engine.EngineError as problem:
            messages.error(request, str(problem))
        return redirect(f"{reverse('cbt:invigilate', args=[exam.id])}?part={part.id}")
    data = engine.console(part)
    if request.GET.get('format') == 'json':
        return JsonResponse({'counts': data['counts'], 'rows': [{
            'id': r['student'].id, 'state': r['state'], 'answered': r['answered'], 'events': r['events'],
            'left': r['left'], 'connected': r['connected'],
            'score': r['attempt'].score if r['attempt'] and r['state'] == 'submitted' else None}
            for r in data['rows']], 'time': timezone.localtime().strftime('%H:%M:%S')})
    return render(request, 'cbt/invigilate.html', {'exam': exam, 'part': part, 'data': data,
                                                   'parts': exam.parts.all()})


@login_required
def invigilate_events(request, attempt_id):
    attempt = get_object_or_404(ExamAttempt.objects.select_related('exam_part__exam', 'student'), pk=attempt_id)
    if not _may_invigilate(request.user, attempt.exam_part.exam):
        return JsonResponse({'ok': False}, status=403)
    return JsonResponse({'student': attempt.student.full_name, 'events': [
        {'kind': e.get_kind_display(), 'at': timezone.localtime(e.at).strftime('%H:%M:%S'),
         'seconds_away': e.seconds_away} for e in attempt.integrity_events.all()[:100]]})


@login_required
def review_answers(request, attempt_id):
    student = getattr(request.user, 'student', None)
    attempt = get_object_or_404(ExamAttempt.objects.select_related('exam_part__exam__subject'), pk=attempt_id,
                                student=student)
    if not engine.review_open(attempt):
        exam = attempt.exam_part.exam
        messages.info(request, 'Answers for this exam are not open for review'
                      + (f' until {timezone.localtime(exam.end_time):%d %b, %H:%M}.' if exam.answer_review == 'AFTER_CLOSE'
                         else '.'))
        return redirect('cbt:available_exams')
    items = engine.review(attempt)
    auto = [i for i in items if i['auto_marked']]
    from . import solutions
    for i in items:
        i['solvable'] = solutions.solvable(i['question'])
    return render(request, 'cbt/review_answers.html', {
        'attempt': attempt, 'exam': attempt.exam_part.exam, 'items': items,
        'right': sum(1 for i in auto if i['right']), 'auto_total': len(auto),
        'any_solvable': any(i['solvable'] for i in items),
    })


@login_required
@require_POST
def review_solutions(request, attempt_id):
    """Worked solutions for a review page: the stored ones, plus the next few written by the AI."""
    from assistant.providers import AIError

    from . import solutions
    student = getattr(request.user, 'student', None)
    attempt = get_object_or_404(ExamAttempt.objects.select_related('exam_part__exam__subject', 'student__class_assigned'),
                                pk=attempt_id, student=student)
    if not engine.review_open(attempt):
        return JsonResponse({'error': 'Answers for this exam are not open for review.'}, status=403)
    questions = [q for q in engine.paper(attempt) if solutions.solvable(q)]
    have = solutions.existing(questions)
    missing = [q for q in questions if q.id not in have]
    error = ''
    if missing and request.POST.get('write') == '1':
        try:
            have.update(solutions.write(request.tenant, missing[:solutions.BATCH], request.user,
                                        subject=attempt.exam_part.exam.subject.name,
                                        level=getattr(attempt.student.class_assigned, 'name', '')))
        except AIError as problem:
            wait = solutions.retry_after(str(problem))
            if wait is None:
                error = str(problem)
            else:                       # the provider's per-minute limit: the page waits and carries on
                return JsonResponse({'solutions': {str(qid): solutions.public(s) for qid, s in have.items()},
                                     'pending': [q.id for q in missing], 'error': '', 'retry_after': wait})
    return JsonResponse({'solutions': {str(qid): solutions.public(s) for qid, s in have.items()},
                         'pending': [q.id for q in questions if q.id not in have], 'error': error})


@login_required
def print_paper(request, exam_id):
    exam = get_object_or_404(Exam.objects.select_related('subject'), pk=exam_id)
    if not _may_invigilate(request.user, exam):
        messages.error(request, 'Only exam managers and the teacher who set this exam can print it.')
        return redirect('dashboard:router')
    sections = []
    for part in exam.parts.prefetch_related('questions__options').order_by('part_type'):
        qs = list(part.questions.all().order_by('id'))
        shown = set()
        for q in qs:
            q.letters = [(chr(65 + i), o) for i, o in enumerate(q.options.all().order_by('id'))]
            key = q.passage_group or f'q{q.id}'
            q.show_passage = bool(q.passage) and key not in shown
            shown.add(key)
        sections.append({'part': part, 'questions': qs})
    return render(request, 'cbt/print_paper.html', {'exam': exam, 'sections': sections, 'school': request.tenant,
                                                    'key': request.GET.get('key') == '1',
                                                    'classes': ', '.join(c.name for c in exam.classes.all())})
