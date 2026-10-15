"""
timetable/views_lessons.py
──────────────────────────
Teachers take registers for their lessons; administrators see which
registers are missing, who missed lessons while in school, and attendance
by subject.
"""
from datetime import date, timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from academics.utils import get_active_term
from users.models import Class
from users.views_profile import can_manage, can_view

from . import lessons
from .models import TimetableSlot


def _day(request):
    try:
        return date.fromisoformat(request.GET.get('date') or request.POST.get('date') or '')
    except ValueError:
        return timezone.localdate()


def _may_take(user, slot):
    staff = getattr(user, 'staff', None)
    return can_manage(user) or (staff and (slot.teacher_id == staff.id or staff.can_mark_attendance))


def _exam_classes(tenant, day):
    from cbt.models import ExamTimetable
    return sorted({c.name for t in ExamTimetable.objects.filter(tenant=tenant, published=True, start_date__lte=day,
                                                                end_date__gte=day).prefetch_related('classes')
                   for c in t.classes.all()})


@login_required
def my_lessons(request):
    staff = getattr(request.user, 'staff', None)
    if not (staff or can_manage(request.user)):
        messages.error(request, 'Only staff can take lesson registers.')
        return redirect('dashboard:router')
    day = _day(request)
    rows = lessons.lessons_on(request.tenant, day, teacher=staff) if staff else []
    from curriculum.staffing import covers_for
    covers = covers_for(staff, day) if staff else []
    if covers:
        from timetable.models import LessonRegister
        taken = {r.slot_id: r for r in LessonRegister.objects.filter(
            tenant=request.tenant, date=day, slot__in=[c.slot_id for c in covers])}
        for c in covers:
            c.register = taken.get(c.slot_id)
    return render(request, 'timetable/my_lessons.html', {
        'covers': covers,
        'exam_classes': _exam_classes(request.tenant, day),
        'day': day, 'rows': rows, 'has_timetable': bool(lessons.published_timetable(request.tenant)),
        'prev': day - timedelta(days=1), 'next': day + timedelta(days=1), 'today': timezone.localdate(),
        'can_manage': can_manage(request.user),
    })


@login_required
def take_register(request, slot_id):
    slot = get_object_or_404(TimetableSlot.objects.select_related('school_class', 'subject', 'teacher',
                                                                  'time_slot'), pk=slot_id)
    day = _day(request)
    from curriculum.staffing import is_covering
    if not (_may_take(request.user, slot) or is_covering(getattr(request.user, 'staff', None), slot, day)):
        messages.error(request, "You can only take registers for your own lessons, or ones you are covering.")
        return redirect('timetable:my_lessons')
    if request.method == 'POST':
        marks = {}
        for key, value in request.POST.items():
            if key.startswith('s_') and key[2:].isdigit():
                sid = int(key[2:])
                marks[sid] = (value, request.POST.get(f'late_{sid}') or 0)
        try:
            lessons.save_register(slot, day, marks, getattr(request.user, 'staff', None),
                                  request.POST.get('note'))
            absent = sum(1 for s, _ in marks.values() if s == 'A')
            messages.success(request, f'{slot.subject.name} register saved for {slot.school_class.name}'
                             + (f' — {absent} absent.' if absent else ' — everyone here.'))
            return redirect(reverse('timetable:my_lessons') + f'?date={day.isoformat()}')
        except lessons.LessonError as problem:
            messages.error(request, str(problem))
            return redirect(reverse('timetable:take_register', args=[slot.id]) + f'?date={day.isoformat()}')
    rows, register = lessons.roster(slot, day)
    return render(request, 'timetable/take_register.html', {'slot': slot, 'day': day, 'rows': rows,
                                                            'register': register})


@login_required
def lessons_overview(request):
    if not can_view(request.user):
        messages.error(request, 'Only staff can see lesson attendance.')
        return redirect('dashboard:router')
    day = _day(request)
    rows = lessons.lessons_on(request.tenant, day)
    return render(request, 'timetable/lessons_overview.html', {
        'day': day, 'rows': rows, 'missing': [r for r in rows if not r['taken']],
        'missed': lessons.missed_lessons(request.tenant, day),
        'has_timetable': bool(lessons.published_timetable(request.tenant)),
        'prev': day - timedelta(days=1), 'next': day + timedelta(days=1),
    })


@login_required
def subject_attendance(request):
    if not can_view(request.user):
        messages.error(request, 'Only staff can see lesson attendance.')
        return redirect('dashboard:router')
    cls = Class.objects.filter(pk=request.GET.get('class') or 0).first()
    term = get_active_term()
    start = term.start_date if term and term.start_date else timezone.localdate() - timedelta(days=90)
    end = timezone.localdate()
    students, subjects, table = lessons.subject_attendance(cls, start, end) if cls else ([], [], {})
    return render(request, 'timetable/subject_attendance.html', {
        'classes': Class.objects.order_by('name'), 'cls': cls, 'students': students, 'subjects': subjects,
        'table': table, 'start': start, 'end': end,
    })
