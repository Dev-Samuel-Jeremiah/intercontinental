"""
finance/views_tax.py
────────────────────
Withholding tax and VAT: the monthly deductions, what is still owed to each
tax authority, recording each remittance, the schedule to file with it, and
the school's own rate table.
"""
import calendar
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify

from .models import Expense, WhtRate, WhtRemittance
from .services import tax
from .views import _require_finance_staff

MAX_RECEIPT_BYTES = 5 * 1024 * 1024


def _month(raw):
    """'2026-09' → (1 Sep 2026, 30 Sep 2026). Defaults to last month — the one being filed."""
    today = timezone.localdate()
    try:
        year, month = (int(x) for x in (raw or '').split('-'))
        first = date(year, month, 1)
    except (ValueError, TypeError):
        first = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
    last = first.replace(day=calendar.monthrange(first.year, first.month)[1])
    return first, last


def _rate(raw):
    try:
        value = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError):
        return None
    return value if Decimal('0') <= value <= Decimal('100') else None


def _back(first):
    return redirect(reverse('finance:tax') + f'?month={first:%Y-%m}')


@_require_finance_staff
def tax_centre(request):
    tax.ensure_rate_table(request.tenant)
    first, last = _month(request.GET.get('month') or request.POST.get('month'))

    if request.method == 'POST':
        action = request.POST.get('action')

        if action == 'remit':
            authority = request.POST.get('authority')
            receipt = request.FILES.get('receipt')
            if authority not in dict(Expense.AUTHORITY_CHOICES):
                messages.error(request, 'Choose which tax authority was paid.')
            elif receipt and receipt.size > MAX_RECEIPT_BYTES:
                messages.error(request, 'The receipt file is larger than 5 MB.')
            else:
                try:
                    paid_on = date.fromisoformat(request.POST.get('paid_on') or '')
                except ValueError:
                    paid_on = None
                try:
                    remittance = tax.record_remittance(
                        request.tenant, authority, first, last, paid_on=paid_on,
                        reference=(request.POST.get('reference') or '').strip()[:80],
                        method=request.POST.get('method') or Expense.TRANSFER,
                        receipt=receipt, note=(request.POST.get('note') or '').strip()[:255],
                        user=request.user)
                except ValueError as problem:
                    messages.error(request, str(problem))
                else:
                    messages.success(request,
                                     f'NGN {remittance.amount:,.2f} paid to '
                                     f'{remittance.get_authority_display()} for '
                                     f'{first:%B %Y} recorded.')
            return _back(first)

        if action == 'undo_remit':
            remittance = get_object_or_404(WhtRemittance, pk=request.POST.get('remittance_id'))
            tax.undo_remittance(remittance)
            messages.success(request, 'Remittance removed — those deductions are owed again.')
            return _back(first)

        if action == 'save_rates':
            bad = []
            for rate in WhtRate.objects.all():
                company = _rate(request.POST.get(f'company_{rate.id}'))
                individual = _rate(request.POST.get(f'individual_{rate.id}'))
                if company is None or individual is None:
                    bad.append(rate.name)
                    continue
                rate.company_rate, rate.individual_rate = company, individual
                rate.is_active = bool(request.POST.get(f'active_{rate.id}'))
                rate.save(update_fields=['company_rate', 'individual_rate', 'is_active'])
            name = (request.POST.get('new_name') or '').strip()[:120]
            if name:
                company = _rate(request.POST.get('new_company'))
                individual = _rate(request.POST.get('new_individual'))
                if company is None or individual is None:
                    bad.append(name)
                elif WhtRate.objects.filter(name__iexact=name).exists():
                    messages.error(request, f'"{name}" is already in the table.')
                else:
                    WhtRate.objects.create(tenant=request.tenant, name=name,
                                           company_rate=company, individual_rate=individual)
            if bad:
                messages.error(request, 'Rates must be between 0 and 100. Not saved: '
                                        + ', '.join(bad) + '.')
            else:
                messages.success(request, 'Rate table saved. New expenses use these rates; '
                                          'ones already recorded keep the rate they used.')
            return _back(first)

    groups = []
    for key, label in Expense.AUTHORITY_CHOICES:
        rows = list(tax.deductions(request.tenant, first, last, key))
        groups.append({
            'key': key, 'label': label, 'rows': rows,
            'total': sum((r.wht_amount for r in rows), Decimal('0.00')),
            'waiting': sum((r.wht_amount for r in rows if not r.wht_remittance_id),
                           Decimal('0.00')),
        })

    return render(request, 'finance/tax.html', {
        'first': first, 'last': last,
        'groups': groups,
        'position': tax.position(request.tenant),
        'remittances': WhtRemittance.objects.select_related('recorded_by')[:24],
        'rates': WhtRate.objects.all(),
        'vat_paid': tax.vat_paid(request.tenant, first, last),
        # Tax is nearly always paid by transfer, so that comes first.
        'methods': sorted((m for m in Expense.METHOD_CHOICES if m[0] != Expense.PETTY_CASH),
                          key=lambda m: m[0] != Expense.TRANSFER),
        'today': timezone.localdate(),
    })


@_require_finance_staff
def wht_schedule_csv(request):
    first, last = _month(request.GET.get('month'))
    authority = request.GET.get('authority')
    if authority not in dict(Expense.AUTHORITY_CHOICES):
        authority = Expense.FEDERAL
    school = slugify(getattr(request.tenant, 'name', '') or 'school')[:40]
    who = 'nrs' if authority == Expense.FEDERAL else 'state-irs'
    response = HttpResponse(tax.schedule_csv(request.tenant, first, last, authority),
                            content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = \
        f'attachment; filename="{school}-wht-schedule-{who}-{first:%Y-%m}.csv"'
    return response
