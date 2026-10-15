"""
timetable/views_edit.py
───────────────────────
Hand-editing a generated timetable (drag a lesson to another period, with
live clash checks), rooms, teacher availability, teacher-load and clash
reports, and printable class timetables.
"""
import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from users.models import Class, Staff
from users.views_profile import can_manage

from . import editing
from .models import (GeneratedTimetable, TeacherUnavailability, TeachingRoom, TimetableConfiguration, TimetableSlot,
                     TimeSlot)

DAYS = dict(TimetableConfiguration.DAYS)


def _deny(request):
    messages.error(request, 'Only administrators can change the timetable.')
    return redirect('dashboard:router')


def _grid(timetable, school_class):
    config = timetable.config
    days = [(c, DAYS[c]) for c, _ in TimetableConfiguration.DAYS if c in config.active_days]
    periods = list(config.time_slots.all().order_by('order', 'start_time'))
    cells = {(e.day, e.time_slot_id): e for e in timetable.slots.filter(school_class=school_class)
             .select_related('subject', 'teacher', 'room')}
    return days, [{'slot': p, 'cells': [{'day': d, 'entry': cells.get((d, p.id))} for d, _ in days]} for p in periods]


@login_required
def edit(request, pk):
    if not can_manage(request.user):
        return _deny(request)
    tt = get_object_or_404(GeneratedTimetable.objects.select_related('config'), pk=pk)
    classes = Class.objects.filter(pk__in=tt.slots.values('school_class')).order_by('name')
    school_class = classes.filter(pk=request.GET.get('class')).first() or classes.first()
    days, rows = _grid(tt, school_class) if school_class else ([], [])
    return render(request, 'timetable/admin/edit.html', {
        'tt': tt, 'classes': classes, 'school_class': school_class, 'days': days, 'rows': rows,
        'changes': tt.changes.select_related('school_class', 'subject', 'from_slot', 'to_slot')[:15],
        'problems': editing.clash_report(tt),
    })


@login_required
def targets(request, entry_id):
    """For a lesson being dragged: every period it could go to, or why not."""
    if not can_manage(request.user):
        return JsonResponse({'ok': False}, status=403)
    e = get_object_or_404(TimetableSlot.objects.select_related('timetable__config', 'teacher', 'room'), pk=entry_id)
    out = {}
    for d in e.timetable.config.active_days:
        for p in e.timetable.config.time_slots.all():
            occ = TimetableSlot.objects.filter(timetable=e.timetable, school_class=e.school_class, day=d,
                                               time_slot=p).exclude(pk=e.pk).first()
            issues = editing.problems_for(e, d, p, ignore=[occ.pk] if occ else [])
            if occ:
                issues += editing.problems_for(occ, e.day, e.time_slot, ignore=[e.pk])
            out[f'{d}-{p.id}'] = '; '.join(issues)
    return JsonResponse({'ok': True, 'targets': out})


@login_required
@require_POST
def move(request, entry_id):
    if not can_manage(request.user):
        return JsonResponse({'ok': False, 'error': 'Only administrators can change the timetable.'}, status=403)
    e = get_object_or_404(TimetableSlot.objects.select_related('timetable__config'), pk=entry_id)
    try:
        data = json.loads(request.body)
        slot = get_object_or_404(TimeSlot, pk=data.get('slot'))
        changed = editing.move(e, data.get('day'), slot, request.user)
        return JsonResponse({'ok': True, 'moved': len(changed),
                             'notified': e.timetable.is_published})
    except (editing.EditError, ValueError) as problem:
        return JsonResponse({'ok': False, 'error': str(problem)}, status=400)


@login_required
def rooms(request):
    if not can_manage(request.user):
        return _deny(request)
    if request.method == 'POST':
        room = get_object_or_404(TeachingRoom, pk=request.POST.get('id')) if request.POST.get('id') else TeachingRoom(tenant=request.tenant)
        if request.POST.get('action') == 'delete':
            room.delete()
            messages.success(request, 'Room removed.')
            return redirect('timetable:rooms')
        name = (request.POST.get('name') or '').strip()[:80]
        if not name:
            messages.error(request, 'Give the room a name.')
        elif TeachingRoom.objects.filter(tenant=request.tenant, name__iexact=name).exclude(pk=room.pk).exists():
            messages.error(request, f'There is already a room called {name}.')
        else:
            room.name = name
            room.kind = request.POST.get('kind') if request.POST.get('kind') in dict(TeachingRoom.KINDS) else 'OTHER'
            cap = request.POST.get('capacity') or ''
            room.capacity = int(cap) if cap.isdigit() else None
            room.save()
            messages.success(request, f'{room.name} saved.')
        return redirect('timetable:rooms')
    return render(request, 'timetable/admin/rooms.html', {'rooms': TeachingRoom.objects.filter(tenant=request.tenant),
                                                          'kinds': TeachingRoom.KINDS})


@login_required
def availability(request, config_pk):
    if not can_manage(request.user):
        return _deny(request)
    config = get_object_or_404(TimetableConfiguration, pk=config_pk)
    teachers = Staff.objects.filter(tenant=request.tenant, role__in=('TEACHER', 'ADMIN')).order_by('full_name')
    teacher = teachers.filter(pk=request.GET.get('teacher') or request.POST.get('teacher')).first()
    days = [(c, DAYS[c]) for c, _ in TimetableConfiguration.DAYS if c in config.active_days]
    periods = list(config.get_active_slots())
    if request.method == 'POST' and teacher:
        wanted = set(request.POST.getlist('off'))
        TeacherUnavailability.objects.filter(config=config, staff=teacher).delete()
        for key in wanted:
            d, _, sid = key.partition('-')
            if d in config.active_days and any(str(p.id) == sid for p in periods):
                TeacherUnavailability.objects.create(tenant=request.tenant, config=config, staff=teacher, day=d,
                                                     time_slot_id=int(sid))
        messages.success(request, f'{teacher.full_name}: {len(wanted)} period{"s" if len(wanted) != 1 else ""} '
                                  'marked unavailable. Regenerate the timetable to apply this.')
        return redirect(f"{reverse('timetable:availability', args=[config.pk])}?teacher={teacher.pk}")
    off = set(f'{u.day}-{u.time_slot_id}' for u in TeacherUnavailability.objects.filter(config=config, staff=teacher)) \
        if teacher else set()
    counts = {}
    for u in TeacherUnavailability.objects.filter(config=config).values('staff_id'):
        counts[u['staff_id']] = counts.get(u['staff_id'], 0) + 1
    for t in teachers:
        t.off_count = counts.get(t.id, 0)
    grid = [{'period': p, 'cells': [{'key': f'{d}-{p.id}', 'off': f'{d}-{p.id}' in off} for d, _ in days]}
            for p in periods]
    return render(request, 'timetable/admin/availability.html', {
        'config': config, 'teachers': teachers, 'teacher': teacher, 'days': days, 'grid': grid})


@login_required
def reports(request, pk):
    if not can_manage(request.user):
        return _deny(request)
    tt = get_object_or_404(GeneratedTimetable.objects.select_related('config'), pk=pk)
    load, days = editing.teacher_load(tt)
    return render(request, 'timetable/admin/reports.html', {
        'tt': tt, 'load': load, 'days': [DAYS[d] for d in days], 'problems': editing.clash_report(tt),
        'heavy_week': editing.HEAVY_WEEK, 'heavy_day': editing.HEAVY_DAY, 'school': request.tenant})


@login_required
def print_all(request, pk):
    if not (can_manage(request.user) or getattr(request.user, 'is_staff_user', False)):
        return _deny(request)
    tt = get_object_or_404(GeneratedTimetable.objects.select_related('config'), pk=pk)
    sheets = []
    classes = Class.objects.filter(pk__in=tt.slots.values('school_class')).order_by('name')
    only = request.GET.get('class')
    if only:
        classes = classes.filter(pk=only)
    for c in classes:
        days, rows = _grid(tt, c)
        sheets.append({'school_class': c, 'title': f'{c.name} — class timetable', 'kind': 'class',
                       'days': days, 'rows': rows})
    colour = getattr(request.tenant, 'highlight_color', '') or '#0a1f44'
    return render(request, 'timetable/admin/print_all.html', {'tt': tt, 'sheets': sheets, 'school': request.tenant,
                                                              'colour': colour})
