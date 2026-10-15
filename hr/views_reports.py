"""
hr/views_reports.py
───────────────────
Staff work reports: "My work reports" and the report form for every staff
member; the daily/weekly register, reading and reviewing for administrators.
"""
from datetime import date

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from users.views_profile import can_manage

from . import reports as rpt
from .models import StaffReport
from .views import _deny, _me, _serve

R = StaffReport


def _period(raw):
    return R.WEEKLY if (raw or '').upper() == R.WEEKLY else R.DAILY


def _day(raw):
    try:
        return date.fromisoformat(raw or '')
    except ValueError:
        return timezone.localdate()


@login_required
def my_reports(request):
    me = _me(request)
    if not me:
        return _deny(request, 'Only staff write work reports.')
    today = timezone.localdate()
    current = {p: rpt.report_for(me, p, today) for p in (R.DAILY, R.WEEKLY)}
    mine = R.objects.filter(tenant=request.tenant, staff=me).select_related('reviewed_by')
    page = Paginator(mine, 15).get_page(request.GET.get('page'))
    return render(request, 'hr/reports_mine.html', {
        'me': me, 'today_report': current[R.DAILY], 'week_report': current[R.WEEKLY], 'page': page,
        'today_label': rpt.label(R.DAILY, today), 'week_label': rpt.label(R.WEEKLY, rpt.bounds(R.WEEKLY, today)[0]),
        'reviewed_count': mine.filter(status=R.REVIEWED).count(), 'total': mine.exclude(status=R.DRAFT).count(),
    })


@login_required
def write_report(request):
    me = _me(request)
    if not me:
        return _deny(request, 'Only staff write work reports.')
    period, day = _period(request.GET.get('period') or request.POST.get('period')), _day(
        request.GET.get('date') or request.POST.get('date'))
    report = rpt.report_for(me, period, day)
    if report.pk and not report.editable:
        return redirect('hr:report', report.pk)
    if request.method == 'POST':
        submit = request.POST.get('action') == 'submit'
        try:
            rpt.save(report, request.POST, request.FILES.get('attachment'), submit=submit,
                     remove_file=bool(request.POST.get('remove_attachment')))
        except rpt.ReportError as problem:
            messages.error(request, str(problem))
        else:
            if submit:
                messages.success(request, 'Report submitted — the administrators have been notified.')
                return redirect('hr:report', report.pk)
            messages.success(request, 'Draft saved. Only you can see it until you submit.')
            return redirect(f"{request.path}?period={period.lower()}&date={report.period_start.isoformat()}")
    today = timezone.localdate()
    start = report.period_start
    following = rpt.shift(period, start, 1)
    return render(request, 'hr/report_write.html', {
        'report': report, 'period': period, 'period_label': rpt.label(period, start), 'me': me,
        'prev': rpt.shift(period, start, -1),
        'next': following if following <= rpt.bounds(period, today)[0] else None,
        'fields': [(name, label, hint, required, request.POST.get(name, getattr(report, name)))
                   for name, label, hint, required in _form_fields(period)],
    })


def _form_fields(period):
    """(field, label, hint, required) for the report form."""
    return [
        ('work_done', 'What I did', 'The main tasks you carried out — classes taught, duties, meetings, '
                                    'marking, admin work.', True),
        ('highlights', 'Highlights & achievements', 'Anything that went especially well.', False),
        ('challenges', 'Challenges', 'Problems you met and how you handled them.', False),
        ('support_needed', 'Support I need', 'Anything the school can do to help — resources, approvals, '
                                             'follow-up.', False),
        ('next_plan', f"Plan for {'tomorrow' if period == R.DAILY else 'next week'}",
         'What you will focus on next.', False),
    ]


@login_required
def report_detail(request, pk):
    report = get_object_or_404(StaffReport.objects.select_related('staff', 'reviewed_by'), pk=pk, tenant=request.tenant)
    admin = can_manage(request.user)
    mine = report.staff.user_id == request.user.pk
    if not (mine or admin) or (report.status == R.DRAFT and not mine):
        return _deny(request, 'You can only open your own reports.')
    if request.method == 'POST' and admin and not mine:
        try:
            rpt.review(report, request.user, request.POST.get('feedback'))
        except rpt.ReportError as problem:
            messages.error(request, str(problem))
        else:
            messages.success(request, f'Marked as reviewed — {report.staff.full_name} has been notified.')
        return redirect('hr:report', report.pk)
    history = (R.objects.filter(tenant=request.tenant, staff=report.staff).exclude(pk=report.pk)
               .exclude(status=R.DRAFT)[:6]) if admin else []
    return render(request, 'hr/report_detail.html', {
        'report': report, 'admin': admin, 'mine': mine, 'history': history,
        'period_label': rpt.label(report.period, report.period_start),
        'sections': [(label, getattr(report, f)) for f, label in (
            ('work_done', 'What I did'), ('highlights', 'Highlights & achievements'), ('challenges', 'Challenges'),
            ('support_needed', 'Support needed'), ('next_plan', 'Plan for the next period'))],
    })


@login_required
def report_attachment(request, pk):
    report = get_object_or_404(StaffReport, pk=pk, tenant=request.tenant)
    if not (report.staff.user_id == request.user.pk or (can_manage(request.user) and report.status != R.DRAFT)):
        return _deny(request, 'You can only open your own reports.')
    return _serve(report.attachment)


@login_required
def staff_reports(request):
    if not can_manage(request.user):
        return _deny(request, 'Only administrators can see every staff report.')
    period, day = _period(request.GET.get('period')), _day(request.GET.get('date'))
    staff_qs = None
    from saas_admin.models import Campus
    campus = Campus.objects.filter(tenant=request.tenant, pk=request.session.get('campus_id')).first()
    if campus:
        staff_qs = campus.staff.all()
    rows, counts, closed, (start, end) = rpt.register(request.tenant, period, day, staff_qs)
    today = timezone.localdate()
    nxt = rpt.shift(period, start, 1)
    return render(request, 'hr/reports_admin.html', {
        'rows': rows, 'counts': counts, 'closed': closed, 'period': period, 'start': start, 'end': end,
        'period_label': rpt.label(period, start), 'campus': campus,
        'prev': rpt.shift(period, start, -1), 'next': nxt if nxt <= rpt.bounds(period, today)[0] else None,
        'is_current': start == rpt.bounds(period, today)[0],
    })
