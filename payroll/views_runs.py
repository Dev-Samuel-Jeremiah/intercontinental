"""
payroll/views_runs.py
─────────────────────
Payroll runs with the prepare → approve → release chain, payslips, the bank
file and remittance schedules, staff pay details and loans, payroll
settings, and the staff self-service page.
"""
from datetime import date
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from analytics import export
from users.models import Staff
from users.views_profile import can_manage

from . import engine, runs
from .models import PayDetailsRequest, PayrollRun, Payslip, StaffLoan, StaffPayroll


def _deny(request, text='Only the bursary and administrators can use payroll.'):
    messages.error(request, text)
    return redirect('dashboard:router')


def _pk(raw):
    raw = str(raw or '')
    return int(raw) if raw.isdigit() else 0


def _money(raw, default='0'):
    try:
        v = Decimal(str(raw if raw not in (None, '') else default).replace(',', '').replace('₦', '').strip())
    except InvalidOperation:
        raise runs.RunError(f'"{raw}" is not an amount.')
    if v < 0:
        raise runs.RunError('Amounts cannot be negative.')
    return engine.money(v)


# ─────────────────────────────────────────────────────────────────────────────
# Runs
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def run_list(request):
    if not runs.payroll_staff(request.user):
        return _deny(request)
    if request.method == 'POST':
        try:
            run = runs.create(request.tenant, request.POST.get('kind'), _pk(request.POST.get('month')),
                              _pk(request.POST.get('year')), request.user, request.POST.get('bonus_percent') or 0,
                              request.POST.get('title'))
            messages.success(request, f'{run} prepared for {run.payslips.count()} staff. Check it, then send it for approval.')
            return redirect('payroll:run', pk=run.pk)
        except (runs.RunError, InvalidOperation) as problem:
            messages.error(request, str(problem))
        return redirect('payroll:runs')
    today = timezone.localdate()
    items = list(PayrollRun.objects.filter(tenant=request.tenant).select_related('prepared_by', 'approved_by'))
    for r in items:
        r.money = runs.totals(r)
        r.count = r.payslips.count()
    return render(request, 'payroll/runs.html', {
        'runs': items, 'kinds': PayrollRun.KINDS, 'month': today.month, 'year': today.year,
        'months': [(i, date(2000, i, 1).strftime('%B')) for i in range(1, 13)],
        'on_payroll': StaffPayroll.objects.filter(tenant=request.tenant, is_active=True).count(),
        'missing': Staff.objects.filter(tenant=request.tenant).exclude(
            pk__in=StaffPayroll.objects.filter(tenant=request.tenant).values('staff_id')).order_by('full_name')})


@login_required
def run_detail(request, pk):
    if not runs.payroll_staff(request.user):
        return _deny(request)
    run = get_object_or_404(PayrollRun.objects.select_related('prepared_by', 'approved_by', 'paid_by'),
                            pk=pk, tenant=request.tenant)
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action == 'adjust':
                slip = get_object_or_404(Payslip, pk=_pk(request.POST.get('slip')), run=run)
                runs.adjust(slip, request.POST.get('extra_pay'), request.POST.get('other_deductions'),
                            request.POST.get('note'))
                messages.success(request, f'{slip.staff_payroll.staff.full_name}: recalculated.')
            elif action == 'rebuild':
                runs.build(run)
                messages.success(request, 'Recalculated from the latest pay details, loans and leave.')
            elif action == 'submit':
                runs.submit(run, request.user)
                messages.success(request, 'Sent for approval.')
            elif action == 'approve':
                runs.approve(run, request.user)
                messages.success(request, 'Approved. It can now be released for payment.')
            elif action == 'send_back':
                runs.send_back(run, request.user, request.POST.get('note'))
                messages.success(request, 'Sent back for changes.')
            elif action == 'release':
                runs.release(run, request.user)
                messages.success(request, 'Payment released. Download the bank file; payslips are being emailed.')
            elif action == 'cancel':
                runs.cancel(run, request.user)
                messages.success(request, 'Run cancelled.')
            elif action == 'email':
                n = runs.email_payslips(run.pk)
                messages.success(request, f'{n} payslip{"s" if n != 1 else ""} emailed.')
        except runs.RunError as problem:
            messages.error(request, str(problem))
        return redirect('payroll:run', pk=run.pk)
    s = runs.settings_for(request.tenant)
    me_prepared = run.prepared_by_id == request.user.id
    return render(request, 'payroll/run.html', {
        'run': run, 'slips': run.payslips.select_related('staff_payroll__staff'), 'money': runs.totals(run),
        'problems': runs.problems(run), 'is_admin': can_manage(request.user), 'me_prepared': me_prepared,
        'second': s.separate_approver})


@login_required
def run_download(request, pk, what):
    if not runs.payroll_staff(request.user):
        return _deny(request)
    run = get_object_or_404(PayrollRun, pk=pk, tenant=request.tenant)
    fmt = export.wanted(request) or 'csv'
    if what == 'bank':
        if run.status not in (PayrollRun.APPROVED, PayrollRun.PAID):
            return _deny(request, 'The bank file is available once the run is approved.')
        columns, rows = runs.bank_rows(run)
        return export.respond(fmt, f'Bank file {run}', columns, rows, request.tenant.name)
    if what in ('pension', 'paye', 'nhf'):
        title, columns, rows = runs.schedule(run, what)
        return export.respond(fmt, f'{title} {run}', columns, rows, request.tenant.name)
    if what == 'register':
        cols = ['Name', 'Basic', 'Housing', 'Transport', 'Other allowances', 'Bonus', 'Extra pay', 'Extra lessons',
                'Cover', 'Unpaid leave',
                'Gross', 'Pension (employee)', 'NHF', 'PAYE', 'Union dues', 'Loan', 'Other deductions', 'Net',
                'Pension (employer)']
        rows = [[s.staff_payroll.staff.full_name] + [float(getattr(s, f)) for f in (
            'basic', 'housing', 'transport', 'other_allowances', 'bonus', 'extra_pay', 'extra_lessons', 'cover_pay',
            'unpaid_leave', 'gross',
            'pension_employee', 'nhf', 'paye', 'union_dues', 'loan_repayment', 'other_deductions', 'net',
            'pension_employer')] for s in run.payslips.select_related('staff_payroll__staff')]
        return export.respond(fmt, f'Payroll register {run}', cols, rows, request.tenant.name)
    raise Http404


@login_required
def payslip(request, pk):
    slip = get_object_or_404(Payslip.objects.select_related('run', 'staff_payroll__staff'), pk=pk,
                             tenant=request.tenant)
    own = slip.staff_payroll.staff.user_id == request.user.id
    if not (runs.payroll_staff(request.user) or (own and slip.run.status == PayrollRun.PAID)):
        return _deny(request, 'You can only see your own payslips once they are paid.')
    return render(request, 'payroll/payslip_v2.html', {'slip': slip, 'run': slip.run, 'school': request.tenant,
                                                       'printable': True})


# ─────────────────────────────────────────────────────────────────────────────
# Staff pay details and loans
# ─────────────────────────────────────────────────────────────────────────────

TEXT_FIELDS = ['bank_name', 'account_no', 'account_name', 'tax_id', 'pension_pin', 'pfa_name', 'nhf_number']
MONEY_FIELDS = ['basic_salary', 'housing', 'transport', 'other_allowances', 'annual_rent', 'other_reliefs',
                'union_dues']
FLAGS = ['pays_tax', 'pays_pension', 'pays_nhf', 'is_active']


@login_required
def staff_pay(request, staff_id):
    if not runs.payroll_staff(request.user):
        return _deny(request)
    staff = get_object_or_404(Staff, pk=staff_id, tenant=request.tenant)
    pr = StaffPayroll.objects.filter(staff=staff).first() or StaffPayroll(tenant=request.tenant, staff=staff)
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action == 'loan':
                if not pr.pk:
                    raise runs.RunError('Save the pay details first.')
                amount = _money(request.POST.get('amount'))
                monthly = _money(request.POST.get('monthly_repayment'))
                if amount <= 0 or monthly <= 0:
                    raise runs.RunError('Give the amount and the monthly repayment.')
                today = timezone.localdate()
                StaffLoan.objects.create(
                    tenant=request.tenant, staff_payroll=pr, amount=amount, balance=amount, monthly_repayment=monthly,
                    kind=request.POST.get('kind') if request.POST.get('kind') in ('LOAN', 'ADVANCE') else 'LOAN',
                    start_month=_pk(request.POST.get('start_month')) or today.month,
                    start_year=_pk(request.POST.get('start_year')) or today.year,
                    note=(request.POST.get('note') or '').strip()[:200], created_by=request.user)
                messages.success(request, f'₦{amount:,.2f} recorded; ₦{monthly:,.2f} comes off each salary until it is repaid.')
            elif action == 'stop_loan':
                loan = get_object_or_404(StaffLoan, pk=_pk(request.POST.get('loan')), staff_payroll=pr)
                loan.active = False
                loan.save(update_fields=['active'])
                messages.success(request, 'Repayments stopped.')
            else:
                for f in TEXT_FIELDS:
                    setattr(pr, f, (request.POST.get(f) or '').strip()[:200])
                for f in MONEY_FIELDS:
                    setattr(pr, f, _money(request.POST.get(f)))
                for f in FLAGS:
                    setattr(pr, f, bool(request.POST.get(f)))
                pr.save()
                messages.success(request, f'Pay details saved for {staff.full_name}.')
        except runs.RunError as problem:
            messages.error(request, str(problem))
        return redirect('payroll:staff_pay', staff_id=staff.pk)
    preview = None
    if pr.pk:
        dummy = PayrollRun(tenant=request.tenant, kind=PayrollRun.MONTHLY, month=timezone.localdate().month,
                           year=timezone.localdate().year)
        preview, _ = engine.compute(pr, dummy)
    return render(request, 'payroll/staff_pay.html', {
        'staff': staff, 'pr': pr, 'preview': preview, 'loans': pr.loans.all() if pr.pk else [],
        'slips': pr.payslips.select_related('run').order_by('-run__year', '-run__month')[:12] if pr.pk else []})


@login_required
def payroll_settings(request):
    if not can_manage(request.user):
        return _deny(request, 'Only administrators can change payroll settings.')
    s = runs.settings_for(request.tenant)
    if request.method == 'POST':
        try:
            for f in ('pension_employee_rate', 'pension_employer_rate', 'nhf_rate'):
                v = _money(request.POST.get(f))
                if v > 100:
                    raise runs.RunError('Rates are percentages, at most 100.')
                setattr(s, f, v)
            s.minimum_wage = _money(request.POST.get('minimum_wage'))
            s.cover_rate = _money(request.POST.get('cover_rate'))
            s.deduct_unpaid_leave = bool(request.POST.get('deduct_unpaid_leave'))
            s.separate_approver = bool(request.POST.get('separate_approver'))
            s.bank_narration = (request.POST.get('bank_narration') or 'Salary').strip()[:60]
            s.save()
            messages.success(request, 'Payroll settings saved.')
        except runs.RunError as problem:
            messages.error(request, str(problem))
        return redirect('payroll:settings')
    return render(request, 'payroll/settings.html', {'s': s, 'bands': engine.BANDS})


# ─────────────────────────────────────────────────────────────────────────────
# F234 — staff self-service
# ─────────────────────────────────────────────────────────────────────────────

REQUESTABLE = ['bank_name', 'account_no', 'account_name', 'tax_id', 'pension_pin', 'pfa_name', 'nhf_number',
               'annual_rent']


@login_required
def me(request):
    staff = request.user.staff_profiles.filter(tenant=request.tenant).first()
    if not staff:
        return _deny(request, 'This page is for staff.')
    pr = StaffPayroll.objects.filter(staff=staff).first()
    if request.method == 'POST' and pr:
        changes = {}
        for f in REQUESTABLE:
            v = (request.POST.get(f) or '').strip()[:200]
            if v and v != str(getattr(pr, f) or ''):
                changes[f] = v
        if 'annual_rent' in changes:
            try:
                changes['annual_rent'] = str(_money(changes['annual_rent']))
            except runs.RunError as problem:
                messages.error(request, str(problem))
                return redirect('payroll:me')
        if changes:
            PayDetailsRequest.objects.create(tenant=request.tenant, staff_payroll=pr, changes=changes)
            messages.success(request, 'Sent to the bursary. Your details change once they check them.')
        else:
            messages.error(request, 'Nothing was changed.')
        return redirect('payroll:me')
    from safety.staff_attendance import days_taken
    year = timezone.localdate().year
    slips = (Payslip.objects.filter(staff_payroll=pr, run__status=PayrollRun.PAID).select_related('run')
             .order_by('-run__year', '-run__month', '-run_id')[:24] if pr else [])
    return render(request, 'payroll/me.html', {
        'staff': staff, 'pr': pr, 'slips': slips, 'leave': days_taken(staff, year), 'year': year,
        'loans': pr.loans.filter(active=True) if pr else [],
        'pending': PayDetailsRequest.objects.filter(staff_payroll=pr, status='PENDING').first() if pr else None})


@login_required
def change_requests(request):
    if not runs.payroll_staff(request.user):
        return _deny(request)
    if request.method == 'POST':
        req = get_object_or_404(PayDetailsRequest, pk=_pk(request.POST.get('id')), tenant=request.tenant,
                                status='PENDING')
        if request.POST.get('action') == 'approve':
            pr = req.staff_payroll
            for f, v in req.changes.items():
                if f in REQUESTABLE:
                    setattr(pr, f, Decimal(v) if f == 'annual_rent' else v)
            pr.save()
            req.status = 'APPROVED'
            messages.success(request, f'{pr.staff.full_name}\'s details updated.')
        else:
            req.status = 'REJECTED'
            messages.success(request, 'Declined.')
        req.reviewed_by, req.reviewed_at = request.user, timezone.now()
        req.save()
        return redirect('payroll:change_requests')
    return render(request, 'payroll/change_requests.html', {
        'pending': PayDetailsRequest.objects.filter(tenant=request.tenant, status='PENDING')
        .select_related('staff_payroll__staff'), 'labels': dict(
            bank_name='Bank', account_no='Account number', account_name='Account name', tax_id='Tax ID',
            pension_pin='Pension PIN', pfa_name='PFA', nhf_number='NHF number', annual_rent='Yearly rent')})

