"""
users/views_achievements.py
───────────────────────────
Awards, leadership roles, support needs, parents' correction requests, and
the "everything we hold about this child" export.
"""
from datetime import date

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from academics.utils import get_active_session, get_active_term

from . import change_requests, data_export
from .models import (ENROLLED_STATUSES, Award, LeadershipRole, ProfileChangeRequest, Student,
                     SupportNeeds)
from .views_profile import _date, can_manage, can_view


def _back(student, tab):
    return redirect(reverse('users:student_profile', args=[student.id]) + f'?tab={tab}#{tab}')


def _deny(request, what, student=None):
    messages.error(request, f'Only a school administrator can {what}.')
    return _back(student, 'awards') if student else redirect('dashboard:router')


# ─────────────────────────────────────────────────────────────────────────────
# Awards and roles
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def student_awards(request, student_id):
    student = get_object_or_404(Student, pk=student_id)
    if request.method != 'POST':
        return _back(student, 'awards')
    if not can_view(request.user):
        return _deny(request, 'record awards', student)
    action = request.POST.get('action')
    if action == 'add':
        title = (request.POST.get('title') or '').strip()[:160]
        if not title:
            messages.error(request, 'Give the award a title.')
            return _back(student, 'awards')
        cat, lvl = request.POST.get('category'), request.POST.get('level')
        awarded_on = _date(request.POST.get('awarded_on')) or timezone.localdate()
        if awarded_on > timezone.localdate():
            messages.error(request, 'An award cannot be dated in the future.')
            return _back(student, 'awards')
        term = get_active_term()
        Award.objects.create(
            tenant_id=student.tenant_id, student=student, title=title,
            category=cat if cat in dict(Award.CATEGORIES) else 'OTHER',
            level=lvl if lvl in dict(Award.LEVELS) else 'SCHOOL',
            position=(request.POST.get('position') or '').strip()[:40],
            awarded_on=awarded_on, session=get_active_session(),
            term=term if term and term.start_date and term.start_date <= awarded_on <=
            (term.end_date or timezone.localdate()) else None,
            description=(request.POST.get('description') or '').strip()[:255],
            show_on_report=bool(request.POST.get('show_on_report')), recorded_by=request.user)
        messages.success(request, f'Award recorded: {title}.')
    elif action == 'delete':
        award = get_object_or_404(Award, pk=request.POST.get('award_id'), student=student)
        if not (can_manage(request.user) or award.recorded_by_id == request.user.id):
            return _deny(request, "remove someone else's award", student)
        award.delete()
        messages.success(request, 'Award removed.')
    return _back(student, 'awards')


@login_required
def student_roles(request, student_id):
    student = get_object_or_404(Student, pk=student_id)
    if request.method != 'POST':
        return _back(student, 'awards')
    if not can_manage(request.user):
        return _deny(request, 'appoint student leaders', student)
    action = request.POST.get('action')
    if action == 'add':
        title = (request.POST.get('title') or '').strip()[:100]
        start = _date(request.POST.get('start_date')) or timezone.localdate()
        end = _date(request.POST.get('end_date'))
        if not title:
            messages.error(request, 'Give the role a title, e.g. Head Girl.')
        elif end and end < start:
            messages.error(request, 'The role cannot end before it starts.')
        else:
            kind = request.POST.get('kind')
            LeadershipRole.objects.create(
                tenant_id=student.tenant_id, student=student, title=title,
                kind=kind if kind in dict(LeadershipRole.KINDS) else 'SCHOOL',
                group=(request.POST.get('group') or '').strip()[:100], start_date=start,
                end_date=end, session=get_active_session(),
                note=(request.POST.get('note') or '').strip()[:255], appointed_by=request.user)
            messages.success(request, f'{student.full_name} appointed {title}.')
    elif action == 'end':
        role = get_object_or_404(LeadershipRole, pk=request.POST.get('role_id'), student=student)
        role.end_date = timezone.localdate()
        role.save(update_fields=['end_date'])
        messages.success(request, f'{role.title} ended today.')
    elif action == 'delete':
        get_object_or_404(LeadershipRole, pk=request.POST.get('role_id'), student=student).delete()
        messages.success(request, 'Role removed.')
    return _back(student, 'awards')


@login_required
def achievements(request):
    """School-wide: who won what, and who holds which role now."""
    if not can_view(request.user):
        messages.error(request, 'Only staff can see the awards register.')
        return redirect('dashboard:router')
    awards = Award.objects.select_related('student', 'student__class_assigned')
    cat, q = request.GET.get('category') or '', (request.GET.get('q') or '').strip()
    if cat in dict(Award.CATEGORIES):
        awards = awards.filter(category=cat)
    if q:
        awards = awards.filter(Q(title__icontains=q) | Q(student__full_name__icontains=q))
    today = timezone.localdate()
    leaders = (LeadershipRole.objects.filter(Q(end_date__isnull=True) | Q(end_date__gte=today),
                                             student__status__in=ENROLLED_STATUSES)
               .select_related('student', 'student__class_assigned').order_by('kind', 'title'))
    return render(request, 'students/achievements.html', {
        'awards': awards[:300], 'categories': Award.CATEGORIES, 'category': cat, 'q': q,
        'leaders': leaders,
        'by_category': dict(Award.objects.values_list('category').annotate(n=Count('id'))),
    })


# ─────────────────────────────────────────────────────────────────────────────
# Support needs
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def student_support(request, student_id):
    student = get_object_or_404(Student, pk=student_id)
    if request.method != 'POST' or not can_manage(request.user):
        if request.method == 'POST':
            messages.error(request, 'Only a school administrator can change support needs.')
        return _back(student, 'support')
    support = SupportNeeds.objects.filter(student=student).first() or \
        SupportNeeds(tenant_id=student.tenant_id, student=student)
    try:
        extra = int(request.POST.get('extra_time_percent') or 0)
    except ValueError:
        extra = 0
    support.extra_time_percent = extra if extra in dict(SupportNeeds.EXTRA_TIME) else 0
    for flag in ('large_print', 'reader', 'scribe', 'rest_breaks', 'seat_near_front',
                 'separate_room'):
        setattr(support, flag, bool(request.POST.get(flag)))
    support.classroom_notes = (request.POST.get('classroom_notes') or '').strip()[:255]
    support.private_notes = (request.POST.get('private_notes') or '').strip()[:4000]
    support.review_on = _date(request.POST.get('review_on'))
    support.updated_by = request.user
    support.save()
    messages.success(request, 'Support needs saved.' + (
        f' CBT exams will give {student.full_name} {support.extra_time_percent}% extra time.'
        if support.extra_time_percent else ''))
    return _back(student, 'support')


@login_required
def support_register(request):
    """Every student on the roll with adjustments — for exam officers and teachers."""
    if not can_view(request.user):
        messages.error(request, 'Only staff can see support needs.')
        return redirect('dashboard:router')
    rows = (SupportNeeds.objects.filter(student__status__in=ENROLLED_STATUSES)
            .select_related('student', 'student__class_assigned').order_by('student__class_assigned__name',
                                                                           'student__full_name'))
    rows = [r for r in rows if r.adjustments or r.classroom_notes]
    return render(request, 'students/support_register.html', {
        'rows': rows, 'can_manage': can_manage(request.user), 'today': timezone.localdate()})


# ─────────────────────────────────────────────────────────────────────────────
# Parents' correction requests
# ─────────────────────────────────────────────────────────────────────────────

def _parent_children(request):
    from finance.views import _get_all_children
    if not getattr(request.user, 'is_parent', False):
        return Student.objects.none()
    return _get_all_children(request)


@login_required
def parent_corrections(request):
    children = list(_parent_children(request))
    if not children:
        messages.error(request, 'This page is for parents.')
        return redirect('dashboard:router')
    if request.method == 'POST':
        child = next((c for c in children if str(c.id) == request.POST.get('child_id')), None)
        if not child:
            messages.error(request, 'Choose one of your children.')
        else:
            try:
                change_requests.submit(child, request.POST.get('field'), request.POST.get('new_value'),
                                       request.user, request.POST.get('note'))
                messages.success(request, 'Thank you — the school will check and update it.')
            except change_requests.RequestError as problem:
                messages.error(request, str(problem))
        return redirect('users:parent_corrections')
    return render(request, 'parents/corrections.html', {
        'children': children, 'fields': ProfileChangeRequest.FIELDS,
        'requests': ProfileChangeRequest.objects.filter(student__in=children)
        .select_related('student')[:30],
    })


@login_required
def change_request_queue(request):
    if not can_manage(request.user):
        messages.error(request, 'Only a school administrator can review corrections.')
        return redirect('dashboard:router')
    if request.method == 'POST':
        req = get_object_or_404(ProfileChangeRequest, pk=request.POST.get('request_id'))
        try:
            change_requests.review(req, request.POST.get('decision') == 'approve', request.user,
                                   request.POST.get('review_note'))
            messages.success(request, ('Applied to ' if req.status == req.APPROVED else 'Declined for ')
                             + req.student.full_name + '.')
        except (change_requests.RequestError, ValueError) as problem:
            messages.error(request, str(problem))
        return redirect('users:change_requests')
    status = request.GET.get('status') or ProfileChangeRequest.PENDING
    rows = ProfileChangeRequest.objects.select_related('student', 'requested_by', 'reviewed_by')
    if status in dict(ProfileChangeRequest.STATUSES):
        rows = rows.filter(status=status)
    return render(request, 'students/change_requests.html', {
        'rows': rows[:200], 'status': status, 'statuses': ProfileChangeRequest.STATUSES,
        'waiting': ProfileChangeRequest.objects.filter(status=ProfileChangeRequest.PENDING).count(),
    })


# ─────────────────────────────────────────────────────────────────────────────
# Data export
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def student_export(request, student_id):
    student = get_object_or_404(Student, pk=student_id)
    if request.method != 'POST' or not can_manage(request.user):
        if request.method == 'POST':
            messages.error(request, "Only a school administrator can export a child's records.")
        return _back(student, 'status')
    reason = (request.POST.get('reason') or '').strip()
    if len(reason) < 5:
        messages.error(request, 'Say why the records are being exported — it is kept on file.')
        return _back(student, 'status')
    name, content = data_export.build(student, request.user, reason)
    response = HttpResponse(content, content_type='application/zip')
    response['Content-Disposition'] = f'attachment; filename="{name}"'
    response['Cache-Control'] = 'private, no-store'
    return response
