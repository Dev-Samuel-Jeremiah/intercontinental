"""
users/views_attendance.py
─────────────────────────
Attendance settings, lateness, absence reasons (staff and parents),
students needing follow-up, and calendar heatmaps.
"""
import calendar
from datetime import date, time, timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from academics.utils import get_active_term

from . import attendance as att
from .models import ABSENCE_REASONS, ENROLLED_STATUSES, Attendance, Class, Student
from .views_profile import can_manage, can_view


def _deny(request, what):
    messages.error(request, f'Only {what}.')
    return redirect('dashboard:router')


def _month(raw):
    today = timezone.localdate()
    try:
        y, m = (int(x) for x in (raw or '').split('-'))
        first = date(y, m, 1)
    except (ValueError, TypeError):
        first = today.replace(day=1)
    last = first.replace(day=calendar.monthrange(first.year, first.month)[1])
    return first, min(last, today) if first <= today else last


@login_required
def attendance_settings(request):
    if not can_manage(request.user):
        return _deny(request, 'a school administrator can change attendance settings')
    cfg = att.settings_for(request.tenant)
    if request.method == 'POST':
        try:
            hh, mm = (int(x) for x in (request.POST.get('school_starts_at') or '08:00').split(':')[:2])
            cfg.school_starts_at = time(hh, mm)
        except (ValueError, TypeError):
            messages.error(request, 'Enter the start time like 07:45.')
            return redirect('users:attendance_settings')
        def num(name, lo, hi, default):
            try:
                return max(lo, min(hi, int(request.POST.get(name))))
            except (TypeError, ValueError):
                return default
        cfg.late_grace_minutes = num('late_grace_minutes', 0, 120, cfg.late_grace_minutes)
        cfg.chronic_rate = num('chronic_rate', 50, 100, cfg.chronic_rate)
        cfg.chronic_streak = num('chronic_streak', 2, 30, cfg.chronic_streak)
        cfg.absence_alerts = bool(request.POST.get('absence_alerts'))
        cfg.gate_marks_present = bool(request.POST.get('gate_marks_present'))
        ch = request.POST.get('alert_channel')
        cfg.alert_channel = ch if ch in ('SMS', 'EMAIL', 'BOTH') else 'SMS'
        cfg.save()
        messages.success(request, 'Attendance settings saved.')
        return redirect('users:attendance_settings')
    return render(request, 'attendance/settings.html', {'cfg': cfg})


@login_required
def lateness(request):
    if not can_view(request.user):
        return _deny(request, 'staff can see lateness')
    start, end = _month(request.GET.get('month'))
    cls = Class.objects.filter(pk=request.GET.get('class') or 0).first()
    rows = att.lateness_report(request.tenant, start, end, cls)
    return render(request, 'attendance/lateness.html', {
        'rows': rows, 'start': start, 'end': end, 'cls': cls,
        'classes': Class.objects.order_by('name'),
        'total_days': sum(r['days'] for r in rows), 'total_minutes': sum(r['minutes'] for r in rows),
        'cfg': att.settings_for(request.tenant),
    })


@login_required
def follow_up(request):
    if not can_view(request.user):
        return _deny(request, 'staff can see attendance follow-up')
    term = get_active_term()
    return render(request, 'attendance/follow_up.html', {
        'rows': att.needing_follow_up(request.tenant, term), 'term': term,
        'cfg': att.settings_for(request.tenant),
    })


@login_required
def class_heatmap(request):
    if not can_view(request.user):
        return _deny(request, 'staff can see attendance heatmaps')
    term = get_active_term()
    cls = Class.objects.filter(pk=request.GET.get('class') or 0).first()
    weeks, average, students = [], None, []
    start = term.start_date if term and term.start_date else timezone.localdate().replace(day=1)
    end = min(term.end_date or timezone.localdate(), timezone.localdate()) if term and term.start_date \
        else timezone.localdate()
    if cls:
        weeks, average = att.class_heatmap(cls, start, end)
        for s in Student.objects.filter(class_assigned=cls, status__in=ENROLLED_STATUSES).order_by('full_name'):
            c = att.term_counts(s, term) if term else {'present': 0, 'days': 0, 'late': 0}
            students.append({'student': s, **c,
                             'rate': round(100 * c['present'] / c['days']) if c['days'] else None})
    return render(request, 'attendance/heatmap.html', {
        'classes': Class.objects.order_by('name'), 'cls': cls, 'weeks': weeks,
        'average': average, 'students': students, 'term': term, 'start': start, 'end': end,
    })


@login_required
def absence_reasons(request):
    """Staff: parents' reasons waiting for review, and recording a reason for a child."""
    if not can_view(request.user):
        return _deny(request, 'staff can review absence reasons')
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action in ('accept', 'decline'):
                mark = get_object_or_404(Attendance, pk=request.POST.get('mark_id'))
                att.review_reason(mark, action == 'accept')
                messages.success(request, f"{mark.student.full_name}'s absence on "
                                          f"{mark.date:%d %b} {'excused' if action == 'accept' else 'not excused'}.")
            elif action == 'record':
                student = get_object_or_404(Student, pk=request.POST.get('student_id'))
                day = date.fromisoformat(request.POST.get('date') or '')
                att.give_reason(student, day, request.POST.get('reason'), request.POST.get('note'),
                                by='STAFF', user=request.user)
                messages.success(request, f'Reason recorded for {student.full_name}.')
        except (att.AttendanceError, ValueError) as problem:
            messages.error(request, str(problem) or 'Check the date.')
        return redirect('users:absence_reasons')
    pending = (Attendance.objects.filter(excused='PENDING', subject__isnull=True)
               .select_related('student', 'student__class_assigned').order_by('-date'))
    recent = (Attendance.objects.filter(excused__in=('EXCUSED', 'UNEXCUSED'), subject__isnull=True)
              .select_related('student').order_by('-date')[:30])
    return render(request, 'attendance/reasons.html', {
        'pending': pending, 'recent': recent, 'reasons': ABSENCE_REASONS,
        'students': Student.objects.filter(status__in=ENROLLED_STATUSES).order_by('full_name')
        .only('id', 'full_name', 'admission_number'),
        'today': timezone.localdate(),
    })


@login_required
def parent_absences(request):
    """Parents: explain an absence, or tell the school ahead of time."""
    from .views_achievements import _parent_children
    children = list(_parent_children(request))
    if not children:
        messages.error(request, 'This page is for parents.')
        return redirect('dashboard:router')
    if request.method == 'POST':
        child = next((c for c in children if str(c.id) == request.POST.get('child_id')), None)
        try:
            if not child:
                raise att.AttendanceError('Choose one of your children.')
            day = date.fromisoformat(request.POST.get('date') or '')
            att.give_reason(child, day, request.POST.get('reason'), request.POST.get('note'),
                            by='PARENT', user=request.user)
            messages.success(request, 'Thank you — the school has your reason.')
        except (att.AttendanceError, ValueError) as problem:
            messages.error(request, str(problem) or 'Choose the date.')
        return redirect('users:parent_absences')
    today = timezone.localdate()
    unexplained, history = [], []
    for c in children:
        unexplained += [(c, m) for m in att.unexplained_absences(c)]
    history = (Attendance.objects.filter(student__in=children, subject__isnull=True)
               .exclude(absence_reason='').select_related('student').order_by('-date')[:20])
    return render(request, 'parents/absences.html', {
        'children': children, 'unexplained': unexplained, 'history': history,
        'reasons': ABSENCE_REASONS, 'today': today, 'latest': today + timedelta(days=30),
    })


@login_required
def attendance_certificate(request, student_id):
    """A printable certificate of a student's attendance over a period."""
    if not can_manage(request.user):
        return _deny(request, 'a school administrator can issue attendance certificates')
    student = get_object_or_404(Student.objects.select_related('class_assigned'), pk=student_id)
    term = get_active_term()
    try:
        start = date.fromisoformat(request.GET.get('start') or '')
    except ValueError:
        start = term.start_date if term and term.start_date else timezone.localdate().replace(month=1, day=1)
    try:
        end = date.fromisoformat(request.GET.get('end') or '')
    except ValueError:
        end = timezone.localdate()
    cert, problem = None, ''
    try:
        cert = att.certificate(student, start, end, request.GET.get('purpose') or '')
    except att.AttendanceError as e:
        problem = str(e)
    verify_url = ''
    if cert:
        from django.urls import reverse
        verify_url = request.build_absolute_uri(reverse('users:verify_attendance', args=[cert['reference']]))
    return render(request, 'attendance/certificate.html', {
        'student': student, 'cert': cert, 'problem': problem, 'start': start, 'end': end,
        'purpose': request.GET.get('purpose') or '', 'school': request.tenant, 'verify_url': verify_url,
    })


def verify_attendance(request, reference):
    """Public: anyone holding a certificate can check it is genuine."""
    cert = att.verify_certificate(reference, request.tenant) if getattr(request, 'tenant', None) else None
    return render(request, 'attendance/verify.html', {'cert': cert, 'school': getattr(request, 'tenant', None)},
                  status=200 if cert else 404)


def certificate_qr(request, reference):
    from django.http import HttpResponse
    from django.urls import reverse
    from idcards.utils import qr_png_bytes
    url = request.build_absolute_uri(reverse('users:verify_attendance', args=[reference]))
    return HttpResponse(qr_png_bytes(url), content_type='image/png')
