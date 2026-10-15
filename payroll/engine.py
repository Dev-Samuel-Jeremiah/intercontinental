"""
payroll/engine.py
─────────────────
Works out each payslip: allowances, unpaid leave, pension, NHF, PAYE under
the Nigeria Tax Act 2025 (in force from 1 January 2026), union dues and loan
repayments.

PAYE (personal income tax) — yearly bands on chargeable income:
    first  ₦800,000 ............ 0%
    next   ₦2,200,000 ........... 15%
    next   ₦9,000,000 ........... 18%
    next   ₦13,000,000 .......... 21%
    next   ₦25,000,000 .......... 23%
    above  ₦50,000,000 .......... 25%
Chargeable income = yearly gross − employee pension − NHF − rent relief
(20% of yearly rent, at most ₦500,000) − other documented reliefs (health
or life insurance, mortgage interest). The old Consolidated Relief
Allowance no longer exists. Pay at or below the national minimum wage is
free of income tax.

Pension (Pension Reform Act 2014): employee 8% and employer 10% of basic +
housing + transport. NHF: 2.5% of basic, only for staff who agree to it.
All rates are school settings, so they can be changed if the law changes.
"""
from decimal import ROUND_HALF_UP, Decimal

ZERO = Decimal('0')
CENT = Decimal('0.01')
BANDS = [(Decimal('800000'), Decimal('0')), (Decimal('2200000'), Decimal('15')), (Decimal('9000000'), Decimal('18')),
         (Decimal('13000000'), Decimal('21')), (Decimal('25000000'), Decimal('23')), (None, Decimal('25'))]
RENT_RELIEF_RATE = Decimal('20')
RENT_RELIEF_CAP = Decimal('500000')


def money(v):
    return Decimal(v).quantize(CENT, rounding=ROUND_HALF_UP)


def annual_tax(chargeable):
    """(tax for the year, [(band label, amount in band, rate, tax)])."""
    left, tax, steps = max(ZERO, Decimal(chargeable)), ZERO, []
    for width, rate in BANDS:
        if left <= 0:
            break
        part = left if width is None else min(left, width)
        t = part * rate / 100
        steps.append({'band': 'Above ₦50,000,000' if width is None else f'Next ₦{width:,.0f}' if steps else
                      f'First ₦{width:,.0f}', 'amount': float(money(part)), 'rate': float(rate), 'tax': float(money(t))})
        tax += t
        left -= part
    return money(tax), steps


def rent_relief(annual_rent):
    return min(money(Decimal(annual_rent) * RENT_RELIEF_RATE / 100), RENT_RELIEF_CAP)


def _settings(tenant):
    from .models import PayrollSettings
    obj, _ = PayrollSettings.objects.get_or_create(tenant=tenant)
    return obj


def unpaid_leave_days(staff, year, month):
    """Working days of approved unpaid leave in the month."""
    import calendar
    from datetime import date
    from safety.models import StaffLeave
    from safety.staff_attendance import working_days
    first, last = date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])
    days = 0
    for lv in StaffLeave.objects.filter(tenant_id=staff.tenant_id, staff=staff, status=StaffLeave.APPROVED,
                                        leave_type='UNPAID', start_date__lte=last, end_date__gte=first):
        days += len(working_days(max(lv.start_date, first), min(lv.end_date, last), staff.tenant))
    return days, len(working_days(first, last, staff.tenant))


def extra_lesson_pay(staff, year, month):
    """(amount, [(group name, sessions, rate)]) for after-school groups the teacher ran in the month (F233)."""
    import calendar
    from datetime import date
    from academics.models import PublicHoliday
    from timetable.lessons import DAY_CODES
    from timetable.models import ExtraLessonGroup
    first, last = date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])
    holidays = set(PublicHoliday.objects.filter(tenant_id=staff.tenant_id, date__range=(first, last))
                   .values_list('date', flat=True))
    total, lines = ZERO, []
    for g in ExtraLessonGroup.objects.filter(tenant_id=staff.tenant_id, teacher=staff, active=True, teacher_pay__gt=0):
        from django.utils import timezone
        start = max(first, timezone.localtime(g.created_at).date()) if g.created_at else first
        n = sum(1 for d in range(start.day, last.day + 1)
                if DAY_CODES[date(year, month, d).weekday()] == g.day and date(year, month, d) not in holidays) \
            if start <= last else 0
        if n:
            amount = money(g.teacher_pay * n)
            total += amount
            lines.append({'group': g.name, 'sessions': n, 'rate': float(g.teacher_pay), 'amount': float(amount)})
    return total, lines


def cover_pay(staff, year, month, rate):
    """(amount, lessons) for lessons covered for absent colleagues in the month."""
    from curriculum.models import CoverAssignment
    if not rate:
        return ZERO, 0
    n = CoverAssignment.objects.filter(tenant_id=staff.tenant_id, cover_teacher=staff, date__year=year,
                                       date__month=month).count()
    return money(Decimal(rate) * n), n


def _regular_chargeable(pr, s, monthly_gross, pension, nhf):
    reliefs = (pension + nhf) * 12 + rent_relief(pr.annual_rent) + Decimal(pr.other_reliefs)
    return max(ZERO, monthly_gross * 12 - reliefs), reliefs


def compute(pr, run, extra_pay=ZERO, other_deductions=ZERO, loans=None):
    """
    Field values for one payslip (not saved). `loans` is [(StaffLoan, amount)]
    to deduct; by default every active loan that has started is used.
    """
    from .models import PayrollRun
    s = _settings(run.tenant)
    extra_pay, other_deductions = money(extra_pay or 0), money(other_deductions or 0)
    pension_base = pr.basic_salary + pr.housing + pr.transport
    regular_gross = pr.gross_monthly
    pension_emp = money(pension_base * s.pension_employee_rate / 100) if pr.pays_pension else ZERO
    nhf_regular = money(pr.basic_salary * s.nhf_rate / 100) if pr.pays_nhf else ZERO
    chargeable, reliefs = _regular_chargeable(pr, s, regular_gross, pension_emp, nhf_regular)
    out = {'basic': ZERO, 'housing': ZERO, 'transport': ZERO, 'other_allowances': ZERO, 'bonus': ZERO,
           'extra_pay': extra_pay, 'unpaid_leave': ZERO, 'pension_employee': ZERO, 'pension_employer': ZERO,
           'nhf': ZERO, 'paye': ZERO, 'union_dues': ZERO, 'loan_repayment': ZERO, 'other_deductions': other_deductions,
           'extra_lessons': ZERO, 'cover_pay': ZERO}
    taxable = pr.pays_tax and regular_gross > s.minimum_wage
    if run.kind == PayrollRun.MONTHLY:
        out.update(basic=pr.basic_salary, housing=pr.housing, transport=pr.transport,
                   other_allowances=pr.other_allowances, pension_employee=pension_emp, nhf=nhf_regular,
                   pension_employer=money(pension_base * s.pension_employer_rate / 100) if pr.pays_pension else ZERO,
                   union_dues=money(pr.union_dues))
        if s.deduct_unpaid_leave:
            days, working = unpaid_leave_days(pr.staff, run.year, run.month)
            if days and working:
                out['unpaid_leave'] = money(regular_gross * days / working)
        out['extra_lessons'], lesson_lines = extra_lesson_pay(pr.staff, run.year, run.month)
        out['cover_pay'], covers = cover_pay(pr.staff, run.year, run.month, s.cover_rate)
        additions = extra_pay + out['extra_lessons'] + out['cover_pay']
        gross = regular_gross + additions - out['unpaid_leave']
        # Extra pay is taxed at the margin; unpaid leave lowers the month's taxable pay.
        month_chargeable = max(ZERO, chargeable + (additions - out['unpaid_leave']) * 12)
        yearly, steps = annual_tax(month_chargeable)
        out['paye'] = money(yearly / 12) if taxable else ZERO
        workings = {'yearly_gross': float(money(gross * 12)), 'reliefs': float(money(reliefs)),
                    'rent_relief': float(rent_relief(pr.annual_rent)), 'chargeable': float(money(month_chargeable)),
                    'yearly_tax': float(yearly), 'bands': steps, 'extra_lessons': lesson_lines, 'covers': covers}
        if loans is None:
            loans = [(ln, min(ln.monthly_repayment, ln.balance)) for ln in pr.loans.filter(active=True)
                     if (ln.start_year, ln.start_month) <= (run.year, run.month) and ln.balance > 0]
    else:
        bonus = money(pr.basic_salary * run.bonus_percent / 100)
        out['bonus'] = bonus
        gross = bonus + extra_pay
        base_tax, _ = annual_tax(chargeable)
        with_bonus, steps = annual_tax(chargeable + gross)
        out['paye'] = money(with_bonus - base_tax) if taxable else ZERO
        workings = {'regular_chargeable': float(money(chargeable)), 'tax_without': float(base_tax),
                    'tax_with': float(with_bonus), 'bands': steps,
                    'method': 'Tax on the year with this payment, less tax on the year without it'}
        loans = loans or []
    if not taxable:
        workings['exempt'] = ('Not taxed (exempt in settings).' if not pr.pays_tax else
                              f'Monthly pay is at or below the minimum wage (₦{s.minimum_wage:,.0f}), so no income tax.')
    out['gross'] = money(gross)
    fixed = out['pension_employee'] + out['nhf'] + out['paye'] + out['union_dues'] + other_deductions
    room = max(ZERO, out['gross'] - fixed)
    taken = []
    for ln, amount in loans:
        amount = min(money(amount), room)
        if amount > 0:
            taken.append((ln, amount))
            room -= amount
    out['loan_repayment'] = sum((a for _, a in taken), ZERO)
    out['net'] = money(out['gross'] - fixed - out['loan_repayment'])
    out['workings'] = workings
    return out, taken
