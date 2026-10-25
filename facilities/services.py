"""
facilities/services.py
──────────────────────
Procurement and facilities logic, kept out of the views so it can be tested:
roles, numbering, requisitions and approvals, purchase orders, goods
received, vendors, tagged assets with depreciation and QR codes, disposals,
stock counts, low-stock alerts, maintenance and preventive tasks,
generators, utility bills, cleaning, patrols, chemicals, kitchen stock and
hall hire.
"""
import io
import math
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.db import transaction
from django.db.models import F, Sum
from django.utils import timezone
from django.utils.html import escape

from users.models import Staff

from .models import (AssetEvent, AssetTag, Chemical, ChemicalUse, CleaningCheck, CleaningTask, Disposal,
                     FacilitySettings, GeneratorLog, GoodsReceived, GRNLine, HireBooking, HirePayment, KitchenItem,
                     KitchenMovement, MaintenanceRequest, PatrolLog, POLine, PreventiveTask, PurchaseOrder,
                     Requisition, RequisitionLine, StockAlert, StockCount, StockCountLine, UtilityBill,
                     VendorContract, VendorRating)

ZERO = Decimal('0')
CENT = Decimal('0.01')


class FacilityError(ValueError):
    pass


def settings_for(tenant):
    cfg, _ = FacilitySettings.objects.get_or_create(tenant=tenant)
    return cfg


def staff_of(user, tenant):
    if not getattr(user, 'is_authenticated', False):
        return None
    return Staff.objects.filter(user=user, tenant=tenant).first()


def is_admin(user):
    from users.views_profile import can_manage
    return can_manage(user)


def _in(user, tenant, field):
    s = staff_of(user, tenant)
    return bool(s and getattr(settings_for(tenant), field).filter(pk=s.pk).exists())


def is_store(user, tenant):
    return is_admin(user) or _in(user, tenant, 'store_keepers')


def is_maint(user, tenant):
    return is_admin(user) or _in(user, tenant, 'maintenance_team') or _in(user, tenant, 'store_keepers')


def _emails(tenant, field=None, admins=True):
    from hr.services import admins_emails
    out = set(admins_emails(tenant)) if admins else set()
    if field:
        for s in getattr(settings_for(tenant), field).select_related('user'):
            if s.user.email and s.user.is_active:
                out.add(s.user.email)
    return sorted(out)


def _email(to, subject, html):
    from communications.services import send_email
    return send_email(to, subject, html)


def dec(raw, label='Amount', allow_blank=True, minimum=ZERO):
    raw = str(raw if raw is not None else '').replace(',', '').strip()
    if not raw:
        if allow_blank:
            return None
        raise FacilityError(f'Give the {label.lower()}.')
    try:
        v = Decimal(raw)
    except InvalidOperation:
        raise FacilityError(f'{label} must be a number.')
    if minimum is not None and v < minimum:
        raise FacilityError(f'{label} cannot be less than {minimum}.')
    return v


def date_or(raw, default=None):
    try:
        return datetime.strptime(raw or '', '%Y-%m-%d').date()
    except ValueError:
        return default


def next_number(model, tenant, prefix, field='number'):
    year = timezone.localdate().year
    stem = f'{prefix}-{year}-'
    last = 0
    for v in model.objects.filter(tenant=tenant, **{f'{field}__startswith': stem}).values_list(field, flat=True):
        tail = v[len(stem):]
        if tail.isdigit():
            last = max(last, int(tail))
    return f'{stem}{last + 1:04d}'


# ─────────────────────────────────────────────────────────────────────────────
# F401 — requisitions
# ─────────────────────────────────────────────────────────────────────────────

def parse_lines(post, prefix=''):
    """Lines from a form with desc_N / qty_N / cost_N / item_N fields (N = 0, 1, 2 …)."""
    from inventory.models import Asset
    out = []
    for i in range(30):
        desc = (post.get(f'{prefix}desc_{i}') or '').strip()
        if not desc:
            continue
        qty = dec(post.get(f'{prefix}qty_{i}'), 'Quantity', allow_blank=False, minimum=Decimal('0.01'))
        cost = dec(post.get(f'{prefix}cost_{i}'), 'Price') or ZERO
        item = Asset.objects.filter(pk=post.get(f'{prefix}item_{i}') or 0).first()
        out.append((desc[:200], item, qty, cost))
    return out


def create_requisition(tenant, user, title, reason, lines, department='', needed_by=None):
    if not (title or '').strip() or not (reason or '').strip():
        raise FacilityError('Give a title and say why it is needed.')
    if not lines:
        raise FacilityError('Add at least one item.')
    with transaction.atomic():
        req = Requisition.objects.create(tenant=tenant, title=title.strip()[:160], reason=reason.strip(),
                                         department=(department or '').strip()[:80], needed_by=needed_by,
                                         requested_by=user)
        for desc, item, qty, cost in lines:
            RequisitionLine.objects.create(tenant=tenant, requisition=req, description=desc, item=item,
                                           quantity=qty, unit_cost=cost)
    cfg = settings_for(tenant)
    big = req.total > cfg.approval_limit
    to = _emails(tenant) if big else _emails(tenant, 'store_keepers', admins=not cfg.store_keepers.exists())
    for e in to:
        _email(e, f'Requisition to approve: {req.title}',
               f'<p>{escape(user.get_full_name() or user.username)} asked for {escape(req.title)} '
               f'(about ₦{req.total:,.2f}). {escape(req.reason)}</p>')
    return req


def can_approve(user, req):
    cfg = settings_for(req.tenant)
    if is_admin(user):
        return True
    return _in(user, req.tenant, 'store_keepers') and req.total <= cfg.approval_limit


def decide_requisition(req, user, approve, note=''):
    if req.status != 'SUBMITTED':
        raise FacilityError('This requisition has already been decided.')
    if not can_approve(user, req):
        raise FacilityError(f'Requisitions over ₦{settings_for(req.tenant).approval_limit:,.2f} need an '
                            f'administrator.')
    if req.requested_by_id == user.pk and not is_admin(user):
        raise FacilityError('You cannot approve your own requisition.')
    req.status = 'APPROVED' if approve else 'DECLINED'
    req.decided_by, req.decided_at, req.decision_note = user, timezone.now(), (note or '')[:255]
    req.save()
    if req.requested_by and req.requested_by.email:
        _email(req.requested_by.email, f'Requisition {req.get_status_display().lower()}: {req.title}',
               f'<p>Your requisition "{escape(req.title)}" was {escape(req.get_status_display().lower())}.'
               f'{" " + escape(note) if note else ""}</p>')
    return req


# ─────────────────────────────────────────────────────────────────────────────
# F402 purchase orders, F414 goods received, F403 vendors
# ─────────────────────────────────────────────────────────────────────────────

def create_po(tenant, user, vendor, lines, requisition=None, expected_by=None, deliver_to='', notes=''):
    if not vendor:
        raise FacilityError('Choose the vendor.')
    if requisition is not None:
        if requisition.status != 'APPROVED':
            raise FacilityError('Only an approved requisition can be ordered.')
        if not lines:
            lines = [(ln.description, ln.item, ln.quantity, ln.unit_cost) for ln in requisition.lines.all()]
    if not lines:
        raise FacilityError('Add at least one item.')
    cfg = settings_for(tenant)
    with transaction.atomic():
        po = PurchaseOrder.objects.create(tenant=tenant, number=next_number(PurchaseOrder, tenant, cfg.po_prefix),
                                          vendor=vendor, requisition=requisition, order_date=timezone.localdate(),
                                          expected_by=expected_by, deliver_to=(deliver_to or '')[:160],
                                          notes=notes or '', created_by=user)
        for desc, item, qty, price in lines:
            POLine.objects.create(tenant=tenant, order=po, description=desc, item=item, quantity=qty,
                                  unit_price=price)
        if requisition is not None:
            requisition.status = 'ORDERED'
            requisition.save(update_fields=['status'])
    return po


def po_html(po):
    rows = ''.join(f'<tr><td>{escape(ln.description)}</td><td style="text-align:right">{ln.quantity:g}</td>'
                   f'<td style="text-align:right">₦{ln.unit_price:,.2f}</td>'
                   f'<td style="text-align:right">₦{ln.amount:,.2f}</td></tr>' for ln in po.lines.all())
    return (f'<p>Dear {escape(po.vendor.contact_person or po.vendor.name)},</p><p>Please supply the following for '
            f'{escape(po.tenant.name)} under purchase order <b>{po.number}</b> dated {po.order_date:%d %B %Y}'
            f'{", by " + po.expected_by.strftime("%d %B %Y") if po.expected_by else ""}.</p>'
            f'<table border="1" cellpadding="6" cellspacing="0"><tr><th>Item</th><th>Qty</th><th>Unit price</th>'
            f'<th>Amount</th></tr>{rows}<tr><td colspan="3"><b>Total</b></td>'
            f'<td style="text-align:right"><b>₦{po.total:,.2f}</b></td></tr></table>'
            f'{"<p>Deliver to: " + escape(po.deliver_to) + "</p>" if po.deliver_to else ""}'
            f'{"<p>" + escape(po.notes) + "</p>" if po.notes else ""}'
            f'<p>Please quote {po.number} on your delivery note and invoice.</p>')


def send_po(po):
    if po.status not in ('DRAFT', 'SENT'):
        raise FacilityError('This order can no longer be sent.')
    if not po.vendor.email:
        raise FacilityError(f'{po.vendor} has no email address — print the order instead.')
    _email(po.vendor.email, f'Purchase order {po.number} from {po.tenant.name}', po_html(po))
    po.status, po.sent_at = 'SENT', timezone.now()
    po.save(update_fields=['status', 'sent_at'])


@transaction.atomic
def receive(po, user, quantities, delivery_note='', note=''):
    """quantities: {line_id: (received, rejected)}. Accepted stock lines go into the inventory ledger."""
    from inventory.models import StockMovement
    if po.status in ('CANCELLED', 'RECEIVED'):
        raise FacilityError('This order is closed.')
    lines = {ln.pk: ln for ln in po.lines.select_for_update()}
    taking = []
    for lid, (got, bad) in quantities.items():
        ln = lines.get(int(lid))
        if not ln:
            continue
        got, bad = got or ZERO, bad or ZERO
        if got <= 0 and bad <= 0:
            continue
        if got > ln.outstanding:
            raise FacilityError(f'Only {ln.outstanding:g} of "{ln.description}" are still expected.')
        taking.append((ln, got, bad))
    if not taking:
        raise FacilityError('Enter what was delivered.')
    grn = GoodsReceived.objects.create(tenant=po.tenant, order=po,
                                       number=next_number(GoodsReceived, po.tenant, 'GRN'),
                                       delivery_note=(delivery_note or '')[:60], received_by=user,
                                       note=(note or '')[:255])
    for ln, got, bad in taking:
        GRNLine.objects.create(tenant=po.tenant, receipt=grn, line=ln, quantity=got, rejected=bad)
        ln.received += got
        ln.save(update_fields=['received'])
        if ln.item_id and got == got.to_integral_value() and got > 0:
            StockMovement.objects.create(tenant=po.tenant, asset=ln.item, movement_type='IN', quantity=int(got),
                                         performed_by=user, reason=f'{grn.number} against {po.number}')
    po.status = 'RECEIVED' if all(ln.outstanding <= 0 for ln in lines.values()) else 'PART'
    po.save(update_fields=['status'])
    if po.status == 'RECEIVED' and po.requisition_id:
        Requisition.objects.filter(pk=po.requisition_id).update(status='DONE')
    return grn


def rate_vendor(vendor, user, quality, delivery, price, comment='', order=None):
    vals = []
    for v in (quality, delivery, price):
        v = str(v or '')
        if v not in ('1', '2', '3', '4', '5'):
            raise FacilityError('Score quality, delivery and price from 1 to 5.')
        vals.append(int(v))
    return VendorRating.objects.create(tenant=vendor.tenant, vendor=vendor, order=order, quality=vals[0],
                                       delivery=vals[1], price=vals[2], comment=(comment or '')[:255], by=user)


def vendor_score(vendor):
    rs = list(vendor.ratings.all())
    return round(sum(r.score for r in rs) / len(rs), 1) if rs else None


def vendor_spend(vendor):
    return sum((po.total for po in vendor.orders.exclude(status='CANCELLED').prefetch_related('lines')), ZERO)


def contracts_expiring(tenant, days=30):
    return VendorContract.objects.filter(tenant=tenant, ends__isnull=False,
                                         ends__lte=timezone.localdate() + timedelta(days=days)) \
        .select_related('vendor')


# ─────────────────────────────────────────────────────────────────────────────
# F404 depreciation, F405 QR tags, F412 ICT, F415 disposal
# ─────────────────────────────────────────────────────────────────────────────

def next_tag_code(tenant):
    last = 0
    for c in AssetTag.objects.filter(tenant=tenant, code__regex=r'^TAG-\d+$').values_list('code', flat=True):
        last = max(last, int(c[4:]))
    return f'TAG-{last + 1:05d}'


def log(tag, user, kind, text, cost=ZERO):
    return AssetEvent.objects.create(tenant_id=tag.tenant_id, tag=tag, kind=kind, text=text[:255], cost=cost or ZERO,
                                     by=user)


def _anniversary(start, n):
    """The date n years after start (29 February falls on the 28th in other years)."""
    try:
        return start.replace(year=start.year + n)
    except ValueError:
        return start.replace(year=start.year + n, day=28)


def years_held(tag, on=None):
    """
    Whole years by purchase anniversary plus the fraction of the current year,
    so on each anniversary this is exactly 1, 2, 3… and an asset reaches its
    salvage value on the last day of its useful life (days ÷ 365.25 fell
    short by a few hours, leaving a few naira on the books).
    """
    if not tag.purchase_date:
        return 0.0
    on = on or timezone.localdate()
    start = tag.purchase_date
    if on <= start:
        return 0.0
    n = on.year - start.year
    if _anniversary(start, n) > on:
        n -= 1
    a, b = _anniversary(start, n), _anniversary(start, n + 1)
    return n + (on - a).days / (b - a).days


def book_value(tag, on=None):
    cost, salvage = tag.cost or ZERO, min(tag.salvage_value or ZERO, tag.cost or ZERO)
    if not tag.purchase_date or cost <= 0:
        return cost
    y = years_held(tag, on)
    if tag.method == 'REDUCING':
        rate = float(tag.reducing_rate or 0) / 100
        value = Decimal(str(float(cost) * math.pow(1 - rate, y)))
    else:
        life = max(tag.life_years or 1, 1)
        value = cost - (cost - salvage) * Decimal(str(min(y, life) / life))
    return max(value, salvage).quantize(CENT, ROUND_HALF_UP)


def schedule(tag):
    """Year-by-year depreciation: [(year, opening, depreciation, closing)]."""
    if not tag.purchase_date or not tag.cost:
        return []
    rows = []
    start = tag.purchase_date
    years = tag.life_years if tag.method == 'STRAIGHT' else max(tag.life_years, 1)
    for n in range(1, years + 1):
        a, b = _anniversary(start, n - 1), _anniversary(start, n)
        opening, closing = book_value(tag, a), book_value(tag, b)
        rows.append((n, opening, opening - closing, closing))
    return rows


def register_totals(tags):
    cost = sum((t.cost or ZERO for t in tags), ZERO)
    value = sum((book_value(t) for t in tags), ZERO)
    return {'cost': cost, 'value': value, 'depreciation': cost - value}


def add_tag(tenant, user, data, item=None, space=None, vendor=None):
    name = (data.get('name') or (item.name if item else '')).strip()
    if not name:
        raise FacilityError('Name the asset.')
    code = (data.get('code') or '').strip().upper() or next_tag_code(tenant)
    if AssetTag.objects.filter(tenant=tenant, code=code).exists():
        raise FacilityError(f'Tag {code} is already used.')
    life = str(data.get('life_years') or '5')
    t = AssetTag.objects.create(
        tenant=tenant, code=code[:30], name=name[:160], item=item, space=space, vendor=vendor,
        category=(data.get('category') or '').strip()[:60], serial_number=(data.get('serial_number') or '')[:80],
        purchase_date=date_or(data.get('purchase_date')), cost=dec(data.get('cost'), 'Cost') or ZERO,
        salvage_value=dec(data.get('salvage_value'), 'Salvage value') or ZERO,
        life_years=int(life) if life.isdigit() and int(life) > 0 else 5,
        method='REDUCING' if data.get('method') == 'REDUCING' else 'STRAIGHT',
        reducing_rate=dec(data.get('reducing_rate'), 'Rate') or Decimal('25'),
        warranty_until=date_or(data.get('warranty_until')), is_ict=bool(data.get('is_ict')),
        cpu=(data.get('cpu') or '')[:80], ram=(data.get('ram') or '')[:30], storage=(data.get('storage') or '')[:30],
        os=(data.get('os') or '')[:60], mac_address=(data.get('mac_address') or '')[:17],
        condition=data.get('condition') if data.get('condition') in dict(AssetTag.CONDITIONS) else 'GOOD',
        notes=data.get('notes') or '')
    log(t, user, 'ADDED', f'Added{" in " + str(space) if space else ""}')
    return t


def move_tag(tag, user, space=None, custodian=None, condition=None, note=''):
    if tag.status == 'DISPOSED':
        raise FacilityError('This asset has been disposed of.')
    if space != tag.space:
        log(tag, user, 'MOVED', f'Moved to {space or "no room"}{": " + note if note else ""}')
        tag.space = space
    if custodian != tag.custodian:
        log(tag, user, 'CUSTODY', f'Now with {custodian.get_full_name() or custodian.username}' if custodian
            else 'No custodian')
        tag.custodian = custodian
    if condition and condition != tag.condition and condition in dict(AssetTag.CONDITIONS):
        log(tag, user, 'CONDITION', f'Condition: {dict(AssetTag.CONDITIONS)[condition]}{": " + note if note else ""}')
        tag.condition = condition
    tag.save()
    return tag


def qr_svg(text, box=6):
    import qrcode
    import qrcode.image.svg
    img = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, box_size=box, border=2)
    buf = io.BytesIO()
    img.save(buf)
    svg = buf.getvalue().decode()
    return svg[svg.find('<svg'):]


def request_disposal(tag, user, method, reason, proceeds=None):
    if tag.status == 'DISPOSED':
        raise FacilityError('Already disposed of.')
    if method not in dict(Disposal.METHODS) or not (reason or '').strip():
        raise FacilityError('Choose how it is leaving and give the reason.')
    if tag.disposals.filter(status='PENDING').exists():
        raise FacilityError('A disposal is already waiting for approval.')
    d = Disposal.objects.create(tenant_id=tag.tenant_id, tag=tag, method=method, reason=reason.strip()[:255],
                                proceeds=proceeds or ZERO, book_value=book_value(tag), requested_by=user)
    for e in _emails(tag.tenant):
        _email(e, f'Asset disposal to approve: {tag}',
               f'<p>{escape(str(tag))} — {escape(d.get_method_display())}: {escape(d.reason)}. Book value '
               f'₦{d.book_value:,.2f}, proceeds ₦{d.proceeds:,.2f}.</p>')
    return d


def decide_disposal(d, user, approve):
    if d.status != 'PENDING':
        raise FacilityError('Already decided.')
    if not is_admin(user):
        raise FacilityError('Only an administrator can approve a disposal.')
    d.status, d.decided_by, d.decided_at = ('APPROVED' if approve else 'DECLINED'), user, timezone.now()
    d.save()
    if approve:
        tag = d.tag
        tag.status = 'DISPOSED'
        tag.save(update_fields=['status'])
        log(tag, user, 'DISPOSED', f'{d.get_method_display()} — written off at ₦{d.book_value:,.2f}; '
                                   f'proceeds ₦{d.proceeds:,.2f}')
    return d


# ─────────────────────────────────────────────────────────────────────────────
# F413 stock count, F410 low-stock alerts
# ─────────────────────────────────────────────────────────────────────────────

def start_count(tenant, user, title):
    from inventory.models import Asset
    if StockCount.objects.filter(tenant=tenant, closed_at__isnull=True).exists():
        raise FacilityError('A stock count is already open.')
    sc = StockCount.objects.create(tenant=tenant, title=(title or 'Stock count').strip()[:120], started_by=user)
    for a in Asset.objects.filter(tenant=tenant, asset_type='CONSUMABLE', status='ACTIVE').order_by('name'):
        StockCountLine.objects.create(tenant=tenant, count=sc, item=a, expected=a.current_stock())
    return sc


def save_counts(sc, values):
    if sc.closed_at:
        raise FacilityError('This count is closed.')
    n = 0
    for line in sc.lines.all():
        raw = str(values.get(str(line.pk), '')).strip()
        if raw == '':
            continue
        if not raw.lstrip('-').isdigit() or int(raw) < 0:
            raise FacilityError(f'Count for {line.item.name} must be a whole number.')
        line.counted = int(raw)
        line.save(update_fields=['counted'])
        n += 1
    return n


def close_count(sc, user, adjust=True):
    from inventory.models import StockMovement
    if sc.closed_at:
        raise FacilityError('Already closed.')
    lines = list(sc.lines.select_related('item'))
    if any(ln.counted is None for ln in lines):
        raise FacilityError('Count every item first (enter 0 for none).')
    with transaction.atomic():
        if adjust:
            for ln in lines:
                if ln.variance:
                    StockMovement.objects.create(tenant=sc.tenant, asset=ln.item, movement_type='ADJUST',
                                                 quantity=ln.variance, performed_by=user,
                                                 reason=f'Stock count "{sc.title}"')
        sc.closed_at, sc.adjusted = timezone.now(), adjust
        sc.save(update_fields=['closed_at', 'adjusted'])
    return variance_report(sc)


def variance_report(sc):
    rows = []
    for ln in sc.lines.select_related('item'):
        cost = last_price(ln.item)
        v = ln.variance
        rows.append({'line': ln, 'variance': v, 'value': (Decimal(v) * cost) if v is not None and cost else None})
    short = [r for r in rows if r['variance'] and r['variance'] < 0]
    return {'rows': rows, 'short': len(short), 'over': len([r for r in rows if r['variance'] and r['variance'] > 0]),
            'value': sum((r['value'] for r in rows if r['value']), ZERO)}


def last_price(item):
    ln = POLine.objects.filter(item=item, unit_price__gt=0).order_by('-order__order_date').first()
    return ln.unit_price if ln else None


def low_stock(tenant):
    from inventory.models import Asset
    items = [a for a in Asset.objects.filter(tenant=tenant, asset_type='CONSUMABLE', status='ACTIVE')
             if a.is_low_stock()]
    chems = list(Chemical.objects.filter(tenant=tenant, active=True, reorder_level__gt=0)
                 .filter(quantity__lte=F('reorder_level')))
    kitchen = list(KitchenItem.objects.filter(tenant=tenant, active=True, reorder_level__gt=0)
                   .filter(quantity__lte=F('reorder_level')))
    return items, chems, kitchen


def send_stock_alerts(tenant):
    """One WhatsApp/SMS (and email) a day per low item to the store keeper."""
    from communications.services import send_sms
    since = timezone.now() - timedelta(hours=20)
    items, chems, kitchen = low_stock(tenant)
    fresh = []
    for a in items:
        if not StockAlert.objects.filter(tenant=tenant, item=a, sent_at__gte=since).exists():
            fresh.append((f'{a.name}: {a.current_stock()} {a.unit} left (reorder at {a.reorder_level})',
                          {'item': a, 'level': a.current_stock()}))
    for c in chems:
        if not StockAlert.objects.filter(tenant=tenant, chemical=c, sent_at__gte=since).exists():
            fresh.append((f'{c.name} (lab): {c.quantity:g} {c.unit} left', {'chemical': c, 'level': c.quantity}))
    for k in kitchen:
        if not StockAlert.objects.filter(tenant=tenant, kitchen_item=k, sent_at__gte=since).exists():
            fresh.append((f'{k.name} (kitchen): {k.quantity:g} {k.unit} left', {'kitchen_item': k,
                                                                                 'level': k.quantity}))
    if not fresh:
        return 0
    text = f'{tenant.name} low stock: ' + '; '.join(t for t, _ in fresh[:6]) + \
        (f' and {len(fresh) - 6} more' if len(fresh) > 6 else '')
    phones = [p.strip() for p in settings_for(tenant).alert_phones.split(',') if p.strip()]
    for p in phones:
        send_sms(p, text[:600])
    for e in _emails(tenant, 'store_keepers', admins=not phones):
        _email(e, 'Low stock', '<ul>' + ''.join(f'<li>{escape(t)}</li>' for t, _ in fresh) + '</ul>')
    for _t, kw in fresh:
        StockAlert.objects.create(tenant=tenant, **kw)
    return len(fresh)


# ─────────────────────────────────────────────────────────────────────────────
# F406 maintenance requests, F407 preventive maintenance
# ─────────────────────────────────────────────────────────────────────────────

def report_job(tenant, user, data, photo=None, space=None, tag=None, from_task=None):
    if not (data.get('title') or '').strip():
        raise FacilityError('Say what is wrong.')
    if photo:
        from users.records import check_upload
        try:
            check_upload(photo)
        except ValueError as problem:
            raise FacilityError(str(problem))
    from .models import JOB_CATEGORIES, PRIORITIES
    job = MaintenanceRequest.objects.create(
        tenant=tenant, title=data['title'].strip()[:160], description=(data.get('description') or '').strip(),
        category=data.get('category') if data.get('category') in dict(JOB_CATEGORIES) else 'OTHER',
        priority=data.get('priority') if data.get('priority') in dict(PRIORITIES) else 'NORMAL',
        space=space, tag=tag, where=(data.get('where') or '').strip()[:120], photo=photo or '', reported_by=user,
        from_task=from_task, assigned_to=from_task.assigned_to if from_task else None,
        status='ASSIGNED' if from_task and from_task.assigned_to_id else 'OPEN')
    urgent = job.priority == 'URGENT'
    for e in _emails(tenant, 'maintenance_team', admins=urgent or not settings_for(tenant).maintenance_team.exists()):
        _email(e, f'{"URGENT " if urgent else ""}Maintenance: {job.title}',
               f'<p>{escape(job.title)} — {escape(str(space or job.where or ""))} '
               f'({escape(job.get_category_display())}, {escape(job.get_priority_display())}).</p>'
               f'<p>{escape(job.description)}</p>')
    return job


def update_job(job, user, status=None, assigned_to=None, contractor=None, resolution='', cost=None):
    if job.status == 'CLOSED':
        raise FacilityError('This job is closed.')
    if assigned_to is not None and assigned_to != job.assigned_to:
        job.assigned_to = assigned_to
        if job.status == 'OPEN':
            job.status = 'ASSIGNED'
        if assigned_to.user.email:
            _email(assigned_to.user.email, f'Job for you: {job.title}',
                   f'<p>{escape(job.title)} — {escape(str(job.space or job.where or ""))}. '
                   f'{escape(job.description)}</p>')
    if contractor is not None:
        job.contractor = contractor[:120]
    if cost is not None:
        job.cost = cost
    if resolution:
        job.resolution = resolution
    if status and status in dict(MaintenanceRequest.STATUSES):
        if status in ('DONE', 'CLOSED') and job.status not in ('DONE', 'CLOSED'):
            job.done_at = timezone.now()
            if job.tag_id:
                log(job.tag, user, 'REPAIR', f'{job.title}: {job.resolution or "fixed"}', job.cost)
            if job.from_task_id:
                complete_task(job.from_task, user)
            if job.reported_by and job.reported_by.email and job.reported_by != user:
                _email(job.reported_by.email, f'Fixed: {job.title}',
                       f'<p>The problem you reported — {escape(job.title)} — has been fixed. '
                       f'{escape(job.resolution)}</p>')
        job.status = status
    job.save()
    return job


def tasks_due(tenant, days=7):
    return PreventiveTask.objects.filter(tenant=tenant, active=True,
                                         next_due__lte=timezone.localdate() + timedelta(days=days)) \
        .select_related('space', 'tag', 'assigned_to')


def complete_task(task, user, day=None):
    day = day or timezone.localdate()
    task.last_done = day
    task.next_due = day + timedelta(days=task.every_days)
    task.save(update_fields=['last_done', 'next_due'])
    if task.tag_id:
        log(task.tag, user, 'REPAIR', f'Scheduled: {task.title}')
    return task


def raise_due_jobs(tenant):
    """Open a maintenance job for every preventive task due today or overdue (once)."""
    n = 0
    for task in PreventiveTask.objects.filter(tenant=tenant, active=True, next_due__lte=timezone.localdate()):
        if task.jobs.exclude(status__in=('DONE', 'CLOSED')).exists():
            continue
        report_job(tenant, None, {'title': task.title, 'description': task.checklist, 'category': task.category},
                   space=task.space, tag=task.tag, from_task=task)
        n += 1
    return n


# ─────────────────────────────────────────────────────────────────────────────
# F408 generators, F409 utility bills
# ─────────────────────────────────────────────────────────────────────────────

def log_generator(gen, user, data):
    day = date_or(data.get('date'), timezone.localdate())
    meter = dec(data.get('hour_meter'), 'Hour meter')
    hours = dec(data.get('run_hours'), 'Run hours')
    if hours is None and meter is not None:
        prev = gen.logs.filter(hour_meter__isnull=False, date__lte=day).order_by('-date', '-id').first()
        if prev and meter < prev.hour_meter:
            raise FacilityError(f'The hour meter cannot go back (last reading {prev.hour_meter}).')
        hours = meter - prev.hour_meter if prev else ZERO
    if hours is not None and hours > 24 * 7:
        raise FacilityError('That is more run hours than possible — check the reading.')
    litres = dec(data.get('litres_added'), 'Litres') or ZERO
    price = dec(data.get('price_per_litre'), 'Price per litre') or ZERO
    if meter is None and not (hours or litres):
        raise FacilityError('Give the run hours (or hour-meter reading) or the diesel added.')
    return GeneratorLog.objects.create(tenant_id=gen.tenant_id, generator=gen, date=day, hour_meter=meter,
                                       run_hours=hours or ZERO, litres_added=litres, price_per_litre=price,
                                       note=(data.get('note') or '')[:160], by=user)


def meter_now(gen):
    last = gen.logs.filter(hour_meter__isnull=False).order_by('-date', '-id').first()
    if last:
        return last.hour_meter
    return gen.last_service_hours + (gen.logs.aggregate(s=Sum('run_hours'))['s'] or ZERO)


def generator_stats(gen, days=30):
    since = timezone.localdate() - timedelta(days=days)
    logs = gen.logs.filter(date__gte=since)
    hours = logs.aggregate(s=Sum('run_hours'))['s'] or ZERO
    litres = logs.aggregate(s=Sum('litres_added'))['s'] or ZERO
    cost = sum((lg.cost for lg in logs), ZERO)
    since_service = meter_now(gen) - gen.last_service_hours
    return {'hours': hours, 'litres': litres, 'cost': cost,
            'lph': (litres / hours).quantize(Decimal('0.1')) if hours else None,
            'cost_per_hour': (cost / hours).quantize(CENT) if hours else None,
            'since_service': since_service, 'service_due': since_service >= gen.service_every_hours * Decimal('0.9')}


def record_service(gen, user):
    gen.last_service_hours = meter_now(gen)
    gen.save(update_fields=['last_service_hours'])
    if gen.tag_id:
        log(gen.tag, user, 'REPAIR', f'Generator serviced at {gen.last_service_hours} hours')


def add_bill(tenant, data, upload=None):
    from .models import UTILITIES
    if data.get('kind') not in dict(UTILITIES) or not (data.get('provider') or '').strip():
        raise FacilityError('Choose the utility and give the provider.')
    a, b = date_or(data.get('period_start')), date_or(data.get('period_end'))
    if not (a and b) or b < a:
        raise FacilityError('Give the billing period.')
    if upload:
        from users.records import check_upload
        try:
            check_upload(upload)
        except ValueError as problem:
            raise FacilityError(str(problem))
    return UtilityBill.objects.create(
        tenant=tenant, kind=data['kind'], provider=data['provider'].strip()[:120],
        account_number=(data.get('account_number') or '')[:60], period_start=a, period_end=b,
        units=dec(data.get('units'), 'Units'), amount=dec(data.get('amount'), 'Amount', allow_blank=False),
        due_date=date_or(data.get('due_date')), paid_on=date_or(data.get('paid_on')),
        reference=(data.get('reference') or '')[:80], file=upload or '')


def monthly_costs(tenant, months=12):
    """{'YYYY-MM': {'ELECTRICITY': x, …, 'DIESEL': y, 'total': z}} — bills by period end, diesel by log date."""
    since = (timezone.localdate().replace(day=1) - timedelta(days=31 * (months - 1))).replace(day=1)
    out = {}
    for b in UtilityBill.objects.filter(tenant=tenant, period_end__gte=since):
        m = out.setdefault(b.period_end.strftime('%Y-%m'), {'total': ZERO})
        m[b.kind] = m.get(b.kind, ZERO) + b.amount
        m['total'] += b.amount
    for lg in GeneratorLog.objects.filter(tenant=tenant, date__gte=since):
        m = out.setdefault(lg.date.strftime('%Y-%m'), {'total': ZERO})
        m['DIESEL'] = m.get('DIESEL', ZERO) + lg.cost
        m['total'] += lg.cost
    return dict(sorted(out.items()))


def bills_due(tenant, days=7):
    return UtilityBill.objects.filter(tenant=tenant, paid_on__isnull=True, due_date__isnull=False,
                                      due_date__lte=timezone.localdate() + timedelta(days=days))


# ─────────────────────────────────────────────────────────────────────────────
# F417 cleaning, F418 patrols
# ─────────────────────────────────────────────────────────────────────────────

def due_today(task, day):
    if task.frequency == 'DAILY':
        return day.weekday() < 5
    last = task.checks.filter(complete=True, date__lt=day).order_by('-date').first()
    if task.frequency == 'WEEKLY':
        return not last or (day - last.date).days >= 7
    return not last or (last.date.year, last.date.month) != (day.year, day.month)


def cleaning_sheet(tenant, day=None):
    day = day or timezone.localdate()
    rows = []
    for t in CleaningTask.objects.filter(tenant=tenant, active=True).select_related('space'):
        check = t.checks.filter(date=day).first()
        if due_today(t, day) or check:
            rows.append({'task': t, 'check': check, 'done': set(check.done) if check else set()})
    return rows


def save_check(task, user, done, day=None, note=''):
    day = day or timezone.localdate()
    items = task.items
    done = [d for d in done if d in items]
    check, _ = CleaningCheck.objects.update_or_create(
        task=task, date=day, defaults={'tenant_id': task.tenant_id, 'done': done, 'by': user,
                                       'complete': len(done) == len(items), 'note': (note or '')[:255]})
    return check


def cleaning_report(tenant, days=14):
    since = timezone.localdate() - timedelta(days=days)
    out = []
    for t in CleaningTask.objects.filter(tenant=tenant, active=True).select_related('space'):
        due = sum(1 for i in range(days) if due_today(t, since + timedelta(days=i + 1)))
        done = t.checks.filter(date__gt=since, complete=True).count()
        out.append({'task': t, 'due': due, 'done': done, 'pct': round(100 * done / due) if due else None})
    return out


def check_in(point, user, all_clear=True, note='', guard_name=''):
    if not point.active:
        raise FacilityError('This patrol point is no longer used.')
    entry = PatrolLog.objects.create(tenant_id=point.tenant_id, point=point, guard=user,
                                     guard_name=(guard_name or (user.get_full_name() if user else ''))[:80],
                                     all_clear=all_clear, note=(note or '')[:255])
    if not all_clear:
        for e in _emails(point.tenant):
            _email(e, f'Security: problem at {point.name}',
                   f'<p>{escape(entry.guard_name or "A guard")} reported a problem at {escape(point.name)} at '
                   f'{timezone.localtime(entry.at):%-I:%M %p}: {escape(entry.note or "no details")}</p>')
    return entry


def patrol_status(tenant):
    from .models import PatrolPoint
    now = timezone.now()
    rows = []
    for p in PatrolPoint.objects.filter(tenant=tenant, active=True):
        last = p.logs.first()
        late = not last or (now - last.at) > timedelta(hours=p.every_hours)
        rows.append({'point': p, 'last': last, 'late': late})
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# F411 chemicals, F419 kitchen
# ─────────────────────────────────────────────────────────────────────────────

@transaction.atomic
def chemical_move(chem, user, kind, qty, purpose='', day=None):
    chem = Chemical.objects.select_for_update().get(pk=chem.pk)
    qty = dec(qty, 'Quantity', allow_blank=False, minimum=Decimal('0.01'))
    if kind not in ('USE', 'IN', 'DISP'):
        raise FacilityError('Choose used, received or disposed.')
    if kind != 'IN' and qty > chem.quantity:
        raise FacilityError(f'Only {chem.quantity:g} {chem.unit} of {chem.name} is recorded in stock.')
    chem.quantity += qty if kind == 'IN' else -qty
    chem.save(update_fields=['quantity'])
    return ChemicalUse.objects.create(tenant_id=chem.tenant_id, chemical=chem, kind=kind, quantity=qty,
                                      purpose=(purpose or '')[:160], date=day or timezone.localdate(), by=user)


def chemicals_expiring(tenant, days=60):
    return Chemical.objects.filter(tenant=tenant, active=True, expires_on__isnull=False,
                                   expires_on__lte=timezone.localdate() + timedelta(days=days))


@transaction.atomic
def kitchen_move(item, user, kind, qty, meal='', headcount=None, day=None, unit_cost=None, note=''):
    from .models import MEALS
    item = KitchenItem.objects.select_for_update().get(pk=item.pk)
    qty = dec(qty, 'Quantity', allow_blank=False, minimum=Decimal('0.01'))
    if kind not in dict(KitchenMovement.KINDS):
        raise FacilityError('Choose received, used or wasted.')
    if kind != 'IN' and qty > item.quantity:
        raise FacilityError(f'Only {item.quantity:g} {item.unit} of {item.name} is in the store.')
    if kind == 'USE' and meal not in dict(MEALS):
        raise FacilityError('Choose the meal.')
    cost = unit_cost if unit_cost is not None else item.unit_cost
    if kind == 'IN':
        if unit_cost is not None and unit_cost > 0:
            total_qty = item.quantity + qty
            item.unit_cost = ((item.quantity * item.unit_cost + qty * unit_cost) / total_qty).quantize(CENT) \
                if total_qty else unit_cost
        item.quantity += qty
    else:
        item.quantity -= qty
    item.save(update_fields=['quantity', 'unit_cost'])
    return KitchenMovement.objects.create(tenant_id=item.tenant_id, item=item, kind=kind, quantity=qty,
                                          unit_cost=cost or ZERO, meal=meal if kind == 'USE' else '',
                                          headcount=headcount, date=day or timezone.localdate(),
                                          note=(note or '')[:160], by=user)


def meal_costs(tenant, days=30):
    since = timezone.localdate() - timedelta(days=days)
    out = {}
    for m in KitchenMovement.objects.filter(tenant=tenant, kind='USE', date__gte=since):
        key = (m.date, m.meal)
        row = out.setdefault(key, {'date': m.date, 'meal': m.meal, 'cost': ZERO, 'heads': 0})
        row['cost'] += m.quantity * m.unit_cost
        row['heads'] = max(row['heads'], m.headcount or 0)
    rows = sorted(out.values(), key=lambda r: (r['date'], r['meal']), reverse=True)
    for r in rows:
        r['per_head'] = (r['cost'] / r['heads']).quantize(CENT) if r['heads'] else None
    waste = sum((m.quantity * m.unit_cost for m in KitchenMovement.objects.filter(tenant=tenant, kind='WASTE',
                                                                                  date__gte=since)), ZERO)
    return {'rows': rows, 'waste': waste, 'total': sum((r['cost'] for r in rows), ZERO)}


# ─────────────────────────────────────────────────────────────────────────────
# F420 hall hire
# ─────────────────────────────────────────────────────────────────────────────

def quote(space, starts, ends):
    hours = Decimal(str((ends - starts).total_seconds() / 3600)).quantize(Decimal('0.01'))
    if hours <= 0:
        raise FacilityError('The end must be after the start.')
    if space.hire_rate_day and hours >= 6:
        days = Decimal(math.ceil(float(hours) / 24))
        return (space.hire_rate_day * days).quantize(CENT)
    return (space.hire_rate_hour * hours).quantize(CENT)


def clashes(space, starts, ends, exclude=None):
    out = []
    qs = HireBooking.objects.filter(space=space, status__in=('ENQUIRY', 'CONFIRMED'), starts__lt=ends, ends__gt=starts)
    if exclude:
        qs = qs.exclude(pk=exclude.pk)
    out += [f'{b.event} ({b.client}) {timezone.localtime(b.starts):%d %b %-I:%M %p}' for b in qs]
    if space.teaching_room_id:
        from timetable.models import RoomBooking
        s, e = timezone.localtime(starts), timezone.localtime(ends)
        for rb in RoomBooking.objects.filter(room_id=space.teaching_room_id, cancelled=False,
                                             date__gte=s.date(), date__lte=e.date()):
            a = timezone.make_aware(datetime.combine(rb.date, rb.start_time))
            b = timezone.make_aware(datetime.combine(rb.date, rb.end_time))
            if a < ends and b > starts:
                out.append(f'School booking: {rb.purpose} {rb.date:%d %b} {rb.start_time:%-I:%M %p}')
    return out


def create_booking(tenant, user, space, data):
    if not space or not space.hireable:
        raise FacilityError('Choose a space that can be hired.')
    try:
        starts = timezone.make_aware(datetime.strptime(data.get('starts') or '', '%Y-%m-%dT%H:%M'))
        ends = timezone.make_aware(datetime.strptime(data.get('ends') or '', '%Y-%m-%dT%H:%M'))
    except ValueError:
        raise FacilityError('Give the start and end date and time.')
    if not ((data.get('client') or '').strip() and (data.get('event') or '').strip()):
        raise FacilityError('Give the client and the event.')
    problems = clashes(space, starts, ends)
    if problems:
        raise FacilityError(f'{space} is already booked: ' + '; '.join(problems))
    amount = dec(data.get('amount'), 'Hire fee')
    guests = str(data.get('guests') or '')
    if guests.isdigit() and space.capacity and int(guests) > space.capacity:
        raise FacilityError(f'{space} holds {space.capacity} people.')
    return HireBooking.objects.create(
        tenant=tenant, space=space, client=data['client'].strip()[:160], phone=(data.get('phone') or '')[:20],
        email=(data.get('email') or '').strip()[:254], event=data['event'].strip()[:160], starts=starts, ends=ends,
        guests=int(guests) if guests.isdigit() else None,
        amount=amount if amount is not None else quote(space, starts, ends),
        extras=dec(data.get('extras'), 'Extras') or ZERO, extras_note=(data.get('extras_note') or '')[:160],
        caution_fee=dec(data.get('caution_fee'), 'Caution fee') or ZERO, notes=data.get('notes') or '',
        created_by=user)


def confirm_booking(b):
    if b.status != 'ENQUIRY':
        raise FacilityError('Only an enquiry can be confirmed.')
    problems = clashes(b.space, b.starts, b.ends, exclude=b)
    if problems:
        raise FacilityError('Clashes with: ' + '; '.join(problems))
    b.status = 'CONFIRMED'
    if not b.invoice_number:
        b.invoice_number = next_number(HireBooking, b.tenant, settings_for(b.tenant).invoice_prefix, 'invoice_number')
    b.save(update_fields=['status', 'invoice_number'])
    if b.email:
        send_invoice(b)
    return b


def invoice_html(b):
    cfg = settings_for(b.tenant)
    rows = [(f'Hire of {b.space} — {b.event}', b.amount)]
    if b.extras:
        rows.append((f'Extras{": " + b.extras_note if b.extras_note else ""}', b.extras))
    if b.caution_fee:
        rows.append(('Refundable caution fee', b.caution_fee))
    body = ''.join(f'<tr><td>{escape(d)}</td><td style="text-align:right">₦{a:,.2f}</td></tr>' for d, a in rows)
    return (f'<p>Invoice <b>{b.invoice_number}</b> from {escape(b.tenant.name)} to {escape(b.client)}.</p>'
            f'<p>{escape(b.event)}: {timezone.localtime(b.starts):%A %d %B %Y %-I:%M %p} to '
            f'{timezone.localtime(b.ends):%-I:%M %p}{" (" + timezone.localtime(b.ends).strftime("%d %b") + ")" if timezone.localtime(b.ends).date() != timezone.localtime(b.starts).date() else ""}'
            f'.</p><table border="1" cellpadding="6" cellspacing="0">{body}<tr><td><b>Total</b></td>'
            f'<td style="text-align:right"><b>₦{b.total:,.2f}</b></td></tr><tr><td>Paid</td>'
            f'<td style="text-align:right">₦{b.paid:,.2f}</td></tr><tr><td><b>Balance</b></td>'
            f'<td style="text-align:right"><b>₦{b.balance:,.2f}</b></td></tr></table>'
            f'{"<p>Pay to: " + escape(cfg.bank_details).replace(chr(10), "<br>") + "</p>" if cfg.bank_details else ""}'
            f'<p>Please quote {b.invoice_number} with your payment.</p>')


def send_invoice(b):
    if not b.email:
        raise FacilityError('There is no email address for this client.')
    _email(b.email, f'Invoice {b.invoice_number} — {b.event}', invoice_html(b))


def record_payment(b, user, amount, method='TRANSFER', reference='', day=None):
    amount = dec(amount, 'Amount', allow_blank=False, minimum=Decimal('0.01'))
    if method == 'REFUND':
        if amount > b.paid:
            raise FacilityError('You cannot refund more than was paid.')
        signed = -amount
    else:
        if amount > b.balance:
            raise FacilityError(f'The balance is only ₦{b.balance:,.2f}.')
        signed = amount
    HirePayment.objects.create(tenant_id=b.tenant_id, booking=b, amount=signed, method=method,
                               reference=(reference or '')[:80], date=day or timezone.localdate(), by=user)
    b.paid = b.payments.aggregate(s=Sum('amount'))['s'] or ZERO
    b.save(update_fields=['paid'])
    return b


def hire_income(tenant, days=365):
    since = timezone.localdate() - timedelta(days=days)
    return HirePayment.objects.filter(tenant=tenant, date__gte=since).aggregate(s=Sum('amount'))['s'] or ZERO


# ─────────────────────────────────────────────────────────────────────────────
# F416 space usage
# ─────────────────────────────────────────────────────────────────────────────

def space_usage(tenant):
    from users.models import ENROLLED_STATUSES, Student
    from .models import Space
    rows = []
    for s in Space.objects.filter(tenant=tenant, active=True).select_related('home_class', 'teaching_room'):
        n = Student.objects.filter(class_assigned=s.home_class, status__in=ENROLLED_STATUSES).count() \
            if s.home_class_id else None
        rows.append({'space': s, 'students': n,
                     'over': bool(n and s.capacity and n > s.capacity),
                     'fill': round(100 * n / s.capacity) if n is not None and s.capacity else None,
                     'assets': s.assets.exclude(status='DISPOSED').count(),
                     'jobs': s.jobs.exclude(status__in=('DONE', 'CLOSED')).count(),
                     'hires': s.hires.filter(status='CONFIRMED', starts__gte=timezone.now()).count()})
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# Daily reminders
# ─────────────────────────────────────────────────────────────────────────────

def send_reminders(tenant):
    counts = {'jobs': raise_due_jobs(tenant), 'alerts': send_stock_alerts(tenant), 'contracts': 0, 'bills': 0}
    now = timezone.now()
    lines = []
    for c in contracts_expiring(tenant).filter(reminded_at__isnull=True):
        lines.append(f'Contract "{c.title}" with {c.vendor} ends {c.ends:%d %b %Y}')
        c.reminded_at = now
        c.save(update_fields=['reminded_at'])
        counts['contracts'] += 1
    for b in bills_due(tenant).filter(reminded_at__isnull=True):
        lines.append(f'{b.get_kind_display()} bill from {b.provider}: ₦{b.amount:,.2f} due {b.due_date:%d %b}')
        b.reminded_at = now
        b.save(update_fields=['reminded_at'])
        counts['bills'] += 1
    for c in chemicals_expiring(tenant, 30).filter(expires_on__gte=timezone.localdate() - timedelta(days=1),
                                                   expires_on__lte=timezone.localdate() + timedelta(days=30)):
        if c.expires_on == timezone.localdate() + timedelta(days=30) or c.expires_on == timezone.localdate():
            lines.append(f'Lab chemical {c.name} expires {c.expires_on:%d %b %Y}')
    if lines:
        for e in _emails(tenant, 'store_keepers'):
            _email(e, f'{tenant.name}: facilities reminders',
                   '<ul>' + ''.join(f'<li>{escape(x)}</li>' for x in lines) + '</ul>')
    return counts


def run_reminders():
    from tenants.middleware import clear_current_tenant, set_current_tenant
    from tenants.models import Tenant
    total = {'jobs': 0, 'alerts': 0, 'contracts': 0, 'bills': 0}
    for t in Tenant.objects.filter(status__in=('ACTIVE', 'TRIAL')):
        set_current_tenant(t)
        try:
            for k, v in send_reminders(t).items():
                total[k] += v
        finally:
            clear_current_tenant()
    return total

