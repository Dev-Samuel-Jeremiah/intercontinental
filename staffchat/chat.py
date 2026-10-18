"""
staffchat/chat.py
─────────────────
The rules of staff chat.

Who: every member of staff and every school administrator. Everyone belongs
to the two community rooms — All Staff (anyone posts and replies) and Staff
Announcements (administrators post, everyone reads and reacts). Anyone can
start a one-to-one chat or a group; the person who starts a group is its
admin. A complaint is a private thread between the member of staff who raised
it and management (every administrator); it can be anonymous, in which case
management sees "Anonymous staff member" instead of the name.

Real time: the page polls (see views.poll) for messages newer than the last
one it has, and for edits, deletions and reactions since its last poll. This
works on any host — no WebSocket server is needed — and SQLite handles it
comfortably for a school's staff.
"""
import os
from datetime import timedelta

from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.db.models import Count, F, Max, Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from users.models import Staff, User

from .models import Member, Message, Presence, Reaction, ChatRoom

TYPING_SECONDS = 6
ONLINE_SECONDS = 60
EDIT_MINUTES = 15
PAGE = 40
MAX_BODY = 4000
MAX_FILE = 10 * 1024 * 1024
FILE_TYPES = {
    '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png', '.gif': 'image/gif', '.webp': 'image/webp',
    '.pdf': 'application/pdf', '.txt': 'text/plain', '.csv': 'text/csv',
    '.doc': 'application/msword',
    '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    '.xls': 'application/vnd.ms-excel',
    '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    '.ppt': 'application/vnd.ms-powerpoint',
    '.pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
    '.mp3': 'audio/mpeg', '.m4a': 'audio/mp4', '.ogg': 'audio/ogg', '.wav': 'audio/wav',
}
IMAGES = {'.jpg', '.jpeg', '.png', '.gif', '.webp'}
REACTIONS = ['👍', '❤️', '😂', '😮', '😢', '🙏']
PALETTE = ['#f4d21f', '#4ade80', '#60a5fa', '#f472b6', '#fb923c', '#a78bfa', '#2dd4bf', '#f87171']
ANONYMOUS = 'Anonymous staff member'


class ChatError(ValueError):
    pass


# ─────────────────────────────────────────────────────────────────────────────
# People
# ─────────────────────────────────────────────────────────────────────────────

def is_staff(user, tenant):
    if not getattr(user, 'is_authenticated', False):
        return False
    return user.is_superuser or Staff.objects.filter(tenant=tenant, user=user).exists()


def is_manager(user, tenant):
    return user.is_superuser or Staff.objects.filter(tenant=tenant, user=user, role='ADMIN').exists()


def people(tenant):
    """{user id: (name, role)} for everyone who can use staff chat."""
    out = {}
    for s in Staff.objects.filter(tenant=tenant, user__is_active=True).select_related('user'):
        out[s.user_id] = (s.full_name or s.user.get_full_name() or s.user.username, s.get_role_display())
    for u in _superusers(tenant):
        out.setdefault(u.id, (u.get_full_name() or u.username, 'Administrator'))
    return out


def _superusers(tenant):
    """School administrators: superusers of this school, or with no school set (they run the whole site)."""
    return User.objects.filter(Q(tenant=tenant) | Q(tenant__isnull=True), is_superuser=True, is_active=True)


def manager_ids(tenant):
    ids = set(Staff.objects.filter(tenant=tenant, role='ADMIN', user__is_active=True).values_list('user_id', flat=True))
    ids |= set(_superusers(tenant).values_list('id', flat=True))
    return ids


def initials(name):
    parts = [p for p in (name or '?').replace('.', ' ').split() if p[:1].isalpha()]
    return ((parts[0][0] + (parts[-1][0] if len(parts) > 1 else '')) if parts else (name or '?')[:1]).upper()


def colour(key):
    return PALETTE[int(key or 0) % len(PALETTE)]


def touch_presence(user, tenant):
    """Note that the user is on chat now (written at most every 30 seconds)."""
    key = f'staffchat:seen:{tenant.pk}:{user.pk}'
    if cache.get(key):
        return
    cache.set(key, 1, 30)
    Presence.objects.update_or_create(tenant=tenant, user=user, defaults={'last_seen': timezone.now()})


def last_seen(user_id, tenant):
    p = Presence.objects.filter(tenant=tenant, user_id=user_id).first()
    return p.last_seen if p else None


# ─────────────────────────────────────────────────────────────────────────────
# Rooms and membership
# ─────────────────────────────────────────────────────────────────────────────

def community(tenant):
    rooms = {}
    for code, name in ChatRoom.COMMUNITY:
        room = ChatRoom.objects.filter(tenant=tenant, community=code).first()
        if room is None:
            try:
                with transaction.atomic():
                    room = ChatRoom.objects.create(
                        tenant=tenant, kind=ChatRoom.GROUP, community=code, name=name,
                        only_admins_post=code == ChatRoom.ANNOUNCEMENTS,
                        description=('Everyone on staff — share, ask and reply.' if code == ChatRoom.ALL_STAFF
                                     else 'Official notices from the school administration.'))
            except IntegrityError:
                room = ChatRoom.objects.get(tenant=tenant, community=code)
        rooms[code] = room
    return rooms


def sync(user, tenant):
    """Make sure the user is in the community rooms (as an admin if they manage the school) and,
    for management, in every complaint thread. Cheap to call: it runs at most once a minute."""
    key = f'staffchat:sync:{tenant.pk}:{user.pk}'
    if cache.get(key):
        return
    manager = is_manager(user, tenant)
    wanted = list(community(tenant).values())
    if manager:
        wanted += list(ChatRoom.objects.filter(tenant=tenant, kind=ChatRoom.COMPLAINT))
    have = {m.room_id: m for m in Member.objects.filter(user=user, room__in=wanted)}
    role = Member.ADMIN if manager else Member.MEMBER
    for room in wanted:
        m = have.get(room.id)
        if m is None:
            Member.objects.get_or_create(room=room, user=user, defaults={'role': role})
        elif room.community and m.role != role:
            m.role = role
            m.save(update_fields=['role'])
    cache.set(key, 1, 60)


def forget_sync(user, tenant):
    cache.delete(f'staffchat:sync:{tenant.pk}:{user.pk}')


def membership(room, user):
    return Member.objects.filter(room=room, user=user).first()


def room_for(user, tenant, pk):
    """The room and the user's membership, or (None, None) if they are not in it."""
    room = ChatRoom.objects.filter(tenant=tenant, pk=pk).first()
    if room is None:
        return None, None
    m = membership(room, user)
    return (room, m) if m else (None, None)


def can_manage(room, member, user, tenant):
    """Group admins (and management, for groups and complaints) can manage a room; nobody manages a direct chat."""
    if room.kind == ChatRoom.DIRECT:
        return False
    return bool(member and member.role == Member.ADMIN) or is_manager(user, tenant)


def can_post(room, member, user, tenant):
    if room.only_admins_post:
        return can_manage(room, member, user, tenant)
    return True


def _system(room, text, user=None):
    msg = Message.objects.create(room=room, sender=user, kind=Message.SYSTEM, body=text)
    ChatRoom.objects.filter(pk=room.pk).update(last_message_at=msg.created_at)
    return msg


@transaction.atomic
def direct_room(user, other, tenant):
    if other.pk == user.pk:
        raise ChatError("That's you.")
    if not is_staff(other, tenant):
        raise ChatError('You can only chat with members of staff.')
    a, b = sorted([user.pk, other.pk])
    room, created = ChatRoom.objects.get_or_create(tenant=tenant, direct_key=f'{a}-{b}',
                                               defaults={'kind': ChatRoom.DIRECT, 'created_by': user})
    for u in (user, other):
        Member.objects.get_or_create(room=room, user=u)
    return room


@transaction.atomic
def create_group(tenant, user, name, description='', member_ids=(), only_admins_post=False):
    name = (name or '').strip()[:80]
    if not name:
        raise ChatError('Give the group a name.')
    allowed = people(tenant)
    room = ChatRoom.objects.create(tenant=tenant, kind=ChatRoom.GROUP, name=name, description=(description or '').strip()[:300],
                               only_admins_post=bool(only_admins_post), created_by=user)
    Member.objects.create(room=room, user=user, role=Member.ADMIN)
    ids = {int(i) for i in member_ids if str(i).isdigit()} & set(allowed) - {user.pk}
    Member.objects.bulk_create([Member(room=room, user_id=i) for i in ids])
    _system(room, f'{allowed.get(user.pk, ("Someone",))[0]} created the group "{name}".', user)
    return room


@transaction.atomic
def create_complaint(tenant, user, subject, category, details, anonymous=False, upload=None):
    subject = (subject or '').strip()[:80]
    details = (details or '').strip()
    if not subject or not details:
        raise ChatError('Give your complaint a subject and describe what happened.')
    if category not in dict(ChatRoom.CATEGORIES):
        category = 'OTHER'
    room = ChatRoom.objects.create(tenant=tenant, kind=ChatRoom.COMPLAINT, name=subject, category=category,
                               status='OPEN', anonymous=bool(anonymous), created_by=user)
    Member.objects.create(room=room, user=user)
    Member.objects.bulk_create([Member(room=room, user_id=i, role=Member.ADMIN)
                                for i in manager_ids(tenant) - {user.pk}])
    _system(room, f'Complaint raised · {room.get_category_display()}'
                  + (' · sent anonymously' if room.anonymous else ''))
    send(room, user, tenant, details, upload=upload, notify=False)
    from notifications import services as notes
    notes.to_admins(tenant, 'ticket', ('New anonymous staff complaint' if room.anonymous else
                                       f'New staff complaint from {people(tenant).get(user.pk, ("a member of staff",))[0]}'),
                    body=f'{subject} · {room.get_category_display()}', url=f'/staff-chat/{room.pk}/', icon='megaphone-fill',
                    level='warning')
    return room


def set_status(room, user, tenant, status):
    if room.kind != ChatRoom.COMPLAINT or not is_manager(user, tenant):
        raise ChatError('Only management can change a complaint\'s status.')
    if status not in dict(ChatRoom.STATUSES) or status == room.status:
        return room
    room.status = status
    room.save(update_fields=['status'])
    _system(room, f'{people(tenant).get(user.pk, ("Management",))[0]} marked this complaint {room.get_status_display().lower()}.', user)
    from notifications import services as notes
    if room.created_by_id and room.created_by_id != user.pk:
        notes.to_user(room.created_by, 'ticket', f'Your complaint is now {room.get_status_display().lower()}',
                      body=room.name, url=f'/staff-chat/{room.pk}/', tenant=tenant, admin_feed=False)
    return room


@transaction.atomic
def manage(room, user, tenant, action, data):
    """Group settings: rename, describe, add/remove members, admins, who can post, leave."""
    member = membership(room, user)
    names = people(tenant)
    me = names.get(user.pk, ('Someone',))[0]
    if action == 'leave':
        if room.community or room.kind != ChatRoom.GROUP:
            raise ChatError("You can't leave this chat.")
        if member:
            member.delete()
            _system(room, f'{me} left.', user)
            if not room.members.filter(role=Member.ADMIN).exists():
                first = room.members.order_by('joined_at').first()
                if first:
                    first.role = Member.ADMIN
                    first.save(update_fields=['role'])
        return
    if action == 'status':
        set_status(room, user, tenant, data.get('status'))
        return
    if room.kind != ChatRoom.GROUP or not can_manage(room, member, user, tenant):
        raise ChatError('Only group admins can change this group.')
    if action == 'edit':
        name = (data.get('name') or '').strip()[:80]
        if not name:
            raise ChatError('Give the group a name.')
        changed = name != room.name
        room.name, room.description = name, (data.get('description') or '').strip()[:300]
        room.only_admins_post = bool(data.get('only_admins_post'))
        room.save(update_fields=['name', 'description', 'only_admins_post'])
        if changed:
            _system(room, f'{me} renamed the group to "{name}".', user)
    elif action == 'add':
        if room.community:
            raise ChatError('Everyone on staff is already in this room.')
        have = set(room.members.values_list('user_id', flat=True))
        new = [i for i in {int(x) for x in data.getlist('members') if str(x).isdigit()} if i in names and i not in have]
        Member.objects.bulk_create([Member(room=room, user_id=i) for i in new])
        if new:
            _system(room, f'{me} added {", ".join(names[i][0] for i in new)}.', user)
    elif action in ('remove', 'admin', 'unadmin'):
        target = room.members.filter(user_id=data.get('user')).first()
        if target is None or (room.community and action == 'remove'):
            raise ChatError('That person is not someone you can change here.')
        who = names.get(target.user_id, ('Someone',))[0]
        if action == 'remove':
            target.delete()
            _system(room, f'{me} removed {who}.', user)
        else:
            target.role = Member.ADMIN if action == 'admin' else Member.MEMBER
            target.save(update_fields=['role'])
            _system(room, f'{me} {"made" if action == "admin" else "dismissed"} {who} {"a group admin" if action == "admin" else "as group admin"}.', user)
    else:
        raise ChatError('Unknown action.')


# ─────────────────────────────────────────────────────────────────────────────
# Messages
# ─────────────────────────────────────────────────────────────────────────────

def _check_file(upload):
    if not upload:
        return None
    ext = os.path.splitext(upload.name)[1].lower()
    if ext not in FILE_TYPES:
        raise ChatError('Send pictures, PDFs, Word, Excel, PowerPoint, text or audio files.')
    if upload.size > MAX_FILE:
        raise ChatError('Files can be up to 10 MB.')
    return ext


@transaction.atomic
def send(room, user, tenant, body, reply_to=None, upload=None, notify=True):
    member = membership(room, user)
    if member is None:
        raise ChatError("You're not in this chat.")
    if not can_post(room, member, user, tenant):
        raise ChatError('Only admins can send messages here.')
    body = (body or '').strip()
    if len(body) > MAX_BODY:
        raise ChatError(f'Messages can be up to {MAX_BODY} characters.')
    _check_file(upload)
    if not body and not upload:
        raise ChatError('Type a message.')
    reply = Message.objects.filter(room=room, pk=reply_to, kind=Message.TEXT).first() if reply_to else None
    msg = Message(room=room, sender=user, body=body, reply_to=reply)
    if upload:
        msg.attachment_name = os.path.basename(upload.name)[:160]
        msg.attachment_size = upload.size
        msg.attachment.save(upload.name, upload, save=False)
    msg.save()
    ChatRoom.objects.filter(pk=room.pk).update(last_message_at=msg.created_at)
    Member.objects.filter(pk=member.pk).update(last_read_id=msg.id, typing_at=None)
    if notify and room.kind == ChatRoom.COMPLAINT and room.created_by_id and user.pk != room.created_by_id:
        from notifications import services as notes
        notes.to_user(room.created_by, 'message', 'Management replied to your complaint', body=room.name,
                      url=f'/staff-chat/{room.pk}/', tenant=tenant, admin_feed=False,
                      dedupe=f'staffchat:{room.pk}:{msg.created_at:%Y%m%d%H}')
    return msg


def _own_or_admin(message, user, tenant):
    if message.sender_id == user.pk:
        return True
    room = message.room
    if room.kind == ChatRoom.DIRECT:
        return False
    return can_manage(room, membership(room, user), user, tenant)


def delete(message, user, tenant):
    if message.kind != Message.TEXT or message.deleted_at:
        return message
    if not _own_or_admin(message, user, tenant):
        raise ChatError('You can only delete your own messages.')
    if message.attachment:
        message.attachment.delete(save=False)
    now = timezone.now()
    Message.objects.filter(pk=message.pk).update(body='', attachment='', attachment_name='', attachment_size=0,
                                                 deleted_at=now, updated_at=now)
    Reaction.objects.filter(message=message).delete()
    message.refresh_from_db()
    return message


def edit(message, user, body):
    body = (body or '').strip()
    if message.sender_id != user.pk or message.kind != Message.TEXT or message.deleted_at:
        raise ChatError('You can only edit your own messages.')
    if timezone.now() - message.created_at > timedelta(minutes=EDIT_MINUTES):
        raise ChatError(f'Messages can be edited for {EDIT_MINUTES} minutes after sending.')
    if not body and not message.attachment:
        raise ChatError('Type a message.')
    if len(body) > MAX_BODY:
        raise ChatError(f'Messages can be up to {MAX_BODY} characters.')
    now = timezone.now()
    Message.objects.filter(pk=message.pk).update(body=body, edited_at=now, updated_at=now)
    message.refresh_from_db()
    return message


def react(message, user, emoji):
    if emoji not in REACTIONS or message.deleted_at or message.kind != Message.TEXT:
        raise ChatError('Choose one of the reactions.')
    current = Reaction.objects.filter(message=message, user=user).first()
    if current and current.emoji == emoji:
        current.delete()                       # tapping the same reaction again takes it back
    else:
        Reaction.objects.update_or_create(message=message, user=user, defaults={'emoji': emoji})
    Message.objects.filter(pk=message.pk).update(updated_at=timezone.now())


def typing(member):
    Member.objects.filter(pk=member.pk).update(typing_at=timezone.now())


def mark_read(member, up_to=None):
    latest = up_to if up_to is not None else (member.room.messages.aggregate(m=Max('id'))['m'] or 0)
    if latest > member.last_read_id:
        Member.objects.filter(pk=member.pk, last_read_id__lt=latest).update(last_read_id=latest)
        member.last_read_id = latest


# ─────────────────────────────────────────────────────────────────────────────
# What the page shows
# ─────────────────────────────────────────────────────────────────────────────

class View:
    """Names and visibility rules for one user looking at one room."""

    def __init__(self, room, user, tenant, names=None):
        self.room, self.user, self.tenant = room, user, tenant
        self.names = names if names is not None else people(tenant)
        # In an anonymous complaint, everyone but the complainant sees "Anonymous staff member"
        self.hidden = room.created_by_id if (room.kind == ChatRoom.COMPLAINT and room.anonymous
                                             and room.created_by_id != user.pk) else None
        self.manager = is_manager(user, tenant)
        self.member = membership(room, user)
        self.admin = can_manage(room, self.member, user, tenant)

    def name(self, user_id):
        if user_id is None:
            return ''
        if user_id == self.user.pk:
            return 'You'
        if self.hidden and user_id == self.hidden:
            return ANONYMOUS
        return self.names.get(user_id, ('Former member of staff',))[0]

    def sender(self, user_id):
        return 0 if self.hidden and user_id == self.hidden else (user_id or 0)

    def message(self, m):
        deleted = m.deleted_at is not None
        name = self.name(m.sender_id)
        out = {
            'id': m.id, 'kind': m.kind, 'sender': self.sender(m.sender_id), 'name': name,
            'initials': '?' if name == ANONYMOUS else initials(name if name != 'You' else self.names.get(self.user.pk, ('You',))[0]),
            'colour': '#94a3b8' if name == ANONYMOUS else colour(m.sender_id), 'mine': m.sender_id == self.user.pk,
            'body': '' if deleted else m.body, 'at': timezone.localtime(m.created_at).isoformat(),
            'edited': bool(m.edited_at) and not deleted, 'deleted': deleted,
            'can_edit': (m.sender_id == self.user.pk and not deleted and m.kind == Message.TEXT
                         and timezone.now() - m.created_at <= timedelta(minutes=EDIT_MINUTES)),
            'can_delete': m.kind == Message.TEXT and not deleted and (
                m.sender_id == self.user.pk or (self.admin and self.room.kind != ChatRoom.DIRECT)),
            'reply': None, 'file': None, 'reactions': [],
        }
        if m.reply_to_id and m.reply_to:
            r = m.reply_to
            out['reply'] = {'id': r.id, 'name': self.name(r.sender_id),
                            'body': 'This message was deleted' if r.deleted_at else (r.body[:140] or ''),
                            'file': r.attachment_name if not r.deleted_at else ''}
        if m.attachment and not deleted:
            ext = os.path.splitext(m.attachment.name)[1].lower()
            out['file'] = {'url': f'/staff-chat/file/{m.id}/', 'name': m.attachment_name or 'file',
                           'size': m.attachment_size, 'image': ext in IMAGES,
                           'audio': FILE_TYPES.get(ext, '').startswith('audio/')}
        groups = {}
        for rx in m.reactions.all():
            g = groups.setdefault(rx.emoji, {'emoji': rx.emoji, 'count': 0, 'mine': False, 'who': []})
            g['count'] += 1
            g['mine'] = g['mine'] or rx.user_id == self.user.pk
            g['who'].append(self.name(rx.user_id))
        out['reactions'] = sorted(groups.values(), key=lambda g: -g['count'])
        return out

    def messages(self, qs):
        return [self.message(m) for m in qs.select_related('reply_to').prefetch_related('reactions')]

    def header(self):
        room = self.room
        out = {'id': room.id, 'kind': room.kind, 'community': room.community, 'name': room_name(room, self.user, self.names),
               'description': room.description, 'can_post': can_post(room, self.member, self.user, self.tenant),
               'admin': self.admin, 'manager': self.manager, 'only_admins_post': room.only_admins_post,
               'status': room.status, 'status_label': room.get_status_display() if room.status else '',
               'category': room.get_category_display() if room.category else '', 'anonymous': room.anonymous,
               'members': room.members.count(), 'other': None, 'mine': room.created_by_id == self.user.pk}
        out.update(avatar(room, self.user, self.names))
        if room.kind == ChatRoom.DIRECT:
            other = room.members.exclude(user=self.user).values_list('user_id', flat=True).first()
            if other:
                out['other'] = {'id': other, 'role': self.names.get(other, ('', ''))[1]}
        return out

    def members(self):
        rows = []
        for m in self.room.members.order_by('-role', 'joined_at'):
            if self.hidden and m.user_id == self.hidden:
                rows.append({'id': 0, 'name': ANONYMOUS, 'role': 'Raised this complaint', 'admin': False,
                             'initials': '?', 'colour': '#94a3b8', 'read': m.last_read_id})
                continue
            name, role = self.names.get(m.user_id, ('Former member of staff', ''))
            rows.append({'id': m.user_id, 'name': 'You' if m.user_id == self.user.pk else name, 'role': role,
                         'admin': m.role == Member.ADMIN, 'initials': initials(name), 'colour': colour(m.user_id),
                         'read': m.last_read_id})
        return rows

    def reads(self):
        """Other members' last-read message ids, for ticks and "seen by"."""
        return list(self.room.members.exclude(user=self.user).values_list('last_read_id', flat=True))

    def typing(self):
        since = timezone.now() - timedelta(seconds=TYPING_SECONDS)
        ids = self.room.members.exclude(user=self.user).filter(typing_at__gte=since).values_list('user_id', flat=True)
        return [self.name(i) for i in ids]


def _other(room, user, member_ids=None):
    """The other person in a direct chat (their user id), or None."""
    ids = member_ids if member_ids is not None else room.members.values_list('user_id', flat=True)
    return next((i for i in ids if i != user.pk), None)


def room_name(room, user, names, member_ids=None):
    if room.kind == ChatRoom.DIRECT:
        other = _other(room, user, member_ids)
        return names.get(other, ('Former member of staff',))[0] if other else 'Just you'
    return room.name


def avatar(room, user, names, member_ids=None):
    if room.community == ChatRoom.ANNOUNCEMENTS:
        return {'icon': 'megaphone-fill', 'initials': '', 'colour': '#f4d21f'}
    if room.community == ChatRoom.ALL_STAFF:
        return {'icon': 'people-fill', 'initials': '', 'colour': '#f4d21f'}
    if room.kind == ChatRoom.COMPLAINT:
        return {'icon': 'shield-exclamation', 'initials': '', 'colour': '#fb923c'}
    if room.kind == ChatRoom.DIRECT:
        other = _other(room, user, member_ids)
        return {'icon': '', 'initials': initials(names.get(other, ('?',))[0]), 'colour': colour(other or 0)}
    return {'icon': '', 'initials': initials(room.name), 'colour': colour(room.pk)}


def rooms(user, tenant, names=None):
    """The user's chats, community rooms first, then the most recent."""
    names = names if names is not None else people(tenant)
    mine = list(_unread(user, tenant).select_related('room'))
    room_ids = [m.room_id for m in mine]
    last_ids = (Message.objects.filter(room_id__in=room_ids).values('room_id').annotate(last=Max('id'))
                .values_list('last', flat=True))
    last = {m.room_id: m for m in Message.objects.filter(id__in=list(last_ids))}
    members = {}
    for room_id, user_id in Member.objects.filter(room_id__in=room_ids, room__kind=ChatRoom.DIRECT).values_list('room_id', 'user_id'):
        members.setdefault(room_id, []).append(user_id)
    out = []
    for m in mine:
        room = m.room
        ids = members.get(room.id, [])
        msg = last.get(room.id)
        hidden = room.created_by_id if room.kind == ChatRoom.COMPLAINT and room.anonymous and room.created_by_id != user.pk else None
        preview, who = '', ''
        if msg:
            if msg.deleted_at:
                preview = 'This message was deleted'
            elif msg.body:
                preview = msg.body[:90]
            elif msg.attachment_name:
                preview = '📎 ' + msg.attachment_name
            if msg.kind == Message.TEXT and room.kind != ChatRoom.DIRECT:
                who = 'You' if msg.sender_id == user.pk else (
                    ANONYMOUS if hidden and msg.sender_id == hidden else names.get(msg.sender_id, ('',))[0].split(' ')[0])
            elif msg.kind == Message.TEXT and msg.sender_id == user.pk:
                who = 'You'
        row = {'id': room.id, 'kind': room.kind, 'community': room.community, 'name': room_name(room, user, names, ids),
               'unread': m.unread, 'preview': preview, 'who': who, 'status': room.status,
               'status_label': room.get_status_display() if room.status else '',
               'at': timezone.localtime(msg.created_at).isoformat() if msg else '',
               'sort': (msg.created_at if msg else room.created_at).timestamp()}
        row.update(avatar(room, user, names, ids))
        out.append(row)
    order = {ChatRoom.ANNOUNCEMENTS: 0, ChatRoom.ALL_STAFF: 1}
    out.sort(key=lambda r: (order.get(r['community'], 2), -r['sort']))
    return out


def _unread(user, tenant):
    return (Member.objects.filter(user=user, room__tenant=tenant)
            .annotate(unread=Count('room__messages', filter=Q(room__messages__id__gt=F('last_read_id'))
                                   & ~Q(room__messages__sender=user))))


def unread_total(user, tenant):
    return sum(m.unread for m in _unread(user, tenant))


def history(view, before=None):
    qs = view.room.messages.all()
    if before:
        qs = qs.filter(id__lt=before)
    ids = list(qs.order_by('-id').values_list('id', flat=True)[:PAGE])
    return view.messages(Message.objects.filter(id__in=ids).order_by('id')), len(ids) == PAGE


def poll(view, after, since):
    """New messages after `after`, and earlier ones changed since `since` (edits, deletions, reactions)."""
    room = view.room
    new = view.messages(room.messages.filter(id__gt=after).order_by('id')[:200]) if after is not None else []
    changed = []
    when = parse_datetime(since) if since else None
    if when and after:
        changed = view.messages(room.messages.filter(id__lte=after, updated_at__gt=when).order_by('id')[:200])
    return new, changed
