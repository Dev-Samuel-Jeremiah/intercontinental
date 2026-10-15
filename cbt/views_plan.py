"""
cbt/views_plan.py
─────────────────
Exam halls and seating plans, and the exam timetable.
"""
from datetime import date, time

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from academics.models import Term
from academics.utils import get_active_term
from users.models import Class

from . import planning
from .models import Exam, ExamHall, ExamTimetable, ExamTimetableEntry
from .permissions import can_manage_exams


def _deny(request):
    messages.error(request, 'Only exam managers can do this.')
    return redirect('dashboard:router')


def _date(raw):
    try:
        return date.fromisoformat(raw)
    except (TypeError, ValueError):
        raise planning.PlanningError('Enter valid dates.')


def _time(raw, default):
    try:
        return time.fromisoformat(raw) if raw else default
    except ValueError:
        return default


@login_required
def halls(request):
    if not can_manage_exams(request.user):
        return _deny(request)
    if request.method == 'POST':
        hall = get_object_or_404(ExamHall, pk=request.POST.get('id')) if request.POST.get('id') else ExamHall(tenant=request.tenant)
        if request.POST.get('action') == 'delete':
            hall.delete()
            messages.success(request, 'Hall removed.')
            return redirect('cbt:halls')
        name = (request.POST.get('name') or '').strip()[:80]
        try:
            rows, cols = int(request.POST.get('rows') or 0), int(request.POST.get('columns') or 0)
        except ValueError:
            rows = cols = 0
        if not name or not (1 <= rows <= 40 and 1 <= cols <= 40):
            messages.error(request, 'Give the hall a name, and 1 to 40 rows and columns.')
        elif ExamHall.objects.filter(tenant=request.tenant, name__iexact=name).exclude(pk=hall.pk).exists():
            messages.error(request, f'There is already a hall called {name}.')
        else:
            hall.name, hall.rows, hall.columns = name, rows, cols
            hall.notes = (request.POST.get('notes') or '').strip()[:160]
            hall.active = bool(request.POST.get('active')) if request.POST.get('id') else True
            hall.save()
            messages.success(request, f'{hall.name} saved — {hall.capacity} seats.')
        return redirect('cbt:halls')
    return render(request, 'cbt/halls.html', {'halls': ExamHall.objects.filter(tenant=request.tenant)})


@login_required
def seating(request, exam_id):
    exam = get_object_or_404(Exam.objects.select_related('subject').prefetch_related('classes'), pk=exam_id)
    if not can_manage_exams(request.user):
        return _deny(request)
    all_halls = ExamHall.objects.filter(tenant=request.tenant, active=True)
    if request.method == 'POST':
        try:
            if request.POST.get('action') == 'clear':
                exam.seats.all().delete()
                messages.success(request, 'Seating plan cleared.')
            else:
                chosen = list(all_halls.filter(pk__in=request.POST.getlist('halls')))
                if not chosen:
                    raise planning.PlanningError('Choose at least one hall.')
                n = planning.allocate(exam, chosen)
                messages.success(request, f'{n} candidate{"s" if n != 1 else ""} seated. Students can see their seat on their exam list.')
        except planning.PlanningError as problem:
            messages.error(request, str(problem))
        return redirect('cbt:seating', exam.id)
    data = planning.chart(exam)
    return render(request, 'cbt/seating.html', {
        'exam': exam, 'halls': all_halls, 'data': data, 'candidates': len(planning.candidates(exam)),
        'same_class': planning.neighbours_same_class(exam) if data['total'] else 0,
        'used': {p['hall'].id for p in data['plans']}, 'school': request.tenant,
    })


@login_required
def timetables(request):
    if not can_manage_exams(request.user):
        return _deny(request)
    classes = Class.objects.filter(tenant=request.tenant).order_by('name')
    if request.method == 'POST':
        try:
            name = (request.POST.get('name') or '').strip()[:120]
            if not name:
                raise planning.PlanningError('Give the timetable a name.')
            chosen = list(classes.filter(pk__in=request.POST.getlist('classes')))
            if not chosen:
                raise planning.PlanningError('Choose the classes sitting these exams.')
            tt = ExamTimetable.objects.create(
                tenant=request.tenant, name=name, term=Term.objects.filter(pk=request.POST.get('term') or None).first(),
                start_date=_date(request.POST.get('start_date')), end_date=_date(request.POST.get('end_date')),
                morning_start=_time(request.POST.get('morning_start'), time(9, 0)),
                afternoon_start=_time(request.POST.get('afternoon_start'), time(12, 0)),
                afternoon=bool(request.POST.get('afternoon')),
                paper_minutes=max(15, min(240, int(request.POST.get('paper_minutes') or 90))))
            tt.classes.set(chosen)
            unplaced = planning.generate(tt)
            if unplaced:
                messages.warning(request, f"{len(unplaced)} paper{'s' if len(unplaced) != 1 else ''} didn't fit: "
                                 + ', '.join(f"{s.name} ({', '.join(c.name for c in cl)})" for s, cl in unplaced[:6])
                                 + '. Add days or allow afternoon papers, then regenerate.')
            else:
                messages.success(request, f'Timetable built with {tt.entries.count()} papers and no clashes.')
            return redirect('cbt:timetable', tt.id)
        except (planning.PlanningError, ValueError) as problem:
            messages.error(request, str(problem))
        return redirect('cbt:timetables')
    return render(request, 'cbt/timetables.html', {
        'items': ExamTimetable.objects.filter(tenant=request.tenant).prefetch_related('classes'),
        'classes': classes, 'terms': Term.objects.filter(tenant=request.tenant).select_related('session').order_by('-start_date'),
        'active_term': get_active_term(),
    })


@login_required
def timetable(request, pk):
    tt = get_object_or_404(ExamTimetable.objects.prefetch_related('classes'), pk=pk)
    if not can_manage_exams(request.user):
        return _deny(request)
    back = reverse('cbt:timetable', args=[tt.id])
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action == 'regenerate':
                unplaced = planning.generate(tt)
                messages.success(request, 'Rebuilt.' if not unplaced else
                                 f"Rebuilt, but {len(unplaced)} paper(s) didn't fit: "
                                 + ', '.join(s.name for s, _ in unplaced[:6]))
            elif action == 'move':
                entry = get_object_or_404(ExamTimetableEntry, pk=request.POST.get('entry'), timetable=tt)
                planning.move(entry, _date(request.POST.get('date')), request.POST.get('sitting'))
                messages.success(request, f'{entry.subject.name} moved.')
            elif action == 'make_exam':
                entry = get_object_or_404(ExamTimetableEntry, pk=request.POST.get('entry'), timetable=tt)
                exam = planning.make_exam(entry, getattr(request.user, 'staff', None))
                messages.success(request, f'CBT exam "{exam.title}" created (not published). Add its questions next.')
            elif action == 'make_all':
                n = 0
                for entry in tt.entries.filter(exam__isnull=True):
                    planning.make_exam(entry, getattr(request.user, 'staff', None))
                    n += 1
                messages.success(request, f'{n} CBT exams created, unpublished.')
            elif action == 'publish':
                tt.published = not tt.published
                tt.save(update_fields=['published'])
                messages.success(request, 'Published — students and parents can see it.' if tt.published else 'Hidden from families.')
            elif action == 'delete':
                tt.delete()
                messages.success(request, 'Timetable deleted. CBT exams created from it are kept.')
                return redirect('cbt:timetables')
        except planning.PlanningError as problem:
            messages.error(request, str(problem))
        return redirect(back)
    entries = list(tt.entries.select_related('subject', 'exam').prefetch_related('classes'))
    days = []
    for d, s in planning.sittings(tt):
        if not days or days[-1]['date'] != d:
            days.append({'date': d, 'AM': [], 'PM': []})
        days[-1][s] = [e for e in entries if e.date == d and e.sitting == s]
    return render(request, 'cbt/timetable.html', {
        'tt': tt, 'days': days, 'clashes': planning.clashes(tt), 'count': len(entries),
        'slots': planning.sittings(tt), 'school': request.tenant,
    })
