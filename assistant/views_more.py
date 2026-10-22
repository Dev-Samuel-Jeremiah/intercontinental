"""
assistant/views_more.py
───────────────────────
AI part 2 pages: questions about school data, at-risk early warning, the
safety queue for held posts, the course tutor (students and teachers),
essay feedback and study plans.
"""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render

from users.models import Class, Student
from users.views_profile import can_manage, can_see_fees, can_view

from . import ask, learning, risk
from .models import AdminQuery, EssayFeedback, StudyPlan, TutorMessage
from .providers import AIError


def _deny(request, text):
    messages.error(request, text)
    return redirect('dashboard:router')


def _pk(raw):
    raw = str(raw or '')
    return int(raw) if raw.isdigit() else 0


# ─────────────────────────────────────────────────────────────────────────────
# F167 — questions about school data
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def ask_page(request):
    if not (can_manage(request.user) or can_see_fees(request.user)):
        return _deny(request, 'Only administrators and the bursary can ask about school records.')
    report = error = None
    question = (request.POST.get('question') or request.GET.get('q') or '').strip()
    if question and (request.method == 'POST' or request.GET.get('q')):
        try:
            report, _, _ = ask.answer(request.tenant, request.user, question, can_see_fees(request.user))
        except AIError as problem:
            error = str(problem)
    return render(request, 'assistant/ask.html', {
        'question': question, 'report': report, 'error': error,
        'recent': AdminQuery.objects.filter(tenant=request.tenant, user=request.user)[:8],
        'examples': ['How many JSS2 students owe more than ₦50,000?', 'Who was absent today?',
                     'Which SS1 students have attendance below 75% this month?',
                     'Top 5 students in SS3 Mathematics', 'How many girls are in JSS 1?',
                     'Birthdays in the next two weeks']})


# ─────────────────────────────────────────────────────────────────────────────
# F164 — at-risk early warning
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def at_risk(request):
    if not can_view(request.user):
        return _deny(request, 'Only staff can see the early-warning list.')
    if can_manage(request.user):
        classes = Class.objects.filter(tenant=request.tenant).order_by('name')
    else:
        staff = request.user.staff_profiles.filter(tenant=request.tenant).first()
        classes = Class.objects.filter(tenant=request.tenant, form_teachers=staff).order_by('name')
    school_class = classes.filter(pk=_pk(request.GET.get('class') or request.POST.get('class'))).first()
    students = risk.students_for(request.user, request.tenant, school_class)
    rows = risk.assess(students, request.tenant, with_fees=can_see_fees(request.user))
    show = request.GET.get('show') or 'flagged'
    suggestions, focus = None, None
    if request.method == 'POST':
        focus = next((r for r in rows if r['student'].id == _pk(request.POST.get('student'))), None)
        if focus:
            try:
                suggestions = risk.suggest(request.tenant, request.user, focus)
            except AIError as problem:
                messages.error(request, str(problem))
    visible = rows if show == 'all' else [r for r in rows if r['level'] != 'low']
    return render(request, 'assistant/at_risk.html', {
        'rows': visible, 'classes': classes, 'school_class': school_class, 'show': show,
        'high': sum(1 for r in rows if r['level'] == 'high'), 'medium': sum(1 for r in rows if r['level'] == 'medium'),
        'total': len(rows), 'suggestions': suggestions, 'focus': focus, 'fees': can_see_fees(request.user)})


# ─────────────────────────────────────────────────────────────────────────────
# F179 — held posts
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def safety_queue(request):
    from classroom.models import Post, Thread
    if not can_view(request.user):
        return _deny(request, 'Only staff can see held posts.')
    mine = Q() if can_manage(request.user) else Q(course__teacher=request.user)
    threads = Thread.objects.filter(mine, tenant=request.tenant, held=True)
    posts = Post.objects.filter(Q() if can_manage(request.user) else Q(thread__course__teacher=request.user),
                                tenant=request.tenant, held=True)
    if request.method == 'POST':
        model = Thread if request.POST.get('kind') == 'thread' else Post
        obj = get_object_or_404((threads if model is Thread else posts), pk=_pk(request.POST.get('id')))
        if request.POST.get('action') == 'delete':
            obj.delete()
            messages.success(request, 'Deleted.')
        else:
            obj.held, obj.held_reason = False, ''
            obj.save(update_fields=['held', 'held_reason'])
            messages.success(request, 'Released — the class can see it now.')
        return redirect('assistant:safety')
    tutor_held = TutorMessage.objects.filter(
        Q() if can_manage(request.user) else Q(course__teacher=request.user), tenant=request.tenant, held=True) \
        .select_related('student', 'course').order_by('-created_at')[:30]
    return render(request, 'assistant/safety.html', {
        'threads': threads.select_related('author', 'course'),
        'posts': posts.select_related('author', 'thread__course'), 'tutor_held': tutor_held})


# ─────────────────────────────────────────────────────────────────────────────
# F163 — course tutor
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def tutor(request, course_id):
    from classroom.access import enrolled, teaches
    from classroom.models import Course
    course = get_object_or_404(Course, pk=course_id, tenant=request.tenant)
    if teaches(request.user, course):
        return redirect('assistant:tutor_admin', course_id=course.pk)
    if not enrolled(request.user, course):
        return _deny(request, 'You aren\'t in this course.')
    settings_ = learning.tutor_settings(course)
    if request.method == 'POST':
        try:
            learning.tutor_reply(course, request.user, request.POST.get('text'))
        except AIError as problem:
            messages.error(request, str(problem))
        return redirect('assistant:tutor', course_id=course.pk)
    chat = TutorMessage.objects.filter(course=course, student=request.user).order_by('-created_at', '-id')[:40]
    return render(request, 'assistant/tutor.html', {
        'course': course, 's': settings_, 'chat': list(chat)[::-1],
        'left': learning.questions_left(course, request.user) if settings_.enabled else 0})


@login_required
def tutor_admin(request, course_id):
    from classroom.access import teaches
    from classroom.models import Course
    course = get_object_or_404(Course, pk=course_id, tenant=request.tenant)
    if not teaches(request.user, course):
        return _deny(request, 'Only the course teacher can manage its tutor.')
    s = learning.tutor_settings(course)
    if request.method == 'POST':
        s.enabled = bool(request.POST.get('enabled'))
        limit = request.POST.get('daily_limit') or '20'
        s.daily_limit = max(1, min(int(limit), 200)) if limit.isdigit() else 20
        s.instructions = (request.POST.get('instructions') or '').strip()[:2000]
        s.save()
        messages.success(request, 'Tutor settings saved.' + (' Students can use it now.' if s.enabled else ''))
        return redirect('assistant:tutor_admin', course_id=course.pk)
    students = {}
    for m in TutorMessage.objects.filter(course=course).select_related('student'):
        row = students.setdefault(m.student_id, {'user': m.student, 'count': 0, 'held': 0, 'last': m.created_at})
        row['count'] += m.role == 'user'
        row['held'] += m.held
        row['last'] = max(row['last'], m.created_at)
    who = _pk(request.GET.get('student'))
    transcript = TutorMessage.objects.filter(course=course, student_id=who).order_by('created_at', 'id') if who else None
    return render(request, 'assistant/tutor_admin.html', {
        'course': course, 's': s, 'students': sorted(students.values(), key=lambda r: r['last'], reverse=True),
        'transcript': transcript, 'who': students.get(who)})


# ─────────────────────────────────────────────────────────────────────────────
# F169 — essay feedback
# ─────────────────────────────────────────────────────────────────────────────

def _open_assignments(user):
    from classroom.models import Assignment
    student = getattr(user, 'student', None)
    q = Q(section__course__students=user)
    if student and student.class_assigned_id:
        q |= Q(section__course__class_assigned_id=student.class_assigned_id)
    return Assignment.objects.filter(q).select_related('section__course').distinct().order_by('-due_date')[:30]


@login_required
def essay(request):
    student = getattr(request.user, 'student', None)
    if not student:
        return _deny(request, 'Essay feedback is for students.')
    assignments = _open_assignments(request.user)
    result = None
    if request.method == 'POST':
        a = next((x for x in assignments if x.pk == _pk(request.POST.get('assignment'))), None)
        try:
            result = learning.essay_feedback(request.tenant, request.user, request.POST.get('text'),
                                             (request.POST.get('title') or '').strip(), a,
                                             str(student.class_assigned or ''))
        except AIError as problem:
            messages.error(request, str(problem))
    return render(request, 'assistant/essay.html', {
        'assignments': assignments, 'result': result, 'left': learning.essays_left(request.user),
        'history': EssayFeedback.objects.filter(student=request.user)[:10],
        'text': request.POST.get('text', '') if not result else result.text})


@login_required
def essay_view(request, pk):
    fb = get_object_or_404(EssayFeedback, pk=pk, tenant=request.tenant)
    if fb.student_id != request.user.id and not can_view(request.user):
        return _deny(request, 'You can only see your own feedback.')
    return render(request, 'assistant/essay.html', {'result': fb, 'readonly': True, 'text': fb.text,
                                                    'history': [], 'assignments': []})


# ─────────────────────────────────────────────────────────────────────────────
# F173 — study plans
# ─────────────────────────────────────────────────────────────────────────────

def _may_see_student(request, student):
    if can_view(request.user):
        return True
    own = getattr(request.user, 'student', None)
    if own and own.pk == student.pk:
        return True
    if getattr(request.user, 'is_parent', False):
        from users.views_achievements import _parent_children
        return _parent_children(request).filter(pk=student.pk).exists()
    return False


@login_required
def my_plan(request):
    student = getattr(request.user, 'student', None)
    if not student:
        if getattr(request.user, 'is_parent', False):
            from users.views_achievements import _parent_children
            child = _parent_children(request).first()
            if child:
                return redirect('assistant:study_plan', student_id=child.pk)
        return _deny(request, 'Study plans are for students and their parents.')
    return redirect('assistant:study_plan', student_id=student.pk)


@login_required
def study_plan(request, student_id):
    student = get_object_or_404(Student.objects.select_related('class_assigned'), pk=student_id, tenant=request.tenant)
    if not _may_see_student(request, student):
        return _deny(request, 'Only the student, their family and staff can see this plan.')
    if request.method == 'POST':
        try:
            learning.study_plan(student, request.user)
            messages.success(request, 'Here is a fresh plan.')
        except AIError as problem:
            messages.error(request, str(problem))
        return redirect('assistant:study_plan', student_id=student.pk)
    weak, subjects, exams = learning.focus_for(student)
    return render(request, 'assistant/study_plan.html', {
        'student': student, 'plan': StudyPlan.objects.filter(student=student).first(),
        'weak': weak, 'subjects': subjects, 'exams': exams})
