"""
classroom/views_extra.py
────────────────────────
Course announcements, live classes with attendance, and copying a course.
"""
from datetime import datetime

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from users.models import Class

from . import services as svc
from .access import enrolled, require_enrolled, require_teacher, teaches
from .models import Course, CourseAnnouncement, LiveClass


def _when(raw):
    try:
        return timezone.make_aware(datetime.fromisoformat(raw)) if raw else None
    except ValueError:
        return None


@login_required
def course_hub(request, course_id):
    """Announcements and live classes for a course — teachers manage, students read and join."""
    course = get_object_or_404(Course.objects.select_related('class_assigned'), pk=course_id)
    is_teacher = teaches(request.user, course)
    if not (is_teacher or enrolled(request.user, course)):
        messages.error(request, "You aren't in this course.")
        return redirect('dashboard:router')
    if request.method == 'POST':
        require_teacher(request.user, course)
        action = request.POST.get('action')
        try:
            if action == 'announce':
                svc.announce(course, request.user, request.POST.get('title'), request.POST.get('body'),
                             request.POST.get('pinned'), request.POST.get('email_parents'))
                messages.success(request, 'Announcement posted.')
            elif action == 'delete_announcement':
                get_object_or_404(CourseAnnouncement, pk=request.POST.get('id'), course=course).delete()
                messages.success(request, 'Announcement removed.')
            elif action == 'live':
                svc.schedule_live(course, request.user, request.POST.get('title'), _when(request.POST.get('starts_at')),
                                  request.POST.get('minutes'), request.POST.get('platform'), request.POST.get('url'))
                messages.success(request, 'Live class scheduled. Students see a Join button from 15 minutes before.')
            elif action == 'recording':
                live = get_object_or_404(LiveClass, pk=request.POST.get('id'), course=course)
                url = (request.POST.get('recording_url') or '').strip()
                if url and not url.startswith('https://'):
                    raise svc.ClassroomError('Paste the full recording link (starting https://).')
                live.recording_url = url
                live.save(update_fields=['recording_url'])
                messages.success(request, 'Recording link saved.')
            elif action == 'delete_live':
                get_object_or_404(LiveClass, pk=request.POST.get('id'), course=course).delete()
                messages.success(request, 'Live class removed.')
        except (svc.ClassroomError, ValueError) as problem:
            messages.error(request, str(problem))
        return redirect('classroom:course_hub', course.id)
    now = timezone.now()
    lives = list(course.live_classes.prefetch_related('attendance'))
    for l in lives:
        l.open, l.why = svc.join_window(l, now)
        l.attended = l.attendance.count()
    return render(request, 'classroom/course_hub.html', {
        'course': course, 'is_teacher': is_teacher, 'announcements': course.announcements.select_related('author'),
        'upcoming': [l for l in lives if l.ends_at >= now][::-1], 'past': [l for l in lives if l.ends_at < now],
        'class_size': svc.students_of(course).count() if is_teacher else None,
        'sections': course.sections.all().order_by('order') if is_teacher else [],
    })


@login_required
def join_live(request, live_id):
    live = get_object_or_404(LiveClass.objects.select_related('course'), pk=live_id)
    if teaches(request.user, live.course):
        return redirect(live.join_url)
    require_enrolled(request.user, live.course)
    try:
        return redirect(svc.join(live, request.user))
    except svc.ClassroomError as problem:
        messages.error(request, str(problem))
        return redirect('classroom:course_hub', live.course_id)


@login_required
def live_attendance(request, live_id):
    live = get_object_or_404(LiveClass.objects.select_related('course'), pk=live_id)
    require_teacher(request.user, live.course)
    rows, present = svc.attendance(live)
    return render(request, 'classroom/live_attendance.html', {'live': live, 'rows': rows, 'present': present})


@login_required
def clone_course(request, course_id):
    course = get_object_or_404(Course, pk=course_id)
    require_teacher(request.user, course)
    classes = Class.objects.filter(tenant=request.tenant).order_by('name')
    if request.method == 'POST':
        try:
            new = svc.clone(course, request.user, request.POST.get('name'),
                            classes.filter(pk=request.POST.get('class') or None).first(),
                            int(request.POST.get('shift_days') or 0))
            messages.success(request, f'"{new.name}" created from "{course.name}". Check the assignment due dates.')
            return redirect('classroom:teacher_course_detail', new.id)
        except (svc.ClassroomError, ValueError) as problem:
            messages.error(request, str(problem))
    return render(request, 'classroom/clone_course.html', {'course': course, 'classes': classes})
