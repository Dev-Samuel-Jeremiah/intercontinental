"""
timetable/views_extras.py
─────────────────────────
After-school lessons: the staff list, one group (who is in it and who has
paid), and the parent page for signing up and paying from the wallet.
"""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render

from users.models import ENROLLED_STATUSES, Class, Staff, Student, Subject
from users.views_profile import can_manage, can_see_fees, can_view

from . import extras as svc
from .models import DAY_CHOICES, ExtraLessonEnrolment, ExtraLessonGroup, TeachingRoom


def _runs_extras(user):
    return can_manage(user) or can_see_fees(user)


def _pk(raw):
    raw = str(raw or '')
    return int(raw) if raw.isdigit() else 0


def _fill_links(request, group):
    """Teacher, subject, room, term and classes from the form, limited to this school."""
    t = request.tenant
    group.teacher = Staff.objects.filter(tenant=t, pk=_pk(request.POST.get('teacher'))).first()
    group.subject = Subject.objects.filter(tenant=t, pk=_pk(request.POST.get('subject'))).first()
    group.room = TeachingRoom.objects.filter(tenant=t, pk=_pk(request.POST.get('room'))).first()
    from academics.models import Term
    group.term = Term.objects.filter(tenant=t, pk=_pk(request.POST.get('term'))).first() \
        or Term.objects.filter(tenant=t, is_active=True).first()


def _form_ctx(request):
    from academics.models import Term
    t = request.tenant
    return {'days': DAY_CHOICES, 'teachers': Staff.objects.filter(tenant=t, role__in=('TEACHER', 'ADMIN')).order_by('full_name'),
            'subjects': Subject.objects.filter(tenant=t).order_by('name'),
            'rooms': TeachingRoom.objects.filter(tenant=t, active=True),
            'terms': Term.objects.filter(tenant=t).order_by('-id')[:6],
            'all_classes': Class.objects.filter(tenant=t).order_by('name')}


@login_required
def extras(request):
    if not can_view(request.user):
        messages.error(request, 'Only staff can see this page.')
        return redirect('dashboard:router')
    runs = _runs_extras(request.user)
    if request.method == 'POST':
        if not runs:
            messages.error(request, 'Only administrators and the bursary can set up extra lessons.')
            return redirect('timetable:extras')
        group = ExtraLessonGroup(tenant=request.tenant)
        _fill_links(request, group)
        try:
            svc.save_group(group, request.POST)
            group.classes.set(Class.objects.filter(tenant=request.tenant,
                                                   pk__in=[_pk(i) for i in request.POST.getlist('classes')]))
            messages.success(request, f'{group.name} created.')
            return redirect('timetable:extra_detail', pk=group.pk)
        except svc.ExtraError as problem:
            messages.error(request, str(problem))
        return redirect('timetable:extras')
    groups = list(ExtraLessonGroup.objects.filter(tenant=request.tenant).select_related('teacher', 'room', 'subject'))
    for g in groups:
        g.money = svc.totals(g)
    staff = request.user.staff_profiles.filter(tenant=request.tenant).first()
    ctx = {'groups': groups, 'runs': runs, 'mine': [g for g in groups if staff and g.teacher_id == staff.pk],
           'class_ids': []}
    ctx.update(_form_ctx(request))
    return render(request, 'timetable/extras.html', ctx)


@login_required
def extra_detail(request, pk):
    group = get_object_or_404(ExtraLessonGroup.objects.select_related('teacher', 'room', 'subject', 'term'),
                              pk=pk, tenant=request.tenant)
    staff = request.user.staff_profiles.filter(tenant=request.tenant).first()
    runs = _runs_extras(request.user)
    if not (runs or (staff and group.teacher_id == staff.pk)):
        messages.error(request, 'Only the group\'s teacher, administrators and the bursary can open this.')
        return redirect('dashboard:router')
    if request.method == 'POST' and runs:
        action = request.POST.get('action')
        try:
            if action == 'enrol':
                added = 0
                for s in Student.objects.filter(tenant=request.tenant, status__in=ENROLLED_STATUSES,
                                                pk__in=[_pk(i) for i in request.POST.getlist('students')]):
                    try:
                        svc.enrol(group, s, request.user)
                        added += 1
                    except svc.ExtraError as problem:
                        messages.error(request, str(problem))
                if added:
                    messages.success(request, f'{added} student{"s" if added != 1 else ""} added.')
            elif action in ('paid', 'waive', 'withdraw'):
                e = get_object_or_404(ExtraLessonEnrolment, pk=_pk(request.POST.get('id')), group=group)
                if action == 'paid':
                    svc.mark_paid(e, request.POST.get('note'))
                    messages.success(request, f'{e.student.full_name} marked as paid.')
                elif action == 'waive':
                    svc.waive(e, request.POST.get('note'))
                    messages.success(request, f'Fee waived for {e.student.full_name}.')
                else:
                    name = e.student.full_name
                    refunded = svc.withdraw(e, request.user)
                    messages.success(request, f'{name} withdrawn' + (f'; ₦{refunded.amount:,.2f} went back to the family wallet.'
                                                                     if refunded else '.'))
            elif action == 'edit':
                _fill_links(request, group)
                svc.save_group(group, request.POST)
                group.classes.set(Class.objects.filter(tenant=request.tenant,
                                                       pk__in=[_pk(i) for i in request.POST.getlist('classes')]))
                messages.success(request, 'Saved. The new fee applies to children who join from now on.')
            elif action == 'toggle':
                group.active = not group.active
                group.save(update_fields=['active'])
                messages.success(request, 'Group reopened.' if group.active else 'Group closed — nobody new can join.')
        except svc.ExtraError as problem:
            messages.error(request, str(problem))
        return redirect('timetable:extra_detail', pk=group.pk)
    members = list(group.enrolments.select_related('student__class_assigned'))
    class_ids = list(group.classes.values_list('pk', flat=True))
    candidates = Student.objects.filter(tenant=request.tenant, status__in=ENROLLED_STATUSES) \
        .exclude(pk__in=[m.student_id for m in members]).select_related('class_assigned').order_by(
            'class_assigned__name', 'full_name')
    if class_ids:
        candidates = candidates.filter(class_assigned_id__in=class_ids)
    ctx = {'g': group, 'members': members, 'money': svc.totals(group), 'runs': runs,
           'candidates': candidates if runs else [], 'clashes': svc.clashes(group),
           'class_ids': class_ids, 'left': svc.places_left(group)}
    ctx.update(_form_ctx(request))
    return render(request, 'timetable/extra_detail.html', ctx)


@login_required
def family_extras(request):
    from users.views_achievements import _parent_children
    if not getattr(request.user, 'is_parent', False):
        messages.error(request, 'This page is for parents.')
        return redirect('dashboard:router')
    children = [c for c in _parent_children(request) if c.class_assigned_id and c.status in ENROLLED_STATUSES]
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action == 'join':
                group = get_object_or_404(ExtraLessonGroup, pk=_pk(request.POST.get('group')), tenant=request.tenant,
                                          open_to_parents=True)
                child = next((c for c in children if c.id == _pk(request.POST.get('child'))), None)
                if not child:
                    raise svc.ExtraError('Choose one of your children.')
                e = svc.enrol(group, child, request.user)
                messages.success(request, f'{child.full_name} joined {group.name}.' +
                                 (f' Fee: ₦{e.fee:,.2f}.' if e.status == ExtraLessonEnrolment.DUE else ''))
            else:
                e = get_object_or_404(ExtraLessonEnrolment, pk=_pk(request.POST.get('id')),
                                      student__in=children, tenant=request.tenant)
                if action == 'pay':
                    svc.pay_from_wallet(e, request.user)
                    messages.success(request, f'Paid ₦{e.fee:,.2f} for {e.group.name} from your wallet.')
                elif action == 'leave':
                    if e.status != ExtraLessonEnrolment.DUE:
                        raise svc.ExtraError('Please ask the school to take your child out of a paid or free group.')
                    svc.withdraw(e, request.user, refund=False)
                    messages.success(request, 'Done — your child has left the group.')
        except svc.ExtraError as problem:
            messages.error(request, str(problem))
        return redirect('timetable:family_extras')
    mine = list(ExtraLessonEnrolment.objects.filter(student__in=children).select_related('group__teacher',
                                                                                          'group__room', 'student'))
    joined = {(m.group_id, m.student_id) for m in mine}
    offers = []
    for g in ExtraLessonGroup.objects.filter(tenant=request.tenant, active=True, open_to_parents=True) \
            .select_related('teacher', 'room'):
        kids = [c for c in children if svc.eligible(g, c) and (g.id, c.id) not in joined]
        if kids:
            offers.append({'g': g, 'kids': kids, 'left': svc.places_left(g)})
    wallet = svc.family_wallet(children[0]) if children else None
    return render(request, 'parents/extras.html', {
        'mine': mine, 'offers': offers, 'wallet': wallet, 'children': children,
        'due': sum(m.fee for m in mine if m.status == ExtraLessonEnrolment.DUE)})
