"""
finance/services/export.py
──────────────────────────
Hand the school's books to its accountant.

Everything the finance module records for a period — fees received, refunds,
spending (with any tax withheld from suppliers), withholding tax paid over,
and petty-cash top-ups — becomes a set of balanced double-entry
journals, using the school's own account codes (`AccountingCodes`). They can
be downloaded as:

  • an Excel workbook: a summary, one sheet per kind of record, and the journal
  • a journal CSV for QuickBooks Online, Xero, or any other package
    (Sage, Zoho Books, Tally) through its general-journal import

The export is on a cash basis: money counts when it moves. Late-payment
charges are not exported, because they are amounts owed, not money received —
once paid, they arrive as ordinary payments.
"""
import csv
import io
from decimal import Decimal

from django.utils import timezone

from finance.models import (AccountingCodes, Expense, ExpenseCategory, Payment,
                            PettyCashTopUp, Refund, Sale, WhtRemittance)

ZERO = Decimal('0.00')

# Which of the school's accounts a payment method lands in.
PAYMENT_ACCOUNT = {'CASH': 'cash', 'BANK_TRANSFER': 'bank', 'CHEQUE': 'bank',
                   'POS': 'bank', 'PAYSTACK': 'online'}
EXPENSE_ACCOUNT = {Expense.CASH: 'cash', Expense.TRANSFER: 'bank', Expense.CHEQUE: 'bank',
                   Expense.PETTY_CASH: 'petty_cash'}
SALE_ACCOUNT = {Sale.CASH: 'cash', Sale.TRANSFER: 'bank', Sale.POS: 'bank'}
REFUND_ACCOUNT = {Refund.CASH: 'cash', Refund.TRANSFER: 'bank', Refund.CHEQUE: 'bank',
                  Refund.WALLET: 'parent_wallets'}

FORMATS = {
    'generic': 'Any accounting package (general journal)',
    'quickbooks': 'QuickBooks Online (journal entries)',
    'xero': 'Xero (manual journals)',
}


def _day(value):
    """A date or datetime as the school's local date."""
    if hasattr(value, 'tzinfo') and value.tzinfo is not None:
        return timezone.localdate(value)
    return value.date() if hasattr(value, 'date') and callable(value.date) else value


def _payments(tenant, start, end):
    return (Payment.objects.filter(tenant=tenant, status=Payment.APPROVED,
                                   created_at__date__gte=start, created_at__date__lte=end)
            .select_related('wallet__student').order_by('created_at', 'id'))


def _refunds(tenant, start, end):
    return (Refund.objects.filter(tenant=tenant, status=Refund.PAID,
                                  paid_on__gte=start, paid_on__lte=end)
            .select_related('student').order_by('paid_on', 'id'))


def _expenses(tenant, start, end):
    return (Expense.objects.filter(tenant=tenant, status=Expense.PAID,
                                   spent_on__gte=start, spent_on__lte=end)
            .select_related('category', 'vendor').order_by('spent_on', 'id'))


def _till_sales(tenant, start, end):
    """Cash, transfer and card sales. Wallet sales spend money already counted."""
    return (Sale.objects.filter(tenant=tenant, status=Sale.COMPLETED,
                                created_at__date__gte=start, created_at__date__lte=end)
            .exclude(method=Sale.WALLET).order_by('created_at', 'id'))


def _remittances(tenant, start, end):
    return (WhtRemittance.objects.filter(tenant=tenant, paid_on__gte=start, paid_on__lte=end)
            .order_by('paid_on', 'id'))


def _topups(tenant, start, end):
    return (PettyCashTopUp.objects.filter(tenant=tenant, topped_up_on__gte=start,
                                          topped_up_on__lte=end)
            .order_by('topped_up_on', 'id'))


def journal(tenant, start, end):
    """
    Balanced journal entries for the period, oldest first. Each entry is
    {'number', 'date', 'reference', 'description', 'lines'}, and each line is
    {'code', 'account', 'debit', 'credit', 'description'}.
    """
    codes = AccountingCodes.for_school(tenant)
    entries = []

    def posting(day, reference, description, debits, credits):
        """debits/credits are lists of ((code, name), amount); zero lines are dropped."""
        lines = [{'code': a[0], 'account': a[1], 'debit': amt, 'credit': ZERO,
                  'description': description} for a, amt in debits if amt] + \
                [{'code': a[0], 'account': a[1], 'debit': ZERO, 'credit': amt,
                  'description': description} for a, amt in credits if amt]
        entries.append({'date': day, 'reference': reference, 'description': description,
                        'lines': lines})

    def entry(day, reference, description, debit, credit, amount):
        """The common case: one account debited, one credited."""
        posting(day, reference, description, [(debit, amount)], [(credit, amount)])

    for p in _payments(tenant, start, end):
        student = p.wallet.student
        entry(_day(p.created_at), p.reference,
              f'School fees — {student.full_name} ({student.admission_number})',
              codes.account(PAYMENT_ACCOUNT.get(p.method, 'bank')),
              codes.account('fee_income'), p.amount)

    for r in _refunds(tenant, start, end):
        entry(r.paid_on, r.reference or f'REFUND-{r.id}',
              f'Refund — {r.student.full_name}: {r.get_reason_display().lower()}',
              codes.account('refunds'),
              codes.account(REFUND_ACCOUNT.get(r.method, 'bank')), r.amount)

    for e in _expenses(tenant, start, end):
        if e.category.account_code:
            expense_account = (e.category.account_code, e.category.name)
        else:
            expense_account = codes.account('expenses')
        # The full bill is the cost. The supplier is paid the net; the tax
        # withheld is owed to the tax authority until it is remitted.
        posting(e.spent_on, e.reference or f'EXP-{e.id}', f'{e.description} — {e.paid_to}',
                [(expense_account, e.amount)],
                [(codes.account(EXPENSE_ACCOUNT.get(e.method, 'bank')), e.net_paid),
                 (codes.account('wht_payable'), e.wht_amount)])

    for sale in _till_sales(tenant, start, end):
        entry(_day(sale.created_at), sale.number,
              f'{sale.get_outlet_display()} sale — {sale.buyer}',
              codes.account(SALE_ACCOUNT.get(sale.method, 'cash')),
              codes.account('shop_sales'), sale.total)

    for w in _remittances(tenant, start, end):
        entry(w.paid_on, w.reference or f'WHT-{w.id}',
              f'Withholding tax paid to {w.get_authority_display()} for '
              f'{w.period_start:%b %Y}',
              codes.account('wht_payable'),
              codes.account(EXPENSE_ACCOUNT.get(w.method, 'bank')), w.amount)

    for t in _topups(tenant, start, end):
        entry(t.topped_up_on, f'PETTY-{t.id}',
              f'Petty cash top-up{" — " + t.note if t.note else ""}',
              codes.account('petty_cash'), codes.account('bank'), t.amount)

    entries.sort(key=lambda x: (x['date'], x['reference']))
    for number, item in enumerate(entries, start=1):
        item['number'] = f'TC{start:%y%m}-{number:04d}'
    return entries


def totals(entries):
    debit = sum((l['debit'] for e in entries for l in e['lines']), ZERO)
    credit = sum((l['credit'] for e in entries for l in e['lines']), ZERO)
    return debit, credit


def trial_balance(entries):
    """Net movement per account for the period, as the accountant checks it."""
    accounts = {}
    for item in entries:
        for line in item['lines']:
            row = accounts.setdefault(line['code'], {'code': line['code'],
                                                     'account': line['account'],
                                                     'debit': ZERO, 'credit': ZERO})
            row['debit'] += line['debit']
            row['credit'] += line['credit']
    return sorted(accounts.values(), key=lambda r: r['code'])


# ─────────────────────────────────────────────────────────────────────────────
# CSV
# ─────────────────────────────────────────────────────────────────────────────

def _money(value):
    return f'{value:.2f}'


def journal_csv(tenant, start, end, fmt='generic'):
    """The journal as CSV text in the chosen package's import layout."""
    entries = journal(tenant, start, end)
    out = io.StringIO()
    writer = csv.writer(out)

    if fmt == 'xero':
        # Xero manual journal import: one row per line, signed amounts,
        # lines grouped into a journal by matching Narration and Date.
        writer.writerow(['*Narration', '*Date', 'Description', '*AccountCode',
                         '*TaxRate', '*Amount'])
        for item in entries:
            narration = f'{item["number"]} {item["reference"]}'
            for line in item['lines']:
                amount = line['debit'] - line['credit']
                writer.writerow([narration, item['date'].strftime('%d/%m/%Y'),
                                 line['description'], line['code'], 'Tax Exempt',
                                 _money(amount)])
    elif fmt == 'quickbooks':
        # QuickBooks Online journal-entry import: accounts are matched by name.
        writer.writerow(['Journal No', 'Journal Date', 'Account', 'Debits', 'Credits',
                         'Description', 'Memo'])
        for item in entries:
            for line in item['lines']:
                writer.writerow([item['number'], item['date'].strftime('%d/%m/%Y'),
                                 line['account'],
                                 _money(line['debit']) if line['debit'] else '',
                                 _money(line['credit']) if line['credit'] else '',
                                 line['description'], item['reference']])
    else:
        writer.writerow(['Date', 'Journal No', 'Reference', 'Account Code', 'Account Name',
                         'Description', 'Debit', 'Credit'])
        for item in entries:
            for line in item['lines']:
                writer.writerow([item['date'].isoformat(), item['number'], item['reference'],
                                 line['code'], line['account'], line['description'],
                                 _money(line['debit']), _money(line['credit'])])
    return out.getvalue()


# ─────────────────────────────────────────────────────────────────────────────
# Excel
# ─────────────────────────────────────────────────────────────────────────────

def workbook(tenant, start, end):
    """An .xlsx file (bytes): summary, payments, refunds, expenses, petty cash, journal."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    from finance.services.accounts import statement

    gold = PatternFill('solid', fgColor='1C1917')
    head_font = Font(bold=True, color='E8D5A8')
    title_font = Font(bold=True, size=14)
    money_fmt = '#,##0.00'
    thin = Border(bottom=Side(style='thin', color='D6D3D1'))

    book = Workbook()

    def sheet(title, headings, rows, money_cols=(), widths=None, first=False):
        ws = book.active if first else book.create_sheet()
        ws.title = title
        ws.append(headings)
        for cell in ws[1]:
            cell.fill = gold
            cell.font = head_font
            cell.alignment = Alignment(vertical='center')
        ws.row_dimensions[1].height = 22
        for row in rows:
            ws.append(row)
        for col in money_cols:
            for cell in ws.iter_rows(min_row=2, min_col=col, max_col=col):
                cell[0].number_format = money_fmt
        for i, heading in enumerate(headings, start=1):
            width = (widths or {}).get(i) or max(12, min(48, len(str(heading)) + 4))
            ws.column_dimensions[get_column_letter(i)].width = width
        ws.freeze_panes = 'A2'
        return ws

    # Summary
    report = statement(tenant, start, end)
    entries = journal(tenant, start, end)
    debit, credit = totals(entries)
    ws = book.active
    ws.title = 'Summary'
    ws['A1'] = getattr(tenant, 'name', 'School')
    ws['A1'].font = title_font
    ws['A2'] = f'Income and expenditure, {start:%d %b %Y} to {end:%d %b %Y}'
    ws['A3'] = f'Exported {timezone.localtime():%d %b %Y %-I:%M %p} · cash basis'
    ws['A3'].font = Font(italic=True, color='78716C')
    rows = [
        ('Fees received', report['received']),
        ('Shop and canteen takings', report['shop_takings']),
        ('Less refunds paid', -report['refunds']),
        ('Income', report['income']),
        ('Expenditure', -report['expenses']),
        ('Surplus / (deficit)', report['net']),
        (None, None),
        ('Refunds awaiting payment', report['refunds_awaiting']),
        ('Expenses awaiting approval', report['awaiting_approval']),
        ('Petty cash in hand', report['petty_cash']),
        (None, None),
        ('Journal entries', len(entries)),
        ('Total debits', debit),
        ('Total credits', credit),
    ]
    for i, (label, value) in enumerate(rows, start=5):
        if label is None:
            continue
        ws.cell(row=i, column=1, value=label)
        cell = ws.cell(row=i, column=2, value=value)
        if isinstance(value, Decimal):
            cell.number_format = money_fmt
        if label in ('Income', 'Surplus / (deficit)'):
            ws.cell(row=i, column=1).font = Font(bold=True)
            cell.font = Font(bold=True)
            cell.border = thin
    row = 5 + len(rows) + 1
    ws.cell(row=row, column=1, value='Spending by category').font = Font(bold=True)
    for item in report['by_category']:
        row += 1
        ws.cell(row=row, column=1, value=item['category'])
        ws.cell(row=row, column=2, value=item['total']).number_format = money_fmt
    ws.column_dimensions['A'].width = 34
    ws.column_dimensions['B'].width = 18

    sheet('Fees received',
          ['Date', 'Reference', 'Student', 'Admission no.', 'Method', 'Amount'],
          [[_day(p.created_at), p.reference, p.wallet.student.full_name,
            p.wallet.student.admission_number, p.get_method_display(), p.amount]
           for p in _payments(tenant, start, end)],
          money_cols=[6], widths={1: 12, 2: 22, 3: 30, 4: 16, 5: 20, 6: 16})
    sheet('Shop and canteen',
          ['Date', 'Receipt', 'Outlet', 'Customer', 'Paid by', 'Items', 'Amount'],
          [[_day(s.created_at), s.number, s.get_outlet_display(), s.buyer,
            s.get_method_display(), ', '.join(f'{l.name} x{l.quantity}' for l in s.lines.all()),
            s.total]
           for s in _till_sales(tenant, start, end).prefetch_related('lines')],
          money_cols=[7], widths={1: 12, 2: 16, 3: 14, 4: 26, 5: 14, 6: 44, 7: 14})
    sheet('Refunds',
          ['Paid on', 'Student', 'Reason', 'Detail', 'Method', 'Reference', 'Amount'],
          [[r.paid_on, r.student.full_name, r.get_reason_display(), r.detail,
            r.get_method_display(), r.reference, r.amount]
           for r in _refunds(tenant, start, end)],
          money_cols=[7], widths={1: 12, 2: 28, 3: 24, 4: 34, 5: 20, 6: 16, 7: 16})
    sheet('Expenses',
          ['Date', 'Category', 'Description', 'Paid to', 'Method', 'Reference', 'Amount',
           'VAT included', 'WHT withheld', 'Paid to supplier'],
          [[e.spent_on, e.category.name, e.description, e.paid_to, e.get_method_display(),
            e.reference, e.amount, e.vat_amount, e.wht_amount, e.net_paid]
           for e in _expenses(tenant, start, end)],
          money_cols=[7, 8, 9, 10],
          widths={1: 12, 2: 22, 3: 36, 4: 26, 5: 16, 6: 16, 7: 16, 8: 14, 9: 14, 10: 16})
    sheet('Withholding tax',
          ['Date', 'Supplier', 'TIN', 'Type', 'Nature', 'Gross', 'Rate %', 'WHT',
           'Authority', 'Remitted on'],
          [[e.spent_on, e.paid_to, e.vendor.tin if e.vendor else '',
            e.get_payee_type_display(), e.wht_rate.name if e.wht_rate else '', e.amount,
            e.wht_percent, e.wht_amount, e.get_wht_authority_display(),
            e.wht_remittance.paid_on if e.wht_remittance else None]
           for e in _expenses(tenant, start, end).filter(wht_amount__gt=0)
           .select_related('wht_rate', 'wht_remittance')],
          money_cols=[6, 8],
          widths={1: 12, 2: 28, 3: 16, 4: 12, 5: 34, 6: 16, 7: 9, 8: 14, 9: 24, 10: 13})
    sheet('Petty cash top-ups', ['Date', 'Note', 'Amount'],
          [[t.topped_up_on, t.note, t.amount] for t in _topups(tenant, start, end)],
          money_cols=[3], widths={1: 12, 2: 40, 3: 16})
    sheet('Journal',
          ['Date', 'Journal No', 'Reference', 'Account Code', 'Account Name', 'Description',
           'Debit', 'Credit'],
          [[item['date'], item['number'], item['reference'], line['code'], line['account'],
            line['description'], line['debit'], line['credit']]
           for item in entries for line in item['lines']],
          money_cols=[7, 8], widths={1: 12, 2: 14, 3: 20, 4: 13, 5: 26, 6: 46, 7: 15, 8: 15})
    sheet('Trial balance', ['Account Code', 'Account Name', 'Debit', 'Credit', 'Net'],
          [[r['code'], r['account'], r['debit'], r['credit'], r['debit'] - r['credit']]
           for r in trial_balance(entries)],
          money_cols=[3, 4, 5], widths={1: 14, 2: 30, 3: 16, 4: 16, 5: 16})

    for ws in book.worksheets[1:]:
        if ws.title == 'Trial balance':
            continue
        for cells in ws.iter_rows(min_row=2, max_col=1):
            cells[0].number_format = 'DD MMM YYYY'

    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()
