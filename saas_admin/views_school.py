"""
saas_admin/views_school.py
──────────────────────────
School administration pages: branding, sections (Nursery & Primary,
Secondary, Darun — stored as campuses), data export and backups, and for
any staff member the in-app help centre, product tours and notices.
"""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from users.models import Class, Staff

from . import services as svc
from .models import Article, Backup, Campus, DataExport, Notice, NoticeDismissal, Tour, TourSeen


def _admin(user):
    from users.views_profile import can_manage
    return can_manage(user)


def admin_only(view):
    def wrap(request, *a, **kw):
        if not _admin(request.user):
            messages.error(request, 'Only a school administrator can open this page.')
            return redirect('dashboard:router')
        return view(request, *a, **kw)
    wrap.__name__ = view.__name__
    return login_required(wrap)


def staff_only(view):
    def wrap(request, *a, **kw):
        if not (request.user.is_superuser or getattr(request.user, 'is_staff_user', False)):
            messages.error(request, 'This page is for staff.')
            return redirect('dashboard:router')
        return view(request, *a, **kw)
    wrap.__name__ = view.__name__
    return login_required(wrap)


# ─────────────────────────────────────────────────────────────────────────────
# Branding
# ─────────────────────────────────────────────────────────────────────────────

@admin_only
def branding(request):
    t = request.tenant
    if request.method == 'POST':
        try:
            fav = request.FILES.get('favicon')
            if fav and fav.size > 512 * 1024:
                raise svc.PlatformError('The icon must be smaller than 500 KB.')
            svc.save_branding(t, request.POST, fav)
            messages.success(request, 'Branding saved.')
        except svc.PlatformError as problem:
            messages.error(request, str(problem))
        return redirect('platform_school:branding')
    from .models import Branding
    return render(request, 'saas_admin/school/branding.html', {'b': Branding.objects.filter(tenant=t).first()})


# ─────────────────────────────────────────────────────────────────────────────
# Data export & backups, sections
# ─────────────────────────────────────────────────────────────────────────────

@admin_only
def data(request):
    t = request.tenant
    if request.method == 'POST':
        a = request.POST.get('action')
        try:
            if a == 'export':
                svc.request_export(t, request.user)
                messages.success(request, 'Your export is being prepared — refresh in a minute.')
            elif a == 'backup':
                svc.make_backup(t, kind='MANUAL')
                messages.success(request, 'Backup taken.')
        except svc.PlatformError as problem:
            messages.error(request, str(problem))
        return redirect('platform_school:data')
    return render(request, 'saas_admin/school/data.html', {
        'exports': DataExport.objects.filter(tenant=t)[:10],
        'backups': Backup.objects.filter(tenant=t)[:20]})


@admin_only
def backup_file(request, pk):
    b = get_object_or_404(Backup, pk=pk, tenant=request.tenant)
    if not b.file:
        raise Http404
    return FileResponse(b.file.open('rb'), as_attachment=True,
                        filename=f'{request.tenant.subdomain}-backup-{b.created_at:%Y%m%d-%H%M}.zip')


@admin_only
def export_file(request, pk):
    ex = get_object_or_404(DataExport, pk=pk, tenant=request.tenant, status='READY')
    if not ex.file:
        raise Http404
    return FileResponse(ex.file.open('rb'), as_attachment=True, filename=f'{request.tenant.subdomain}-data.zip')


@admin_only
def campuses(request):
    t = request.tenant
    if request.method == 'POST':
        a = request.POST.get('action')
        if a == 'add' and (request.POST.get('name') or '').strip():
            Campus.objects.get_or_create(tenant=t, name=request.POST['name'].strip()[:120], defaults={
                'code': (request.POST.get('code') or '')[:10], 'address': (request.POST.get('address') or '')[:255],
                'phone': (request.POST.get('phone') or '')[:20]})
        elif a == 'assign':
            c = get_object_or_404(Campus, pk=request.POST.get('campus'), tenant=t)
            for cls in Class.objects.filter(tenant=t, pk__in=request.POST.getlist('classes')):
                svc.assign_class(c, cls)
            c.staff.add(*Staff.objects.filter(tenant=t, pk__in=request.POST.getlist('staff')))
            head = Staff.objects.filter(tenant=t, pk=request.POST.get('head') or 0).first()
            if head:
                c.head = head
                c.save(update_fields=['head'])
        messages.success(request, 'Saved.')
        return redirect('platform_school:campuses')
    return render(request, 'saas_admin/school/campuses.html', {
        'rows': svc.campus_summary(t), 'classes': Class.objects.filter(tenant=t).order_by('name')
        .prefetch_related('campuses'), 'staff': Staff.objects.filter(tenant=t).order_by('full_name')})


@login_required
def pick_campus(request):
    cid = request.POST.get('campus') or ''
    if cid and Campus.objects.filter(tenant=request.tenant, pk=cid).exists():
        request.session['campus_id'] = int(cid)
    else:
        request.session.pop('campus_id', None)
    return redirect(request.POST.get('next') or 'dashboard:router')


# ─────────────────────────────────────────────────────────────────────────────
# Help centre, tours, notices
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def help_home(request):
    q = request.GET.get('q', '')
    arts = svc.search_articles(q)
    cats = {}
    for a in arts:
        cats.setdefault(a.category, []).append(a)
    return render(request, 'saas_admin/help_home.html', {'q': q, 'cats': cats, 'inside': True, 'base': 'dashboard/base.html'})


@login_required
def help_article(request, slug):
    a = get_object_or_404(Article, slug=slug, published=True)
    if request.method == 'POST':
        field = 'helpful_yes' if request.POST.get('helpful') == 'yes' else 'helpful_no'
        Article.objects.filter(pk=a.pk).update(**{field: getattr(a, field) + 1})
        messages.success(request, 'Thanks for the feedback.')
        return redirect('platform_school:help_article', slug=slug)
    Article.objects.filter(pk=a.pk).update(views=a.views + 1)
    return render(request, 'saas_admin/help_article.html', {
        'a': a, 'body': svc.render_body(a.body), 'inside': True, 'base': 'dashboard/base.html',
        'related': Article.objects.filter(published=True, category=a.category).exclude(pk=a.pk)[:5]})


@login_required
@require_POST
def tour_seen(request, pk):
    tour = get_object_or_404(Tour, pk=pk)
    TourSeen.objects.get_or_create(tour=tour, user=request.user)
    return JsonResponse({'ok': True})


@login_required
@require_POST
def dismiss_notice(request, pk):
    n = get_object_or_404(Notice, pk=pk, dismissible=True)
    NoticeDismissal.objects.get_or_create(notice=n, user=request.user)
    return JsonResponse({'ok': True}) if request.headers.get('x-requested-with') else \
        redirect(request.META.get('HTTP_REFERER') or 'dashboard:router')
