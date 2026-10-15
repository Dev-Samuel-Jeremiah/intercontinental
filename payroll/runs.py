"""
payroll/runs.py
───────────────
A payroll run goes through three hands (F239):

    bursar prepares  →  submits  →  principal approves  →  payment is released
                         ↖────────── sent back with a note ─┘

Monthly runs pay salary; bonus and 13th-month runs (F238) pay a percent of
basic, taxed at the margin. When a run is released, loan repayments are
posted, the bank file is ready (F228) and payslips are emailed (F227).
"""
import csv
import io
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from users.views_profile import can_manage

from . import engine
from .models import LoanRepayment, PayrollRun, PayrollSettings, Payslip, StaffPayroll

ZERO = Decimal('0')


class RunError(ValueError):
    pass


def payroll_staff(user):
    if not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    staff = getattr(user, 'staff', None)
    return bool(staff and staff.role in ('ADMIN', 'ACCOUNT'))


def settings_for(tenant):
    obj, _ = PayrollSettings.objects.get_or_create(tenant=tenant)
    return obj


def create(tenant, kind, month, year, user, bonus_percent=0, title=''):
    if kind not in dict(PayrollRun.KINDS):
        raise RunError('Choose the kind of run.')
    if not (1 <= int(month) <= 12) or not (2000 <= int(year) <= 2100):
        raise RunError('Choose a month and year.')
    if kind == PayrollRun.MONTHLY and PayrollRun.objects.filter(
            tenant=tenant, kind=kind, month=month, year=year).exclude(status=PayrollRun.CANCELLED).exists():
        raise RunError('There is already a salary run for that month.')
    pct = Decimal(str(bonus_percent or 0))
    if kind == PayrollRun.THIRTEENTH:
        pct = Decimal('100')
    if kind != PayrollRun.MONTHLY and pct <= 0:
        raise RunError('Give the bonus as a percent of monthly basic, e.g. 50.')
    run = PayrollRun.objects.create(tenant=tenant, kind=kind, month=month, year=year, bonus_percent=pct,
                                    title=(title or '').strip()[:120], prepared_by=user)
    build(run)
    return run


@transaction.atomic
def build(run):
    """(Re)work out every payslip, keeping extra pay, other deductions and notes typed in by hand."""
    if run.status != PayrollRun.DRAFT:
        raise RunError('Only a run being prepared can be recalculated.')
    keep = {p.staff_payroll_id: p for p in run.payslips.all()}
    active = list(StaffPayroll.objects.filter(tenant_id=run.tenant_id, is_active=True).select_related('staff__tenant'))
    for pr in active:
        slip = keep.pop(pr.pk, None) or Payslip(tenant_id=run.tenant_id, run=run, staff_payroll=pr)
        _fill(slip, pr, run)
    for stale in keep.values():          # staff taken off payroll since the last build
        stale.delete()


def _fill(slip, pr, run):
    values, loans = engine.compute(pr, run, slip.extra_pay, slip.other_deductions)
    for k, v in values.items():
        setattr(slip, k, v)
    slip.workings['loans'] = [[ln.pk, str(a)] for ln, a in loans]
    slip.save()


def adjust(slip, extra_pay=None, other_deductions=None, note=None):
    if slip.run.status != PayrollRun.DRAFT:
        raise RunError('This run is no longer being prepared.')
    try:
        if extra_pay is not None:
            slip.extra_pay = engine.money(Decimal(str(extra_pay or 0).replace(',', '')))
        if other_deductions is not None:
            slip.other_deductions = engine.money(Decimal(str(other_deductions or 0).replace(',', '')))
    except Exception:
        raise RunError('Enter amounts as numbers.')
    if slip.extra_pay < 0 or slip.other_deductions < 0:
        raise RunError('Amounts cannot be negative.')
    if note is not None:
        slip.note = note.strip()[:160]
    _fill(slip, slip.staff_payroll, slip.run)
    return slip


def submit(run, user):
    if run.status != PayrollRun.DRAFT:
        raise RunError('Only a run being prepared can be sent for approval.')
    if not run.payslips.exists():
        raise RunError('There is nobody on this run — add staff to payroll first.')
    run.status, run.submitted_at = PayrollRun.SUBMITTED, timezone.now()
    run.save(update_fields=['status', 'submitted_at'])


def _check_second_person(run, user, what):
    if settings_for(run.tenant).separate_approver and run.prepared_by_id == user.id:
        raise RunError(f'Someone other than the person who prepared this run must {what} it.')


def approve(run, user):
    if not can_manage(user):
        raise RunError('Only the principal or an administrator can approve payroll.')
    if run.status != PayrollRun.SUBMITTED:
        raise RunError('This run is not waiting for approval.')
    _check_second_person(run, user, 'approve')
    run.status, run.approved_by, run.approved_at = PayrollRun.APPROVED, user, timezone.now()
    run.save(update_fields=['status', 'approved_by', 'approved_at'])


def send_back(run, user, note):
    if not can_manage(user) and run.prepared_by_id != user.id:
        raise RunError('Only an approver or the person who prepared it can send it back.')
    if run.status not in (PayrollRun.SUBMITTED, PayrollRun.APPROVED):
        raise RunError('This run is not waiting for approval or payment.')
    run.status, run.approved_by, run.approved_at = PayrollRun.DRAFT, None, None
    run.note = (note or 'Sent back for changes').strip()[:255]
    run.save(update_fields=['status', 'approved_by', 'approved_at', 'note'])


@transaction.atomic
def release(run, user):
    """Mark the run paid, post loan repayments and email payslips."""
    if not can_manage(user):
        raise RunError('Only the principal or an administrator can release payment.')
    if run.status != PayrollRun.APPROVED:
        raise RunError('Approve the run before releasing payment.')
    _check_second_person(run, user, 'release')
    from .models import StaffLoan
    for slip in run.payslips.select_for_update():
        for loan_id, amount in slip.workings.get('loans', []):
            loan = StaffLoan.objects.select_for_update().filter(pk=loan_id).first()
            if not loan:
                continue
            amount = min(Decimal(amount), loan.balance)
            if amount <= 0:
                continue
            LoanRepayment.objects.create(tenant_id=run.tenant_id, loan=loan, payslip=slip, amount=amount)
            loan.balance -= amount
            loan.active = loan.balance > 0
            loan.save(update_fields=['balance', 'active'])
    run.status, run.paid_by, run.paid_at = PayrollRun.PAID, user, timezone.now()
    run.save(update_fields=['status', 'paid_by', 'paid_at'])
    from lms_project.tasks import run_in_background
    run_in_background(email_payslips, run.pk, tenant=run.tenant)


def cancel(run, user):
    if run.status == PayrollRun.PAID:
        raise RunError('A paid run cannot be cancelled.')
    run.status = PayrollRun.CANCELLED
    run.save(update_fields=['status'])


# ─────────────────────────────────────────────────────────────────────────────
# Totals, schedules and the bank file
# ─────────────────────────────────────────────────────────────────────────────

FIELDS = ['gross', 'pension_employee', 'pension_employer', 'nhf', 'paye', 'union_dues', 'loan_repayment',
          'other_deductions', 'net']


def totals(run):
    out = {f: ZERO for f in FIELDS}
    for slip in run.payslips.all():
        for f in FIELDS:
            out[f] += getattr(slip, f)
    out['cost'] = out['gross'] + out['pension_employer']
    out['pension_total'] = out['pension_employee'] + out['pension_employer']
    return out


def problems(run):
    """Things to fix before paying: missing bank details, PINs, TINs."""
    out = []
    for slip in run.payslips.select_related('staff_payroll__staff'):
        pr = slip.staff_payroll
        name = pr.staff.full_name
        if not (pr.account_no and pr.bank_name):
            out.append(f'{name}: no bank account on record.')
        if slip.pension_employee and not pr.pension_pin:
            out.append(f'{name}: pension is deducted but there is no pension PIN.')
        if slip.paye and not pr.tax_id:
            out.append(f'{name}: tax is deducted but there is no Tax ID.')
        if slip.net <= 0:
            out.append(f'{name}: nothing left to pay after deductions.')
    return out


def bank_rows(run):
    narration = f'{settings_for(run.tenant).bank_narration} {run.month_name[:3]} {run.year}'
    rows = []
    for slip in run.payslips.select_related('staff_payroll__staff'):
        pr = slip.staff_payroll
        if slip.net > 0:
            rows.append([pr.account_name or pr.staff.full_name, pr.account_no, pr.bank_name, f'{slip.net:.2f}',
                         narration, f'PAY{run.pk}-{slip.pk}'])
    return ['Beneficiary name', 'Account number', 'Bank', 'Amount', 'Narration', 'Reference'], rows


def schedule(run, kind):
    """(title, columns, rows) for the pension, PAYE or NHF remittance schedule."""
    slips = list(run.payslips.select_related('staff_payroll__staff'))
    if kind == 'pension':
        rows = [[s.staff_payroll.staff.full_name, s.staff_payroll.pfa_name, s.staff_payroll.pension_pin,
                 float(s.pension_employee), float(s.pension_employer), float(s.pension_employee + s.pension_employer)]
                for s in slips if s.pension_employee or s.pension_employer]
        rows.sort(key=lambda r: (r[1], r[0]))
        return 'Pension schedule', ['Name', 'PFA', 'Pension PIN', 'Employee (8%)', 'Employer (10%)', 'Total'], rows
    if kind == 'nhf':
        return 'NHF schedule', ['Name', 'NHF number', 'Basic', 'NHF'], [
            [s.staff_payroll.staff.full_name, s.staff_payroll.nhf_number, float(s.basic), float(s.nhf)]
            for s in slips if s.nhf]
    return 'PAYE schedule', ['Name', 'Tax ID', 'Gross pay', 'Chargeable (year)', 'PAYE'], [
        [s.staff_payroll.staff.full_name, s.staff_payroll.tax_id, float(s.gross),
         s.workings.get('chargeable', s.workings.get('regular_chargeable', '')), float(s.paye)] for s in slips]


def csv_text(columns, rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(columns)
    w.writerows(rows)
    return buf.getvalue()


# ─────────────────────────────────────────────────────────────────────────────
# Payslips by email (F227)
# ─────────────────────────────────────────────────────────────────────────────

def email_payslips(run_id, only=None):
    from django.template.loader import render_to_string
    from communications.services import send_email
    run = PayrollRun.objects.filter(pk=run_id).select_related('tenant').first()
    if not run or run.status != PayrollRun.PAID:
        return 0
    sent = 0
    slips = run.payslips.select_related('staff_payroll__staff__user')
    if only:
        slips = slips.filter(pk__in=only)
    for slip in slips:
        email = slip.staff_payroll.staff.user.email
        if not email:
            continue
        html = render_to_string('payroll/payslip_email.html', {'slip': slip, 'run': run, 'school': run.tenant})
        ok, _ = send_email(email, f'Your payslip — {run}', html)
        if ok:
            Payslip.objects.filter(pk=slip.pk).update(emailed_at=timezone.now())
            sent += 1
    return sent
