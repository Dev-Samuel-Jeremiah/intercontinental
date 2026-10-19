"""
moments/views.py
────────────────
School Moments pages — the feed (or grid), one post with its comments, and
the new-post page — the JSON actions behind the buttons (like, save,
comment, delete, report, pin, comments on/off), and photo/video files served
only to people who can see the post (with byte ranges, so videos can seek and
play on iPhones).
"""
import json
import mimetypes
import os
import re
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.views.decorators.http import require_POST

from . import feed
from . import media as media_mod
from .models import Comment, Media, Moment, MomentReport


def _viewer(request):
    viewer = feed.Viewer(request.user, request.tenant)
    if not viewer.allowed:
        raise Http404
    return viewer


def _json(data, status=200):
    resp = JsonResponse(data, status=status)
    resp['Cache-Control'] = 'no-store'
    return resp


def _post(viewer, pk):
    post = Moment.objects.filter(tenant=viewer.tenant, pk=pk).first()
    if post is None or not viewer.can_see(post):
        raise Http404
    return post


# ─────────────────────────────────────────────────────────────────────────────
# Pages
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def home(request):
    viewer = feed.Viewer(request.user, request.tenant)
    if not viewer.allowed:
        messages.error(request, 'School Moments is for staff, students and parents.')
        return redirect('dashboard:router')
    media_mod.requeue_stale(request.tenant)
    g = request.GET
    f = {'category': g.get('category', ''), 'class': g.get('class', ''), 'tag': (g.get('tag') or '').strip('# ')[:40],
         'saved': g.get('saved') == '1', 'view': 'grid' if g.get('view') == 'grid' else 'feed'}
    qs = feed.filter_posts(viewer, viewer.posts(), f['category'], f['class'] if f['class'].isdigit() else '',
                           f['tag'], f['saved'])
    page = Paginator(qs.select_related('school_class'), 18 if f['view'] == 'grid' else feed.PAGE).get_page(g.get('page'))
    posts = feed.decorate(viewer, page.object_list)
    base = {k: v for k, v in (('category', f['category']), ('class', f['class']), ('tag', f['tag']),
                               ('saved', '1' if f['saved'] else '')) if v}
    ctx = {'viewer': viewer, 'posts': posts, 'page': page, 'f': f, 'categories': Moment.CATEGORIES,
           'feed_query': urlencode(base), 'grid_query': urlencode({**base, 'view': 'grid'})}
    if g.get('partial') == '1':
        html = render_to_string('moments/_cards.html' if f['view'] == 'feed' else 'moments/_grid.html', ctx, request)
        return _json({'html': html, 'next': page.next_page_number() if page.has_next() else None})
    ctx.update(highlights=feed.highlights(viewer), classes=viewer.classes(),
               pending=Comment.objects.filter(post__tenant=request.tenant, status=Comment.HELD).count() if viewer.is_staff else 0,
               reports=MomentReport.objects.filter(tenant=request.tenant, resolved=False).count() if viewer.manager else 0)
    return render(request, 'moments/feed.html', ctx)


@login_required
def detail(request, pk):
    viewer = feed.Viewer(request.user, request.tenant)
    if not viewer.allowed:
        return redirect('dashboard:router')
    post = Moment.objects.filter(tenant=request.tenant, pk=pk).select_related('school_class').first()
    if post is None or not viewer.can_see(post):
        messages.error(request, "That post doesn't exist, or it's for another class.")
        return redirect('moments:home')
    post = feed.decorate(viewer, [post], comments_preview=0)[0]
    return render(request, 'moments/detail.html', {'viewer': viewer, 'post': post,
                                                     'comments': feed.comment_rows(viewer, post)})


@login_required
def new(request):
    viewer = feed.Viewer(request.user, request.tenant)
    if not viewer.is_staff:
        messages.error(request, 'Only staff can post to School Moments.')
        return redirect('moments:home')
    if request.method == 'POST':
        p = request.POST
        try:
            post = feed.create_post(viewer, p.get('caption'), p.get('category'), p.get('school_class'),
                                    request.FILES.getlist('files'), p.get('comments_on') == 'on')
        except (feed.FeedError, media_mod.MediaError) as problem:
            if request.headers.get('x-requested-with') == 'XMLHttpRequest':
                return _json({'ok': False, 'error': str(problem)}, status=400)
            messages.error(request, str(problem))
            return redirect('moments:new')
        processing = post.media.filter(status=Media.PROCESSING).exists()
        messages.success(request, 'Posted! ' + ('Your video is being prepared — it will appear in a minute or two.'
                                                if processing else 'Everyone it is for can see it now.'))
        if request.headers.get('x-requested-with') == 'XMLHttpRequest':
            return _json({'ok': True, 'url': f'/moments/{post.pk}/'})
        return redirect('moments:detail', post.pk)
    return render(request, 'moments/new.html', {
        'viewer': viewer, 'categories': Moment.CATEGORIES, 'classes': viewer.classes(),
        'max_files': media_mod.MAX_FILES, 'max_video_mb': media_mod.MAX_VIDEO // 1048576,
        'max_image_mb': media_mod.MAX_IMAGE // 1048576,
        'consent': {str(k): v for k, v in feed.photo_consent(request.tenant).items()}})


@login_required
def review(request):
    """Held comments and open reports, for staff."""
    viewer = feed.Viewer(request.user, request.tenant)
    if not viewer.is_staff:
        return redirect('moments:home')
    held = list(Comment.objects.filter(post__tenant=request.tenant, status=Comment.HELD).select_related('post'))
    reports = list(MomentReport.objects.filter(tenant=request.tenant, resolved=False).select_related('post', 'comment')) \
        if viewer.manager else []
    who = feed.names(request.tenant, [c.user_id for c in held] + [r.user_id for r in reports])
    for c in held:
        c.name = who.get(c.user_id, ('Someone',))[0]
    for r in reports:
        r.name = who.get(r.user_id, ('Someone',))[0]
    if request.method == 'POST' and viewer.manager and request.POST.get('report'):
        MomentReport.objects.filter(tenant=request.tenant, pk=request.POST.get('report')).update(resolved=True)
        messages.success(request, 'MomentReport marked as dealt with.')
        return redirect('moments:review')
    return render(request, 'moments/review.html', {'viewer': viewer, 'held': held, 'reports': reports})


# ─────────────────────────────────────────────────────────────────────────────
# Actions (JSON)
# ─────────────────────────────────────────────────────────────────────────────

def _data(request):
    if request.content_type == 'application/json':
        try:
            return json.loads(request.body or b'{}')
        except ValueError:
            return {}
    return request.POST


def _action(fn):
    @login_required
    @require_POST
    def wrapped(request, *args, **kwargs):
        try:
            viewer = _viewer(request)
            return fn(request, viewer, *args, **kwargs)
        except (feed.FeedError, media_mod.MediaError) as problem:
            return _json({'ok': False, 'error': str(problem)}, status=400)
    wrapped.__name__ = fn.__name__
    return wrapped


@_action
def like(request, viewer, pk):
    post = _post(viewer, pk)
    want = _data(request).get('like')
    liked, count = feed.toggle_like(viewer, post, None if want is None else bool(want))
    return _json({'ok': True, 'liked': liked, 'count': count})


@_action
def save(request, viewer, pk):
    return _json({'ok': True, 'saved': feed.toggle_save(viewer, _post(viewer, pk))})


@_action
def comment(request, viewer, pk):
    post = _post(viewer, pk)
    data = _data(request)
    c = feed.add_comment(viewer, post, data.get('body'), data.get('reply_to'))
    post.refresh_from_db(fields=['comment_count'])
    name = feed.names(viewer.tenant, [viewer.user.pk])[viewer.user.pk][0]
    return _json({'ok': True, 'count': post.comment_count, 'held': c.status == Comment.HELD, 'reason': c.flag_reason,
                  'comment': {'id': c.pk, 'name': name, 'initials': feed.initials(name), 'colour': feed.colour(viewer.user.pk),
                              'body': c.body, 'reply_to': c.reply_to_id}})


@_action
def comment_delete(request, viewer, comment_id):
    c = get_object_or_404(Comment.objects.select_related('post'), pk=comment_id, post__tenant=viewer.tenant)
    _post(viewer, c.post_id)
    feed.delete_comment(viewer, c)
    return _json({'ok': True, 'count': Moment.objects.get(pk=c.post_id).comment_count})


@_action
def comment_review(request, viewer, comment_id):
    c = get_object_or_404(Comment, pk=comment_id, post__tenant=viewer.tenant)
    feed.moderate_comment(viewer, c, _data(request).get('allow') in (True, '1', 'true', 'on'))
    if request.content_type != 'application/json':
        return redirect('moments:review')
    return _json({'ok': True, 'status': c.status})


@_action
def edit(request, viewer, pk):
    post = _post(viewer, pk)
    feed.edit_post(viewer, post, _data(request))
    return _json({'ok': True, 'caption_html': str(feed.caption_html(post.caption)), 'comments_on': post.comments_on,
                  'pinned': post.pinned})


@_action
def delete(request, viewer, pk):
    feed.delete_post(viewer, _post(viewer, pk))
    if request.content_type != 'application/json':
        messages.success(request, 'Moment deleted.')
        return redirect('moments:home')
    return _json({'ok': True})


@_action
def report(request, viewer, pk):
    post = _post(viewer, pk)
    data = _data(request)
    c = post.comments.filter(pk=data.get('comment')).first() if data.get('comment') else None
    feed.report(viewer, post, data.get('reason'), c)
    return _json({'ok': True})


@login_required
def likers(request, pk):
    viewer = _viewer(request)
    post = _post(viewer, pk)
    ids = list(post.likes.order_by('-created_at').values_list('user_id', flat=True)[:200])
    who = feed.names(viewer.tenant, ids)
    return _json({'ok': True, 'people': [{'name': who.get(i, ('Someone', ''))[0], 'role': who.get(i, ('', ''))[1],
                                          'initials': feed.initials(who.get(i, ('?',))[0]), 'colour': feed.colour(i)}
                                         for i in ids]})


@login_required
def status(request, pk):
    """Whether a post's videos are ready yet (polled by the page while they are being prepared)."""
    viewer = _viewer(request)
    post = _post(viewer, pk)
    return _json({'ok': True, 'processing': post.media.filter(status=Media.PROCESSING).exists()})


# ─────────────────────────────────────────────────────────────────────────────
# Files
# ─────────────────────────────────────────────────────────────────────────────

RANGE = re.compile(r'bytes=(\d*)-(\d*)$')
CHUNK = 8 * 1024 * 1024


@login_required
def file(request, media_id, which='file'):
    viewer = _viewer(request)
    m = get_object_or_404(Media.objects.select_related('post'), pk=media_id, post__tenant=viewer.tenant)
    if not viewer.can_see(m.post):
        raise Http404
    f = m.thumb if which == 'thumb' else m.file
    if not f:
        raise Http404
    try:
        path = f.path
        size = os.path.getsize(path)
    except (OSError, NotImplementedError, ValueError):
        raise Http404
    ctype = mimetypes.guess_type(path)[0] or 'application/octet-stream'
    rng = RANGE.match(request.headers.get('Range', '').strip())
    if rng and (rng.group(1) or rng.group(2)):
        start, end = rng.group(1), rng.group(2)
        if start:
            start, end = int(start), min(int(end) if end else size - 1, size - 1)
        else:                                          # the last N bytes
            start, end = max(0, size - int(end)), size - 1
        end = min(end, start + CHUNK - 1)             # browsers ask again for the rest
        if start >= size or start > end:
            resp = HttpResponse(status=416)
            resp['Content-Range'] = f'bytes */{size}'
            return resp
        with open(path, 'rb') as fh:
            fh.seek(start)
            data = fh.read(end - start + 1)
        resp = HttpResponse(data, status=206, content_type=ctype)
        resp['Content-Range'] = f'bytes {start}-{end}/{size}'
    else:
        resp = FileResponse(open(path, 'rb'), content_type=ctype)
        resp['Content-Length'] = str(size)
    resp['Accept-Ranges'] = 'bytes'
    resp['Cache-Control'] = 'private, max-age=86400'
    resp['X-Content-Type-Options'] = 'nosniff'
    return resp
