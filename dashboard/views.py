from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import authenticate, login, logout
from django.contrib import messages
from django.utils import timezone
from django.db.models import Q

from academics.utils import get_active_session, get_active_term
from cbt import engine as cbt_engine
from cbt.models import Exam, ExamAttempt
from results.models import TermResult, Term, AcademicSession
from classroom.models import Course
from users.models import Student, Attendance, ClassSubject
from idcards.services import get_or_create_student_card

import random
import string


# ─── auth helper — replaces @login_required to avoid redirect loops ──────────
def _require_login(request):
    """Return a redirect response if not logged in, else None."""
    if not request.user.is_authenticated:
        return redirect('users:staff_login')
    return None


# ─── dashboard router ─────────────────────────────────────────────────────────
def dashboard_router(request):
    guard = _require_login(request)
    if guard:
        return guard

    user = request.user

    if user.is_superuser:
        return redirect('dashboard:super_admin')

    staff = user.staff      # uses tenant-scoped property on User
    if staff is not None:
        if staff.role == 'ADMIN':
            return redirect('dashboard:admin')
        if staff.role == 'TEACHER':
            return redirect('dashboard:teacher')
        return redirect('dashboard:admin')

    student = user.student  # uses tenant-scoped property on User
    if student is not None:
        return redirect('dashboard:student')

    if getattr(user, 'is_parent', False):
        return redirect('users:parent_dashboard')

    # No role at all: sign out first. The login page sends signed-in users
    # straight back here, so redirecting without logging out would loop.
    logout(request)
    messages.error(request, 'Your account has no role assigned. Contact the administrator.')
    return redirect('users:staff_login')


# ─── super admin dashboard ────────────────────────────────────────────────────
def super_admin_dashboard(request):
    guard = _require_login(request)
    if guard:
        return guard

    from users.models import User, Student, Staff, Subject, Class
    from cbt.models import Exam
    from saas_admin.models import Campus
    from saas_admin.services import campus_class_ids

    active_session = get_active_session()
    active_term    = get_active_term()
    active_class   = Class.objects.first()

    # The section (campus) picked in the header narrows every figure below
    campuses = Campus.objects.filter(tenant=request.tenant, active=True)
    picked_campus = campuses.filter(pk=request.session.get('campus_id')).first()
    class_ids = campus_class_ids(request)

    students = Student.objects.all()
    staff = Staff.objects.all()
    classes = Class.objects.all()
    exams = Exam.objects.all()
    attendance = Attendance.objects.filter(date=timezone.localdate())
    if class_ids is not None:
        students = students.filter(class_assigned_id__in=class_ids)
        staff = staff.filter(campuses=picked_campus)
        classes = classes.filter(pk__in=class_ids)
        exams = exams.filter(classes__in=class_ids).distinct()
        attendance = attendance.filter(school_class_id__in=class_ids)

    # Today's register: share of marks that are "present"
    marked = attendance.count()
    attendance_today = round(attendance.filter(status='P').count() * 100 / marked, 1) if marked else None

    # How far through the active term we are, from its start and end dates
    term_progress = None
    if active_term and active_term.start_date and active_term.end_date and active_term.end_date > active_term.start_date:
        today = timezone.localdate()
        total_days = (active_term.end_date - active_term.start_date).days
        done = min(max((today - active_term.start_date).days, 0), total_days)
        term_progress = {'percent': round(done * 100 / total_days), 'week': done // 7 + 1,
                         'weeks': -(-total_days // 7), 'days_left': total_days - done}

    now = timezone.now()
    total_students = students.count()
    total_classes = classes.count()

    # All counts auto-scoped to current tenant via TenantManager
    context = {
        'active_session':  active_session,
        'active_class':    active_class,
        'active_term':     active_term,
        'total_users':     User.objects.filter(tenant=request.tenant).count(),
        'total_students':  total_students,
        'total_staff':     staff.count(),
        'total_teachers':  staff.filter(role='TEACHER').count(),
        'total_subjects':  Subject.objects.count(),
        'total_classes':   total_classes,
        'total_exams':     exams.count(),
        'published_exams': exams.filter(published=True).count(),
        'live_exams':      exams.filter(published=True, start_time__lte=now, end_time__gte=now).count(),
        'students_per_class': round(total_students / total_classes) if total_classes else None,
        'attendance_today': attendance_today,
        'term_progress':   term_progress,
        'campuses':        campuses,
        'picked_campus':   picked_campus,
        'recent_students': students.select_related('user', 'class_assigned').order_by('-id')[:5],
        'recent_staff':    staff.select_related('user').order_by('-id')[:5],
        'recent_exams':    exams.select_related('subject').prefetch_related('classes').order_by('-id')[:5],
    }
    return render(request, 'dashboard/super_admin.html', context)


# ─── admin dashboard ──────────────────────────────────────────────────────────
def admin_dashboard(request):
    guard = _require_login(request)
    if guard:
        return guard

    from users.models import User, Student, Staff, Subject, Class
    from cbt.models import Exam

    active_session = get_active_session()
    active_term    = get_active_term()
    active_class   = Class.objects.first()

    # All auto-scoped to current tenant via TenantManager
    context = {
        'active_session':  active_session,
        'active_class':    active_class,
        'active_term':     active_term,
        'total_users':     User.objects.filter(tenant=request.tenant).count(),
        'total_students':  Student.objects.count(),
        'total_staff':     Staff.objects.count(),
        'total_subjects':  Subject.objects.count(),
        'total_classes':   Class.objects.count(),
        'total_exams':     Exam.objects.count(),
        'recent_students': Student.objects.select_related('user').order_by('-id')[:5],
        'recent_staff':    Staff.objects.select_related('user').order_by('-id')[:5],
        'recent_exams':    Exam.objects.order_by('-id')[:5],
    }
    return render(request, 'dashboard/admin.html', context)


# ─── teacher dashboard ────────────────────────────────────────────────────────
def teacher_dashboard(request):
    guard = _require_login(request)
    if guard:
        return guard

    from users.models import User, Student, Staff, Subject, Class, StaffSubjectClass

    staff = request.user.staff
    if staff is None:
        return redirect('dashboard:router')

    active_session = get_active_session()
    active_term = get_active_term()
    assignments = StaffSubjectClass.objects.filter(staff=staff)
    my_exams = Exam.objects.filter(created_by=staff)

    context = {
        'active_session': active_session,
        'active_term': active_term,
        'assignments': assignments,
        'my_exams': my_exams,
        'upcoming_exams': my_exams.filter(published=True).order_by('start_time')[:5],
        # School-wide visibility — all auto-scoped to current tenant via TenantManager
        'total_users':     User.objects.filter(tenant=request.tenant).count(),
        'total_students':  Student.objects.count(),
        'total_staff':     Staff.objects.count(),
        'total_subjects':  Subject.objects.count(),
        'total_classes':   Class.objects.count(),
        'total_exams':     Exam.objects.count(),
        'recent_students': Student.objects.select_related('user').order_by('-id')[:5],
        'recent_staff':    Staff.objects.select_related('user').order_by('-id')[:5],
        'recent_exams':    Exam.objects.order_by('-id')[:5],
    }
    return render(request, 'dashboard/teacher.html', context)


# ─── student dashboard ────────────────────────────────────────────────────────
def student_dashboard(request):
    guard = _require_login(request)
    if guard:
        return guard

    student = request.user.student
    if student is None:
        return redirect('dashboard:router')

    courses = Course.objects.filter(
        Q(students=request.user) | Q(class_assigned=student.class_assigned)
    ).distinct()

    now = timezone.now()
    active_session = get_active_session()
    active_term = get_active_term()

    upcoming_exams = Exam.objects.filter(
        classes=student.class_assigned, start_time__gte=now
    ).order_by('start_time')

    next_open_exam = None
    if upcoming_exams.exists():
        next_exam = upcoming_exams.first()
        days_left = max((next_exam.start_time.date() - now.date()).days, 0)
        next_open_exam = {
            'id': next_exam.id, 'title': next_exam.title,
            'days_left': days_left, 'start_time': next_exam.start_time,
            'end_time': next_exam.end_time, 'published': next_exam.published,
        }

    available_exams = Exam.objects.filter(
        classes=student.class_assigned, published=True,
        session=active_session, term=active_term
    ).order_by('start_time')

    attempts = ExamAttempt.objects.filter(student=student)
    completed_exam_ids = attempts.filter(completed=True).values_list('exam_part__exam__id', flat=True)

    exam_list = []
    closed_exams = []
    for exam in available_exams:
        status = 'Open' if exam.is_active else ('Closed' if exam.is_past else 'Upcoming')
        if status == 'Closed':
            closed_exams.append(exam)
        exam_parts = []
        for p in exam.parts.all():
            last = attempts.filter(exam_part=p, completed=True).select_related('exam_part__exam').order_by('-id').first()
            exam_parts.append({'part_type': p.part_type, 'is_completed': last is not None,
                               'review': cbt_engine.review_link(last)})
        exam_list.append({
            'id': exam.id, 'title': exam.title, 'subject': exam.subject.name,
            'start_time': exam.start_time, 'end_time': exam.end_time,
            'status': status, 'completed': exam.id in completed_exam_ids,
            'allow_retake': exam.allow_retake,
            'days_left': max((exam.start_time - now).days, 0) if status == 'Upcoming' else 0,
            'parts': exam_parts,
        })

    assigned_subject_ids = ClassSubject.objects.filter(
        school_class=student.class_assigned
    ).values_list('subject_id', flat=True)

    term_results = TermResult.objects.filter(
        student=student, published=True,
        subject_id__in=assigned_subject_ids
    ).select_related('term', 'session', 'subject')

    has_published_results = TermResult.objects.filter(
        student=student, session=active_session, term=active_term,
        published=True, subject_id__in=assigned_subject_ids
    ).exists() if (active_session and active_term) else False

    results_grouped = {}
    for result in term_results:
        results_grouped.setdefault(result.session.name, {}).setdefault(result.term.name, []).append(result)

    published_periods_qs = (
        TermResult.objects
        .filter(student=student, published=True, subject_id__in=assigned_subject_ids)
        .select_related('session', 'term')
        .values('session__id', 'session__name', 'term__id', 'term__name')
        .distinct().order_by('-session__name', 'term__name')
    )
    seen = set()
    result_periods = []
    for p in published_periods_qs:
        key = (p['session__id'], p['term__id'])
        if key not in seen:
            seen.add(key)
            result_periods.append({
                'session_id': p['session__id'], 'session_name': p['session__name'],
                'term_id': p['term__id'], 'term_name': p['term__name'],
                'is_current': bool(
                    active_session and active_term and
                    p['session__id'] == active_session.id and
                    p['term__id'] == active_term.id
                ),
            })

    total_present = Attendance.objects.filter(student=student, status='P').count()
    total_days = Attendance.objects.filter(student=student).count()
    attendance_percentage = int((total_present / total_days) * 100) if total_days > 0 else 0

    id_card = get_or_create_student_card(student)

    context = {
        'student': student, 'courses': courses, 'exams': exam_list,
        'completed_exams_count': attempts.filter(completed=True).count(),
        'pending_exams': available_exams.count() - attempts.count(),
        'active_session': active_session, 'active_term': active_term,
        'recent_closed_exam': closed_exams[-1] if closed_exams else None,
        'next_open_exam': next_open_exam, 'now': now,
        'term_results': term_results, 'results_grouped': results_grouped,
        'has_published_results': has_published_results,
        'result_periods': result_periods,
        'total_present': total_present, 'total_days': total_days,
        'attendance_percentage': attendance_percentage,
        'id_card': id_card,
    }
    return render(request, 'dashboard/student.html', context)