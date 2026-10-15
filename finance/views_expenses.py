"""
finance/views_expenses.py
─────────────────────────
Money going out: expenses and their approval, vendors, petty cash, budgets,
and the income-and-expenditure statement.
"""
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.db.models import Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from academics.utils import get_active_session, get_active_term

from .models import Budget, Expense, ExpenseCategory, PettyCashTopUp, Vendor, WhtRate
from .services import tax
from .services.accounts import petty_cash_balance, statement
from .views import _require_finance_staff


def _amount(raw):
    try:
        value = Decimal(str(raw or '0'))
    except (InvalidOperation, ValueError):
        return None
    return value if value > 0 else None


def _period(request):
    """The date range the user picked, defaulting to this month."""
    today = timezone.now().date()
    start_raw = request.GET.get('start') or ''
    end_raw = request.GET.get('end') or ''
    try:
        start = date.fromisoformat(start_raw) if start_raw else today.replace(day=1)
    except ValueError:
        start = today.replace(day=1)
    try:
        end = date.fromisoformat(end_raw) if end_raw else today
    except ValueError:
        end = today
    return start, end


# ─────────────────────────────────────────────────────────────────────────────
# Expenses
# ─────────────────────────────────────────────────────────────────────────────

@_require_finance_staff
def expenses(request):
    if request.method == 'POST':
        action = request.POST.get('action')

        if action == 'add':
            amount = _amount(request.POST.get('amount'))
            # Looked up through the school's own lists, so an id from another
            # school's form can never attach that school's category or supplier.
            category = ExpenseCategory.objects.filter(pk=request.POST.get('category_id') or 0).first()
            vendor = Vendor.objects.filter(pk=request.POST.get('vendor_id') or 0).first()
            rate = WhtRate.objects.filter(pk=request.POST.get('wht_rate_id') or 0,
                                          is_active=True).first()
            vat = _amount(request.POST.get('vat_amount')) or Decimal('0.00')
            description = (request.POST.get('description') or '').strip()
            if not amount or not category or not description:
                messages.error(request, 'Enter what the money was for, the category and an amount.')
                return redirect('finance:expenses')
            if vat >= amount:
                messages.error(request, 'The VAT should be part of the amount, so it must be '
                                        'less than the amount.')
                return redirect('finance:expenses')
            spent_on = request.POST.get('spent_on') or ''
            try:
                spent_on = date.fromisoformat(spent_on) if spent_on else timezone.now().date()
            except ValueError:
                spent_on = timezone.now().date()
            expense = Expense(
                tenant=request.tenant, spent_on=spent_on, category=category, vendor=vendor,
                payee=(request.POST.get('payee') or '').strip(),
                description=description, amount=amount, vat_amount=vat,
                method=request.POST.get('method') or Expense.CASH,
                reference=(request.POST.get('reference') or '').strip(),
                receipt=request.FILES.get('receipt'),
                note=(request.POST.get('note') or '').strip(),
                session=get_active_session(), term=get_active_term(),
                requested_by=request.user,
            )
            payee_type = request.POST.get('payee_type')
            if payee_type not in (Expense.COMPANY, Expense.INDIVIDUAL):
                payee_type = Expense.COMPANY
            tax.apply_wht(expense, rate, payee_type, vendor=vendor)
            expense.save()
            if rate and vendor and vendor.wht_exempt:
                messages.info(request, f'{vendor.name} is marked exempt from withholding tax, '
                                       f'so nothing was deducted.')
            # A school admin recording a payment they made is both request and approval.
            if request.user.is_superuser and request.POST.get('already_paid'):
                expense.status = Expense.PAID
                expense.approved_by = request.user
                expense.approved_at = timezone.now()
                expense.save(update_fields=['status', 'approved_by', 'approved_at'])
            if expense.wht_amount:
                messages.success(request,
                                 f'Expense recorded: {description}. Pay the supplier NGN '
                                 f'{expense.net_paid:,.2f}; NGN {expense.wht_amount:,.2f} '
                                 f'withholding tax is owed to '
                                 f'{expense.get_wht_authority_display()}.')
            else:
                messages.success(request, f'Expense recorded: {description}.')
            return redirect('finance:expenses')

        if action in ('approve', 'reject', 'mark_paid'):
            expense = get_object_or_404(Expense, pk=request.POST.get('expense_id'))
            if action == 'approve':
                expense.status = Expense.APPROVED
            elif action == 'reject':
                expense.status = Expense.REJECTED
            else:
                expense.status = Expense.PAID
            expense.approved_by = request.user
            expense.approved_at = timezone.now()
            expense.save(update_fields=['status', 'approved_by', 'approved_at'])
            messages.success(request, f'“{expense.description}” marked as {expense.get_status_display().lower()}.')
            return redirect('finance:expenses')

        if action == 'add_category':
            name = (request.POST.get('name') or '').strip()
            if name:
                ExpenseCategory.objects.get_or_create(tenant=request.tenant, name=name)
                messages.success(request, f'Category “{name}” added.')
            return redirect('finance:expenses')

    start, end = _period(request)
    rows = Expense.objects.select_related('category', 'vendor', 'requested_by')
    status = request.GET.get('status') or ''
    category_id = request.GET.get('category_id') or ''
    search = (request.GET.get('q') or '').strip()
    if status:
        rows = rows.filter(status=status)
    if category_id:
        rows = rows.filter(category_id=category_id)
    if search:
        rows = rows.filter(Q(description__icontains=search) | Q(payee__icontains=search)
                           | Q(vendor__name__icontains=search) | Q(reference__icontains=search))
    rows = rows.filter(spent_on__gte=start, spent_on__lte=end)

    return render(request, 'finance/expenses.html', {
        'expenses': rows.select_related('wht_rate'),
        'categories': ExpenseCategory.objects.filter(is_active=True),
        'vendors': Vendor.objects.filter(is_active=True),
        'wht_rates': tax.ensure_rate_table(request.tenant).filter(is_active=True),
        'payee_types': Expense.PAYEE_TYPE_CHOICES,
        'methods': Expense.METHOD_CHOICES,
        'statuses': Expense.STATUS_CHOICES,
        'status': status, 'category_id': category_id, 'search': search,
        'start': start, 'end': end, 'today': timezone.now().date(),
        'total': rows.filter(status=Expense.PAID).aggregate(t=Sum('amount'))['t'] or Decimal('0'),
        'pending_total': rows.filter(status=Expense.PENDING).aggregate(t=Sum('amount'))['t'] or Decimal('0'),
        'pending_count': rows.filter(status=Expense.PENDING).count(),
        'petty_cash': petty_cash_balance(request.tenant),
    })


# ─────────────────────────────────────────────────────────────────────────────
# Vendors
# ─────────────────────────────────────────────────────────────────────────────

@_require_finance_staff
def vendors(request):
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'add':
            name = (request.POST.get('name') or '').strip()
            if not name:
                messages.error(request, 'Give the supplier a name.')
            elif Vendor.objects.filter(name__iexact=name).exists():
                messages.error(request, f'“{name}” is already on the list.')
            else:
                Vendor.objects.create(
                    tenant=request.tenant, name=name,
                    phone=(request.POST.get('phone') or '').strip(),
                    email=(request.POST.get('email') or '').strip(),
                    service=(request.POST.get('service') or '').strip(),
                    bank_name=(request.POST.get('bank_name') or '').strip(),
                    account_number=(request.POST.get('account_number') or '').strip(),
                    account_name=(request.POST.get('account_name') or '').strip(),
                    payee_type=('INDIVIDUAL' if request.POST.get('payee_type') == 'INDIVIDUAL'
                                else 'COMPANY'),
                    tin=(request.POST.get('tin') or '').strip(),
                    wht_exempt=bool(request.POST.get('wht_exempt')),
                    note=(request.POST.get('note') or '').strip(),
                )
                messages.success(request, f'{name} added.')
            return redirect('finance:vendors')

        if action == 'tax':
            vendor = get_object_or_404(Vendor, pk=request.POST.get('vendor_id'))
            vendor.payee_type = ('INDIVIDUAL' if request.POST.get('payee_type') == 'INDIVIDUAL'
                                 else 'COMPANY')
            vendor.tin = (request.POST.get('tin') or '').strip()[:30]
            vendor.wht_exempt = bool(request.POST.get('wht_exempt'))
            vendor.save(update_fields=['payee_type', 'tin', 'wht_exempt'])
            messages.success(request, f'Tax details for {vendor.name} saved.')
            return redirect('finance:vendors')

        if action == 'toggle':
            vendor = get_object_or_404(Vendor, pk=request.POST.get('vendor_id'))
            vendor.is_active = not vendor.is_active
            vendor.save(update_fields=['is_active'])
            return redirect('finance:vendors')

    return render(request, 'finance/vendors.html', {
        'vendors': Vendor.objects.all(),
    })


# ─────────────────────────────────────────────────────────────────────────────
# Petty cash
# ─────────────────────────────────────────────────────────────────────────────

@_require_finance_staff
def petty_cash(request):
    if request.method == 'POST':
        amount = _amount(request.POST.get('amount'))
        if not amount:
            messages.error(request, 'Enter how much went into the tin.')
        else:
            PettyCashTopUp.objects.create(
                tenant=request.tenant, amount=amount,
                note=(request.POST.get('note') or '').strip(),
                recorded_by=request.user)
            messages.success(request, f'Petty cash topped up by NGN {amount:,.2f}.')
        return redirect('finance:petty_cash')

    spending = (Expense.objects.filter(method=Expense.PETTY_CASH, status=Expense.PAID)
                .select_related('category')[:50])
    return render(request, 'finance/petty_cash.html', {
        'balance': petty_cash_balance(request.tenant),
        'topups': PettyCashTopUp.objects.select_related('recorded_by')[:50],
        'spending': spending,
        'topped_total': PettyCashTopUp.objects.aggregate(t=Sum('amount'))['t'] or Decimal('0'),
        'spent_total': Expense.objects.filter(method=Expense.PETTY_CASH, status=Expense.PAID)
                       .aggregate(t=Sum('amount'))['t'] or Decimal('0'),
    })


# ─────────────────────────────────────────────────────────────────────────────
# Budgets & statement
# ─────────────────────────────────────────────────────────────────────────────

@_require_finance_staff
def budgets(request):
    session, term = get_active_session(), get_active_term()
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'set':
            amount = _amount(request.POST.get('amount'))
            category = get_object_or_404(ExpenseCategory, pk=request.POST.get('category_id'))
            if amount is None:
                messages.error(request, 'Enter a budget above zero.')
            else:
                Budget.objects.update_or_create(
                    tenant=request.tenant, category=category, session=session, term=term,
                    defaults={'amount': amount,
                              'note': (request.POST.get('note') or '').strip()})
                messages.success(request, f'Budget for {category.name} set to NGN {amount:,.2f}.')
            return redirect('finance:budgets')
        if action == 'delete':
            Budget.objects.filter(pk=request.POST.get('budget_id')).delete()
            messages.success(request, 'Budget removed.')
            return redirect('finance:budgets')

    start = term.start_date if term and term.start_date else timezone.now().date().replace(day=1)
    end = timezone.now().date()
    report = statement(request.tenant, start, end, session=session, term=term)
    return render(request, 'finance/budgets.html', {
        'report': report, 'session': session, 'term': term,
        'categories': ExpenseCategory.objects.filter(is_active=True),
        'budgets': Budget.objects.filter(session=session, term=term).select_related('category'),
        'start': start, 'end': end,
    })


@_require_finance_staff
def income_statement(request):
    start, end = _period(request)
    session, term = get_active_session(), get_active_term()
    report = statement(request.tenant, start, end, session=session, term=term)
    quick = {
        'this_month': timezone.now().date().replace(day=1).isoformat(),
        'last_30': (timezone.now().date() - timedelta(days=30)).isoformat(),
        'today': timezone.now().date().isoformat(),
    }
    return render(request, 'finance/statement.html', {
        'report': report, 'start': start, 'end': end, 'quick': quick,
        'session': session, 'term': term,
    })
