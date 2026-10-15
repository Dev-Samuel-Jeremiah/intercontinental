"""
classroom/views_more.py
───────────────────────
Course discussion, rubric editing, completion certificates, voice feedback
playback and whiteboard photos.
"""
import os

from django.contrib import messages
from django.db.models import Q
from django.contrib.auth.decorators import login_required
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from . import extras
from .access import enrolled, require_teacher, teaches
from .models import Assignment, Course, CourseCertificate, Post, Section, Submission, Thread


def _member(request, course):
    if not (teaches(request.user, course) or enrolled(request.user, course)):
        messages.error(request, "You aren't in this course.")
        return False
    return True


@login_required
def discussion(request, course_id):
    course = get_object_or_404(Course, pk=course_id)
    if not _member(request, course):
        return redirect('dashboard:router')
    if request.method == 'POST':
        try:
            t = extras.start_thread(course, request.user, request.POST.get('title'), request.POST.get('body'))
            if t.held:
                messages.warning(request, 'Your post is waiting for your teacher to check it before others can see it.')
            return redirect('classroom:thread', t.id)
        except extras.ExtrasError as problem:
            messages.error(request, str(problem))
        return redirect('classroom:discussion', course.id)
    threads = course.threads.select_related('author').prefetch_related('posts')
    is_teacher = teaches(request.user, course)
    if not is_teacher:
        threads = threads.filter(Q(held=False) | Q(author=request.user))
    for t in threads:
        t.replies = len(t.posts.all())
        t.answered = any(p.is_answer for p in t.posts.all())
    return render(request, 'classroom/discussion.html', {'course': course, 'threads': threads,
                                                         'is_teacher': is_teacher})


@login_required
def thread(request, thread_id):
    t = get_object_or_404(Thread.objects.select_related('course', 'author'), pk=thread_id)
    if not _member(request, t.course):
        return redirect('dashboard:router')
    is_teacher = teaches(request.user, t.course)
    if t.held and not (is_teacher or t.author_id == request.user.id):
        return redirect('classroom:discussion', t.course_id)
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action == 'reply':
                p = extras.reply(t, request.user, request.POST.get('body'))
                if p.held:
                    messages.warning(request, 'Your reply is waiting for your teacher to check it before others can see it.')
            elif action == 'release' and is_teacher:
                obj = get_object_or_404(Post, pk=request.POST.get('post'), thread=t) if request.POST.get('post') else t
                extras.release(obj)
                messages.success(request, 'Released — everyone in the course can see it now.')
            elif action == 'answer':
                extras.mark_answer(get_object_or_404(Post, pk=request.POST.get('post'), thread=t), request.user)
            elif action in ('pin', 'lock') and is_teacher:
                setattr(t, 'pinned' if action == 'pin' else 'locked', not getattr(t, 'pinned' if action == 'pin' else 'locked'))
                t.save(update_fields=['pinned', 'locked'])
            elif action == 'delete_post':
                p = get_object_or_404(Post, pk=request.POST.get('post'), thread=t)
                if is_teacher or p.author_id == request.user.id:
                    p.delete()
            elif action == 'delete_thread' and (is_teacher or t.author_id == request.user.id):
                course_id = t.course_id
                t.delete()
                messages.success(request, 'Discussion removed.')
                return redirect('classroom:discussion', course_id)
        except extras.ExtrasError as problem:
            messages.error(request, str(problem))
        return redirect('classroom:thread', t.id)
    posts = t.posts.select_related('author')
    if not is_teacher:
        posts = posts.filter(Q(held=False) | Q(author=request.user))
    return render(request, 'classroom/thread.html', {'t': t, 'posts': posts, 'is_teacher': is_teacher})


@login_required
def rubric(request, assignment_id):
    a = get_object_or_404(Assignment.objects.select_related('section__course'), pk=assignment_id)
    require_teacher(request.user, a.section.course)
    if request.method == 'POST':
        rows = [(request.POST.get(f'name_{i}'), request.POST.get(f'desc_{i}'), request.POST.get(f'levels_{i}'))
                for i in range(10)]
        try:
            n = extras.set_rubric(a, rows)
            messages.success(request, f'Rubric saved with {n} criteria. Students can see it on the assignment.' if n
                             else 'Rubric removed — you will give a single mark.')
            return redirect('classroom:assignment_detail', a.id)
        except extras.ExtrasError as problem:
            messages.error(request, str(problem))
    existing = [{'name': c.name, 'desc': c.description,
                 'levels': ', '.join(f'{l.label} {l.points}' for l in c.levels.all())}
                for c in a.criteria.prefetch_related('levels')]
    if not existing:
        existing = [{'name': n, 'desc': '', 'levels': 'Excellent 4, Good 3, Fair 2, Needs work 1'}
                    for n in ('Understanding of the topic', 'Organisation', 'Use of examples', 'Spelling and grammar')]
    rows = existing + [{'name': '', 'desc': '', 'levels': ''}] * (10 - len(existing))
    from .models import RubricScore
    return render(request, 'classroom/rubric.html', {
        'a': a, 'rows': rows, 'locked': RubricScore.objects.filter(criterion__assignment=a).exists()})


@login_required
def feedback_audio(request, submission_id):
    s = get_object_or_404(Submission.objects.select_related('assignment__section__course'), pk=submission_id)
    if not (s.student_id == request.user.id or teaches(request.user, s.assignment.section.course)):
        raise Http404
    if not s.feedback_audio:
        raise Http404
    ext = os.path.splitext(s.feedback_audio.name)[1].lower()
    try:
        return FileResponse(s.feedback_audio.open('rb'), content_type=extras.AUDIO.get(ext, 'application/octet-stream'))
    except (FileNotFoundError, OSError):
        raise Http404


@login_required
def certificate(request, course_id):
    course = get_object_or_404(Course, pk=course_id)
    if not enrolled(request.user, course):
        messages.error(request, "You aren't in this course.")
        return redirect('dashboard:router')
    cert = extras.certificate_for(course, request.user)
    prog = extras.progress(course, request.user)
    return render(request, 'classroom/certificate.html', {
        'course': course, 'cert': cert, 'prog': prog, 'school': request.tenant,
        'ref': extras.reference(cert) if cert else '',
        'name': getattr(getattr(request.user, 'student', None), 'full_name', '') or request.user.get_full_name()})


@login_required
def completion(request, course_id):
    course = get_object_or_404(Course, pk=course_id)
    require_teacher(request.user, course)
    from .services import students_of
    rows = []
    for st in students_of(course):
        if not st.user_id:
            continue
        prog = extras.progress(course, st.user)
        rows.append({'student': st, 'prog': prog,
                     'cert': extras.certificate_for(course, st.user) if prog['complete']
                     else CourseCertificate.objects.filter(course=course, student=st.user).first()})
    return render(request, 'classroom/completion.html', {'course': course, 'rows': rows})


def verify_certificate(request, ref):
    cert = extras.verify(ref, request.tenant) if getattr(request, 'tenant', None) else None
    name = ''
    if cert:
        st = getattr(cert.student, 'student', None)
        name = getattr(st, 'full_name', '') or cert.student.get_full_name() or cert.student.username
    return render(request, 'classroom/certificate_verify.html', {'cert': cert, 'name': name,
                                                                 'school': getattr(request, 'tenant', None)},
                  status=200 if cert else 404)


def certificate_qr(request, ref):
    from idcards.utils import qr_png_bytes
    return HttpResponse(qr_png_bytes(request.build_absolute_uri(reverse('classroom:verify_certificate', args=[ref]))),
                        content_type='image/png')


@login_required
def board_photo(request, course_id):
    course = get_object_or_404(Course, pk=course_id)
    require_teacher(request.user, course)
    if request.method == 'POST':
        section = get_object_or_404(Section, pk=request.POST.get('section'), course=course)
        try:
            extras.add_board_photo(section, request.FILES.get('photo'), request.POST.get('title'))
            messages.success(request, f'Board photo added to "{section.title}".')
        except extras.ExtrasError as problem:
            messages.error(request, str(problem))
    return redirect('classroom:course_hub', course.id)
