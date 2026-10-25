"""
teaching/views.py
─────────────────
Teacher productivity pages.
"""
import json
import os
from datetime import datetime

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from users.models import Class, Staff, Student, Subject

from . import services as svc
from .models import (CATEGORIES, BehaviourEntry, CommentTemplate, DutyAssignment, DutyPost, GradeItem, HandoverNote,
                     Resource, TeacherTask)


def _deny(request, text='This page is for teachers.'):
    messages.error(request, text)
    return redirect('dashboard:router')


def _date(raw):
    try:
        return datetime.strptime(raw or '', '%Y-%m-%d').date()
    except ValueError:
        return None


def _time(raw):
    try:
        return datetime.strptime(raw or '', '%H:%M').time()
    except ValueError:
        return None


def _staff_only(view):
    def wrap(request, *a, **kw):
        me = Staff.objects.filter(user=request.user, tenant=request.tenant).first()
        if not me:
            return _deny(request)
        request.me = me
        request.admin = svc.is_admin(request.user)
        return view(request, *a, **kw)
    wrap.__name__ = view.__name__
    return login_required(wrap)


def _classes(request):
    if request.admin:
        return list(Class.objects.filter(tenant=request.tenant).order_by('name'))
    return svc.my_classes(request.me)


def _serve(field, name='file'):
    if not field:
        raise Http404
    from users.records import CONTENT_TYPES
    ext = os.path.splitext(field.name)[1].lower()
    try:
        return FileResponse(field.open('rb'), content_type=CONTENT_TYPES.get(ext, 'application/octet-stream'),
                            filename=f'{name}{ext}')
    except (FileNotFoundError, OSError):
        raise Http404


# ─────────────────────────────────────────────────────────────────────────────
# Home: marking queue (F307) + tasks summary
# ─────────────────────────────────────────────────────────────────────────────

@_staff_only
def home(request):
    q = svc.marking_queue(request.me, request.user, request.tenant)
    duties = DutyAssignment.objects.filter(staff=request.me, date__gte=timezone.localdate()).select_related('post')[:5]
    handovers = HandoverNote.objects.filter(to_staff=request.me, acknowledged_at__isnull=True).select_related(
        'from_staff')
    return render(request, 'teaching/home.html', {'q': q, 'duties': duties, 'handovers': handovers,
                                                  'classes': _classes(request)})


# ─────────────────────────────────────────────────────────────────────────────
# F303 — class teacher dashboard
# ─────────────────────────────────────────────────────────────────────────────

@_staff_only
def class_dashboard(request, class_id=None):
    classes = Class.objects.filter(tenant=request.tenant, form_teachers=request.me) if not request.admin else \
        Class.objects.filter(tenant=request.tenant)
    cls = get_object_or_404(classes, pk=class_id) if class_id else classes.order_by('name').first()
    if not cls:
        return _deny(request, 'You are not the form teacher of any class.')
    return render(request, 'teaching/class_dashboard.html', {'d': svc.class_dashboard(cls),
                                                             'classes': classes.order_by('name')})


# ─────────────────────────────────────────────────────────────────────────────
# F304 — gradebook
# ─────────────────────────────────────────────────────────────────────────────

def _teaching_pairs(request):
    from users.models import StaffSubjectClass
    qs = StaffSubjectClass.objects.filter(tenant=request.tenant).select_related('school_class', 'subject')
    if not request.admin:
        qs = qs.filter(staff=request.me)
    return qs.order_by('school_class__name', 'subject__name')


@_staff_only
def gradebooks(request):
    term = svc.current_term(request.tenant)
    if request.method == 'POST':
        if not term:
            messages.error(request, 'Set up the academic session and term first.')
            return redirect('teaching:gradebooks')
        cls = get_object_or_404(Class, pk=request.POST.get('school_class'), tenant=request.tenant)
        subject = get_object_or_404(Subject, pk=request.POST.get('subject'), tenant=request.tenant)
        if not (request.admin or svc.teaches(request.me, cls, subject)):
            return _deny(request, 'You do not teach that subject in that class.')
        gb = svc.gradebook_for(cls, subject, term, request.me)
        return redirect('teaching:gradebook', pk=gb.pk)
    return render(request, 'teaching/gradebooks.html', {'pairs': _teaching_pairs(request), 'term': term})


@_staff_only
def gradebook(request, pk):
    from .models import Gradebook
    gb = get_object_or_404(Gradebook.objects.select_related('school_class', 'subject', 'term__session'), pk=pk,
                           tenant=request.tenant)
    if not (request.admin or svc.teaches(request.me, gb.school_class, gb.subject)):
        return _deny(request, 'You do not teach that subject in that class.')
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action == 'item':
                it = svc.add_item(gb, request.POST.get('title'), request.POST.get('category'),
                                  request.POST.get('max_score'), _date(request.POST.get('date')))
                return redirect('teaching:grade_item', pk=it.pk)
            elif action == 'weights':
                svc.set_weights(gb, request.POST)
                messages.success(request, 'Weights saved.')
            elif action == 'push':
                n = svc.push_to_report(gb)
                messages.success(request, f'Sent to the report card for {n} student{"s" if n != 1 else ""}. '
                                          'Published results were left alone.')
        except svc.TeachError as problem:
            messages.error(request, str(problem))
        return redirect('teaching:gradebook', pk=gb.pk)
    items, rows = svc.standings(gb)
    if request.GET.get('export') in ('csv', 'xlsx'):
        from analytics.export import respond
        head = ['Student'] + [f'{i.title} (/{i.max_score})' for i in items] + \
            [label for k, label in CATEGORIES if k in gb.weights] + ['Overall %']
        data = [[r['student'].full_name] + [('ex' if m and m.excused else (float(m.score) if m and m.score is not None
                                                                        else '')) for m in r['marks']] +
                [r['cats'][k] if r['cats'][k] is not None else '' for k, _ in CATEGORIES if k in gb.weights] +
                [r['overall'] if r['overall'] is not None else ''] for r in rows]
        return respond(request.GET['export'], f'{gb.school_class.name} {gb.subject.name}', head, data,
                       request.tenant.name)
    from results.models import AssessmentScheme
    return render(request, 'teaching/gradebook.html', {
        'gb': gb, 'items': items, 'rows': rows, 'categories': CATEGORIES,
        'used': [(k, label) for k, label in CATEGORIES if k in gb.weights],
        'ca_columns': AssessmentScheme.for_tenant(request.tenant).ca_columns, 'today': timezone.localdate()})


@_staff_only
def grade_item(request, pk):
    it = get_object_or_404(GradeItem.objects.select_related('gradebook__school_class', 'gradebook__subject'), pk=pk,
                           tenant=request.tenant)
    gb = it.gradebook
    if not (request.admin or svc.teaches(request.me, gb.school_class, gb.subject)):
        return _deny(request, 'You do not teach that subject in that class.')
    if request.method == 'POST':
        if request.POST.get('action') == 'delete':
            it.delete()
            return redirect('teaching:gradebook', pk=gb.pk)
        raw = {k[2:]: v for k, v in request.POST.items() if k.startswith('m_') and k[2:].isdigit()}
        problems = svc.save_marks(it, raw)
        if problems:
            messages.error(request, f'Check the marks for: {", ".join(problems)} (0 to {it.max_score}, or x if '
                                    f'excused).')
        else:
            messages.success(request, 'Marks saved.')
        return redirect('teaching:gradebook', pk=gb.pk) if not problems else redirect('teaching:grade_item', pk=it.pk)
    marks = {m.student_id: m for m in it.marks.all()}
    rows = [(s, marks.get(s.pk)) for s in svc.class_students(gb.school_class)]
    return render(request, 'teaching/grade_item.html', {'it': it, 'gb': gb, 'rows': rows})


# ─────────────────────────────────────────────────────────────────────────────
# F305 — behaviour points
# ─────────────────────────────────────────────────────────────────────────────

@_staff_only
def behaviour(request):
    classes = _classes(request)
    cls = next((c for c in classes if str(c.pk) == (request.POST.get('class_id') or request.GET.get('class'))),
               classes[0] if classes else None)
    if request.method == 'POST' and cls:
        students = list(Student.objects.filter(class_assigned=cls, pk__in=request.POST.getlist('students')))
        try:
            n = len(svc.give_points(request.me, students, request.POST.get('category'), request.POST.get('note')))
            messages.success(request, f'Recorded for {n} student{"s" if n != 1 else ""}.')
        except svc.TeachError as problem:
            messages.error(request, str(problem))
        return redirect(f'{request.path}?class={cls.pk}')
    students = svc.class_students(cls) if cls else []
    return render(request, 'teaching/behaviour.html', {
        'classes': classes, 'cls': cls, 'summary': svc.behaviour_summary(students) if students else [],
        'positive': BehaviourEntry.POSITIVE, 'negative': BehaviourEntry.NEGATIVE,
        'recent': BehaviourEntry.objects.filter(student__in=students).select_related('student', 'staff')[:25]})


# ─────────────────────────────────────────────────────────────────────────────
# F308 — comment bank
# ─────────────────────────────────────────────────────────────────────────────

@_staff_only
def comments(request):
    svc.ensure_comments(request.tenant)
    if request.method == 'POST':
        a = request.POST.get('action')
        if a == 'add' and (request.POST.get('text') or '').strip():
            shared = request.admin and bool(request.POST.get('shared'))
            CommentTemplate.objects.create(tenant=request.tenant, staff=None if shared else request.me,
                                           band=request.POST.get('band') if request.POST.get('band') in
                                           dict(CommentTemplate.BANDS) else 'GOOD',
                                           text=request.POST['text'].strip()[:255])
        elif a == 'delete':
            q = CommentTemplate.objects.filter(tenant=request.tenant, pk=request.POST.get('id'))
            if not request.admin:
                q = q.filter(staff=request.me)
            q.delete()
        return redirect('teaching:comments')
    return render(request, 'teaching/comments.html', {
        'items': svc.comments_for(request.me), 'bands': CommentTemplate.BANDS, 'pairs': _teaching_pairs(request),
        'form_classes': Class.objects.filter(tenant=request.tenant, form_teachers=request.me) if not request.admin
        else Class.objects.filter(tenant=request.tenant)})


@_staff_only
def remarks(request):
    term = svc.current_term(request.tenant)
    if not term:
        return _deny(request, 'Set up the academic session and term first.')
    cls = get_object_or_404(Class, pk=request.GET.get('class') or request.POST.get('class'), tenant=request.tenant)
    sid = request.GET.get('subject') or request.POST.get('subject')
    subject = Subject.objects.filter(tenant=request.tenant, pk=sid).first() if sid else None
    if request.method == 'POST':
        try:
            n = svc.apply_remarks(request.me, cls, subject, term, request.POST, admin=request.admin)
            messages.success(request, f'Saved {n} remark{"s" if n != 1 else ""} on the report cards.')
        except svc.TeachError as problem:
            messages.error(request, str(problem))
        return redirect(request.get_full_path())
    if subject and not (request.admin or svc.teaches(request.me, cls, subject)):
        return _deny(request, 'You do not teach that subject in that class.')
    if not subject and not (request.admin or cls.has_form_teacher(request.me)):
        return _deny(request, "Only the form teacher writes the class teacher's remark.")
    svc.ensure_comments(request.tenant)
    bank = list(svc.comments_for(request.me))
    return render(request, 'teaching/remarks.html', {
        'cls': cls, 'subject': subject, 'term': term, 'rows': svc.remarks_sheet(cls, subject, term),
        'bank': bank, 'bank_json': json.dumps([{'id': c.pk, 'band': c.band, 'text': c.text} for c in bank]),
        'bands': CommentTemplate.BANDS})


# ─────────────────────────────────────────────────────────────────────────────
# F309 — resource library
# ─────────────────────────────────────────────────────────────────────────────

@_staff_only
def resources(request):
    if request.method == 'POST':
        a = request.POST.get('action')
        try:
            if a == 'add':
                svc.add_resource(request.me, request.POST, request.FILES.get('file'))
                messages.success(request, 'Added to the library.')
            else:
                r = get_object_or_404(Resource, pk=request.POST.get('id'), tenant=request.tenant)
                if a == 'save':
                    if r.saved_by.filter(pk=request.me.pk).exists():
                        r.saved_by.remove(request.me)
                    else:
                        r.saved_by.add(request.me)
                elif a == 'delete' and (r.uploaded_by_id == request.me.pk or request.admin):
                    r.delete()
        except svc.TeachError as problem:
            messages.error(request, str(problem))
        return redirect(request.get_full_path())
    subject = Subject.objects.filter(tenant=request.tenant, pk=request.GET.get('subject') or 0).first()
    items = svc.find_resources(request.me, (request.GET.get('q') or '').strip(), subject,
                               (request.GET.get('level') or '').strip(), request.GET.get('kind') or '',
                               saved=bool(request.GET.get('saved')))
    saved = set(request.me.saved_resources.values_list('pk', flat=True))
    return render(request, 'teaching/resources.html', {
        'items': items[:100], 'saved': saved, 'subjects': Subject.objects.filter(tenant=request.tenant).order_by('name'),
        'kinds': Resource.KINDS, 'q': request.GET.get('q', ''), 'subject': subject,
        'level': request.GET.get('level', ''), 'kind': request.GET.get('kind', ''),
        'levels': sorted({c.level for c in Class.objects.filter(tenant=request.tenant) if c.level})})


@_staff_only
def resource_file(request, pk):
    r = get_object_or_404(Resource, pk=pk, tenant=request.tenant)
    if not (r.shared or r.uploaded_by_id == request.me.pk):
        raise Http404
    Resource.objects.filter(pk=r.pk).update(downloads=r.downloads + 1)
    return _serve(r.file, 'resource')


# ─────────────────────────────────────────────────────────────────────────────
# F310 — name picker and timer (no data stored)
# ─────────────────────────────────────────────────────────────────────────────

@_staff_only
def picker(request):
    classes = _classes(request)
    cls = next((c for c in classes if str(c.pk) == request.GET.get('class')), classes[0] if classes else None)
    names = [s.full_name for s in svc.class_students(cls)] if cls else []
    return render(request, 'teaching/picker.html', {'classes': classes, 'cls': cls, 'names_json': json.dumps(names),
                                                    'count': len(names)})


# ─────────────────────────────────────────────────────────────────────────────
# F311 — tasks
# ─────────────────────────────────────────────────────────────────────────────

@_staff_only
def tasks(request):
    if request.method == 'POST':
        a = request.POST.get('action')
        try:
            if a == 'add':
                target = request.me
                if request.admin and request.POST.get('staff'):
                    target = get_object_or_404(Staff, pk=request.POST.get('staff'), tenant=request.tenant)
                svc.add_task(target, request.POST.get('title'), _date(request.POST.get('due_date')),
                             request.POST.get('priority'), request.POST.get('notes'),
                             assigned_by=request.user if target != request.me else None)
            else:
                t = get_object_or_404(TeacherTask, pk=request.POST.get('id'), staff=request.me)
                if a == 'toggle':
                    svc.toggle_task(t)
                elif a == 'delete' and not t.assigned_by_id:
                    t.delete()
        except svc.TeachError as problem:
            messages.error(request, str(problem))
        return redirect('teaching:tasks')
    assigned = TeacherTask.objects.filter(tenant=request.tenant, assigned_by=request.user).exclude(
        staff=request.me).select_related('staff')[:30] if request.admin else []
    return render(request, 'teaching/tasks.html', {
        'items': TeacherTask.objects.filter(staff=request.me).select_related('assigned_by'),
        'staff': Staff.objects.filter(tenant=request.tenant, user__is_active=True).order_by('full_name')
        if request.admin else [], 'assigned': assigned, 'today': timezone.localdate()})


# ─────────────────────────────────────────────────────────────────────────────
# F312 — duty roster
# ─────────────────────────────────────────────────────────────────────────────

@_staff_only
def duties(request):
    if request.method == 'POST' and request.admin:
        a = request.POST.get('action')
        try:
            if a == 'post':
                start, end = _time(request.POST.get('start_time')), _time(request.POST.get('end_time'))
                if not ((request.POST.get('name') or '').strip() and start and end and end > start):
                    raise svc.TeachError('Give the post a name and a start and end time.')
                need = str(request.POST.get('staff_needed') or '1')
                DutyPost.objects.create(tenant=request.tenant, name=request.POST['name'].strip()[:100],
                                        location=(request.POST.get('location') or '')[:100], start_time=start,
                                        end_time=end, staff_needed=int(need) if need.isdigit() and int(need) else 1)
            elif a == 'post_toggle':
                p = get_object_or_404(DutyPost, pk=request.POST.get('id'), tenant=request.tenant)
                p.active = not p.active
                p.save(update_fields=['active'])
            elif a == 'generate':
                staff = Staff.objects.filter(tenant=request.tenant, pk__in=request.POST.getlist('staff'))
                n = svc.generate_rota(request.tenant, _date(request.POST.get('start')), _date(request.POST.get('end')),
                                      staff)
                messages.success(request, f'Added {n} duties to the rota.')
            elif a == 'swap':
                d = get_object_or_404(DutyAssignment, pk=request.POST.get('id'), tenant=request.tenant)
                d.staff = get_object_or_404(Staff, pk=request.POST.get('staff'), tenant=request.tenant)
                d.reminded = False
                d.save(update_fields=['staff', 'reminded'])
            elif a == 'remove':
                DutyAssignment.objects.filter(tenant=request.tenant, pk=request.POST.get('id')).delete()
        except svc.TeachError as problem:
            messages.error(request, str(problem))
        return redirect('teaching:duties')
    today = timezone.localdate()
    rota = DutyAssignment.objects.filter(tenant=request.tenant, date__gte=today).select_related('post', 'staff')[:200]
    if request.GET.get('export') in ('csv', 'xlsx') and request.admin:
        from analytics.export import respond
        return respond(request.GET['export'], 'Duty rota', ['Date', 'Duty', 'Time', 'Staff'],
                       [[a.date.isoformat(), a.post.name, f'{a.post.start_time:%-I:%M %p}-{a.post.end_time:%-I:%M %p}',
                         a.staff.full_name] for a in rota], request.tenant.name)
    return render(request, 'teaching/duties.html', {
        'mine': DutyAssignment.objects.filter(staff=request.me, date__gte=today).select_related('post')[:20],
        'rota': rota, 'posts': DutyPost.objects.filter(tenant=request.tenant), 'admin': request.admin,
        'staff': Staff.objects.filter(tenant=request.tenant, user__is_active=True).order_by('full_name'),
        'today': today})


# ─────────────────────────────────────────────────────────────────────────────
# F313 — handover notes
# ─────────────────────────────────────────────────────────────────────────────

@_staff_only
def handovers(request):
    if request.method == 'POST':
        try:
            h = svc.create_handover(request.me, request.POST, request.FILES.get('file'))
            messages.success(request, f'Handover sent to {h.to_staff.full_name}.')
        except svc.TeachError as problem:
            messages.error(request, str(problem))
        return redirect('teaching:handovers')
    return render(request, 'teaching/handovers.html', {
        'received': HandoverNote.objects.filter(to_staff=request.me).select_related('from_staff', 'school_class',
                                                                                   'subject'),
        'given': HandoverNote.objects.filter(from_staff=request.me).select_related('to_staff', 'school_class'),
        'staff': Staff.objects.filter(tenant=request.tenant, user__is_active=True).exclude(pk=request.me.pk)
        .order_by('full_name'), 'classes': _classes(request) or Class.objects.filter(tenant=request.tenant),
        'subjects': Subject.objects.filter(tenant=request.tenant).order_by('name'), 'kinds': HandoverNote.KINDS})


@_staff_only
def handover(request, pk):
    h = get_object_or_404(HandoverNote.objects.select_related('from_staff', 'to_staff', 'school_class', 'subject'),
                          pk=pk, tenant=request.tenant)
    if not (request.me.pk in (h.from_staff_id, h.to_staff_id) or request.admin):
        raise Http404
    if request.method == 'POST' and request.me.pk == h.to_staff_id and not h.acknowledged_at:
        h.acknowledged_at = timezone.now()
        h.save(update_fields=['acknowledged_at'])
        return redirect('teaching:handover', pk=h.pk)
    if request.GET.get('file'):
        return _serve(h.file, 'handover')
    return render(request, 'teaching/handover.html', {'h': h})
