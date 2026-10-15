"""
analytics/views.py
──────────────────
The principal's command dashboard, fee collection, enrolment and retention,
demographics, cohort comparison, click-through student lists, KPI targets,
the activity feed, the board pack and summary emails. Any table can be
downloaded with ?export=csv or ?export=xlsx.
"""
from datetime import date, timedelta
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.utils import timezone

from users.models import LEFT_STATUSES, Attendance, Class, Student
from users.views_profile import can_manage, can_see_fees

from . import export, metrics, reports
from .models import METRICS, KPITarget, ReportSubscription


def _allowed(user):
    return can_manage(user) or can_see_fees(user)


def _deny(request):
    messages.error(request, 'Only administrators and the bursary can see school analytics.')
    return redirect('dashboard:router')


def _pk(raw):
    raw = str(raw or '')
    return int(raw) if raw.isdigit() else 0


def _money(v):
    return float(v) if isinstance(v, Decimal) else v


# ─────────────────────────────────────────────────────────────────────────────
# F181 — command dashboard
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def home(request):
    if not _allowed(request.user):
        return _deny(request)
    from curriculum.services import upcoming
    t = request.tenant
    fees = can_see_fees(request.user)
    return render(request, 'analytics/home.html', {
        'today': metrics.attendance_today(t), 'kpis': metrics.kpis(t), 'results': metrics.results_summary(t),
        'fee': metrics.fee_position(t) if fees else None, 'cash': metrics.collections(t) if fees else None,
        'events': upcoming(t, 'STAFF', days=21, limit=6), 'feed': metrics.activity(t, limit=8), 'fees': fees,
        'roll': metrics.on_roll(t).count(), 'is_admin': can_manage(request.user)})


# ─────────────────────────────────────────────────────────────────────────────
# F185 — fee collection
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def fees(request):
    if not can_see_fees(request.user):
        return _deny(request)
    t = request.tenant
    by_class = metrics.collection_by_class(t)
    fmt = export.wanted(request)
    if fmt:
        return export.respond(fmt, 'Fee collection by class', ['Class', 'Students', 'Expected', 'Paid', 'Outstanding',
                                                               'Collected %', 'Families owing'],
                              [[r['name'], r['students'], _money(r['expected']), _money(r['paid']),
                                _money(r['outstanding']), r['rate'], r['debtors']] for r in by_class], t.name)
    return render(request, 'analytics/fees.html', {
        'fee': metrics.fee_position(t), 'cash': metrics.collections(t, weeks=10), 'by_class': by_class,
        'ageing': metrics.debt_ageing(t)})


# ─────────────────────────────────────────────────────────────────────────────
# F184 / F199 — enrolment and retention
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def enrolment(request):
    if not _allowed(request.user):
        return _deny(request)
    t = request.tenant
    series, ahead = metrics.enrolment_series(t, 24)
    movement = metrics.joiners_and_leavers(t, 12)
    fmt = export.wanted(request)
    if fmt:
        return export.respond(fmt, 'Enrolment by month', ['Month', 'On roll', 'Joined', 'Left'],
                              [[p['month'].strftime('%b %Y'), p['count'],
                                next((m['joined'] for m in movement if m['month'] == p['month']), ''),
                                next((m['left'] for m in movement if m['month'] == p['month']), '')] for p in series],
                              t.name)
    peak = max([m['joined'] for m in movement] + [m['left'] for m in movement] + [1])
    for m in movement:
        m['jw'], m['lw'] = round(100 * m['joined'] / peak), round(100 * m['left'] / peak)
    return render(request, 'analytics/enrolment.html', {
        'series': series, 'ahead': ahead, 'levels': metrics.by_level(t), 'movement': movement,
        'retention': metrics.retention(t, 12), 'roll': metrics.on_roll(t).count(),
        'change': (series[-1]['count'] - series[-13]['count']) if len(series) > 12 else None})


# ─────────────────────────────────────────────────────────────────────────────
# F186 — demographics
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def demographics(request):
    if not _allowed(request.user):
        return _deny(request)
    d = metrics.demographics(request.tenant)
    fmt = export.wanted(request)
    if fmt:
        rows = [[r['name'], r['male'], r['female'], r['unspecified'], r['total'], *r['ages']] for r in d['rows']]
        rows.append(['All classes', d['totals']['male'], d['totals']['female'], d['totals']['unspecified'],
                     d['totals']['total'], *d['totals']['ages']])
        return export.respond(fmt, 'Students by gender and age', ['Class', 'Boys', 'Girls', 'Not recorded', 'Total',
                                                                  *d['bands']], rows, request.tenant.name)
    return render(request, 'analytics/demographics.html', {'d': d, 'today': timezone.localdate()})


# ─────────────────────────────────────────────────────────────────────────────
# F189 — cohort comparison
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def cohort(request):
    if not _allowed(request.user):
        return _deny(request)
    t = request.tenant
    levels = metrics.levels(t)
    level = request.GET.get('level') if request.GET.get('level') in levels else (levels[0] if levels else '')
    term = request.GET.get('term') if request.GET.get('term') in ('1st Term', '2nd Term', '3rd Term') else '1st Term'
    data = metrics.cohort(t, level, term) if level else None
    fmt = export.wanted(request)
    if fmt and data:
        return export.respond(fmt, f'{level} {term} by session', ['Session', 'Students', 'Average', 'Pass rate',
                                                                  *data['subjects']],
                              [[s['session'], s['students'], s['average'], s['pass_rate'], *s['subjects']]
                               for s in data['sessions']], t.name)
    return render(request, 'analytics/cohort.html', {'levels': levels, 'level': level, 'term': term, 'data': data,
                                                     'terms': ['1st Term', '2nd Term', '3rd Term']})


# ─────────────────────────────────────────────────────────────────────────────
# F190 — from a number to the students behind it
# ─────────────────────────────────────────────────────────────────────────────

def drill(tenant, params):
    """(title, students queryset or list, extra column name, {student_id: extra value})."""
    from users.models import ENROLLED_STATUSES
    qs = Student.objects.filter(tenant=tenant).select_related('class_assigned').order_by('class_assigned__name',
                                                                                         'full_name')
    bits, extra_name, extra = [], '', {}
    if params.get('left'):
        months = _pk(params.get('left')) or 12
        since = timezone.localdate() - timedelta(days=30 * months)
        qs = [s for s in qs.filter(status__in=LEFT_STATUSES) if (s.left_on or s.status_changed_on or date.min) >= since]
        if params.get('status') in LEFT_STATUSES:
            qs = [s for s in qs if s.status == params['status']]
        extra_name = 'Left'
        extra = {s.id: f'{s.status} {(s.left_on or s.status_changed_on or "")}' for s in qs}
        return f'Students who left in the last {months} months', qs, extra_name, extra
    qs = qs.filter(status__in=ENROLLED_STATUSES)
    cls = Class.objects.filter(tenant=tenant, pk=_pk(params.get('class'))).first()
    if cls:
        qs = qs.filter(class_assigned=cls)
        bits.append(cls.name)
    if params.get('gender') in ('Male', 'Female'):
        qs = qs.filter(gender=params['gender'])
        bits.append('boys' if params['gender'] == 'Male' else 'girls')
    elif params.get('gender') == 'none':
        qs = qs.exclude(gender__in=('Male', 'Female'))
        bits.append('gender not recorded')
    if params.get('age'):
        qs = [s for s in qs if metrics.age_band(s.age) == params['age']]
        bits.append(f'aged {params["age"]}')
    if params.get('absent'):
        try:
            day = date.fromisoformat(params['absent'])
        except ValueError:
            day = timezone.localdate()
        absent = dict(Attendance.objects.filter(tenant=tenant, date=day, status='A', subject__isnull=True)
                      .values_list('student_id', 'remarks'))
        qs = [s for s in qs if s.id in absent]
        bits.append(f'absent on {day:%d %b}')
    if params.get('owing'):
        from finance.models import ChildBilling
        session, term = metrics.active_period(tenant)
        balances = {}
        if session and term:
            for b in ChildBilling.objects.filter(tenant=tenant, parent_billing__session=session,
                                                 parent_billing__term=term):
                if b.balance > 0:
                    balances[b.student_id] = b.balance
        qs = sorted((s for s in qs if s.id in balances), key=lambda s: -balances[s.id])
        extra_name, extra = 'Owes', {k: f'₦{v:,.2f}' for k, v in balances.items()}
        bits.append('owing fees')
    title = 'Students' + (' — ' + ', '.join(bits) if bits else '')
    return title, qs, extra_name, extra


@login_required
def students(request):
    if not _allowed(request.user):
        return _deny(request)
    if request.GET.get('owing') and not can_see_fees(request.user):
        return _deny(request)
    title, qs, extra_name, extra = drill(request.tenant, request.GET)
    rows = [[s.full_name, s.admission_number, s.class_assigned.name if s.class_assigned else '', s.gender or '',
             s.age if s.age is not None else '', s.parent_phone or '', *([extra.get(s.id, '')] if extra_name else [])]
            for s in qs]
    columns = ['Name', 'Admission no.', 'Class', 'Gender', 'Age', 'Parent phone'] + ([extra_name] if extra_name else [])
    fmt = export.wanted(request)
    if fmt:
        return export.respond(fmt, title, columns, rows, request.tenant.name)
    query = request.GET.urlencode()
    return render(request, 'analytics/students.html', {'title': title, 'columns': columns, 'rows': rows,
                                                       'query': query})


# ─────────────────────────────────────────────────────────────────────────────
# F193 — targets
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def kpis(request):
    if not _allowed(request.user):
        return _deny(request)
    if request.method == 'POST':
        if not can_manage(request.user):
            return _deny(request)
        for key, _ in METRICS:
            raw = (request.POST.get(key) or '').replace(',', '').replace('%', '').strip()
            if not raw:
                KPITarget.objects.filter(tenant=request.tenant, metric=key).delete()
                continue
            try:
                value = Decimal(raw)
            except Exception:
                messages.error(request, f'"{raw}" is not a number.')
                continue
            if value < 0 or (key != 'enrolment' and value > 100):
                messages.error(request, 'Percentages must be between 0 and 100.')
                continue
            KPITarget.objects.update_or_create(tenant=request.tenant, metric=key, defaults={'target': value})
        messages.success(request, 'Targets saved.')
        return redirect('analytics:kpis')
    return render(request, 'analytics/kpis.html', {'kpis': metrics.kpis(request.tenant),
                                                   'is_admin': can_manage(request.user)})


# ─────────────────────────────────────────────────────────────────────────────
# F198 — activity
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def activity(request):
    if not can_manage(request.user):
        return _deny(request)
    return render(request, 'analytics/activity.html', {'feed': metrics.activity(request.tenant, limit=100, days=30)})


# ─────────────────────────────────────────────────────────────────────────────
# F200 — board pack
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def board_pack(request):
    if not can_manage(request.user):
        return _deny(request)
    t = request.tenant
    session, term = metrics.active_period(t)
    series, ahead = metrics.enrolment_series(t, 12)
    colour = getattr(t, 'highlight_color', '') or '#0a1f44'
    return render(request, 'analytics/board_pack.html', {
        'school': t, 'session': session, 'term': term, 'today': timezone.localdate(), 'colour': colour,
        'kpis': metrics.kpis(t), 'fee': metrics.fee_position(t), 'by_class': metrics.collection_by_class(t),
        'ageing': metrics.debt_ageing(t), 'results': metrics.results_summary(t), 'series': series, 'ahead': ahead,
        'levels': metrics.by_level(t), 'retention': metrics.retention(t, 6), 'demo': metrics.demographics(t),
        'attendance': metrics.attendance_rate(t, 30), 'roll': metrics.on_roll(t).count()})


# ─────────────────────────────────────────────────────────────────────────────
# F183 — summary emails
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def email_reports(request):
    if not can_manage(request.user):
        return _deny(request)
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'add':
            email = (request.POST.get('email') or '').strip().lower()
            freq = request.POST.get('frequency') if request.POST.get('frequency') in ('WEEKLY', 'MONTHLY') else 'WEEKLY'
            if '@' not in email:
                messages.error(request, 'Enter an email address.')
            else:
                ReportSubscription.objects.update_or_create(tenant=request.tenant, email=email,
                                                            defaults={'frequency': freq, 'active': True,
                                                                      'created_by': request.user})
                messages.success(request, f'{email} will get the summary.')
        else:
            sub = ReportSubscription.objects.filter(tenant=request.tenant, pk=_pk(request.POST.get('id'))).first()
            if sub and action == 'remove':
                sub.delete()
                messages.success(request, 'Removed.')
            elif sub and action == 'send':
                if reports.send(sub):
                    messages.success(request, f'Sent to {sub.email}.')
                else:
                    messages.error(request, f'Could not send to {sub.email} — check the email settings.')
        return redirect('analytics:email_reports')
    return render(request, 'analytics/email_reports.html', {
        'subs': ReportSubscription.objects.filter(tenant=request.tenant),
        'preview': reports.summary(request.tenant)})
