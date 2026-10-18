"""
cbt/views_practice.py
─────────────────────
The student Quiz Centre — quick quizzes, the daily challenge, fixing mistakes,
UTME-style mocks, points, streaks and the class leaderboard — practising from
the questions teachers have released in the bank, and topic mastery for
students, families and teachers.
"""
import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from users.models import Class, Student, Subject
from users.views_profile import can_view

from . import practice
from .models import BankQuestion, PracticeSession


def _student(request):
    return getattr(request.user, 'student', None)


@login_required
def home(request):
    student = _student(request)
    if not student:
        messages.error(request, 'Practice is for students.')
        return redirect('dashboard:router')
    if request.method == 'POST':
        p = request.POST
        try:
            mode = p.get('mode') or 'JAMB'
            ids = p.getlist('subjects') if mode == 'JAMB' else [p.get('subject')]
            source = None
            if p.get('source'):
                source = get_object_or_404(PracticeSession, pk=p.get('source'), student=student,
                                           submitted_at__isnull=False)
            s = practice.start(student, [i for i in ids if i], mode, p.get('per_subject'),
                               (p.get('topic') or '').strip(), p.get('difficulty') or '',
                               p.get('feedback') or 'END', p.get('timed') == 'on', source)
            return redirect('cbt:practice_take', s.id)
        except (practice.PracticeError, ValueError) as problem:
            messages.error(request, str(problem))
        return redirect('cbt:practice')
    current = practice.current(student)
    sessions = PracticeSession.objects.filter(student=student).prefetch_related('subjects')[:15]
    board = 'all' if request.GET.get('board') == 'all' else 'week'
    top, me = practice.leaderboard(student, None if board == 'all' else 7)
    progress = practice.progress(student)
    return render(request, 'cbt/practice_home.html', {
        'student': student, 'available': practice.available(student), 'sessions': sessions, 'current': current,
        'mastery': practice.student_mastery(student), 'max_subjects': practice.MAX_SUBJECTS,
        'stats': practice.stats(student), 'board': board, 'top': top, 'me': me,
        'me_off_board': me and me not in top, 'progress': progress,
        'subject_accuracy': practice.subject_accuracy(student), 'sizes': practice.QUIZ_SIZES,
        'difficulties': BankQuestion.DIFFICULTY, 'daily_size': practice.DAILY_SIZE, 'points': practice.POINTS,
    })


def _own(request, pk):
    return get_object_or_404(PracticeSession.objects.prefetch_related('subjects'), pk=pk, student=_student(request))


@login_required
def take(request, pk):
    session = _own(request, pk)
    if session.finished:
        return redirect('cbt:practice_result', session.id)
    if practice.seconds_left(session) == 0:
        practice.submit(session)
        messages.info(request, 'Time is up — your practice has been marked.')
        return redirect('cbt:practice_result', session.id)
    if request.method == 'POST':
        practice.submit(session)
        return redirect('cbt:practice_result', session.id)
    left = practice.seconds_left(session)
    return render(request, 'cbt/practice_take.html', {
        'session': session, 'sections': practice.sections(session), 'left': -1 if left is None else left,
        'instant': session.feedback == 'INSTANT',
        'elapsed': int((timezone.now() - session.started_at).total_seconds())})


@login_required
@require_POST
def save_answer(request, pk):
    session = _own(request, pk)
    try:
        data = json.loads(request.body)
        verdict = practice.answer(session, data.get('question'), data.get('option'))
        return JsonResponse({'ok': True, 'answered': session.answers.exclude(option__isnull=True).count(), **verdict})
    except (practice.PracticeError, ValueError, TypeError) as problem:
        return JsonResponse({'ok': False, 'error': str(problem)}, status=400)


@login_required
def result(request, pk):
    session = _own(request, pk)
    if not session.finished:
        return redirect('cbt:practice_take', session.id)
    subjects, topics = practice.breakdown(session)
    sections = practice.sections(session)
    return render(request, 'cbt/practice_result.html', {
        'session': session, 'subjects': subjects, 'topics': topics, 'sections': sections,
        'missed': sum(1 for sec in sections for q in sec['questions'] if not q.was_right),
        'subject_id': next(iter(session.paper), ''), 'stats': practice.stats(session.student),
        'utme': round(400 * session.score / session.total) if session.mode == 'JAMB' and session.total else None})


# ─────────────────────────────────────────────────────────────────────────────
# Mastery
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def mastery_class(request):
    if not can_view(request.user):
        messages.error(request, 'Only staff can see class mastery.')
        return redirect('dashboard:router')
    classes = Class.objects.filter(tenant=request.tenant).order_by('name')
    subjects = Subject.objects.filter(tenant=request.tenant, bank_questions__isnull=False).distinct().order_by('name')
    school_class = classes.filter(pk=request.GET.get('class')).first() if request.GET.get('class') else classes.first()
    subject = subjects.filter(pk=request.GET.get('subject')).first() if request.GET.get('subject') else subjects.first()
    data = practice.class_mastery(school_class, subject) if school_class and subject else None
    return render(request, 'cbt/mastery_class.html', {'classes': classes, 'subjects': subjects,
                                                      'school_class': school_class, 'subject': subject, 'data': data})


@login_required
def mastery_student(request, student_id):
    student = get_object_or_404(Student.objects.select_related('class_assigned'), pk=student_id)
    family = False
    if not can_view(request.user):
        own = _student(request) and _student(request).id == student.id
        parent = False
        if getattr(request.user, 'is_parent', False):
            from users.views_achievements import _parent_children
            parent = _parent_children(request).filter(pk=student.pk).exists()
        if not (own or parent):
            messages.error(request, "Only the student's family and staff can see this.")
            return redirect('dashboard:router')
        family = True
    sessions = PracticeSession.objects.filter(student=student, submitted_at__isnull=False).prefetch_related('subjects')[:10]
    return render(request, 'cbt/mastery_family.html' if family else 'cbt/mastery_student.html', {
        'student': student, 'mastery': practice.student_mastery(student), 'sessions': sessions,
        'school': request.tenant})
