"""
notifications/views.py
──────────────────────
The bell's JSON feed (polled by the page), read marking, a click-through
that marks a notice read and opens it, and the full notifications page.
"""
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_POST

from . import services as svc


def _json(data, status=200):
    resp = JsonResponse(data, status=status)
    resp['Cache-Control'] = 'no-store'
    return resp


@require_GET
def feed(request):
    """Unread count plus the newest notices. With ?after=<id>, only notices newer than
    that id (what the bell polls for); otherwise the latest FEED_SIZE."""
    if not request.user.is_authenticated:
        return _json({'error': 'signed out'}, status=401)
    user, tenant = request.user, request.tenant
    visible = svc.visible_to(user, tenant)
    unread_qs = svc.unread(user, tenant)
    try:
        after = int(request.GET.get('after') or 0)
    except ValueError:
        after = 0
    items = visible.filter(pk__gt=after)[:svc.FEED_SIZE] if after else visible[:svc.FEED_SIZE]
    items = list(items)
    unread_ids = set(unread_qs.filter(pk__in=[n.pk for n in items]).values_list('pk', flat=True))
    latest = visible.values_list('pk', flat=True).first() or 0
    return _json({'unread': unread_qs.count(), 'latest': latest,
                  'items': svc.serialize(items, user, unread_ids)})


@require_POST
def mark_read(request):
    if not request.user.is_authenticated:
        return _json({'error': 'signed out'}, status=401)
    if request.POST.get('all'):
        svc.mark_all_read(request.user)
    else:
        ids = [int(i) for i in request.POST.getlist('id') if i.isdigit()]
        svc.mark_read(request.user, request.tenant, ids)
    return _json({'unread': svc.unread(request.user, request.tenant).count()})


@login_required
def open_notification(request, pk):
    """Mark the notice read and go to the page it is about."""
    n = get_object_or_404(svc.visible_to(request.user, request.tenant), pk=pk)
    svc.mark_read(request.user, request.tenant, [n.pk])
    if n.url and url_has_allowed_host_and_scheme(n.url, allowed_hosts={request.get_host()}):
        return redirect(n.url)
    return redirect('notifications:all')


@login_required
def all_notifications(request):
    user, tenant = request.user, request.tenant
    if request.method == 'POST' and request.POST.get('all'):
        svc.mark_all_read(user)
        return redirect(request.get_full_path())
    show = request.GET.get('show', 'all')
    category = request.GET.get('category', '')
    qs = svc.unread(user, tenant) if show == 'unread' else svc.visible_to(user, tenant)
    if category in svc.CATEGORIES:
        qs = qs.filter(kind__in=[k for k, (c, _i) in svc.KINDS.items() if c == category])
    page = Paginator(qs, 25).get_page(request.GET.get('page'))
    unread_ids = set(svc.unread(user, tenant).filter(pk__in=[n.pk for n in page]).values_list('pk', flat=True))
    items = svc.serialize(page, user, unread_ids)
    for row, n in zip(items, page):
        row['when'] = n.created_at
    return render(request, 'notifications/all.html', {
        'page': page, 'items': items, 'show': show, 'category': category,
        'categories': svc.CATEGORIES, 'unread_total': svc.unread(user, tenant).count(),
        'role': svc.role_of(user, tenant),
    })
