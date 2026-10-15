"""
finance/views_bank.py
─────────────────────
Bank reconciliation: import the bank's statement and see, line by line,
whether it agrees with what the school recorded.
"""
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from .models import (BankAccount, BankLine, BankStatement, Expense, Payment, Refund,
                     WhtRemittance)
from .services import reconcile
from .views import _require_finance_staff

_RECORDS = {'payment': Payment, 'expense': Expense, 'refund': Refund,
            'wht_remittance': WhtRemittance}
MAX_STATEMENT_BYTES = 5 * 1024 * 1024


def _back(account, status=None):
    url = reverse('finance:bank_reconciliation') + f'?account={account.id}'
    return redirect(url + (f'&status={status}' if status else ''))


@_require_finance_staff
def bank_reconciliation(request):
    accounts = BankAccount.objects.filter(is_active=True)
    account = (accounts.filter(pk=request.GET.get('account') or request.POST.get('account_id'))
               .first() or accounts.first())

    if request.method == 'POST':
        action = request.POST.get('action')

        if action == 'add_account':
            name = (request.POST.get('name') or '').strip()
            bank = (request.POST.get('bank_name') or '').strip()
            number = (request.POST.get('account_number') or '').strip()
            if not (name and bank and number):
                messages.error(request, 'Give the account a name, the bank and the number.')
                return redirect('finance:bank_reconciliation')
            if BankAccount.objects.filter(bank_name__iexact=bank,
                                          account_number=number).exists():
                messages.error(request, f'{bank} {number} is already set up.')
                return redirect('finance:bank_reconciliation')
            account = BankAccount.objects.create(tenant=request.tenant, name=name,
                                                 bank_name=bank, account_number=number)
            messages.success(request, f'{name} added. Import a statement to start.')
            return _back(account)

        if not account:
            messages.error(request, 'Add the bank account first.')
            return redirect('finance:bank_reconciliation')

        if action == 'import':
            upload = request.FILES.get('statement')
            if not upload:
                messages.error(request, 'Choose the statement file you downloaded from the bank.')
            elif upload.size > MAX_STATEMENT_BYTES:
                messages.error(request, 'That file is larger than 5 MB. Download a shorter period.')
            elif not upload.name.lower().endswith(('.csv', '.xlsx', '.xlsm', '.xls')):
                messages.error(request, 'Upload the statement as CSV or Excel. PDF statements '
                                        'cannot be read — most banks offer a CSV or Excel '
                                        'download beside the PDF.')
            else:
                try:
                    _, added, skipped, matched = reconcile.import_statement(
                        request.tenant, account, upload, request.user)
                except reconcile.StatementError as problem:
                    messages.error(request, str(problem))
                else:
                    note = f' {skipped} line(s) were already imported and were skipped.' \
                        if skipped else ''
                    messages.success(request, f'{added} line(s) imported, {matched} matched '
                                              f'automatically.{note}')
            return _back(account)

        if action == 'rematch':
            count = 0
            for statement in account.statements.all():
                count += reconcile.auto_match(statement)
            messages.success(request, f'{count} more line(s) matched.' if count
                             else 'Nothing new could be matched automatically.')
            return _back(account)

        line = get_object_or_404(BankLine, pk=request.POST.get('line_id'),
                                 statement__account=account)
        status = request.POST.get('status') or None

        if action == 'match':
            kind, _, record_id = (request.POST.get('record') or '').partition(':')
            model = _RECORDS.get(kind)
            record = model.objects.filter(pk=record_id).first() if model else None
            if not record:
                messages.error(request, 'Choose what this line is for.')
            else:
                try:
                    reconcile.match(line, kind, record, request.user)
                    messages.success(request, 'Line matched.')
                except ValueError as problem:
                    messages.error(request, str(problem))
        elif action == 'unmatch':
            reconcile.unmatch(line)
            messages.success(request, 'Match removed — the line is back in the unmatched list.')
        elif action == 'ignore':
            reconcile.ignore(line, (request.POST.get('note') or '').strip(), request.user)
            messages.success(request, 'Line set aside.')
        return _back(account, status)

    context = {'accounts': accounts, 'account': account, 'status': ''}
    if account:
        status = request.GET.get('status', BankLine.UNMATCHED)
        lines = (BankLine.objects.filter(statement__account=account)
                 .select_related('payment__wallet__student', 'expense__category',
                                 'expense__vendor', 'refund__student', 'wht_remittance',
                                 'matched_by')
                 .order_by('-date', '-id'))
        if status:
            lines = lines.filter(status=status)
        lines = list(lines[:300])
        for line in lines:
            if line.status == BankLine.UNMATCHED:
                line.options = reconcile.options_for(line)

        statements = account.statements.all()
        period = statements.order_by('period_start').values_list('period_start', flat=True)
        start = period.first()
        end = statements.order_by('-period_end').values_list('period_end', flat=True).first()
        missing = reconcile.recorded_not_banked(request.tenant, start, end) \
            if start and end else {'payments': [], 'expenses': [], 'refunds': [],
                                   'remittances': []}

        context.update({
            'status': status, 'lines': lines, 'statements': statements[:10],
            'summary': reconcile.summary(account), 'missing': missing,
            'missing_count': sum(len(v) for v in missing.values()),
            'start': start, 'end': end,
            'statuses': BankLine.STATUS_CHOICES,
        })
    return render(request, 'finance/bank_reconciliation.html', context)
