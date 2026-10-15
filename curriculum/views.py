"""
curriculum/views.py
───────────────────
Departments, schemes of work and syllabus coverage, lesson notes and their
review, homework, the school calendar, and the family's homework diary.
"""
import os
from datetime import date, timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from academics.models import PublicHoliday, Term
from academics.utils import get_active_term
from users import records
from users.models import Class, Staff, Subject
from users.views_profile import can_manage, can_view

from . import services as svc
from .models import CalendarEvent, Department, Homework, LessonNote, SchemeOfWork, SchemeTopic


def _deny(request, who):
    messages.error(request, f'Only {who}.')
    return redirect('dashboard:router')


def _date(raw, default=None):
    try:
        return date.fromisoformat(raw) if raw else default
    except ValueError:
        return default


def _attach(obj, upload):
    if upload:
        try:
            records.check_upload(upload)
        except records.RecordError as problem:
            raise svc.CurriculumError(str(problem))
        obj.attachment = upload


def _serve(field):
    if not field:
        raise Http404
    try:
        handle = field.open('rb')
    except (FileNotFoundError, OSError):
        raise Http404
    ext = os.path.splitext(field.name)[1].lower()
    return FileResponse(handle, content_type=records.CONTENT_TYPES.get(ext, 'application/octet-stream'),
                        as_attachment=False, filename=f'attachment{ext}')


def _staff(request):
    return getattr(request.user, 'staff', None)


def _clock(raw):
    from datetime import datetime as _dt
    try:
        return _dt.strptime(raw or '', '%H:%M').time()
    except ValueError:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Overview and coverage
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def home(request):
    if not can_view(request.user):
        return _deny(request, 'staff can see the curriculum')
    term = get_active_term()
    staff = _staff(request)
    rows = svc.coverage_report(request.tenant, term) if term else []
    heads = svc.hod_subject_ids(staff)
    if not can_manage(request.user):
        mine = {(s.id, c.id) for s, c in svc.teaching_pairs(request.user, request.tenant)}
        rows = [r for r in rows if (r['scheme'].subject_id, r['scheme'].school_class_id) in mine]
    return render(request, 'curriculum/home.html', {
        'term': term, 'week': svc.current_week(term), 'rows': rows,
        'behind': sum(1 for r in rows if r['state'] == 'behind'),
        'to_review': svc.notes_to_review(request.user, request.tenant).count(),
        'is_hod': bool(heads), 'can_manage': can_manage(request.user),
        'upcoming': svc.upcoming(request.tenant, 'STAFF', limit=5),
        'missing': _missing_schemes(request, term),
    })


def _missing_schemes(request, term):
    if not term:
        return []
    have = set(SchemeOfWork.objects.filter(tenant=request.tenant, term=term)
               .values_list('subject_id', 'school_class_id'))
    return [p for p in svc.teaching_pairs(request.user, request.tenant) if (p[0].id, p[1].id) not in have]


# ─────────────────────────────────────────────────────────────────────────────
# Departments
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def departments(request):
    if not can_view(request.user):
        return _deny(request, 'staff can see departments')
    if request.method == 'POST':
        if not can_manage(request.user):
            return _deny(request, 'administrators can change departments')
        action = request.POST.get('action')
        if action == 'delete':
            get_object_or_404(Department, pk=request.POST.get('id')).delete()
            messages.success(request, 'Department removed.')
            return redirect('curriculum:departments')
        dept = (get_object_or_404(Department, pk=request.POST.get('id')) if request.POST.get('id')
                else Department(tenant=request.tenant))
        name = (request.POST.get('name') or '').strip()[:80]
        if not name:
            messages.error(request, 'Give the department a name.')
            return redirect('curriculum:departments')
        if Department.objects.filter(tenant=request.tenant, name__iexact=name).exclude(pk=dept.pk).exists():
            messages.error(request, f'There is already a {name} department.')
            return redirect('curriculum:departments')
        dept.name = name
        dept.description = (request.POST.get('description') or '').strip()[:255]
        dept.hod = Staff.objects.filter(pk=request.POST.get('hod') or None).first()
        dept.save()
        dept.subjects.set(Subject.objects.filter(pk__in=request.POST.getlist('subjects')))
        messages.success(request, f'{dept.name} saved.')
        return redirect('curriculum:departments')
    depts = Department.objects.filter(tenant=request.tenant).select_related('hod').prefetch_related('subjects')
    placed = set(Department.subjects.through.objects.filter(department__tenant=request.tenant)
                 .values_list('subject_id', flat=True))
    subjects = Subject.objects.filter(tenant=request.tenant).order_by('name')
    return render(request, 'curriculum/departments.html', {
        'departments': depts, 'subjects': subjects,
        'staff': Staff.objects.filter(tenant=request.tenant).order_by('full_name'),
        'unplaced': [s for s in subjects if s.id not in placed],
        'can_manage': can_manage(request.user),
    })


# ─────────────────────────────────────────────────────────────────────────────
# Schemes of work
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def schemes(request):
    if not can_view(request.user):
        return _deny(request, 'staff can see schemes of work')
    terms = Term.objects.filter(tenant=request.tenant).select_related('session').order_by('-start_date')
    term = terms.filter(pk=request.GET.get('term')).first() if request.GET.get('term') else get_active_term()
    pairs = svc.teaching_pairs(request.user, request.tenant)
    if request.method == 'POST':
        subject = get_object_or_404(Subject, pk=request.POST.get('subject'))
        school_class = get_object_or_404(Class, pk=request.POST.get('school_class'))
        term = get_object_or_404(Term, pk=request.POST.get('term'))
        if not svc.may_plan(request.user, subject, school_class):
            return _deny(request, f'teachers of {subject.name} in {school_class.name}, its head of department '
                                  f'or an administrator can write this scheme')
        try:
            source = SchemeOfWork.objects.filter(pk=request.POST.get('copy_from') or None).first()
            if source:
                if source.subject_id != subject.id:
                    raise svc.CurriculumError('You can only copy a scheme for the same subject.')
                scheme = svc.copy_scheme(source, school_class, term, _staff(request))
            else:
                scheme, created = SchemeOfWork.objects.get_or_create(
                    tenant=request.tenant, subject=subject, school_class=school_class, term=term,
                    defaults={'created_by': _staff(request),
                              'weeks': max(1, min(16, int(request.POST.get('weeks') or 12)))})
                if not created:
                    messages.info(request, 'That scheme already exists — here it is.')
            return redirect('curriculum:scheme', scheme.id)
        except (svc.CurriculumError, ValueError) as problem:
            messages.error(request, str(problem))
            return redirect('curriculum:schemes')
    qs = SchemeOfWork.objects.filter(tenant=request.tenant).select_related('subject', 'school_class', 'term')
    if term:
        qs = qs.filter(term=term)
    if not can_manage(request.user):
        mine = {(s.id, c.id) for s, c in pairs}
        qs = [s for s in qs if (s.subject_id, s.school_class_id) in mine]
    return render(request, 'curriculum/schemes.html', {
        'schemes': [svc.coverage(s) for s in qs], 'terms': terms, 'term': term, 'pairs': pairs,
        'copyable': SchemeOfWork.objects.filter(tenant=request.tenant).select_related(
            'subject', 'school_class', 'term', 'term__session').order_by('subject__name', '-term__start_date'),
    })


@login_required
def scheme(request, pk):
    s = get_object_or_404(SchemeOfWork.objects.select_related('subject', 'school_class', 'term', 'term__session',
                                                              'created_by', 'approved_by'), pk=pk)
    if not can_view(request.user):
        return _deny(request, 'staff can see schemes of work')
    editable = svc.may_plan(request.user, s.subject, s.school_class)
    if request.method == 'POST':
        if not editable:
            return _deny(request, f'teachers of {s.subject.name} in {s.school_class.name} can change this scheme')
        action = request.POST.get('action')
        try:
            if action == 'add':
                n = len(svc.add_topics(s, request.POST.get('topics')))
                messages.success(request, f'{n} topic{"s" if n != 1 else ""} added.')
            elif action in ('taught', 'untaught', 'delete_topic', 'edit_topic'):
                t = get_object_or_404(SchemeTopic, pk=request.POST.get('topic_id'), scheme=s)
                if action == 'taught':
                    svc.mark_taught(t, _staff(request), _date(request.POST.get('on')))
                elif action == 'untaught':
                    svc.unmark_taught(t)
                elif action == 'delete_topic':
                    t.delete()
                else:
                    t.topic = (request.POST.get('topic') or t.topic).strip()[:200]
                    t.week = max(1, min(s.weeks, int(request.POST.get('week') or t.week)))
                    t.objectives = (request.POST.get('objectives') or '').strip()
                    t.resources = (request.POST.get('resources') or '').strip()[:255]
                    t.save()
            elif action == 'weeks':
                weeks = max(1, min(16, int(request.POST.get('weeks') or s.weeks)))
                if s.topics.filter(week__gt=weeks).exists():
                    raise svc.CurriculumError('Move or remove the topics in the later weeks first.')
                s.weeks = weeks
                s.save(update_fields=['weeks'])
            elif action == 'approve':
                if not svc.may_review(request.user, s.subject):
                    raise svc.CurriculumError('Only the head of department or an administrator can approve a scheme.')
                s.approved_by, s.approved_at = _staff(request), timezone.now()
                s.save(update_fields=['approved_by', 'approved_at'])
                messages.success(request, 'Scheme approved.')
            elif action == 'delete':
                if s.topics.filter(taught_on__isnull=False).exists() and not can_manage(request.user):
                    raise svc.CurriculumError('Topics have been taught from this scheme — ask an administrator.')
                s.delete()
                messages.success(request, 'Scheme deleted.')
                return redirect('curriculum:schemes')
        except (svc.CurriculumError, ValueError) as problem:
            messages.error(request, str(problem))
        return redirect('curriculum:scheme', s.id)
    topics = list(s.topics.select_related('taught_by'))
    weeks = [{'n': w, 'topics': [t for t in topics if t.week == w]} for w in range(1, s.weeks + 1)]
    return render(request, 'curriculum/scheme.html', {
        'scheme': s, 'weeks': weeks, 'cov': svc.coverage(s), 'editable': editable,
        'can_approve': svc.may_review(request.user, s.subject), 'today': timezone.localdate(),
    })


# ─────────────────────────────────────────────────────────────────────────────
# Lesson notes
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def notes(request):
    staff = _staff(request)
    if can_manage(request.user) and not staff:
        return redirect('curriculum:review')
    if not can_view(request.user) or not staff:
        return _deny(request, 'teachers can write lesson notes')
    mine = (LessonNote.objects.filter(tenant=request.tenant, teacher=staff)
            .select_related('subject', 'school_class', 'term', 'reviewed_by'))
    return render(request, 'curriculum/notes.html', {
        'notes': mine[:100], 'to_review': svc.notes_to_review(request.user, request.tenant).count(),
        'returned': mine.filter(status=LessonNote.RETURNED).count(),
    })


@login_required
def note_edit(request, pk=None):
    staff = _staff(request)
    if not can_view(request.user) or not staff:
        return _deny(request, 'teachers can write lesson notes')
    note = get_object_or_404(LessonNote, pk=pk, teacher=staff) if pk else None
    if note and note.status in (LessonNote.SUBMITTED, LessonNote.APPROVED):
        return redirect('curriculum:note', note.id)
    term = note.term if note else get_active_term()
    pairs = svc.teaching_pairs(request.user, request.tenant)
    if request.method == 'POST' and request.POST.get('ai'):
        draft = _ai_note_draft(request, note, term, staff)
        if draft is not None:
            topics = (SchemeTopic.objects.filter(tenant=request.tenant, scheme__term=term)
                      .select_related('scheme__subject', 'scheme__school_class') if term else [])
            return render(request, 'curriculum/note_edit.html', {
                'note': draft, 'term': term, 'pairs': pairs, 'week': draft.week, 'topics': topics, 'ai_draft': True,
                'minutes': request.POST.get('minutes') or 40})
    elif request.method == 'POST':
        try:
            if not term:
                raise svc.CurriculumError('Set the current term before writing lesson notes.')
            subject = get_object_or_404(Subject, pk=request.POST.get('subject'))
            school_class = get_object_or_404(Class, pk=request.POST.get('school_class'))
            if not svc.may_plan(request.user, subject, school_class):
                raise svc.CurriculumError(f"You don't teach {subject.name} in {school_class.name}.")
            note = note or LessonNote(tenant=request.tenant, teacher=staff, term=term)
            note.subject, note.school_class = subject, school_class
            note.week = max(1, min(16, int(request.POST.get('week') or 1)))
            note.title = (request.POST.get('title') or '').strip()[:200]
            note.objectives = (request.POST.get('objectives') or '').strip()
            note.content = (request.POST.get('content') or '').strip()
            note.topic = SchemeTopic.objects.filter(pk=request.POST.get('topic') or None,
                                                    scheme__subject=subject, scheme__school_class=school_class,
                                                    scheme__term=term).first()
            if not note.title:
                note.title = note.topic.topic if note.topic else ''
            if not note.title:
                raise svc.CurriculumError('Give the lesson a title.')
            _attach(note, request.FILES.get('attachment'))
            note.save()
            if request.POST.get('submit'):
                svc.submit_note(note)
                messages.success(request, 'Lesson note sent to your head of department.')
            else:
                messages.success(request, 'Draft saved.')
            return redirect('curriculum:note', note.id)
        except (svc.CurriculumError, ValueError) as problem:
            messages.error(request, str(problem))
    week = svc.current_week(term) or 1
    topics = (SchemeTopic.objects.filter(tenant=request.tenant, scheme__term=term)
              .select_related('scheme__subject', 'scheme__school_class') if term else [])
    return render(request, 'curriculum/note_edit.html', {
        'note': note, 'term': term, 'pairs': pairs, 'week': note.week if note else week, 'topics': topics,
    })


def _ai_note_draft(request, note, term, staff):
    """An unsaved lesson note filled in by AI from the form, or None (with a message) if that failed."""
    from assistant import writers
    from assistant.providers import AIError
    try:
        if not term:
            raise AIError('Set the current term before writing lesson notes.')
        subject = Subject.objects.filter(pk=request.POST.get('subject') or 0, tenant=request.tenant).first()
        school_class = Class.objects.filter(pk=request.POST.get('school_class') or 0, tenant=request.tenant).first()
        if not (subject and school_class):
            raise AIError('Choose the subject and class.')
        if not svc.may_plan(request.user, subject, school_class):
            raise AIError(f"You don't teach {subject.name} in {school_class.name}.")
        topic = SchemeTopic.objects.filter(pk=request.POST.get('topic') or None, scheme__subject=subject,
                                           scheme__school_class=school_class, scheme__term=term).first()
        week = max(1, min(16, int(request.POST.get('week') or 1)))
        title = (request.POST.get('title') or '').strip() or (topic.topic if topic else '')
        draft = writers.lesson_note(
            request.tenant, request.user, subject.name, school_class.name, week, title,
            minutes=request.POST.get('minutes') or 40, subtopics=topic.subtopics if topic else '',
            objectives=(request.POST.get('objectives') or '').strip() or (topic.objectives if topic else ''),
            teacher_notes=(request.POST.get('content') or '').strip()[:3000])
    except (AIError, ValueError) as problem:
        messages.error(request, str(problem))
        return None
    out = note or LessonNote(tenant=request.tenant, teacher=staff, term=term)
    out.subject, out.school_class, out.week, out.topic = subject, school_class, week, topic
    out.title, out.objectives, out.content = draft['title'], draft['objectives'], draft['content']
    messages.success(request, 'AI draft below — nothing is saved yet. Read it, change what you need, then save.')
    return out


@login_required
def note(request, pk):
    n = get_object_or_404(LessonNote.objects.select_related('teacher', 'subject', 'school_class', 'term',
                                                            'term__session', 'topic', 'reviewed_by'), pk=pk)
    staff = _staff(request)
    is_owner = staff and n.teacher_id == staff.id
    can_review = svc.may_review(request.user, n.subject) and not is_owner
    if not (is_owner or svc.may_review(request.user, n.subject)):
        return _deny(request, 'the teacher, their head of department or an administrator can see this note')
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action == 'submit' and is_owner:
                svc.submit_note(n)
                messages.success(request, 'Lesson note sent to your head of department.')
            elif action == 'withdraw' and is_owner and n.status == LessonNote.SUBMITTED:
                n.status = LessonNote.DRAFT
                n.save(update_fields=['status'])
                messages.success(request, 'Withdrawn — you can edit it again.')
            elif action in ('approve', 'return'):
                svc.review_note(n, action == 'approve', request.user, request.POST.get('comment'))
                messages.success(request, 'Approved.' if action == 'approve' else 'Sent back to the teacher.')
                return redirect('curriculum:review')
        except svc.CurriculumError as problem:
            messages.error(request, str(problem))
        return redirect('curriculum:note', n.id)
    return render(request, 'curriculum/note.html', {'note': n, 'is_owner': is_owner, 'can_review': can_review})


@login_required
def note_file(request, pk):
    n = get_object_or_404(LessonNote, pk=pk)
    staff = _staff(request)
    if not ((staff and n.teacher_id == staff.id) or svc.may_review(request.user, n.subject)):
        raise Http404
    return _serve(n.attachment)


@login_required
def review(request):
    if not can_view(request.user):
        return _deny(request, 'staff can review lesson notes')
    waiting = svc.notes_to_review(request.user, request.tenant)
    staff = _staff(request)
    if staff:
        waiting = waiting.exclude(teacher=staff)
    if not can_manage(request.user) and not svc.hod_subject_ids(staff):
        return _deny(request, 'heads of department and administrators review lesson notes')
    recent = (LessonNote.objects.filter(tenant=request.tenant, reviewed_by=staff)
              .select_related('teacher', 'subject', 'school_class').order_by('-reviewed_at')[:15]) if staff else []
    return render(request, 'curriculum/review.html', {'waiting': waiting, 'recent': recent})


# ─────────────────────────────────────────────────────────────────────────────
# Homework
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def homework(request):
    staff = _staff(request)
    if not can_view(request.user):
        return _deny(request, 'staff can set homework')
    pairs = svc.teaching_pairs(request.user, request.tenant)
    if request.method == 'POST':
        try:
            if request.POST.get('action') == 'delete':
                hw = get_object_or_404(Homework, pk=request.POST.get('id'))
                if not (can_manage(request.user) or (staff and hw.set_by_id == staff.id)):
                    raise svc.CurriculumError('Only the teacher who set it can remove it.')
                hw.delete()
                messages.success(request, 'Homework removed.')
            else:
                subject = get_object_or_404(Subject, pk=request.POST.get('subject'))
                school_class = get_object_or_404(Class, pk=request.POST.get('school_class'))
                if not svc.may_plan(request.user, subject, school_class):
                    raise svc.CurriculumError(f"You don't teach {subject.name} in {school_class.name}.")
                due = _date(request.POST.get('due_on'))
                if not due:
                    raise svc.CurriculumError('Choose when it is due.')
                upload = request.FILES.get('attachment')
                if upload:
                    try:
                        records.check_upload(upload)
                    except records.RecordError as problem:
                        raise svc.CurriculumError(str(problem))
                hw = svc.set_homework(staff, school_class, subject, request.POST.get('title'),
                                      request.POST.get('details'), due)
                if upload:
                    hw.attachment = upload
                    hw.save(update_fields=['attachment'])
                messages.success(request, f'Homework set for {school_class.name}. '
                                          f'Students and parents can see it in their portal.')
        except svc.CurriculumError as problem:
            messages.error(request, str(problem))
        return redirect('curriculum:homework')
    since = timezone.localdate() - timedelta(days=14)
    qs = Homework.objects.filter(tenant=request.tenant, due_on__gte=since).select_related('subject', 'school_class', 'set_by')
    if not can_manage(request.user):
        mine = {(s.id, c.id) for s, c in pairs}
        qs = [h for h in qs if (h.subject_id, h.school_class_id) in mine or (staff and h.set_by_id == staff.id)]
    return render(request, 'curriculum/homework.html', {
        'items': qs, 'pairs': pairs, 'today': timezone.localdate(),
        'default_due': timezone.localdate() + timedelta(days=2), 'can_manage': can_manage(request.user),
    })


def _family_classes(request):
    """The classes whose homework this student or parent may see, with the children in them."""
    student = getattr(request.user, 'student', None)
    if student and student.class_assigned_id:
        return [student]
    if getattr(request.user, 'is_parent', False):
        from users.views_achievements import _parent_children
        return [c for c in _parent_children(request) if c.class_assigned_id]
    return []


@login_required
def homework_file(request, pk):
    hw = get_object_or_404(Homework, pk=pk)
    if not (can_view(request.user) or any(c.class_assigned_id == hw.school_class_id
                                          for c in _family_classes(request))):
        raise Http404
    return _serve(hw.attachment)


@login_required
def family(request):
    """Homework diary and calendar for a student or parent."""
    children = _family_classes(request)
    is_student = getattr(request.user, 'student', None) is not None
    if not children and not getattr(request.user, 'is_parent', False) and not is_student:
        return _deny(request, 'students and parents have a homework diary')
    today = timezone.localdate()
    diaries = []
    for child in children:
        items = list(svc.homework_for(child.class_assigned, days_back=3))
        from .staffing import clubs_of
        from cbt.planning import for_class as exam_timetable_for
        diaries.append({'child': child, 'due': [h for h in items if h.due_on >= today],
                        'past': [h for h in items if h.due_on < today], 'clubs': clubs_of(child),
                        'exams': exam_timetable_for(child.class_assigned)})
    holidays = PublicHoliday.objects.filter(tenant=request.tenant, date__gte=today).order_by('date')[:6]
    ctx = {'diaries': diaries, 'today': today, 'events': svc.upcoming(request.tenant, 'PARENTS', days=120, limit=12),
           'holidays': holidays, 'is_student': is_student}
    return render(request, 'curriculum/student_diary.html' if is_student else 'parents/homework.html', ctx)


# ─────────────────────────────────────────────────────────────────────────────
# Calendar
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def school_calendar(request):
    if not can_view(request.user):
        return redirect('curriculum:family')
    today = timezone.localdate()
    try:
        year, month = int(request.GET.get('y') or today.year), int(request.GET.get('m') or today.month)
        first = date(year, month, 1)
    except ValueError:
        first = today.replace(day=1)
    if request.method == 'POST':
        if not can_manage(request.user):
            return _deny(request, 'administrators can change the school calendar')
        action = request.POST.get('action')
        if action == 'delete':
            get_object_or_404(CalendarEvent, pk=request.POST.get('id')).delete()
            messages.success(request, 'Event removed.')
        elif action == 'holidays':
            year = int(request.POST.get('year') or today.year)
            added = svc.import_holidays(request.tenant, year)
            messages.success(request, f'Added {len(added)} public holiday{"s" if len(added) != 1 else ""} for {year}. '
                                      'Add Eid-el-Fitr, Eid-el-Kabir and Mawlid once the Federal Government '
                                      'announces them.' if added else f'All {year} holidays are already in.')
        elif action == 'holiday':
            day = _date(request.POST.get('date'))
            name = (request.POST.get('name') or '').strip()[:100]
            if day and name:
                PublicHoliday.objects.get_or_create(tenant=request.tenant, date=day, defaults={'name': name})
                messages.success(request, f'{name} added. Term school-day counts are updated.')
        else:
            start = _date(request.POST.get('start_date'))
            end = _date(request.POST.get('end_date'))
            title = (request.POST.get('title') or '').strip()[:160]
            if not (start and title):
                messages.error(request, 'Give the event a title and a date.')
            elif end and end < start:
                messages.error(request, 'The event ends before it starts.')
            else:
                kind = request.POST.get('kind')
                start_time, end_time = _clock(request.POST.get('start_time')), _clock(request.POST.get('end_time'))
                cap = request.POST.get('capacity') or ''
                days = request.POST.get('reminder_days') or '0'
                ev = CalendarEvent.objects.create(
                    tenant=request.tenant, title=title, start_date=start, end_date=end if end != start else None,
                    kind=kind if kind in dict(CalendarEvent.KINDS) else 'OTHER',
                    audience=request.POST.get('audience') if request.POST.get('audience') in dict(CalendarEvent.AUDIENCES) else 'ALL',
                    description=(request.POST.get('description') or '').strip()[:255], created_by=request.user,
                    start_time=start_time, end_time=end_time if start_time and end_time and end_time > start_time else None,
                    venue=(request.POST.get('venue') or '').strip()[:120], rsvp=bool(request.POST.get('rsvp')),
                    rsvp_deadline=_date(request.POST.get('rsvp_deadline')),
                    capacity=int(cap) if cap.isdigit() and int(cap) > 0 else None,
                    reminder_days=min(int(days), 30) if days.isdigit() else 0)
                messages.success(request, f'{title} is on the calendar.')
                if ev.rsvp or ev.venue or ev.start_time:
                    return redirect('curriculum:event', pk=ev.pk)
                first = start.replace(day=1)
        return redirect(f"{request.path}?y={first.year}&m={first.month}")
    prev = (first - timedelta(days=1)).replace(day=1)
    nxt = (first + timedelta(days=32)).replace(day=1)
    return render(request, 'curriculum/calendar.html', {
        'weeks': svc.month_grid(request.tenant, first.year, first.month), 'first': first, 'prev': prev, 'next': nxt,
        'today': today, 'kinds': CalendarEvent.KINDS, 'audiences': CalendarEvent.AUDIENCES,
        'colours': CalendarEvent.COLOURS, 'can_manage': can_manage(request.user),
        'upcoming': svc.upcoming(request.tenant, 'STAFF', days=90, limit=10),
        'holidays': PublicHoliday.objects.filter(tenant=request.tenant, date__year=first.year).order_by('date'),
    })


# ─────────────────────────────────────────────────────────────────────────────
# Who teaches what
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def allocation(request):
    from . import allocation as alloc
    if not can_view(request.user):
        return _deny(request, 'staff can see who teaches what')
    if request.method == 'POST':
        if not can_manage(request.user):
            return _deny(request, 'administrators can change teaching assignments')
        changed = 0
        staff_by_id = {str(s.id): s for s in Staff.objects.filter(tenant=request.tenant)}
        subjects = {str(s.id): s for s in Subject.objects.filter(tenant=request.tenant)}
        classes = {str(c.id): c for c in Class.objects.filter(tenant=request.tenant)}
        for key, value in request.POST.items():
            if not key.startswith('cell_'):
                continue
            try:
                _, subject_id, class_id = key.split('_')
            except ValueError:
                continue
            subject, school_class = subjects.get(subject_id), classes.get(class_id)
            if not (subject and school_class) or (value and value not in staff_by_id):
                continue
            alloc.set_teacher(request.tenant, subject, school_class, staff_by_id.get(value))
            changed += 1
        messages.success(request, f'{changed} assignment{"s" if changed != 1 else ""} updated.' if changed
                         else 'Nothing changed.')
        return redirect('curriculum:allocation')
    grid = alloc.matrix(request.tenant)
    return render(request, 'curriculum/allocation.html', {
        **grid, 'load': alloc.loads(request.tenant), 'can_manage': can_manage(request.user),
        'teachers': Staff.objects.filter(tenant=request.tenant, role__in=('TEACHER', 'ADMIN')).order_by('full_name'),
        'heavy_periods': alloc.HEAVY_PERIODS, 'heavy_allocations': alloc.HEAVY_ALLOCATIONS,
    })
