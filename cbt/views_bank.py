"""
cbt/views_bank.py
─────────────────
The question bank, putting bank questions on a paper, saving a paper into
the bank, random pools, and item analysis after an exam.
"""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from users.models import Subject
from users.views_profile import can_view

from . import analysis, bank
from .models import BankQuestion, Exam, ExamPart, Question
from .permissions import can_manage_exams


def _deny(request, who):
    messages.error(request, f'Only {who}.')
    return redirect('dashboard:router')


def _may_edit(user, q):
    staff = getattr(user, 'staff', None)
    return can_manage_exams(user) or bool(staff and q.created_by_id == staff.id)


def _may_use_exam(user, exam):
    staff = getattr(user, 'staff', None)
    return can_manage_exams(user) or bool(staff and exam.created_by_id == staff.id)


@login_required
def bank_list(request):
    if not can_view(request.user):
        return _deny(request, 'staff can use the question bank')
    subjects = Subject.objects.filter(tenant=request.tenant).order_by('name')
    f = {k: (request.GET.get(k) or '').strip() for k in ('subject', 'level', 'topic', 'difficulty', 'q', 'students')}
    subject = subjects.filter(pk=f['subject']).first() if f['subject'] else None
    qs = _filtered(request, f, subject)
    page = Paginator(qs, 25).get_page(request.GET.get('page'))
    staff = getattr(request.user, 'staff', None)
    exams = Exam.objects.filter(tenant=request.tenant).select_related('subject').prefetch_related('parts').order_by('-start_time')
    if not can_manage_exams(request.user):
        exams = exams.filter(created_by=staff)
    if subject:
        exams = exams.filter(subject=subject)
    parts = [p for e in exams[:40] for p in e.parts.all() if p.part_type == 'CBT']
    # Exams with objective questions not yet saved in the bank, for "Import from an exam"
    importable = (exams.annotate(unsaved=Count('parts__questions', filter=Q(
                      parts__questions__question_type__in=('MCQ', 'TF'),
                      parts__questions__bank_question__isnull=True)))
                  .filter(unsaved__gt=0)[:60])
    levels = sorted(set(BankQuestion.objects.filter(tenant=request.tenant).exclude(level='')
                        .values_list('level', flat=True)))
    for q in page:
        q.editable = _may_edit(request.user, q)
    query = '&'.join(f'{k}={v}' for k, v in f.items() if v)
    return render(request, 'cbt/bank_list.html', {
        'page': page, 'subjects': subjects, 'f': f, 'parts': parts, 'levels': levels, 'query': query,
        'difficulties': BankQuestion.DIFFICULTY, 'total': qs.count(), 'importable': importable,
        'hidden': qs.filter(for_practice=False).count(),
    })


def _filtered(request, f, subject):
    qs = bank.search(request.tenant, subject, f['level'], f['topic'], f['difficulty'], f['q'])
    if f.get('students') in ('released', 'hidden'):
        qs = qs.filter(for_practice=f['students'] == 'released')
    return qs


@login_required
def bank_release(request):
    """Release questions to students' Quiz Centre, or hide them again — ticked ones, one, or all matching a filter."""
    if not can_view(request.user) or request.method != 'POST':
        return _deny(request, 'staff can use the question bank')
    p = request.POST
    if p.get('toggle'):
        chosen = BankQuestion.objects.filter(tenant=request.tenant, pk=p.get('toggle'))
        release = not chosen.filter(for_practice=True).exists()
    else:
        release = p.get('action') != 'hide'
        if p.get('scope') == 'filter':
            f = {k: (p.get(k) or '').strip() for k in ('subject', 'level', 'topic', 'difficulty', 'q', 'students')}
            subject = Subject.objects.filter(tenant=request.tenant, pk=f['subject']).first() if f['subject'] else None
            chosen = _filtered(request, f, subject)
        else:
            chosen = BankQuestion.objects.filter(tenant=request.tenant, pk__in=[i for i in p.getlist('ids') if i.isdigit()])
    allowed = [q.id for q in chosen.select_related(None).only('id', 'created_by_id') if _may_edit(request.user, q)]
    skipped = chosen.count() - len(allowed)
    n = BankQuestion.objects.filter(pk__in=allowed).exclude(for_practice=release).update(for_practice=release)
    if not allowed:
        messages.error(request, 'Choose questions you may change — your own, or any if you manage exams.')
    elif release:
        messages.success(request, f'{n} question{"s" if n != 1 else ""} released — students can now meet '
                                  f'{"them" if n != 1 else "it"} in the Quiz Centre.' if n else 'Those questions were already released.')
    else:
        messages.success(request, f'{n} question{"s" if n != 1 else ""} hidden from students.' if n
                         else 'Those questions were already hidden from students.')
    if skipped and allowed:
        messages.info(request, f'{skipped} question{"s" if skipped != 1 else ""} written by other teachers {"were" if skipped != 1 else "was"} left as {"they were" if skipped != 1 else "it was"}.')
    nxt = p.get('next') or ''
    return redirect(nxt if nxt.startswith('/') and not nxt.startswith('//') else reverse('cbt:bank'))


@login_required
def bank_edit(request, pk=None):
    if not can_view(request.user):
        return _deny(request, 'staff can use the question bank')
    q = get_object_or_404(BankQuestion.objects.prefetch_related('options'), pk=pk) if pk else None
    if q and not _may_edit(request.user, q):
        return _deny(request, 'exam managers and the teacher who wrote it can edit this question')
    subjects = Subject.objects.filter(tenant=request.tenant).order_by('name')
    if request.method == 'POST':
        if q and request.POST.get('action') == 'delete':
            q.delete()
            messages.success(request, 'Question removed from the bank. Exams that used it keep their copy.')
            return redirect('cbt:bank')
        try:
            subject = get_object_or_404(Subject, pk=request.POST.get('subject'))
            saved = bank.save_question(request.tenant, request.POST, getattr(request.user, 'staff', None), q, subject)
            messages.success(request, 'Question saved to the bank.')
            if request.POST.get('another'):
                return redirect(f"{reverse('cbt:bank_new')}?subject={subject.id}&level={saved.level}&topic={saved.topic}")
            return redirect(f"{reverse('cbt:bank')}?subject={subject.id}")
        except bank.BankError as problem:
            messages.error(request, str(problem))
    fields = ('subject', 'level', 'topic', 'outcome', 'difficulty', 'question_type', 'text', 'passage',
              'explanation', 'marks')
    if request.method == 'POST':           # redisplay what was typed after an error
        v = {k: request.POST.get(k, '') for k in fields}
        v['for_practice'] = bool(request.POST.get('for_practice'))
        rows = [{'i': i, 'text': request.POST.get(f'option_{i}', ''), 'correct': request.POST.get('correct') == str(i)}
                for i in range(5)]
    elif q:
        v = {k: getattr(q, k) for k in fields if k != 'subject'}
        v.update(subject=str(q.subject_id), for_practice=q.for_practice)
        options = list(q.options.all())
        rows = [{'i': i, 'text': options[i].text if i < len(options) else '',
                 'correct': options[i].is_correct if i < len(options) else False} for i in range(5)]
    else:
        v = {'subject': request.GET.get('subject', ''), 'level': request.GET.get('level', ''),
             'topic': request.GET.get('topic', ''), 'difficulty': 'MEDIUM', 'question_type': 'MCQ', 'marks': 1,
             'for_practice': True}
        rows = [{'i': i, 'text': '', 'correct': False} for i in range(5)]
    return render(request, 'cbt/bank_edit.html', {'q': q, 'v': v, 'subjects': subjects, 'rows': rows,
                                                  'difficulties': BankQuestion.DIFFICULTY})


@login_required
def bank_add_to_exam(request):
    if request.method != 'POST':
        return redirect('cbt:bank')
    part = get_object_or_404(ExamPart.objects.select_related('exam__subject'), pk=request.POST.get('part'))
    if not _may_use_exam(request.user, part.exam):
        return _deny(request, 'exam managers and the teacher who set the exam can add questions to it')
    chosen = BankQuestion.objects.filter(tenant=request.tenant, pk__in=request.POST.getlist('ids')).prefetch_related('options')
    try:
        n = bank.add_to_part(chosen, part)
        messages.success(request, f'{n} question{"s" if n != 1 else ""} added to {part.exam.title}.'
                         + (' Some were already on it.' if n < chosen.count() else ''))
    except bank.BankError as problem:
        messages.error(request, str(problem))
    return redirect(request.POST.get('next') or 'cbt:bank')


@login_required
def exam_to_bank(request, exam_id):
    exam = get_object_or_404(Exam, pk=exam_id)
    if not _may_use_exam(request.user, exam) or request.method != 'POST':
        return _deny(request, 'exam managers and the teacher who set the exam can do this')
    staff = getattr(request.user, 'staff', None)
    release = bool(request.POST.get('release'))
    n = 0
    for q in Question.objects.filter(exam_part__exam=exam, question_type__in=('MCQ', 'TF'),
                                     bank_question__isnull=True).select_related('exam_part__exam').prefetch_related('options'):
        if bank.to_bank(q, staff, (request.POST.get('level') or '').strip()[:40], (request.POST.get('topic') or '').strip()[:120],
                        for_practice=release):
            n += 1
    messages.success(request, (f'{n} question{"s" if n != 1 else ""} from {exam.title} saved to the bank'
                               + (' and released to students.' if release else ' — hidden from students until you release them.'))
                     if n else 'Every objective question in this exam is already in the bank.')
    return redirect(request.POST.get('next') or f"{reverse('cbt:bank')}?subject={exam.subject_id}")


@login_required
def exam_analysis(request, exam_id):
    exam = get_object_or_404(Exam.objects.select_related('subject'), pk=exam_id)
    if not _may_use_exam(request.user, exam):
        return _deny(request, 'exam managers and the teacher who set the exam can see its analysis')
    parts = list(exam.parts.filter(part_type='CBT'))
    part = next((p for p in parts if str(p.id) == request.GET.get('part')), parts[0] if parts else None)
    if request.method == 'POST' and part:
        # Set the random pool size from this page
        try:
            size = max(0, int(request.POST.get('pool_size') or 0))
            total = Question.objects.filter(exam_part=part).count()
            if size >= total:
                size = 0
            part.pool_size = size
            part.save(update_fields=['pool_size'])
            messages.success(request, f'Each candidate now gets {size} of the {total} questions at random.' if size
                             else 'Every candidate gets all the questions.')
        except ValueError:
            messages.error(request, 'Pool size must be a whole number.')
        return redirect(f"{reverse('cbt:exam_analysis', args=[exam.id])}?part={part.id}")
    data = analysis.item_analysis(part) if part else {'rows': [], 'candidates': 0, 'summary': None}
    return render(request, 'cbt/exam_analysis.html', {
        'exam': exam, 'part': part, 'parts': parts, 'data': data,
        'question_count': Question.objects.filter(exam_part=part).count() if part else 0,
        'in_bank': Question.objects.filter(exam_part__exam=exam, bank_question__isnull=False).count(),
    })
