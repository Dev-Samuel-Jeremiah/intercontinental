"""
curriculum/views_staffing.py
────────────────────────────
Cover for absent teachers, lesson observations, and clubs and societies.
"""
from datetime import date, timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from users.models import ENROLLED_STATUSES, Class, Staff, Student, Subject
from users.views_profile import can_manage, can_view

from . import staffing as sf
from .models import SCALE, Club, ClubMeeting, ClubMember, CoverAssignment, LessonObservation, ObservationTemplate


def _deny(request, who):
    messages.error(request, f'Only {who}.')
    return redirect('dashboard:router')


def _date(raw, default):
    try:
        return date.fromisoformat(raw) if raw else default
    except ValueError:
        return default


# ─────────────────────────────────────────────────────────────────────────────
# Cover
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def cover(request):
    if not can_manage(request.user):
        return _deny(request, 'administrators arrange cover')
    today = timezone.localdate()
    day = _date(request.GET.get('date') or request.POST.get('date'), today)
    away = [i for i in request.GET.getlist('away') if i.isdigit()]
    back = reverse('curriculum:cover') + f'?date={day.isoformat()}' + ''.join(f'&away={i}' for i in away)
    if request.method == 'POST':
        try:
            if request.POST.get('action') == 'cancel':
                get_object_or_404(CoverAssignment, pk=request.POST.get('id')).delete()
                messages.success(request, 'Cover cancelled.')
            else:
                from timetable.models import TimetableSlot
                slot = get_object_or_404(TimetableSlot.objects.select_related('time_slot', 'subject', 'school_class'),
                                         pk=request.POST.get('slot_id'))
                teacher = get_object_or_404(Staff, pk=request.POST.get('cover_id'))
                sf.assign_cover(slot, day, teacher, request.user, request.POST.get('note'))
                messages.success(request, f'{teacher.full_name} will cover {slot.subject.name} for '
                                          f'{slot.school_class.name}. They have been emailed.')
        except sf.StaffingError as problem:
            messages.error(request, str(problem))
        return redirect(back)
    rows = sf.absences(request.tenant, day, away)
    for r in rows:
        for lesson in r['lessons']:
            lesson['free'] = sf.free_teachers(lesson['slot'], day) if day >= today else []
    need = sum(1 for r in rows for lesson in r['lessons'] if not lesson['cover'])
    from timetable.lessons import published_timetable
    return render(request, 'curriculum/cover.html', {
        'day': day, 'today': today, 'rows': rows, 'need': need,
        'prev': day - timedelta(days=1), 'next': day + timedelta(days=1),
        'teachers': Staff.objects.filter(tenant=request.tenant, role__in=('TEACHER', 'ADMIN')).order_by('full_name'),
        'away': [int(i) for i in away], 'has_timetable': bool(published_timetable(request.tenant)),
        'summary': sf.cover_summary(request.tenant, today - timedelta(days=30), today + timedelta(days=30)),
    })


# ─────────────────────────────────────────────────────────────────────────────
# Lesson observations
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def observations(request):
    if not can_view(request.user):
        return _deny(request, 'staff can see lesson observations')
    staff = getattr(request.user, 'staff', None)
    manage = can_manage(request.user)
    from .services import hod_subject_ids
    heads = hod_subject_ids(staff)
    if request.method == 'POST' and request.POST.get('action') == 'rubric':
        if not manage:
            return _deny(request, 'administrators can change the observation form')
        tpl = sf.template_for(request.tenant)
        lines = [l.strip()[:120] for l in (request.POST.get('criteria') or '').splitlines() if l.strip()]
        if not 3 <= len(lines) <= 25:
            messages.error(request, 'List 3 to 25 things to observe, one per line.')
        else:
            tpl.criteria = '\n'.join(lines)
            tpl.save()
            messages.success(request, 'Observation form saved. Past observations keep their own criteria.')
        return redirect('curriculum:observations')
    qs = LessonObservation.objects.filter(tenant=request.tenant).select_related('teacher', 'observer', 'subject',
                                                                               'school_class')
    if not manage:
        from django.db.models import Q
        qs = qs.filter(Q(teacher=staff, shared=True) | Q(observer=staff) | Q(subject_id__in=heads))
    observable = []
    if manage or heads:
        from users.models import StaffSubjectClass
        teachers = Staff.objects.filter(tenant=request.tenant, role__in=('TEACHER', 'ADMIN'))
        if not manage:
            teachers = teachers.filter(id__in=StaffSubjectClass.objects.filter(
                tenant=request.tenant, subject_id__in=heads).values('staff_id'))
        observable = teachers.exclude(id=getattr(staff, 'id', None)).order_by('full_name')
    return render(request, 'curriculum/observations.html', {
        'items': qs[:200], 'observable': observable, 'can_manage': manage,
        'template': sf.template_for(request.tenant), 'staff': staff,
        'unread': qs.filter(teacher=staff, shared=True, acknowledged_at__isnull=True).count() if staff else 0,
    })


@login_required
def observation_new(request):
    teacher = get_object_or_404(Staff, pk=request.GET.get('teacher') or request.POST.get('teacher'))
    if not sf.may_observe(request.user, teacher):
        return _deny(request, "administrators and the teacher's head of department can observe lessons")
    criteria = sf.template_for(request.tenant).criteria_list
    if request.method == 'POST':
        try:
            obs = sf.record_observation(teacher, request.user, request.POST, criteria)
            messages.success(request, 'Observation saved' + (' and shared with the teacher.' if obs.shared else
                                                             '. Share it with the teacher when you are ready.'))
            return redirect('curriculum:observation', obs.id)
        except sf.StaffingError as problem:
            messages.error(request, str(problem))
    from users.models import StaffSubjectClass
    pairs = StaffSubjectClass.objects.filter(tenant=request.tenant, staff=teacher).select_related('subject', 'school_class')
    return render(request, 'curriculum/observation_new.html', {
        'teacher': teacher, 'scale': SCALE, 'today': timezone.localdate(),
        'criteria': [{'i': i, 'name': c, 'score': request.POST.get(f'score_{i}', ''),
                      'comment': request.POST.get(f'comment_{i}', '')} for i, c in enumerate(criteria)],
        'subjects': sorted({p.subject for p in pairs}, key=lambda s: s.name) or Subject.objects.filter(tenant=request.tenant).order_by('name'),
        'classes': sorted({p.school_class for p in pairs}, key=lambda c: c.name) or Class.objects.filter(tenant=request.tenant).order_by('name'),
        'posted': request.POST,
    })


@login_required
def observation(request, pk):
    obs = get_object_or_404(LessonObservation.objects.select_related('teacher', 'observer', 'subject', 'school_class'),
                            pk=pk)
    staff = getattr(request.user, 'staff', None)
    is_teacher = staff and obs.teacher_id == staff.id
    may_manage = sf.may_observe(request.user, obs.teacher, obs.subject) or (staff and obs.observer_id == staff.id)
    if not ((is_teacher and obs.shared) or may_manage):
        return _deny(request, 'the observer, administrators and (once shared) the teacher can see this')
    if request.method == 'POST':
        try:
            if request.POST.get('action') == 'share' and may_manage:
                sf.share(obs)
                messages.success(request, f'Shared with {obs.teacher.full_name}.')
            elif request.POST.get('action') == 'acknowledge' and is_teacher:
                sf.acknowledge(obs, staff, request.POST.get('response'))
                messages.success(request, 'Thank you — your response is recorded.')
        except sf.StaffingError as problem:
            messages.error(request, str(problem))
        return redirect('curriculum:observation', obs.id)
    return render(request, 'curriculum/observation.html', {
        'obs': obs, 'is_teacher': is_teacher, 'may_manage': may_manage, 'scale': dict(SCALE),
        'trend': sf.teacher_trend(obs.teacher) if may_manage or is_teacher else [],
    })


# ─────────────────────────────────────────────────────────────────────────────
# Clubs
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def clubs(request):
    if not can_view(request.user):
        return _deny(request, 'staff can see clubs')
    manage = can_manage(request.user)
    if request.method == 'POST':
        if not manage:
            return _deny(request, 'administrators can set up clubs')
        club = get_object_or_404(Club, pk=request.POST.get('id')) if request.POST.get('id') else Club(tenant=request.tenant)
        if request.POST.get('action') == 'delete':
            club.delete()
            messages.success(request, 'Club removed.')
            return redirect('curriculum:clubs')
        name = (request.POST.get('name') or '').strip()[:80]
        if not name:
            messages.error(request, 'Give the club a name.')
            return redirect('curriculum:clubs')
        if Club.objects.filter(tenant=request.tenant, name__iexact=name).exclude(pk=club.pk).exists():
            messages.error(request, f'There is already a club called {name}.')
            return redirect('curriculum:clubs')
        club.name = name
        club.description = (request.POST.get('description') or '').strip()[:255]
        club.meeting_day = request.POST.get('meeting_day') if request.POST.get('meeting_day') in dict(Club.DAYS) else ''
        try:
            from datetime import time
            club.meeting_time = time.fromisoformat(request.POST['meeting_time']) if request.POST.get('meeting_time') else None
        except ValueError:
            club.meeting_time = None
        club.venue = (request.POST.get('venue') or '').strip()[:80]
        cap = request.POST.get('capacity') or ''
        club.capacity = int(cap) if cap.isdigit() and int(cap) > 0 else None
        club.active = request.POST.get('active', 'on') == 'on' if request.POST.get('id') else True
        club.save()
        club.patrons.set(Staff.objects.filter(tenant=request.tenant, pk__in=request.POST.getlist('patrons')))
        messages.success(request, f'{club.name} saved.')
        return redirect('curriculum:club', club.id) if not request.POST.get('id') else redirect('curriculum:clubs')
    from django.db.models import Count
    items = (Club.objects.filter(tenant=request.tenant).prefetch_related('patrons')
             .annotate(n_members=Count('members', distinct=True), n_meetings=Count('meetings', distinct=True)))
    return render(request, 'curriculum/clubs.html', {
        'clubs': items, 'can_manage': manage, 'days': Club.DAYS,
        'staff': Staff.objects.filter(tenant=request.tenant).order_by('full_name'),
    })


@login_required
def club(request, pk):
    c = get_object_or_404(Club.objects.prefetch_related('patrons'), pk=pk)
    if not can_view(request.user):
        return _deny(request, 'staff can see clubs')
    runs = sf.may_run(request.user, c)
    back = reverse('curriculum:club', args=[c.id])
    if request.method == 'POST':
        if not runs:
            return _deny(request, "the club's patrons and administrators can change it")
        action = request.POST.get('action')
        try:
            if action == 'add':
                students = Student.objects.filter(tenant=request.tenant, pk__in=request.POST.getlist('student_ids'))
                added, full = sf.add_members(c, students)
                messages.success(request, f'{added} member{"s" if added != 1 else ""} added.'
                                 + (f' The club is full ({c.capacity}).' if full else ''))
            elif action == 'role':
                sf.set_role(get_object_or_404(ClubMember, pk=request.POST.get('member_id'), club=c),
                            request.POST.get('role'))
                messages.success(request, 'Role updated.')
            elif action == 'remove':
                get_object_or_404(ClubMember, pk=request.POST.get('member_id'), club=c).delete()
                messages.success(request, 'Removed from the club.')
            elif action == 'meeting':
                day = _date(request.POST.get('date'), timezone.localdate())
                sf.record_meeting(c, day, request.POST.getlist('present'), request.POST.get('topic'),
                                  request.POST.get('notes'), getattr(request.user, 'staff', None))
                messages.success(request, f'Meeting on {day:%d %b} recorded.')
        except sf.StaffingError as problem:
            messages.error(request, str(problem))
        return redirect(back + (f"?class={request.POST.get('class_id')}" if request.POST.get('class_id') else ''))
    members = list(c.members.select_related('student', 'student__class_assigned'))
    rates = sf.member_rates(c)
    for m in members:
        m.rate = rates.get(m.student_id)
    meetings = list(c.meetings.prefetch_related('attendance')[:20])
    for mt in meetings:
        mt.present_count = sum(1 for a in mt.attendance.all() if a.present)
    classes = Class.objects.filter(tenant=request.tenant).order_by('name')
    pick_class = classes.filter(pk=request.GET.get('class')).first() if request.GET.get('class') else None
    in_club = {m.student_id for m in members}
    candidates = (Student.objects.filter(tenant=request.tenant, class_assigned=pick_class, status__in=ENROLLED_STATUSES)
                  .exclude(id__in=in_club).order_by('full_name') if pick_class else [])
    return render(request, 'curriculum/club.html', {
        'club': c, 'members': members, 'runs': runs, 'roles': ClubMember.ROLES,
        'meetings': meetings, 'classes': classes, 'pick_class': pick_class,
        'candidates': candidates, 'today': timezone.localdate(),
    })
