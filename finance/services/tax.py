"""
finance/services/tax.py
───────────────────────
Withholding tax and VAT on what the school buys.

When the school pays a supplier, part of the bill may have to be withheld and
paid to the tax authority instead: to the NRS (formerly FIRS) when the
supplier is a company, and to the State Internal Revenue Service when it is
an individual. The supplier receives the rest, and claims the withheld part
back against their own tax with the credit note the authority issues.

This module works out the deduction from the school's own rate table, keeps
track of what has been withheld but not yet paid over, records each
remittance, and produces the monthly schedule the authority asks for.

VAT on the school's own fees is not handled here: tuition is an exempt
supply. VAT a supplier charges is recorded on the expense so the school can
see what it paid, but it stays part of the cost — a school making exempt
supplies generally cannot reclaim it.

The starting rate table is a convenience, not tax advice. Rates change; every
school can edit its own table, and should confirm it with its tax adviser.
"""
import csv
import io
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.db.models import Sum

from finance.models import Expense, WhtRate, WhtRemittance

ZERO = Decimal('0.00')

# Starting values only — each school edits its own copy.
DEFAULT_RATES = [
    ('Supply of goods', '2', '2'),
    ('Construction and maintenance work', '2', '5'),
    ('Consultancy, professional and technical services', '5', '5'),
    ('Rent and hire of equipment', '10', '10'),
    ('Commission, brokerage and agency', '5', '5'),
    ('Other services', '2', '5'),
]


def ensure_rate_table(tenant):
    """
    Give a school the starting table the first time it is needed.

    Looked up by the school passed in, not the request's school, so this is
    right whichever school is active — otherwise it could decide a school has
    no table and create a second one.
    """
    rates = WhtRate.objects.for_tenant(tenant)
    if not rates.exists():
        WhtRate.objects.bulk_create([
            WhtRate(tenant=tenant, name=name, company_rate=Decimal(company),
                    individual_rate=Decimal(individual))
            for name, company, individual in DEFAULT_RATES])
    return WhtRate.objects.for_tenant(tenant)


def authority_for(payee_type):
    return Expense.STATE if payee_type == Expense.INDIVIDUAL else Expense.FEDERAL


def compute_wht(amount, rate, payee_type):
    """(percent, amount withheld) for a bill. No rate means nothing withheld."""
    if not rate or not amount:
        return ZERO, ZERO
    percent = Decimal(rate.rate_for(payee_type))
    withheld = (Decimal(amount) * percent / Decimal('100')).quantize(
        Decimal('0.01'), rounding=ROUND_HALF_UP)
    return percent, withheld


def apply_wht(expense, rate, payee_type=None, vendor=None):
    """
    Work out and store the deduction on an expense (not saved here).
    A supplier marked exempt is never deducted from, whatever rate is chosen.
    """
    if vendor is not None:
        payee_type = vendor.payee_type
        if vendor.wht_exempt:
            rate = None
    payee_type = payee_type or Expense.COMPANY
    percent, withheld = compute_wht(expense.amount, rate, payee_type)
    expense.payee_type = payee_type
    expense.wht_rate = rate if withheld else None
    expense.wht_percent = percent if withheld else ZERO
    expense.wht_amount = withheld
    expense.wht_authority = authority_for(payee_type) if withheld else ''
    return expense


def _paid_with_wht(tenant):
    return Expense.objects.filter(tenant=tenant, status=Expense.PAID, wht_amount__gt=0)


def deductions(tenant, start=None, end=None, authority=None):
    rows = _paid_with_wht(tenant).select_related('vendor', 'category', 'wht_rate',
                                                  'wht_remittance')
    if start:
        rows = rows.filter(spent_on__gte=start)
    if end:
        rows = rows.filter(spent_on__lte=end)
    if authority:
        rows = rows.filter(wht_authority=authority)
    return rows.order_by('spent_on', 'id')


def unremitted(tenant, authority=None, start=None, end=None):
    """Withheld from suppliers, but not yet paid over to the authority."""
    return deductions(tenant, start, end, authority).filter(wht_remittance__isnull=True)


def _total(rows, field='wht_amount'):
    return (rows.aggregate(t=Sum(field))['t'] or ZERO).quantize(Decimal('0.01'))


def position(tenant, start=None, end=None):
    """Per authority: withheld in the period, remitted, and still owed overall."""
    out = []
    for key, label in Expense.AUTHORITY_CHOICES:
        withheld = _total(deductions(tenant, start, end, key))
        remitted_rows = WhtRemittance.objects.filter(tenant=tenant, authority=key)
        if start:
            remitted_rows = remitted_rows.filter(paid_on__gte=start)
        if end:
            remitted_rows = remitted_rows.filter(paid_on__lte=end)
        out.append({
            'key': key, 'label': label,
            'withheld': withheld,
            'remitted': _total(remitted_rows, 'amount'),
            'owed': _total(unremitted(tenant, key)),
            'owed_count': unremitted(tenant, key).count(),
        })
    return out


def vat_paid(tenant, start=None, end=None):
    rows = Expense.objects.filter(tenant=tenant, status=Expense.PAID, vat_amount__gt=0)
    if start:
        rows = rows.filter(spent_on__gte=start)
    if end:
        rows = rows.filter(spent_on__lte=end)
    return _total(rows, 'vat_amount')


@transaction.atomic
def record_remittance(tenant, authority, start, end, *, paid_on=None, reference='',
                      method=Expense.TRANSFER, receipt=None, note='', user=None):
    """
    Pay over everything withheld for `authority` in the period that has not
    been paid over yet. The amount is the sum of those deductions, so the
    remittance always agrees with the schedule.
    """
    # Lock only these rows: PostgreSQL refuses FOR UPDATE on the optional (outer-joined) related tables.
    rows = list(unremitted(tenant, authority, start, end).select_for_update(of=('self',)))
    if not rows:
        raise ValueError('Nothing withheld in that period is waiting to be paid over.')
    remittance = WhtRemittance.objects.create(
        tenant=tenant, authority=authority, period_start=start, period_end=end,
        amount=sum((r.wht_amount for r in rows), ZERO), reference=reference,
        method=method, receipt=receipt, note=note, recorded_by=user,
        **({'paid_on': paid_on} if paid_on else {}))
    Expense.objects.filter(id__in=[r.id for r in rows]).update(wht_remittance=remittance)
    return remittance


@transaction.atomic
def undo_remittance(remittance):
    """Remove a remittance recorded by mistake; its deductions are owed again."""
    Expense.objects.filter(wht_remittance=remittance).update(wht_remittance=None)
    remittance.delete()


def schedule_csv(tenant, start, end, authority):
    """The deduction schedule the tax authority asks for with each remittance."""
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(['Beneficiary name', 'Beneficiary TIN', 'Beneficiary type', 'Address',
                     'Transaction date', 'Nature of transaction', 'Invoice / reference',
                     'Gross amount', 'WHT rate (%)', 'WHT deducted', 'Remitted on',
                     'Remittance reference'])
    for e in deductions(tenant, start, end, authority):
        vendor = e.vendor
        writer.writerow([
            e.paid_to, vendor.tin if vendor else '',
            e.get_payee_type_display(), (vendor.address if vendor else '').replace('\n', ', '),
            e.spent_on.strftime('%d/%m/%Y'),
            e.wht_rate.name if e.wht_rate else e.category.name,
            e.reference or e.description,
            f'{e.amount:.2f}', f'{e.wht_percent:.2f}', f'{e.wht_amount:.2f}',
            e.wht_remittance.paid_on.strftime('%d/%m/%Y') if e.wht_remittance else '',
            e.wht_remittance.reference if e.wht_remittance else '',
        ])
    return out.getvalue()
