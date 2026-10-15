"""
finance/views_shop.py
─────────────────────
The shop and canteen till, its receipts, the sales report and voids — and
the parent's control over what each child may spend.
"""
from datetime import date
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from users.models import Student

from .models import Sale, SchoolItem, SpendingLimit
from .services import shop
from .views import _get_all_children, _require_finance_staff


def _money(raw):
    try:
        value = Decimal(str(raw).replace(',', '').strip())
    except (InvalidOperation, ValueError, AttributeError):
        return None
    return value if value >= 0 else None


def _outlet(raw):
    return Sale.CANTEEN if raw == Sale.CANTEEN else Sale.SHOP


def _items_for(outlet):
    items = SchoolItem.objects.filter(is_active=True).order_by('name')
    if outlet == Sale.CANTEEN:
        return items.filter(item_type='FOOD')
    return items.exclude(item_type='FOOD')


@_require_finance_staff
def till(request):
    outlet = _outlet(request.GET.get('outlet') or request.POST.get('outlet'))

    if request.method == 'POST':
        raw = [(key[5:], value) for key, value in request.POST.items()
               if key.startswith('item_') and value not in ('', '0')]
        student = None
        if request.POST.get('student_id'):
            student = Student.objects.filter(pk=request.POST.get('student_id')).first()
            if not student:
                messages.error(request, 'That student could not be found.')
                return redirect(f"{request.path}?outlet={outlet}")
        tendered = _money(request.POST.get('tendered')) if request.POST.get('tendered') else None
        try:
            basket = shop.parse_basket(request.tenant, raw)
            sale = shop.sell(request.tenant, basket, outlet=outlet,
                             method=request.POST.get('method') or Sale.CASH,
                             student=student,
                             customer_name=(request.POST.get('customer_name') or '').strip(),
                             tendered=tendered, user=request.user)
        except shop.SaleError as problem:
            messages.error(request, str(problem))
            return redirect(f"{request.path}?outlet={outlet}")
        return redirect('finance:sale_receipt', sale_id=sale.id)

    today = timezone.localdate()
    return render(request, 'finance/till.html', {
        'outlet': outlet, 'items': _items_for(outlet),
        'methods': Sale.METHOD_CHOICES,
        'today_summary': shop.summary(request.tenant, today, today, outlet),
        'recent': Sale.objects.filter(outlet=outlet).select_related('student')[:6],
    })


@_require_finance_staff
def till_students(request):
    """Students matching a name or admission number, with what their wallet can spend."""
    q = (request.GET.get('q') or '').strip()
    outlet = _outlet(request.GET.get('outlet'))
    if len(q) < 2:
        return JsonResponse({'results': []})
    found = (Student.objects.filter(Q(full_name__icontains=q) | Q(admission_number__icontains=q))
             .select_related('class_assigned').order_by('full_name')[:10])
    results = []
    for s in found:
        wallet = shop.family_wallet(s)
        limit = shop.limits_for(s)
        canteen_left = None
        if limit and limit.daily_canteen is not None:
            canteen_left = max(limit.daily_canteen - shop.canteen_spent_today(s), Decimal('0'))
        results.append({
            'id': s.id, 'name': s.full_name, 'admission': s.admission_number,
            'class': getattr(s.class_assigned, 'name', ''),
            'balance': f'{wallet.balance:.2f}',
            'canteen_left': f'{canteen_left:.2f}' if canteen_left is not None else None,
            'shop_allowed': limit.shop_allowed if limit else True,
            'outlet': outlet,
        })
    return JsonResponse({'results': results})


@_require_finance_staff
def sale_receipt(request, sale_id):
    sale = get_object_or_404(Sale.objects.select_related('student', 'sold_by', 'voided_by'),
                             pk=sale_id)
    wallet = shop.family_wallet(sale.student) if sale.method == Sale.WALLET and sale.student \
        else None
    return render(request, 'finance/sale_receipt.html', {
        'sale': sale, 'lines': sale.lines.all(), 'wallet': wallet,
        'school': request.tenant,
    })


def _range(request):
    today = timezone.localdate()

    def read(name, fallback):
        try:
            return date.fromisoformat(request.GET.get(name) or '')
        except ValueError:
            return fallback
    start, end = read('start', today), read('end', today)
    return (end, start) if start > end else (start, end)


@_require_finance_staff
def sales(request):
    if request.method == 'POST' and request.POST.get('action') == 'void':
        sale = get_object_or_404(Sale, pk=request.POST.get('sale_id'))
        try:
            shop.void(sale, request.user, request.POST.get('reason') or '')
            messages.success(request, f'Sale {sale.number} voided. Stock returned'
                             + (' and the wallet refunded.' if sale.method == Sale.WALLET
                                else '. Give the customer their money back.'))
        except shop.SaleError as problem:
            messages.error(request, str(problem))
        return redirect(request.get_full_path())

    start, end = _range(request)
    outlet = request.GET.get('outlet') or ''
    outlet = outlet if outlet in dict(Sale.OUTLET_CHOICES) else ''
    rows = (Sale.objects.filter(created_at__date__gte=start, created_at__date__lte=end)
            .select_related('student', 'sold_by').prefetch_related('lines'))
    if outlet:
        rows = rows.filter(outlet=outlet)
    q = (request.GET.get('q') or '').strip()
    if q:
        rows = rows.filter(Q(number__icontains=q) | Q(student__full_name__icontains=q) |
                           Q(customer_name__icontains=q))
    return render(request, 'finance/sales.html', {
        'start': start, 'end': end, 'outlet': outlet, 'q': q,
        'sales': rows[:300], 'outlets': Sale.OUTLET_CHOICES,
        'summary': shop.summary(request.tenant, start, end, outlet or None),
        'low_stock': shop.low_stock(request.tenant),
    })


# ─────────────────────────────────────────────────────────────────────────────
# Parent: spending limits
# ─────────────────────────────────────────────────────────────────────────────

@login_required
@require_POST
def set_spending_limit(request):
    """A parent sets a child's daily canteen limit and whether they may use the shop."""
    child = _get_all_children(request).filter(pk=request.POST.get('child_id')).first()
    if not child:
        messages.error(request, 'Choose one of your children.')
        return redirect('finance:parent_wallet')
    raw = (request.POST.get('daily_canteen') or '').strip()
    daily = None
    if raw:
        daily = _money(raw)
        if daily is None:
            messages.error(request, 'Enter the daily canteen limit as an amount, '
                                    'or leave it blank for no limit.')
            return redirect('finance:parent_wallet')
    SpendingLimit.objects.update_or_create(
        tenant=request.tenant, student=child,
        defaults={'daily_canteen': daily, 'shop_allowed': bool(request.POST.get('shop_allowed')),
                  'set_by': request.user})
    note = f'NGN {daily:,.2f} a day at the canteen' if daily is not None else 'no canteen limit'
    messages.success(request, f'Saved for {child.full_name}: {note}.')
    return redirect('finance:parent_wallet')
