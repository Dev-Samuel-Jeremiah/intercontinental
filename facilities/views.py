"""
facilities/views.py
───────────────────
Procurement (requisitions, orders, goods received, vendors), assets (tagged
register, QR scan pages, disposals, stock counts), facilities (spaces,
maintenance, preventive tasks, generators, utilities, cleaning, patrols),
stores (chemicals, kitchen) and hall hire.
"""
from datetime import datetime

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from users.models import Class, Staff, User

from . import services as svc
from .models import (HAZARDS, JOB_CATEGORIES, MEALS, PRIORITIES, SPACE_KINDS, UTILITIES, AssetTag, Chemical,
                     CleaningTask, Disposal, Generator, HireBooking, KitchenItem, KitchenMovement, MaintenanceRequest,
                     PatrolPoint, PreventiveTask, PurchaseOrder, Requisition, Space, StockCount, UtilityBill, Vendor,
                     VendorContract)


def _deny(request, text='You do not have access to that page.'):
    messages.error(request, text)
    return redirect('dashboard:router')


def _gate(check, text='You do not have access to that page.'):
    def deco(view):
        def wrap(request, *a, **kw):
            if not check(request.user, request.tenant):
                return _deny(request, text)
            return view(request, *a, **kw)
        wrap.__name__ = view.__name__
        return login_required(wrap)
    return deco


staff_only = _gate(lambda u, t: bool(svc.staff_of(u, t)) or getattr(u, 'is_superuser', False),
                   'This page is for staff.')
store_only = _gate(svc.is_store, 'Only the store keeper or an administrator can do that.')
maint_only = _gate(svc.is_maint, 'Only the maintenance team can do that.')
admin_only = _gate(lambda u, t: svc.is_admin(u), 'Only an administrator can do that.')


def _obj(model, request, key, **extra):
    raw = request.POST.get(key) or request.GET.get(key) or 0
    return model.objects.filter(tenant=request.tenant, pk=raw, **extra).first() if str(raw).isdigit() else None


def _dt(raw):
    try:
        return timezone.make_aware(datetime.strptime(raw or '', '%Y-%m-%dT%H:%M'))
    except ValueError:
        return None


def _items(request):
    from inventory.models import Asset
    return Asset.objects.filter(tenant=request.tenant, status='ACTIVE').order_by('name')


def _staff(request):
    return Staff.objects.filter(tenant=request.tenant, user__is_active=True).order_by('full_name')


# ─────────────────────────────────────────────────────────────────────────────
# Overview
# ─────────────────────────────────────────────────────────────────────────────

@staff_only
def home(request):
    t, u = request.tenant, request.user
    store, maint = svc.is_store(u, t), svc.is_maint(u, t)
    ctx = {'store': store, 'maint': maint, 'admin': svc.is_admin(u), 'today': timezone.localdate(),
           'my_jobs': MaintenanceRequest.objects.filter(tenant=t, reported_by=u).exclude(status='CLOSED')[:5],
           'my_reqs': Requisition.objects.filter(tenant=t, requested_by=u)[:5]}
    if store:
        items, chems, kitchen = svc.low_stock(t)
        ctx.update(waiting=[r for r in Requisition.objects.filter(tenant=t, status='SUBMITTED')
                            .prefetch_related('lines') if svc.can_approve(u, r)],
                   approved=Requisition.objects.filter(tenant=t, status='APPROVED').count(),
                   open_pos=PurchaseOrder.objects.filter(tenant=t, status__in=('SENT', 'PART')).select_related('vendor'),
                   low=items, low_chem=chems, low_kitchen=kitchen, low_count=len(items) + len(chems) + len(kitchen), bills=svc.bills_due(t),
                   contracts=svc.contracts_expiring(t), disposals=Disposal.objects.filter(tenant=t, status='PENDING')
                   .select_related('tag'),
                   hires=HireBooking.objects.filter(tenant=t, status='CONFIRMED', starts__gte=timezone.now())
                   .select_related('space')[:5])
    if maint:
        ctx.update(jobs=MaintenanceRequest.objects.filter(tenant=t).exclude(status__in=('DONE', 'CLOSED'))
                   .select_related('space', 'assigned_to')[:12],
                   tasks=svc.tasks_due(t),
                   gens=[(g, svc.generator_stats(g)) for g in Generator.objects.filter(tenant=t, active=True)],
                   patrols=[p for p in svc.patrol_status(t) if p['late']])
    return render(request, 'facilities/home.html', ctx)


# ─────────────────────────────────────────────────────────────────────────────
# F401 requisitions
# ─────────────────────────────────────────────────────────────────────────────

@staff_only
def requisitions(request):
    t, u = request.tenant, request.user
    store = svc.is_store(u, t)
    if request.method == 'POST':
        try:
            if request.POST.get('action') in ('approve', 'decline'):
                req = get_object_or_404(Requisition, pk=request.POST.get('id'), tenant=t)
                svc.decide_requisition(req, u, request.POST['action'] == 'approve', request.POST.get('note'))
                messages.success(request, f'Requisition {req.get_status_display().lower()}.')
            else:
                req = svc.create_requisition(t, u, request.POST.get('title'), request.POST.get('reason'),
                                             svc.parse_lines(request.POST), request.POST.get('department'),
                                             svc.date_or(request.POST.get('needed_by')))
                messages.success(request, f'Sent for approval (about ₦{req.total:,.2f}).')
        except svc.FacilityError as problem:
            messages.error(request, str(problem))
        return redirect('facilities:requisitions')
    qs = Requisition.objects.filter(tenant=t).select_related('requested_by').prefetch_related('lines')
    if not store:
        qs = qs.filter(requested_by=u)
    status = request.GET.get('status')
    if status in dict(Requisition.STATUSES):
        qs = qs.filter(status=status)
    rows = list(qs[:80])
    for r in rows:
        r.can = r.status == 'SUBMITTED' and svc.can_approve(u, r) and (r.requested_by_id != u.pk or svc.is_admin(u))
    return render(request, 'facilities/requisitions.html', {
        'rows': rows, 'store': store, 'items': _items(request), 'line_range': range(6),
        'statuses': Requisition.STATUSES, 'status': status, 'limit': svc.settings_for(t).approval_limit})


# ─────────────────────────────────────────────────────────────────────────────
# F402 orders, F414 goods received
# ─────────────────────────────────────────────────────────────────────────────

@store_only
def orders(request):
    t = request.tenant
    if request.method == 'POST':
        try:
            req = _obj(Requisition, request, 'requisition')
            po = svc.create_po(t, request.user, _obj(Vendor, request, 'vendor'), svc.parse_lines(request.POST), req,
                               svc.date_or(request.POST.get('expected_by')), request.POST.get('deliver_to'),
                               request.POST.get('notes'))
            messages.success(request, f'{po.number} created.')
            return redirect('facilities:order', pk=po.pk)
        except svc.FacilityError as problem:
            messages.error(request, str(problem))
        return redirect('facilities:orders')
    qs = PurchaseOrder.objects.filter(tenant=t).select_related('vendor', 'requisition').prefetch_related('lines')
    if request.GET.get('status') in dict(PurchaseOrder.STATUSES):
        qs = qs.filter(status=request.GET['status'])
    return render(request, 'facilities/orders.html', {
        'rows': qs[:100], 'vendors': Vendor.objects.filter(tenant=t, active=True), 'items': _items(request),
        'approved': Requisition.objects.filter(tenant=t, status='APPROVED'), 'line_range': range(6),
        'statuses': PurchaseOrder.STATUSES, 'pre': _obj(Requisition, request, 'from')})


@store_only
def order(request, pk):
    po = get_object_or_404(PurchaseOrder.objects.select_related('vendor', 'requisition'), pk=pk, tenant=request.tenant)
    if request.method == 'POST':
        a = request.POST.get('action')
        try:
            if a == 'send':
                svc.send_po(po)
                messages.success(request, f'Emailed to {po.vendor.email}.')
            elif a == 'sent':
                po.status, po.sent_at = 'SENT', timezone.now()
                po.save(update_fields=['status', 'sent_at'])
            elif a == 'cancel' and po.status in ('DRAFT', 'SENT'):
                po.status = 'CANCELLED'
                po.save(update_fields=['status'])
            elif a == 'receive':
                qty = {}
                for ln in po.lines.all():
                    got = svc.dec(request.POST.get(f'got_{ln.pk}'), 'Quantity received')
                    bad = svc.dec(request.POST.get(f'bad_{ln.pk}'), 'Quantity rejected')
                    if got or bad:
                        qty[ln.pk] = (got, bad)
                grn = svc.receive(po, request.user, qty, request.POST.get('delivery_note'), request.POST.get('note'))
                messages.success(request, f'{grn.number} recorded. Stock items were added to the inventory ledger.')
            elif a == 'rate':
                svc.rate_vendor(po.vendor, request.user, request.POST.get('quality'), request.POST.get('delivery'),
                                request.POST.get('price'), request.POST.get('comment'), po)
                messages.success(request, 'Thanks — rating saved.')
        except svc.FacilityError as problem:
            messages.error(request, str(problem))
        return redirect('facilities:order', pk=po.pk)
    return render(request, 'facilities/order.html', {
        'po': po, 'lines': po.lines.select_related('item'), 'receipts': po.receipts.prefetch_related('lines__line'),
        'rated': po.vendor.ratings.filter(order=po).exists()})


@store_only
def order_print(request, pk):
    po = get_object_or_404(PurchaseOrder.objects.select_related('vendor'), pk=pk, tenant=request.tenant)
    return render(request, 'facilities/print.html', {'title': f'Purchase order {po.number}', 'html': svc.po_html(po),
                                                     'school': request.tenant.name})


# ─────────────────────────────────────────────────────────────────────────────
# F403 vendors
# ─────────────────────────────────────────────────────────────────────────────

VENDOR_FIELDS = ('contact_person', 'phone', 'email', 'address', 'supplies', 'tax_id', 'bank_name', 'account_name',
                 'account_number', 'notes')


@store_only
def vendors(request):
    t = request.tenant
    if request.method == 'POST':
        if not (request.POST.get('name') or '').strip():
            messages.error(request, 'Give the vendor name.')
        else:
            v = Vendor(tenant=t, name=request.POST['name'].strip()[:160])
            for f in VENDOR_FIELDS:
                setattr(v, f, (request.POST.get(f) or '').strip()[:Vendor._meta.get_field(f).max_length or 5000])
            v.save()
            messages.success(request, f'{v.name} added.')
        return redirect('facilities:vendors')
    rows = list(Vendor.objects.filter(tenant=t).prefetch_related('ratings'))
    for v in rows:
        v.score = svc.vendor_score(v)
    return render(request, 'facilities/vendors.html', {'rows': rows, 'expiring': svc.contracts_expiring(t, 60)})


@store_only
def vendor(request, pk):
    v = get_object_or_404(Vendor, pk=pk, tenant=request.tenant)
    if request.method == 'POST':
        a = request.POST.get('action')
        try:
            if a == 'contract':
                starts = svc.date_or(request.POST.get('starts'))
                if not ((request.POST.get('title') or '').strip() and starts):
                    raise svc.FacilityError('Give the contract title and start date.')
                f = request.FILES.get('file')
                if f:
                    from users.records import check_upload
                    try:
                        check_upload(f)
                    except ValueError as problem:
                        raise svc.FacilityError(str(problem))
                VendorContract.objects.create(tenant=request.tenant, vendor=v, title=request.POST['title'].strip()[:160],
                                              starts=starts, ends=svc.date_or(request.POST.get('ends')),
                                              value=svc.dec(request.POST.get('value'), 'Value') or 0, file=f or '')
            elif a == 'edit':
                v.name = (request.POST.get('name') or v.name).strip()[:160]
                for f in VENDOR_FIELDS:
                    setattr(v, f, (request.POST.get(f) or '').strip()[:Vendor._meta.get_field(f).max_length or 5000])
                v.active = bool(request.POST.get('active'))
                v.save()
            elif a == 'rate':
                svc.rate_vendor(v, request.user, request.POST.get('quality'), request.POST.get('delivery'),
                                request.POST.get('price'), request.POST.get('comment'))
            messages.success(request, 'Saved.')
        except svc.FacilityError as problem:
            messages.error(request, str(problem))
        return redirect('facilities:vendor', pk=v.pk)
    return render(request, 'facilities/vendor.html', {
        'v': v, 'score': svc.vendor_score(v), 'spend': svc.vendor_spend(v), 'contracts': v.contracts.all(),
        'ratings': v.ratings.select_related('by')[:20], 'orders': v.orders.prefetch_related('lines')[:20],
        'today': timezone.localdate()})


@store_only
def contract_file(request, pk):
    c = get_object_or_404(VendorContract, pk=pk, tenant=request.tenant)
    return _serve(c.file, 'contract')


def _serve(field, name):
    import os

    from django.http import FileResponse

    from users.records import CONTENT_TYPES
    if not field:
        raise Http404
    ext = os.path.splitext(field.name)[1].lower()
    try:
        return FileResponse(field.open('rb'), content_type=CONTENT_TYPES.get(ext, 'application/octet-stream'),
                            filename=f'{name}{ext}')
    except (FileNotFoundError, OSError):
        raise Http404


# ─────────────────────────────────────────────────────────────────────────────
# F404/F405/F412 assets, F415 disposals
# ─────────────────────────────────────────────────────────────────────────────

@store_only
def assets(request):
    t = request.tenant
    if request.method == 'POST':
        try:
            n = int(request.POST.get('count') or 1) if str(request.POST.get('count') or '1').isdigit() else 1
            if not 1 <= n <= 100:
                raise svc.FacilityError('Tag between 1 and 100 at a time.')
            made = []
            for _i in range(n):
                data = request.POST.copy()
                if n > 1:
                    data['code'] = ''
                made.append(svc.add_tag(t, request.user, data, _obj_item(request), _obj(Space, request, 'space'),
                                        _obj(Vendor, request, 'vendor')))
            messages.success(request, f'{len(made)} asset{"s" if len(made) != 1 else ""} tagged: {made[0].code}'
                                      + (f' to {made[-1].code}' if len(made) > 1 else '') + '.')
            if len(made) == 1:
                return redirect('facilities:asset', pk=made[0].pk)
        except svc.FacilityError as problem:
            messages.error(request, str(problem))
        return redirect('facilities:assets')
    qs = AssetTag.objects.filter(tenant=t).select_related('space', 'custodian')
    show = request.GET.get('show', '')
    if show == 'ict':
        qs = qs.filter(is_ict=True)
    if request.GET.get('space'):
        qs = qs.filter(space_id=request.GET['space'])
    qs = qs.filter(status='DISPOSED') if show == 'disposed' else qs.exclude(status='DISPOSED')
    if request.GET.get('q'):
        q = request.GET['q']
        qs = qs.filter(name__icontains=q) | qs.filter(code__iexact=q) | qs.filter(serial_number__iexact=q)
    rows = list(qs[:400])
    for r in rows:
        r.value = svc.book_value(r)
    return render(request, 'facilities/assets.html', {
        'rows': rows, 'totals': svc.register_totals(rows), 'show': show, 'spaces': Space.objects.filter(tenant=t),
        'items': _items(request).filter(asset_type='FIXED'), 'vendors': Vendor.objects.filter(tenant=t),
        'conditions': AssetTag.CONDITIONS, 'q': request.GET.get('q', ''), 'next_code': svc.next_tag_code(t)})


def _obj_item(request):
    from inventory.models import Asset
    raw = request.POST.get('item') or 0
    return Asset.objects.filter(tenant=request.tenant, pk=raw).first() if str(raw).isdigit() else None


@store_only
def asset(request, pk):
    tag = get_object_or_404(AssetTag.objects.select_related('space', 'custodian', 'vendor', 'item'), pk=pk,
                            tenant=request.tenant)
    if request.method == 'POST':
        a = request.POST.get('action')
        try:
            if a == 'move':
                svc.move_tag(tag, request.user, _obj(Space, request, 'space'),
                             User.objects.filter(tenant=request.tenant, pk=request.POST.get('custodian') or 0).first(),
                             request.POST.get('condition'), request.POST.get('note'))
            elif a == 'note':
                if (request.POST.get('text') or '').strip():
                    svc.log(tag, request.user, 'NOTE', request.POST['text'].strip())
            elif a == 'status' and request.POST.get('status') in ('USE', 'STORE', 'REPAIR'):
                tag.status = request.POST['status']
                tag.save(update_fields=['status'])
                svc.log(tag, request.user, 'NOTE', f'Status: {tag.get_status_display()}')
            elif a == 'dispose':
                svc.request_disposal(tag, request.user, request.POST.get('method'), request.POST.get('reason'),
                                     svc.dec(request.POST.get('proceeds'), 'Proceeds'))
                messages.success(request, 'Sent to an administrator for approval.')
                return redirect('facilities:asset', pk=tag.pk)
            messages.success(request, 'Saved.')
        except svc.FacilityError as problem:
            messages.error(request, str(problem))
        return redirect('facilities:asset', pk=tag.pk)
    return render(request, 'facilities/asset.html', {
        'tag': tag, 'value': svc.book_value(tag), 'schedule': svc.schedule(tag), 'events': tag.events.select_related('by'),
        'jobs': tag.jobs.all()[:10], 'qr': svc.qr_svg(request.build_absolute_uri(f'/facilities/tag/{tag.uid}/')),
        'spaces': Space.objects.filter(tenant=request.tenant),
        'users': User.objects.filter(tenant=request.tenant, is_staff_user=True, is_active=True).order_by('first_name'),
        'conditions': AssetTag.CONDITIONS, 'methods': Disposal.METHODS,
        'pending': tag.disposals.filter(status='PENDING').first()})


@login_required
def tag_scan(request, uid):
    """What anyone on the staff sees when they scan an asset's QR tag."""
    tag = get_object_or_404(AssetTag.objects.select_related('space', 'custodian'), uid=uid, tenant=request.tenant)
    if not (svc.staff_of(request.user, request.tenant) or request.user.is_superuser):
        raise Http404
    if request.method == 'POST':
        try:
            svc.report_job(request.tenant, request.user, request.POST, space=tag.space, tag=tag)
            messages.success(request, 'Fault reported to the maintenance team.')
        except svc.FacilityError as problem:
            messages.error(request, str(problem))
        return redirect('facilities:tag_scan', uid=uid)
    return render(request, 'facilities/tag_scan.html', {
        'tag': tag, 'events': tag.events.select_related('by')[:10], 'store': svc.is_store(request.user, request.tenant),
        'categories': JOB_CATEGORIES, 'priorities': PRIORITIES})


@store_only
def tag_labels(request):
    qs = AssetTag.objects.filter(tenant=request.tenant).exclude(status='DISPOSED')
    if request.GET.get('ids'):
        qs = qs.filter(pk__in=[i for i in request.GET['ids'].split(',') if i.isdigit()])
    elif request.GET.get('space'):
        qs = qs.filter(space_id=request.GET['space'])
    else:
        qs = qs.filter(created_at__date=timezone.localdate())
    items = [{'tag': t, 'qr': svc.qr_svg(request.build_absolute_uri(f'/facilities/tag/{t.uid}/'), box=3)}
             for t in qs[:200]]
    return render(request, 'facilities/labels.html', {'items': items, 'school': request.tenant.name})


@store_only
def disposals(request):
    if request.method == 'POST':
        d = get_object_or_404(Disposal, pk=request.POST.get('id'), tenant=request.tenant)
        try:
            svc.decide_disposal(d, request.user, request.POST.get('action') == 'approve')
            messages.success(request, 'Done.')
        except svc.FacilityError as problem:
            messages.error(request, str(problem))
        return redirect('facilities:disposals')
    return render(request, 'facilities/disposals.html', {
        'rows': Disposal.objects.filter(tenant=request.tenant).select_related('tag', 'requested_by')[:100],
        'admin': svc.is_admin(request.user)})


# ─────────────────────────────────────────────────────────────────────────────
# F413 stock count
# ─────────────────────────────────────────────────────────────────────────────

@store_only
def counts(request):
    if request.method == 'POST':
        try:
            sc = svc.start_count(request.tenant, request.user, request.POST.get('title'))
            return redirect('facilities:count', pk=sc.pk)
        except svc.FacilityError as problem:
            messages.error(request, str(problem))
        return redirect('facilities:counts')
    return render(request, 'facilities/counts.html', {'rows': StockCount.objects.filter(tenant=request.tenant)[:30]})


@store_only
def count(request, pk):
    sc = get_object_or_404(StockCount, pk=pk, tenant=request.tenant)
    if request.method == 'POST':
        try:
            svc.save_counts(sc, {k[2:]: v for k, v in request.POST.items() if k.startswith('c_')})
            if request.POST.get('action') == 'close':
                rep = svc.close_count(sc, request.user, bool(request.POST.get('adjust')))
                messages.success(request, f'Closed: {rep["short"]} item{"s" if rep["short"] != 1 else ""} short, '
                                          f'{rep["over"]} over.')
            else:
                messages.success(request, 'Counts saved.')
        except svc.FacilityError as problem:
            messages.error(request, str(problem))
        return redirect('facilities:count', pk=sc.pk)
    return render(request, 'facilities/count.html', {'sc': sc, 'rep': svc.variance_report(sc)})


# ─────────────────────────────────────────────────────────────────────────────
# F406 maintenance, F407 preventive tasks
# ─────────────────────────────────────────────────────────────────────────────

@staff_only
def maintenance(request):
    t, u = request.tenant, request.user
    maint = svc.is_maint(u, t)
    if request.method == 'POST':
        try:
            if request.POST.get('op') == 'update' and maint:
                job = get_object_or_404(MaintenanceRequest, pk=request.POST.get('id'), tenant=t)
                svc.update_job(job, u, request.POST.get('status'), _obj(Staff, request, 'assigned_to'),
                               request.POST.get('contractor'), request.POST.get('resolution') or '',
                               svc.dec(request.POST.get('cost'), 'Cost'))
                messages.success(request, 'Job updated.')
            else:
                svc.report_job(t, u, request.POST, request.FILES.get('photo'), _obj(Space, request, 'space'))
                messages.success(request, 'Reported to the maintenance team. You will get an email when it is fixed.')
        except svc.FacilityError as problem:
            messages.error(request, str(problem))
        return redirect(request.get_full_path())
    qs = MaintenanceRequest.objects.filter(tenant=t).select_related('space', 'assigned_to', 'reported_by', 'tag')
    if not maint:
        qs = qs.filter(reported_by=u)
    show = request.GET.get('show', 'open')
    qs = qs.filter(status__in=('DONE', 'CLOSED')) if show == 'done' else qs.exclude(status__in=('DONE', 'CLOSED'))
    return render(request, 'facilities/maintenance.html', {
        'rows': qs[:100], 'maint': maint, 'show': show, 'spaces': Space.objects.filter(tenant=t, active=True),
        'categories': JOB_CATEGORIES, 'priorities': PRIORITIES, 'statuses': MaintenanceRequest.STATUSES,
        'staff': _staff(request)})


@maint_only
def job_photo(request, pk):
    job = get_object_or_404(MaintenanceRequest, pk=pk, tenant=request.tenant)
    return _serve(job.photo, 'photo')


@maint_only
def preventive(request):
    t = request.tenant
    if request.method == 'POST':
        try:
            if request.POST.get('action') == 'done':
                task = get_object_or_404(PreventiveTask, pk=request.POST.get('id'), tenant=t)
                svc.complete_task(task, request.user)
                task.jobs.exclude(status__in=('DONE', 'CLOSED')).update(status='DONE', done_at=timezone.now())
                messages.success(request, f'Done — next due {task.next_due:%d %b %Y}.')
            elif request.POST.get('action') == 'stop':
                PreventiveTask.objects.filter(tenant=t, pk=request.POST.get('id')).update(active=False)
            else:
                every = str(request.POST.get('every_days') or '')
                due = svc.date_or(request.POST.get('next_due'))
                if not ((request.POST.get('title') or '').strip() and every.isdigit() and int(every) > 0 and due):
                    raise svc.FacilityError('Give the task, how often (in days) and when it is next due.')
                PreventiveTask.objects.create(
                    tenant=t, title=request.POST['title'].strip()[:160], every_days=int(every), next_due=due,
                    category=request.POST.get('category') if request.POST.get('category') in dict(JOB_CATEGORIES)
                    else 'OTHER', space=_obj(Space, request, 'space'), tag=_obj(AssetTag, request, 'tag'),
                    checklist=request.POST.get('checklist') or '', assigned_to=_obj(Staff, request, 'assigned_to'))
                messages.success(request, 'Task scheduled.')
        except svc.FacilityError as problem:
            messages.error(request, str(problem))
        return redirect('facilities:preventive')
    return render(request, 'facilities/preventive.html', {
        'rows': PreventiveTask.objects.filter(tenant=t, active=True).select_related('space', 'tag', 'assigned_to'),
        'spaces': Space.objects.filter(tenant=t, active=True), 'tags': AssetTag.objects.filter(tenant=t)
        .exclude(status='DISPOSED'), 'staff': _staff(request), 'categories': JOB_CATEGORIES,
        'today': timezone.localdate()})


# ─────────────────────────────────────────────────────────────────────────────
# F408 generators, F409 utilities
# ─────────────────────────────────────────────────────────────────────────────

@maint_only
def generators(request):
    t = request.tenant
    if request.method == 'POST':
        a = request.POST.get('action')
        try:
            if a == 'add':
                if not (request.POST.get('name') or '').strip():
                    raise svc.FacilityError('Name the generator.')
                kva = str(request.POST.get('kva') or '')
                hrs = str(request.POST.get('service_every_hours') or '250')
                Generator.objects.create(tenant=t, name=request.POST['name'].strip()[:80],
                                         kva=int(kva) if kva.isdigit() else None,
                                         service_every_hours=int(hrs) if hrs.isdigit() else 250,
                                         last_service_hours=svc.dec(request.POST.get('meter'), 'Hour meter') or 0)
            else:
                gen = get_object_or_404(Generator, pk=request.POST.get('generator'), tenant=t)
                if a == 'service':
                    svc.record_service(gen, request.user)
                else:
                    svc.log_generator(gen, request.user, request.POST)
            messages.success(request, 'Saved.')
        except svc.FacilityError as problem:
            messages.error(request, str(problem))
        return redirect('facilities:generators')
    gens = list(Generator.objects.filter(tenant=t, active=True))
    return render(request, 'facilities/generators.html', {
        'rows': [(g, svc.generator_stats(g), g.logs.select_related('by')[:10]) for g in gens],
        'today': timezone.localdate()})


@store_only
def utilities(request):
    t = request.tenant
    if request.method == 'POST':
        try:
            if request.POST.get('action') == 'paid':
                b = get_object_or_404(UtilityBill, pk=request.POST.get('id'), tenant=t)
                b.paid_on = svc.date_or(request.POST.get('paid_on'), timezone.localdate())
                b.reference = (request.POST.get('reference') or b.reference)[:80]
                b.save(update_fields=['paid_on', 'reference'])
            else:
                svc.add_bill(t, request.POST, request.FILES.get('file'))
            messages.success(request, 'Saved.')
        except svc.FacilityError as problem:
            messages.error(request, str(problem))
        return redirect('facilities:utilities')
    months = svc.monthly_costs(t)
    kinds = [k for k, _ in UTILITIES if any(k in m for m in months.values())] + \
        (['DIESEL'] if any('DIESEL' in m for m in months.values()) else [])
    labels = dict(UTILITIES, DIESEL='Generator diesel')
    return render(request, 'facilities/utilities.html', {
        'rows': UtilityBill.objects.filter(tenant=t)[:60], 'kinds': UTILITIES, 'months': months,
        'cols': [(k, labels[k]) for k in kinds], 'today': timezone.localdate()})


@store_only
def bill_file(request, pk):
    b = get_object_or_404(UtilityBill, pk=pk, tenant=request.tenant)
    return _serve(b.file, 'bill')


# ─────────────────────────────────────────────────────────────────────────────
# F416 spaces
# ─────────────────────────────────────────────────────────────────────────────

@store_only
def spaces(request):
    t = request.tenant
    if request.method == 'POST':
        s = (_obj(Space, request, 'id') if request.POST.get('id') else None) or Space(tenant=t)
        if not (request.POST.get('name') or '').strip():
            messages.error(request, 'Name the space.')
            return redirect('facilities:spaces')
        s.name = request.POST['name'].strip()[:80]
        s.building = (request.POST.get('building') or '')[:80]
        s.kind = request.POST.get('kind') if request.POST.get('kind') in dict(SPACE_KINDS) else 'OTHER'
        cap = str(request.POST.get('capacity') or '')
        s.capacity = int(cap) if cap.isdigit() else None
        s.home_class = Class.objects.filter(tenant=t, pk=request.POST.get('home_class') or 0).first()
        s.features = (request.POST.get('features') or '')[:255]
        s.hireable = bool(request.POST.get('hireable'))
        try:
            s.hire_rate_hour = svc.dec(request.POST.get('hire_rate_hour'), 'Hourly rate') or 0
            s.hire_rate_day = svc.dec(request.POST.get('hire_rate_day'), 'Daily rate') or 0
        except svc.FacilityError as problem:
            messages.error(request, str(problem))
            return redirect('facilities:spaces')
        from timetable.models import TeachingRoom
        s.teaching_room = TeachingRoom.objects.filter(tenant=t, pk=request.POST.get('teaching_room') or 0).first()
        s.save()
        messages.success(request, 'Saved.')
        return redirect('facilities:spaces')
    from timetable.models import TeachingRoom
    return render(request, 'facilities/spaces.html', {
        'rows': svc.space_usage(t), 'kinds': SPACE_KINDS, 'classes': Class.objects.filter(tenant=t).order_by('name'),
        'rooms': TeachingRoom.objects.filter(tenant=t)})


# ─────────────────────────────────────────────────────────────────────────────
# F417 cleaning, F418 patrols
# ─────────────────────────────────────────────────────────────────────────────

@staff_only
def cleaning(request):
    t = request.tenant
    maint = svc.is_maint(request.user, t)
    if request.method == 'POST':
        try:
            if request.POST.get('action') == 'task' and maint:
                if not (request.POST.get('checklist') or '').strip() or not (_obj(Space, request, 'space') or
                                                                            (request.POST.get('area') or '').strip()):
                    raise svc.FacilityError('Choose the space (or name the area) and list the jobs.')
                CleaningTask.objects.create(
                    tenant=t, space=_obj(Space, request, 'space'), area=(request.POST.get('area') or '')[:120],
                    frequency=request.POST.get('frequency') if request.POST.get('frequency') in dict(CleaningTask.FREQ)
                    else 'DAILY', checklist=request.POST['checklist'], cleaner=(request.POST.get('cleaner') or '')[:120])
            elif request.POST.get('action') == 'stop' and maint:
                CleaningTask.objects.filter(tenant=t, pk=request.POST.get('id')).update(active=False)
            else:
                task = get_object_or_404(CleaningTask, pk=request.POST.get('task'), tenant=t)
                svc.save_check(task, request.user, request.POST.getlist('done'), note=request.POST.get('note'))
            messages.success(request, 'Saved.')
        except svc.FacilityError as problem:
            messages.error(request, str(problem))
        return redirect('facilities:cleaning')
    return render(request, 'facilities/cleaning.html', {
        'sheet': svc.cleaning_sheet(t), 'report': svc.cleaning_report(t) if maint else [], 'maint': maint,
        'spaces': Space.objects.filter(tenant=t, active=True), 'freqs': CleaningTask.FREQ,
        'today': timezone.localdate()})


@maint_only
def patrols(request):
    t = request.tenant
    if request.method == 'POST':
        if request.POST.get('action') == 'stop':
            PatrolPoint.objects.filter(tenant=t, pk=request.POST.get('id')).update(active=False)
        elif (request.POST.get('name') or '').strip():
            hrs = str(request.POST.get('every_hours') or '2')
            PatrolPoint.objects.create(tenant=t, name=request.POST['name'].strip()[:80],
                                       every_hours=int(hrs) if hrs.isdigit() and int(hrs) > 0 else 2)
        return redirect('facilities:patrols')
    from .models import PatrolLog
    return render(request, 'facilities/patrols.html', {
        'rows': svc.patrol_status(t), 'logs': PatrolLog.objects.filter(tenant=t).select_related('point')[:40]})


@maint_only
def patrol_sheet(request):
    items = [{'p': p, 'qr': svc.qr_svg(request.build_absolute_uri(f'/facilities/patrol/{p.uid}/'), box=4)}
             for p in PatrolPoint.objects.filter(tenant=request.tenant, active=True)]
    return render(request, 'facilities/patrol_sheet.html', {'items': items, 'school': request.tenant.name})


@staff_only
def patrol_check(request, uid):
    p = get_object_or_404(PatrolPoint, uid=uid, tenant=request.tenant)
    if request.method == 'POST':
        try:
            svc.check_in(p, request.user, request.POST.get('all_clear') != '0', request.POST.get('note'))
            messages.success(request, f'Checked in at {p.name}.')
        except svc.FacilityError as problem:
            messages.error(request, str(problem))
        return redirect('facilities:patrol_check', uid=uid)
    return render(request, 'facilities/patrol_check.html', {'p': p, 'recent': p.logs.all()[:5]})


# ─────────────────────────────────────────────────────────────────────────────
# F411 chemicals, F419 kitchen
# ─────────────────────────────────────────────────────────────────────────────

@staff_only
def chemicals(request):
    t = request.tenant
    store = svc.is_store(request.user, t)
    if request.method == 'POST':
        try:
            if request.POST.get('action') == 'add' and store:
                if not (request.POST.get('name') or '').strip():
                    raise svc.FacilityError('Name the chemical.')
                sds = request.FILES.get('sds')
                if sds:
                    from users.records import check_upload
                    try:
                        check_upload(sds)
                    except ValueError as problem:
                        raise svc.FacilityError(str(problem))
                Chemical.objects.create(
                    tenant=t, name=request.POST['name'].strip()[:120], formula=(request.POST.get('formula') or '')[:60],
                    cas_number=(request.POST.get('cas_number') or '')[:20],
                    hazards=[h for h in request.POST.getlist('hazards') if h in dict(HAZARDS)],
                    quantity=svc.dec(request.POST.get('quantity'), 'Quantity') or 0,
                    unit=(request.POST.get('unit') or 'g')[:10],
                    reorder_level=svc.dec(request.POST.get('reorder_level'), 'Reorder level') or 0,
                    space=_obj(Space, request, 'space'), storage=(request.POST.get('storage') or '')[:160],
                    expires_on=svc.date_or(request.POST.get('expires_on')), supplier=_obj(Vendor, request, 'supplier'),
                    sds=sds or '', first_aid=(request.POST.get('first_aid') or '')[:255])
            else:
                chem = get_object_or_404(Chemical, pk=request.POST.get('id'), tenant=t)
                kind = request.POST.get('kind') if store else 'USE'
                svc.chemical_move(chem, request.user, kind, request.POST.get('quantity'), request.POST.get('purpose'))
            messages.success(request, 'Saved.')
        except svc.FacilityError as problem:
            messages.error(request, str(problem))
        return redirect('facilities:chemicals')
    rows = list(Chemical.objects.filter(tenant=t, active=True).select_related('space'))
    labels = dict(HAZARDS)
    for c in rows:
        c.hazard_labels = [labels.get(h, h) for h in c.hazards]
    from .models import ChemicalUse
    return render(request, 'facilities/chemicals.html', {
        'rows': rows, 'store': store, 'hazards': HAZARDS, 'spaces': Space.objects.filter(tenant=t, active=True),
        'vendors': Vendor.objects.filter(tenant=t, active=True), 'expiring': svc.chemicals_expiring(t),
        'uses': ChemicalUse.objects.filter(tenant=t).select_related('chemical', 'by')[:20],
        'today': timezone.localdate()})


@staff_only
def sds_file(request, pk):
    c = get_object_or_404(Chemical, pk=pk, tenant=request.tenant)
    return _serve(c.sds, 'safety-data-sheet')


@store_only
def kitchen(request):
    t = request.tenant
    if request.method == 'POST':
        try:
            if request.POST.get('action') == 'add':
                if not (request.POST.get('name') or '').strip():
                    raise svc.FacilityError('Name the item.')
                KitchenItem.objects.create(tenant=t, name=request.POST['name'].strip()[:80],
                                           unit=(request.POST.get('unit') or 'kg')[:12],
                                           reorder_level=svc.dec(request.POST.get('reorder_level'), 'Reorder level') or 0)
            else:
                item = get_object_or_404(KitchenItem, pk=request.POST.get('item'), tenant=t)
                heads = str(request.POST.get('headcount') or '')
                svc.kitchen_move(item, request.user, request.POST.get('kind'), request.POST.get('quantity'),
                                 request.POST.get('meal') or '', int(heads) if heads.isdigit() else None,
                                 svc.date_or(request.POST.get('date')), svc.dec(request.POST.get('unit_cost'), 'Price'),
                                 request.POST.get('note'))
            messages.success(request, 'Saved.')
        except svc.FacilityError as problem:
            messages.error(request, str(problem))
        return redirect('facilities:kitchen')
    return render(request, 'facilities/kitchen.html', {
        'items': KitchenItem.objects.filter(tenant=t, active=True), 'meals': MEALS, 'kinds': KitchenMovement.KINDS,
        'costs': svc.meal_costs(t), 'moves': KitchenMovement.objects.filter(tenant=t).select_related('item')[:30],
        'today': timezone.localdate()})


# ─────────────────────────────────────────────────────────────────────────────
# F420 hall hire
# ─────────────────────────────────────────────────────────────────────────────

@store_only
def hire(request):
    t = request.tenant
    if request.method == 'POST':
        try:
            b = svc.create_booking(t, request.user, _obj(Space, request, 'space'), request.POST)
            messages.success(request, f'Enquiry saved — quoted ₦{b.total:,.2f}.')
            return redirect('facilities:booking', pk=b.pk)
        except svc.FacilityError as problem:
            messages.error(request, str(problem))
        return redirect('facilities:hire')
    return render(request, 'facilities/hire.html', {
        'rows': HireBooking.objects.filter(tenant=t).select_related('space')[:80],
        'spaces': Space.objects.filter(tenant=t, active=True, hireable=True),
        'income': svc.hire_income(t)})


@store_only
def booking(request, pk):
    b = get_object_or_404(HireBooking.objects.select_related('space'), pk=pk, tenant=request.tenant)
    if request.method == 'POST':
        a = request.POST.get('action')
        try:
            if a == 'confirm':
                svc.confirm_booking(b)
                messages.success(request, f'Confirmed — invoice {b.invoice_number}' +
                                 (' emailed to the client.' if b.email else '.'))
            elif a == 'pay':
                svc.record_payment(b, request.user, request.POST.get('amount'), request.POST.get('method') or 'TRANSFER',
                                   request.POST.get('reference'), svc.date_or(request.POST.get('date')))
                messages.success(request, 'Payment recorded.')
            elif a == 'invoice':
                svc.send_invoice(b)
                messages.success(request, 'Invoice emailed.')
            elif a in ('DONE', 'CANCELLED'):
                b.status = a
                b.save(update_fields=['status'])
        except svc.FacilityError as problem:
            messages.error(request, str(problem))
        return redirect('facilities:booking', pk=b.pk)
    return render(request, 'facilities/booking.html', {'b': b, 'payments': b.payments.select_related('by'),
                                                       'today': timezone.localdate()})


@store_only
def invoice_print(request, pk):
    b = get_object_or_404(HireBooking.objects.select_related('space'), pk=pk, tenant=request.tenant)
    if not b.invoice_number:
        messages.error(request, 'Confirm the booking first to create the invoice.')
        return redirect('facilities:booking', pk=b.pk)
    return render(request, 'facilities/print.html', {'title': f'Invoice {b.invoice_number}',
                                                     'html': svc.invoice_html(b), 'school': request.tenant.name})


# ─────────────────────────────────────────────────────────────────────────────
# Settings
# ─────────────────────────────────────────────────────────────────────────────

@admin_only
def settings_page(request):
    t = request.tenant
    cfg = svc.settings_for(t)
    if request.method == 'POST':
        staff = Staff.objects.filter(tenant=t)
        cfg.store_keepers.set(staff.filter(pk__in=request.POST.getlist('store_keepers')))
        cfg.maintenance_team.set(staff.filter(pk__in=request.POST.getlist('maintenance_team')))
        cfg.alert_phones = (request.POST.get('alert_phones') or '')[:255]
        try:
            cfg.approval_limit = svc.dec(request.POST.get('approval_limit'), 'Approval limit') or 0
        except svc.FacilityError as problem:
            messages.error(request, str(problem))
            return redirect('facilities:settings')
        cfg.po_prefix = (request.POST.get('po_prefix') or 'PO').strip()[:10]
        cfg.invoice_prefix = (request.POST.get('invoice_prefix') or 'HIRE').strip()[:10]
        cfg.bank_details = request.POST.get('bank_details') or ''
        cfg.save()
        messages.success(request, 'Saved.')
        return redirect('facilities:settings')
    return render(request, 'facilities/settings.html', {
        'cfg': cfg, 'staff': _staff(request), 'keepers': set(cfg.store_keepers.values_list('pk', flat=True)),
        'team': set(cfg.maintenance_team.values_list('pk', flat=True))})
