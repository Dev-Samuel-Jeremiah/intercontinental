"""
finance/services/accounts.py
────────────────────────────
Money in, money out, and what is left — for a date range the school chooses.

Income counts approved payments from families, less any refunds actually paid
back. Expenditure counts expenses actually paid out (not ones still waiting
for approval), so the figures match what left the bank.
"""
from decimal import Decimal

from django.db.models import Count, F, Sum

from finance.models import (Budget, Expense, ExpenseCategory, Payment, PettyCashTopUp,
                            Vendor)


def _money(value):
    return (value or Decimal('0.00')).quantize(Decimal('0.01'))


def income_total(tenant, start=None, end=None):
    """Approved payments received in the period."""
    payments = Payment.objects.filter(tenant=tenant, status=Payment.APPROVED)
    if start:
        payments = payments.filter(created_at__date__gte=start)
    if end:
        payments = payments.filter(created_at__date__lte=end)
    return _money(payments.aggregate(total=Sum('amount'))['total'])


def income_by_method(tenant, start=None, end=None):
    payments = Payment.objects.filter(tenant=tenant, status=Payment.APPROVED)
    if start:
        payments = payments.filter(created_at__date__gte=start)
    if end:
        payments = payments.filter(created_at__date__lte=end)
    labels = dict(Payment.METHOD_CHOICES)
    return [{'label': labels.get(row['method'], row['method']),
             'total': _money(row['total']), 'count': row['n']}
            for row in payments.values('method').annotate(total=Sum('amount'), n=Count('id'))
            .order_by('-total')]


def expenses_total(tenant, start=None, end=None, status=Expense.PAID):
    expenses = Expense.objects.filter(tenant=tenant)
    if status:
        expenses = expenses.filter(status=status)
    if start:
        expenses = expenses.filter(spent_on__gte=start)
    if end:
        expenses = expenses.filter(spent_on__lte=end)
    return _money(expenses.aggregate(total=Sum('amount'))['total'])


def expenses_by_category(tenant, start=None, end=None, status=Expense.PAID):
    expenses = Expense.objects.filter(tenant=tenant)
    if status:
        expenses = expenses.filter(status=status)
    if start:
        expenses = expenses.filter(spent_on__gte=start)
    if end:
        expenses = expenses.filter(spent_on__lte=end)
    return [{'category': row['category__name'], 'total': _money(row['total']), 'count': row['n']}
            for row in expenses.values('category__name')
            .annotate(total=Sum('amount'), n=Count('id')).order_by('-total')]


def petty_cash_balance(tenant):
    """What should be left in the tin: top-ups minus petty-cash spending."""
    topped = PettyCashTopUp.objects.filter(tenant=tenant).aggregate(
        total=Sum('amount'))['total'] or Decimal('0.00')
    # Tax withheld from a supplier never leaves the tin; only the net is paid.
    spent = Expense.objects.filter(tenant=tenant, method=Expense.PETTY_CASH,
                                   status=Expense.PAID).aggregate(
        total=Sum(F('amount') - F('wht_amount')))['total'] or Decimal('0.00')
    return _money(topped - spent)


def statement(tenant, start=None, end=None, session=None, term=None):
    """Income and expenditure for the period, with budgets where they exist."""
    from finance.services.refunds import awaiting_total, refunds_total
    from finance.services.shop import takings

    received = income_total(tenant, start, end)
    # Cash, transfer and card sales at the till are new money. Wallet sales are
    # not: that money was counted when the wallet was topped up.
    shop_takings = takings(tenant, start, end)
    refunded = refunds_total(tenant, start, end)
    income = _money(received + shop_takings - refunded)
    spent = expenses_total(tenant, start, end)
    by_category = expenses_by_category(tenant, start, end)

    budgets = Budget.objects.filter(tenant=tenant)
    if session:
        budgets = budgets.filter(session=session)
    if term:
        budgets = budgets.filter(term=term)
    budget_by_category = {b.category.name: b.amount for b in budgets.select_related('category')}

    rows = []
    for row in by_category:
        budget = budget_by_category.pop(row['category'], None)
        rows.append({**row, 'budget': budget,
                     'left': (budget - row['total']) if budget is not None else None,
                     'over': bool(budget is not None and row['total'] > budget)})
    # categories with a budget but nothing spent yet
    for name, budget in budget_by_category.items():
        rows.append({'category': name, 'total': Decimal('0.00'), 'count': 0,
                     'budget': budget, 'left': budget, 'over': False})

    return {
        'income': income,
        'received': received,
        'shop_takings': shop_takings,
        'refunds': refunded,
        'refunds_awaiting': awaiting_total(tenant),
        'expenses': spent,
        'net': _money(income - spent),
        'by_category': rows,
        'by_method': income_by_method(tenant, start, end),
        'awaiting_approval': _money(
            Expense.objects.filter(tenant=tenant, status=Expense.PENDING)
            .aggregate(total=Sum('amount'))['total']),
        'petty_cash': petty_cash_balance(tenant),
        'budget_total': _money(sum((r['budget'] for r in rows if r['budget']), Decimal('0.00'))),
    }
