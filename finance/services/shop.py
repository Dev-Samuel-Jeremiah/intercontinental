"""
finance/services/shop.py
────────────────────────
The school shop and canteen till.

A sale is a basket of items paid for one of two ways:

  • from the family wallet — the child (or parent) is identified, the wallet
    the siblings share is debited, and the parent sees it on their statement;
  • in cash, by transfer or by card — new money taken at the till.

Parents can cap what a child spends at the canteen each day, and can stop a
child buying from the shop on the wallet at all. The till enforces both;
a parent buying through their own wallet page is not limited by them.

Stock is locked while a sale is made, so two tills selling the last blazer at
the same moment cannot both succeed. A voided sale puts the stock back and
returns wallet money to the wallet.

Money: wallet sales spend money the school already received when the wallet
was topped up, so they are not counted as income a second time. Cash,
transfer and card sales are new income, reported as shop takings.
"""
from collections import defaultdict
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import Count, F, Sum
from django.utils import timezone

from finance.models import (Purchase, Sale, SaleLine, SchoolItem, SpendingLimit, Wallet,
                            _gen_ref)
from users.models import Student

ZERO = Decimal('0.00')


class SaleError(ValueError):
    """The sale cannot go through; the message says why, for the till screen."""


def family_wallet(student):
    """The wallet the siblings share — the oldest sibling's (lowest id)."""
    oldest = student
    if student.parent_email:
        oldest = (Student.objects.filter(tenant_id=student.tenant_id,
                                         parent_email=student.parent_email)
                  .order_by('id').first()) or student
    wallet = Wallet.objects.filter(tenant_id=student.tenant_id, student=oldest).first()
    if wallet:
        return wallet
    return Wallet.objects.create(tenant_id=student.tenant_id, student=oldest)


def limits_for(student):
    return SpendingLimit.objects.filter(tenant_id=student.tenant_id, student=student).first()


def canteen_spent_today(student):
    today = timezone.localdate()
    return (Sale.objects.filter(tenant_id=student.tenant_id, student=student,
                                outlet=Sale.CANTEEN, status=Sale.COMPLETED,
                                created_at__date=today)
            .aggregate(t=Sum('total'))['t'] or ZERO)


def check_limits(student, outlet, total, method):
    """Raise SaleError when the parent's limits forbid this sale."""
    if method != Sale.WALLET:
        return
    limit = limits_for(student)
    if not limit:
        return
    if outlet == Sale.SHOP and not limit.shop_allowed:
        raise SaleError(f"{student.full_name}'s parent has not allowed shop purchases on the "
                        f"wallet.")
    if outlet == Sale.CANTEEN and limit.daily_canteen is not None:
        left = limit.daily_canteen - canteen_spent_today(student)
        if total > left:
            raise SaleError(f"{student.full_name} can spend NGN {max(left, ZERO):,.2f} more at "
                            f"the canteen today (daily limit NGN {limit.daily_canteen:,.2f}).")


def _next_number(tenant, outlet):
    """
    The next receipt number for today, e.g. S261002-0007.

    Continues from the highest number already used today rather than counting
    today's sales, so it never repeats a number — and a retry after two tills
    clash moves on to the next one instead of trying the same number again.
    """
    prefix = f"{'C' if outlet == Sale.CANTEEN else 'S'}{timezone.localdate():%y%m%d}-"
    used = Sale.objects.filter(tenant=tenant, number__startswith=prefix) \
        .values_list('number', flat=True)
    highest = max((int(n[len(prefix):]) for n in used if n[len(prefix):].isdigit()), default=0)
    return f'{prefix}{highest + 1:04d}'


def parse_basket(tenant, raw):
    """
    [(item_id, qty), …] from the till → [(SchoolItem, qty), …], merging
    repeats and rejecting anything that is not this school's active item.
    """
    wanted = defaultdict(int)
    for item_id, qty in raw:
        try:
            item_id, qty = int(item_id), int(qty)
        except (TypeError, ValueError):
            raise SaleError('A quantity in the basket is not a whole number.')
        if qty < 1:
            raise SaleError('Quantities must be at least 1.')
        wanted[item_id] += qty
    if not wanted:
        raise SaleError('The basket is empty.')
    items = {i.id: i for i in SchoolItem.objects.filter(tenant=tenant, id__in=wanted,
                                                        is_active=True)}
    missing = set(wanted) - set(items)
    if missing:
        raise SaleError('An item in the basket is no longer on sale.')
    return [(items[i], q) for i, q in wanted.items()]


@transaction.atomic
def sell(tenant, basket, *, outlet=Sale.SHOP, method=Sale.CASH, student=None,
         customer_name='', tendered=None, user=None, enforce_limits=True):
    """
    Make a sale. `basket` is [(SchoolItem, qty), …] — see parse_basket.
    Returns the Sale. Raises SaleError with a message for the till.
    """
    if method not in dict(Sale.METHOD_CHOICES):
        raise SaleError('Choose how the customer is paying.')
    if method == Sale.WALLET and not student:
        raise SaleError('Choose the student whose family wallet pays.')
    if student and student.tenant_id != tenant.id:
        raise SaleError('That student is not at this school.')

    # Lock the items so two tills cannot sell the same last unit.
    locked = {i.id: i for i in SchoolItem.objects.select_for_update()
              .filter(tenant=tenant, id__in=[item.id for item, _ in basket])}
    lines, total, cost = [], ZERO, ZERO
    for item, qty in basket:
        item = locked[item.id]
        if item.stock_qty < qty:
            raise SaleError(f'Only {item.stock_qty} {item.name} left in stock.')
        line_total = (item.price * qty).quantize(Decimal('0.01'))
        lines.append((item, qty, line_total))
        total += line_total
        cost += item.cost_price * qty

    if enforce_limits and student:
        check_limits(student, outlet, total, method)

    if tendered is not None:
        tendered = Decimal(str(tendered))
        if method == Sale.CASH and tendered < total:
            raise SaleError(f'NGN {tendered:,.2f} is less than the total of NGN {total:,.2f}.')

    txn = None
    if method == Sale.WALLET:
        wallet = Wallet.objects.select_for_update().get(pk=family_wallet(student).pk)
        if wallet.balance < total:
            raise SaleError(f'The family wallet has NGN {wallet.balance:,.2f}; this sale is '
                            f'NGN {total:,.2f}.')
        names = ', '.join(f'{item.name} x{qty}' for item, qty, _ in lines)
        place = 'Canteen' if outlet == Sale.CANTEEN else 'School shop'
        txn = wallet.debit(total, f'{place}: {names} — {student.full_name}'[:300],
                           ref=_gen_ref('POS'), performed_by=user,
                           category='CANTEEN' if outlet == Sale.CANTEEN else 'SHOP')

    for attempt in range(5):
        try:
            with transaction.atomic():
                sale = Sale.objects.create(
                    tenant=tenant, number=_next_number(tenant, outlet), outlet=outlet,
                    student=student, customer_name=customer_name[:120], method=method,
                    total=total, cost=cost.quantize(Decimal('0.01')),
                    amount_tendered=tendered, transaction=txn, sold_by=user)
            break
        except IntegrityError:
            if attempt == 4:
                raise

    for item, qty, line_total in lines:
        SaleLine.objects.create(tenant=tenant, sale=sale, item=item, name=item.name,
                                quantity=qty, unit_price=item.price, unit_cost=item.cost_price,
                                total=line_total)
        SchoolItem.objects.filter(pk=item.pk).update(stock_qty=F('stock_qty') - qty)
        if item.inventory_asset_id:
            from inventory.models import StockMovement
            StockMovement.objects.create(
                asset=item.inventory_asset, movement_type='OUT', quantity=qty,
                performed_by=user,
                reason=f'{sale.get_outlet_display()} sale {sale.number}'
                       + (f' — {student.full_name}' if student else ''))
        if student:
            # So the parent sees what was bought, alongside wallet-page purchases.
            Purchase.objects.create(tenant=tenant, student=student, item=item, quantity=qty,
                                    unit_price=item.price, total_price=line_total,
                                    note=f'Till sale {sale.number}')
    return sale


@transaction.atomic
def void(sale, user=None, reason=''):
    """Cancel a sale: stock goes back, wallet money goes back to the wallet."""
    sale = Sale.objects.select_for_update().get(pk=sale.pk)
    if sale.status == Sale.VOIDED:
        raise SaleError('That sale was already voided.')
    if not reason.strip():
        raise SaleError('Say why the sale is being voided.')
    for line in sale.lines.select_related('item'):
        SchoolItem.objects.filter(pk=line.item_id).update(stock_qty=F('stock_qty') + line.quantity)
    if sale.method == Sale.WALLET and sale.student:
        wallet = Wallet.objects.select_for_update().get(pk=family_wallet(sale.student).pk)
        wallet.credit(sale.total, f'Voided {sale.get_outlet_display().lower()} sale {sale.number}',
                      ref=_gen_ref('VOID'), performed_by=user, category='REVERSAL')
    Purchase.objects.filter(tenant=sale.tenant, note=f'Till sale {sale.number}') \
        .update(status='REFUNDED')
    sale.status = Sale.VOIDED
    sale.voided_by = user
    sale.voided_at = timezone.now()
    sale.void_reason = reason.strip()[:255]
    sale.save(update_fields=['status', 'voided_by', 'voided_at', 'void_reason'])
    return sale


# ─────────────────────────────────────────────────────────────────────────────
# Reports
# ─────────────────────────────────────────────────────────────────────────────

def _completed(tenant, start=None, end=None, outlet=None):
    sales = Sale.objects.filter(tenant=tenant, status=Sale.COMPLETED)
    if start:
        sales = sales.filter(created_at__date__gte=start)
    if end:
        sales = sales.filter(created_at__date__lte=end)
    if outlet:
        sales = sales.filter(outlet=outlet)
    return sales


def takings(tenant, start=None, end=None, outlet=None):
    """New money taken at the till — everything except wallet sales."""
    return (_completed(tenant, start, end, outlet).exclude(method=Sale.WALLET)
            .aggregate(t=Sum('total'))['t'] or ZERO).quantize(Decimal('0.01'))


def summary(tenant, start=None, end=None, outlet=None):
    sales = _completed(tenant, start, end, outlet)
    totals = sales.aggregate(total=Sum('total'), cost=Sum('cost'), n=Count('id'))
    labels = dict(Sale.METHOD_CHOICES)
    by_method = [{'method': row['method'], 'label': labels[row['method']],
                  'total': row['t'], 'count': row['n']}
                 for row in sales.values('method').annotate(t=Sum('total'), n=Count('id'))
                 .order_by('-t')]
    by_staff = list(sales.values('sold_by__first_name', 'sold_by__last_name', 'sold_by__username')
                    .annotate(t=Sum('total'), n=Count('id')).order_by('-t'))
    lines = SaleLine.objects.filter(sale__in=sales)
    best = list(lines.values('item_id', 'name').annotate(qty=Sum('quantity'), t=Sum('total'))
                .order_by('-qty')[:10])
    total = totals['total'] or ZERO
    cost = totals['cost'] or ZERO
    return {
        'count': totals['n'] or 0, 'total': total, 'cost': cost, 'profit': total - cost,
        'takings': takings(tenant, start, end, outlet),
        'from_wallets': sum((m['total'] for m in by_method if m['method'] == Sale.WALLET), ZERO),
        'by_method': by_method, 'by_staff': by_staff, 'best_sellers': best,
        'voided': Sale.objects.filter(tenant=tenant, status=Sale.VOIDED,
                                      **({'created_at__date__gte': start} if start else {}),
                                      **({'created_at__date__lte': end} if end else {}))
                              .count(),
    }


def low_stock(tenant):
    return SchoolItem.objects.filter(tenant=tenant, is_active=True,
                                     stock_qty__lte=F('reorder_level')).order_by('stock_qty')
