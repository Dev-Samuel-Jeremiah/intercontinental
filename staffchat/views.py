"""
staffchat/views.py
──────────────────
The chat page, its JSON API (history, polling, sending, reactions, edits,
deletions, typing), starting chats, groups and complaints, group settings,
and attachments (served only to members of the chat).
"""
import json
import os

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from users.models import User

from . import chat
from .models import Message, ChatRoom


def _json(data, status=200):
    resp = JsonResponse(data, status=status)
    resp['Cache-Control'] = 'no-store'
    return resp


def _staff_only(view):
    """Signed-in staff and administrators only; JSON callers get a JSON error."""
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated or not chat.is_staff(request.user, request.tenant):
            if request.path.startswith('/staff-chat/api/'):
                return _json({'ok': False, 'error': 'Staff chat is for members of staff.'}, status=403)
            if not request.user.is_authenticated:
                return redirect('users:staff_login')
            messages.error(request, 'Staff chat is for members of staff.')
            return redirect('dashboard:router')
        return view(request, *args, **kwargs)
    wrapped.__name__ = view.__name__
    wrapped.__doc__ = view.__doc__
    return wrapped


def _int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ─────────────────────────────────────────────────────────────────────────────
# The page
# ─────────────────────────────────────────────────────────────────────────────

@login_required
@_staff_only
def home(request, pk=None):
    user, tenant = request.user, request.tenant
    chat.sync(user, tenant)
    names = chat.people(tenant)
    room = None
    if pk:
        room, member = chat.room_for(user, tenant, pk)
        if room is None:
            messages.error(request, "That chat doesn't exist, or you're not in it.")
            return redirect('staffchat:home')
    colleagues = sorted(({'id': i, 'name': n, 'role': r, 'initials': chat.initials(n), 'colour': chat.colour(i)}
                         for i, (n, r) in names.items() if i != user.pk), key=lambda p: p['name'].lower())
    return render(request, 'staffchat/chat.html', {
        'rooms': chat.rooms(user, tenant, names), 'open_room': room.pk if room else None,
        'colleagues': colleagues, 'categories': ChatRoom.CATEGORIES, 'statuses': ChatRoom.STATUSES,
        'manager': chat.is_manager(user, tenant), 'reactions': chat.REACTIONS,
        'me': {'id': user.pk, 'name': names.get(user.pk, (user.get_username(),))[0]},
    })


@login_required
@_staff_only
def direct(request, user_id):
    other = get_object_or_404(User, pk=user_id)
    try:
        room = chat.direct_room(request.user, other, request.tenant)
    except chat.ChatError as problem:
        messages.error(request, str(problem))
        return redirect('staffchat:home')
    return redirect('staffchat:room', room.pk)


@login_required
@_staff_only
@require_POST
def new_group(request):
    p = request.POST
    try:
        room = chat.create_group(request.tenant, request.user, p.get('name'), p.get('description'),
                                 p.getlist('members'), bool(p.get('only_admins_post')))
    except chat.ChatError as problem:
        messages.error(request, str(problem))
        return redirect('staffchat:home')
    return redirect('staffchat:room', room.pk)


@login_required
@_staff_only
@require_POST
def new_complaint(request):
    p = request.POST
    try:
        room = chat.create_complaint(request.tenant, request.user, p.get('subject'), p.get('category'),
                                     p.get('details'), bool(p.get('anonymous')), request.FILES.get('file'))
    except chat.ChatError as problem:
        messages.error(request, str(problem))
        return redirect('staffchat:home')
    messages.success(request, 'Your complaint has been sent to management. You can follow it up here.')
    return redirect('staffchat:room', room.pk)


@login_required
@_staff_only
@require_POST
def manage(request, pk):
    room, member = chat.room_for(request.user, request.tenant, pk)
    if room is None:
        raise Http404
    action = request.POST.get('action')
    try:
        chat.manage(room, request.user, request.tenant, action, request.POST)
    except chat.ChatError as problem:
        messages.error(request, str(problem))
    if action == 'leave':
        messages.success(request, f'You left "{room.name}".')
        return redirect('staffchat:home')
    return redirect('staffchat:room', room.pk)


@login_required
@_staff_only
def attachment(request, msg_id):
    msg = get_object_or_404(Message.objects.select_related('room'), pk=msg_id, room__tenant=request.tenant)
    if not msg.attachment or msg.deleted_at or not chat.membership(msg.room, request.user):
        raise Http404
    ext = os.path.splitext(msg.attachment.name)[1].lower()
    inline = ext in chat.IMAGES or ext == '.pdf' or chat.FILE_TYPES.get(ext, '').startswith('audio/')
    try:
        resp = FileResponse(msg.attachment.open('rb'), content_type=chat.FILE_TYPES.get(ext, 'application/octet-stream'),
                            as_attachment=not inline, filename=msg.attachment_name or f'file{ext}')
    except (FileNotFoundError, OSError):
        raise Http404
    resp['X-Content-Type-Options'] = 'nosniff'
    resp['Cache-Control'] = 'private, max-age=3600'
    return resp


# ─────────────────────────────────────────────────────────────────────────────
# JSON API
# ─────────────────────────────────────────────────────────────────────────────

def _room_or_404(request, pk):
    room, member = chat.room_for(request.user, request.tenant, pk)
    if room is None:
        raise Http404
    return room, member


@login_required
@_staff_only
@require_GET
def api_room(request, pk):
    """Open a chat: its header, members and the latest page of messages (or the page before ?before=<id>)."""
    room, member = _room_or_404(request, pk)
    view = chat.View(room, request.user, request.tenant)
    items, more = chat.history(view, _int(request.GET.get('before')))
    out = {'ok': True, 'messages': items, 'more': more, 'now': timezone.now().isoformat()}
    if not request.GET.get('before'):
        chat.mark_read(member)
        out.update(room=view.header(), members=view.members(), reads=view.reads())
    return _json(out)


@login_required
@_staff_only
@require_GET
def api_poll(request):
    """What changed: new and changed messages in the open chat, who is typing, read receipts, and the chat list."""
    user, tenant = request.user, request.tenant
    chat.touch_presence(user, tenant)
    chat.sync(user, tenant)
    names = chat.people(tenant)
    out = {'ok': True, 'now': timezone.now().isoformat()}
    pk = _int(request.GET.get('room'))
    if pk:
        room, member = chat.room_for(user, tenant, pk)
        if room is None:
            out['gone'] = True
        else:
            view = chat.View(room, user, tenant, names)
            new, changed = chat.poll(view, _int(request.GET.get('after'), 0), request.GET.get('since'))
            if request.GET.get('visible') == '1':
                chat.mark_read(member, new[-1]['id'] if new else _int(request.GET.get('after'), 0))
            out.update(messages=new, changed=changed, reads=view.reads(), typing=view.typing(),
                       can_post=chat.can_post(room, member, user, tenant))
            header = view.header()
            if header['other']:
                seen = chat.last_seen(header['other']['id'], tenant)
                out['presence'] = {'online': bool(seen and (timezone.now() - seen).total_seconds() < chat.ONLINE_SECONDS),
                                   'seen': timezone.localtime(seen).isoformat() if seen else ''}
            if request.GET.get('header') == '1':
                out.update(room=header, members=view.members())
    out['rooms'] = chat.rooms(user, tenant, names)
    return _json(out)


def _body(request):
    if request.content_type == 'application/json':
        try:
            return json.loads(request.body or b'{}')
        except ValueError:
            return {}
    return request.POST


@login_required
@_staff_only
@require_POST
def api_send(request, pk):
    room, member = _room_or_404(request, pk)
    data = _body(request)
    try:
        msg = chat.send(room, request.user, request.tenant, data.get('body'), _int(data.get('reply_to')),
                        request.FILES.get('file'))
    except chat.ChatError as problem:
        return _json({'ok': False, 'error': str(problem)}, status=400)
    view = chat.View(room, request.user, request.tenant)
    return _json({'ok': True, 'message': view.message(Message.objects.select_related('reply_to').get(pk=msg.pk))})


def _message(request, msg_id):
    msg = get_object_or_404(Message.objects.select_related('room'), pk=msg_id, room__tenant=request.tenant)
    if not chat.membership(msg.room, request.user):
        raise Http404
    return msg


def _changed(request, msg):
    view = chat.View(msg.room, request.user, request.tenant)
    return _json({'ok': True, 'message': view.message(Message.objects.select_related('reply_to').get(pk=msg.pk))})


@login_required
@_staff_only
@require_POST
def api_react(request, msg_id):
    msg = _message(request, msg_id)
    try:
        chat.react(msg, request.user, _body(request).get('emoji'))
    except chat.ChatError as problem:
        return _json({'ok': False, 'error': str(problem)}, status=400)
    return _changed(request, msg)


@login_required
@_staff_only
@require_POST
def api_delete(request, msg_id):
    msg = _message(request, msg_id)
    try:
        chat.delete(msg, request.user, request.tenant)
    except chat.ChatError as problem:
        return _json({'ok': False, 'error': str(problem)}, status=403)
    return _changed(request, msg)


@login_required
@_staff_only
@require_POST
def api_edit(request, msg_id):
    msg = _message(request, msg_id)
    try:
        chat.edit(msg, request.user, _body(request).get('body'))
    except chat.ChatError as problem:
        return _json({'ok': False, 'error': str(problem)}, status=400)
    return _changed(request, msg)


@login_required
@_staff_only
@require_POST
def api_typing(request, pk):
    room, member = _room_or_404(request, pk)
    chat.typing(member)
    return _json({'ok': True})


@require_GET
def api_unread(request):
    """Unread messages across all chats, for the sidebar badge."""
    if not request.user.is_authenticated or not chat.is_staff(request.user, request.tenant):
        return _json({'unread': 0})
    chat.sync(request.user, request.tenant)
    return _json({'unread': chat.unread_total(request.user, request.tenant)})
