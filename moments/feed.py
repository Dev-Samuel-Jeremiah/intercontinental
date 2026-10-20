"""
moments/feed.py
───────────────
The rules of School Moments.

Who sees a post: staff see everything; a post for the whole school is seen by
every student and parent; a post for one class only by that class's students
and their parents (and staff). Who posts: staff. Everyone who can see a post
can like, save and comment on it (unless comments are turned off).

Comments go through the same safety check as students' discussion posts: a
comment that looks like bullying, threats or sexual content is held — only
its writer and staff see it — until a member of staff lets it through or
removes it. Anyone can report a post or comment to the administrators.
"""
import re
from types import SimpleNamespace

from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from users.models import Class, Staff, Student

from . import media as media_mod
from .models import Comment, Like, Media, Moment, MomentReport, Saved

PAGE = 8
HASHTAG = re.compile(r'#(\w{2,40})')


class FeedError(ValueError):
    pass


# ─────────────────────────────────────────────────────────────────────────────
# Who is looking
# ─────────────────────────────────────────────────────────────────────────────

class Viewer:
    """The signed-in person and what they may see and do."""

    def __init__(self, user, tenant):
        self.user, self.tenant = user, tenant
        self.staff = Staff.objects.filter(tenant=tenant, user=user).first() if user.is_authenticated else None
        self.is_staff = bool(user.is_authenticated and (user.is_superuser or self.staff))
        self.manager = bool(user.is_authenticated and (user.is_superuser or (self.staff and self.staff.role == 'ADMIN')))
        self.student = None if self.is_staff else Student.objects.filter(tenant=tenant, user=user).first()
        self.children = []
        if not self.is_staff and not self.student and getattr(user, 'is_parent', False):
            from finance.views import _get_all_children
            self.children = list(_get_all_children(SimpleNamespace(user=user)).filter(tenant=tenant))

    @property
    def allowed(self):
        return self.is_staff or bool(self.student) or bool(self.children)

    @property
    def class_ids(self):
        if self.student:
            return [self.student.class_assigned_id] if self.student.class_assigned_id else []
        return sorted({c.class_assigned_id for c in self.children if c.class_assigned_id})

    def posts(self):
        qs = Moment.objects.filter(tenant=self.tenant)
        if self.is_staff:
            return qs
        if not self.allowed:
            return qs.none()
        return qs.filter(Q(school_class__isnull=True) | Q(school_class_id__in=self.class_ids))

    def can_see(self, post):
        return self.posts().filter(pk=post.pk).exists()

    def can_manage(self, post):
        return self.manager or (self.is_staff and post.author_id == self.user.pk)

    def classes(self):
        """Classes this person can post to (staff) or follows (students and parents)."""
        if self.is_staff:
            return Class.objects.filter(tenant=self.tenant).order_by('name')
        return Class.objects.filter(tenant=self.tenant, pk__in=self.class_ids).order_by('name')


def names(tenant, user_ids):
    """{user id: (display name, role)} — staff by name, students by name, parents as "Parent of …"."""
    ids = set(i for i in user_ids if i)
    out = {}
    for s in Staff.objects.filter(tenant=tenant, user_id__in=ids):
        out[s.user_id] = (s.full_name, 'Staff' if s.role != 'ADMIN' else 'Administrator')
    for s in Student.objects.filter(tenant=tenant, user_id__in=ids - set(out)):
        out[s.user_id] = (s.full_name, 'Student')
    from users.models import User
    for u in User.objects.filter(pk__in=ids - set(out)):
        if u.is_superuser:
            out[u.id] = (u.get_full_name() or 'School Administrator', 'Administrator')
        elif getattr(u, 'is_parent', False):
            from finance.views import _get_all_children
            kids = list(_get_all_children(SimpleNamespace(user=u)).filter(tenant=tenant)[:2])
            label = ' & '.join(k.full_name.split(' ')[0] for k in kids)
            out[u.id] = (f'Parent of {label}' if label else (u.get_full_name() or 'Parent'), 'Parent')
        else:
            out[u.id] = (u.get_full_name() or u.username, '')
    return out


def initials(name):
    words = [w for w in re.split(r'[\s.&]+', name or '') if w[:1].isalpha() and w.lower() not in ('parent', 'of')]
    return ((words[0][0] + (words[-1][0] if len(words) > 1 else '')) if words else '?').upper()


PALETTE = ['#f4d21f', '#4ade80', '#60a5fa', '#f472b6', '#fb923c', '#a78bfa', '#2dd4bf', '#f87171']


def colour(user_id):
    return PALETTE[(user_id or 0) % len(PALETTE)]


# ─────────────────────────────────────────────────────────────────────────────
# Posting
# ─────────────────────────────────────────────────────────────────────────────

def create_post(viewer, caption, category, class_id, uploads, comments_on=True):
    if not viewer.is_staff:
        raise FeedError('Only staff can post to School Moments.')
    caption = (caption or '').strip()
    if len(caption) > 2200:
        raise FeedError('Captions can be up to 2,200 characters.')
    if category not in dict(Moment.CATEGORIES):
        category = 'CLASSROOM'
    school_class = None
    if class_id:
        school_class = Class.objects.filter(tenant=viewer.tenant, pk=class_id).first()
        if school_class is None:
            raise FeedError('Choose one of your school\'s classes.')
    media_mod.check(uploads)
    with transaction.atomic():
        post = Moment.objects.create(tenant=viewer.tenant, author=viewer.user, caption=caption, category=category,
                                   school_class=school_class, comments_on=bool(comments_on))
        for i, upload in enumerate(uploads):
            kind = media_mod.kind_of(upload)
            m = Media(post=post, kind=kind, order=i)
            if kind == Media.IMAGE:
                media_mod.save_image(m, upload)
            else:
                media_mod.save_video(m, upload)
        transaction.on_commit(lambda: _announce(post))
    return post


def _announce(post):
    from notifications import services as notes
    from notifications.models import Notification as N
    who = names(post.tenant, [post.author_id]).get(post.author_id, ('The school',))[0]
    title = f'{who} shared {post.get_category_display().lower()} photos' if post.media.filter(kind=Media.IMAGE).exists() \
        else f'{who} shared a {post.get_category_display().lower()} video'
    body = (post.caption or '')[:140]
    url = f'/moments/{post.pk}/'
    if post.school_class_id:
        for audience in (N.CLASS_PARENTS, N.CLASS_STUDENTS):
            notes.notify(post.tenant, 'moment', title + f' · {post.school_class.name}', audience,
                         school_class=post.school_class, body=body, url=url, icon='images', admin_feed=False,
                         dedupe=f'moment:{post.pk}:{audience}')
    else:
        for audience in (N.ALL_PARENTS, N.ALL_STUDENTS, N.ALL_STAFF):
            notes.notify(post.tenant, 'moment', title, audience, body=body, url=url, icon='images', admin_feed=False,
                         dedupe=f'moment:{post.pk}:{audience}')


def edit_post(viewer, post, data):
    if not viewer.can_manage(post):
        raise FeedError('Only the person who posted it, or an administrator, can change this post.')
    fields = []
    if 'caption' in data:
        caption = (data.get('caption') or '').strip()
        if len(caption) > 2200:
            raise FeedError('Captions can be up to 2,200 characters.')
        post.caption, post.edited_at = caption, timezone.now()
        fields += ['caption', 'edited_at']
    if 'comments_on' in data:
        post.comments_on = bool(data.get('comments_on'))
        fields.append('comments_on')
    if 'pinned' in data:
        if not viewer.manager:
            raise FeedError('Only administrators can pin posts.')
        post.pinned = bool(data.get('pinned'))
        fields.append('pinned')
    if fields:
        post.save(update_fields=fields)
    return post


def delete_post(viewer, post):
    if not viewer.can_manage(post):
        raise FeedError('Only the person who posted it, or an administrator, can delete this post.')
    for m in post.media.all():
        m.file.delete(save=False)
        if m.thumb:
            m.thumb.delete(save=False)
    post.delete()


# ─────────────────────────────────────────────────────────────────────────────
# Likes, saves, comments, reports
# ─────────────────────────────────────────────────────────────────────────────

def toggle_like(viewer, post, like=None):
    """Like or unlike (or set it with like=True/False). Returns (liked, count)."""
    exists = Like.objects.filter(post=post, user=viewer.user).exists()
    want = (not exists) if like is None else bool(like)
    if want and not exists:
        _, made = Like.objects.get_or_create(post=post, user=viewer.user)
        if made:
            Moment.objects.filter(pk=post.pk).update(like_count=F('like_count') + 1)
    elif not want and exists:
        if Like.objects.filter(post=post, user=viewer.user).delete()[0]:
            Moment.objects.filter(pk=post.pk, like_count__gt=0).update(like_count=F('like_count') - 1)
    post.refresh_from_db(fields=['like_count'])
    return want, post.like_count


def toggle_save(viewer, post):
    gone = Saved.objects.filter(post=post, user=viewer.user).delete()[0]
    if not gone:
        Saved.objects.get_or_create(post=post, user=viewer.user)
    return not gone


def visible_comments(viewer, post):
    qs = post.comments.exclude(status=Comment.HIDDEN)
    if not viewer.is_staff:
        qs = qs.filter(Q(status=Comment.VISIBLE) | Q(user=viewer.user))
    return qs


def add_comment(viewer, post, body, reply_to=None):
    body = (body or '').strip()
    if not body:
        raise FeedError('Write a comment.')
    if len(body) > 1000:
        raise FeedError('Comments can be up to 1,000 characters.')
    if not post.comments_on:
        raise FeedError('Comments are turned off for this post.')
    parent = None
    if reply_to:
        parent = post.comments.filter(pk=reply_to).first()
        if parent and parent.reply_to_id:
            parent = parent.reply_to                       # replies stay one level deep
    from assistant import safety
    verdict = safety.check(viewer.tenant, body, viewer.user)
    held = verdict.flagged and not viewer.is_staff
    c = Comment.objects.create(post=post, user=viewer.user, reply_to=parent, body=body,
                               status=Comment.HELD if held else Comment.VISIBLE,
                               flag_reason=verdict.reason if held else '')
    if not held:
        Moment.objects.filter(pk=post.pk).update(comment_count=F('comment_count') + 1)
    from notifications import services as notes
    who = names(viewer.tenant, [viewer.user.pk])[viewer.user.pk][0]
    if held:
        notes.to_admins(viewer.tenant, 'report', 'A School Moments comment is waiting for review',
                        body=f'{who}: {verdict.reason}', url=f'/moments/{post.pk}/', level='warning')
        if verdict.urgent:
            notes.to_admins(viewer.tenant, 'report', 'Urgent: a comment may be about self-harm',
                            body=f'Written by {who}. Please check on them today.', url=f'/moments/{post.pk}/',
                            level='danger')
    else:
        targets = {post.author_id} | ({parent.user_id} if parent else set())
        for uid in targets - {viewer.user.pk, None}:
            from users.models import User
            notes.to_user(User.objects.get(pk=uid), 'moment',
                          f'{who} {"replied to your comment" if parent and uid == parent.user_id else "commented on your post"}',
                          body=body[:140], url=f'/moments/{post.pk}/#c{c.pk}', tenant=viewer.tenant, admin_feed=False,
                          dedupe=f'moment-comment:{c.pk}:{uid}')
    return c


def delete_comment(viewer, comment):
    if not (comment.user_id == viewer.user.pk or viewer.can_manage(comment.post) or viewer.manager):
        raise FeedError('You can only delete your own comments.')
    counted = 1 if comment.status == Comment.VISIBLE else 0
    counted += comment.replies.filter(status=Comment.VISIBLE).count()
    post_id = comment.post_id
    comment.delete()
    if counted:
        Moment.objects.filter(pk=post_id).update(comment_count=F('comment_count') - counted)


def moderate_comment(viewer, comment, allow):
    if not viewer.is_staff:
        raise FeedError('Only staff can review comments.')
    was = comment.status
    comment.status = Comment.VISIBLE if allow else Comment.HIDDEN
    comment.save(update_fields=['status'])
    delta = (comment.status == Comment.VISIBLE) - (was == Comment.VISIBLE)
    if delta:
        Moment.objects.filter(pk=comment.post_id).update(comment_count=F('comment_count') + delta)
    return comment


def report(viewer, post, reason, comment=None):
    reason = (reason or '').strip()[:300] or 'No reason given'
    MomentReport.objects.create(tenant=viewer.tenant, post=post, comment=comment, user=viewer.user, reason=reason)
    from notifications import services as notes
    who = names(viewer.tenant, [viewer.user.pk])[viewer.user.pk][0]
    notes.to_admins(viewer.tenant, 'report', f'{who} reported a {"comment" if comment else "post"} in School Moments',
                    body=reason, url=f'/moments/{post.pk}/', level='warning')


# ─────────────────────────────────────────────────────────────────────────────
# What the page shows
# ─────────────────────────────────────────────────────────────────────────────

def decorate(viewer, posts, comments_preview=2):
    """Attach what each card needs: media, author, liked/saved, a few comments, who liked it."""
    posts = list(posts)
    ids = [p.pk for p in posts]
    liked = set(Like.objects.filter(post_id__in=ids, user=viewer.user).values_list('post_id', flat=True))
    saved = set(Saved.objects.filter(post_id__in=ids, user=viewer.user).values_list('post_id', flat=True))
    media = {}
    for m in Media.objects.filter(post_id__in=ids):
        media.setdefault(m.post_id, []).append(m)
    likers = {}
    for pid, uid in Like.objects.filter(post_id__in=ids).order_by('-created_at').values_list('post_id', 'user_id'):
        if len(likers.setdefault(pid, [])) < 2:
            likers[pid].append(uid)
    previews = {}
    for p in posts:
        qs = visible_comments(viewer, p).filter(reply_to__isnull=True).order_by('-created_at')[:comments_preview]
        previews[p.pk] = list(reversed(list(qs)))
    who = names(viewer.tenant, [p.author_id for p in posts] + [u for v in likers.values() for u in v]
                + [c.user_id for v in previews.values() for c in v])
    for p in posts:
        p.items = media.get(p.pk, [])
        p.liked, p.saved = p.pk in liked, p.pk in saved
        p.author_name, p.author_role = who.get(p.author_id, ('Former member of staff', ''))
        p.author_initials, p.author_colour = initials(p.author_name), colour(p.author_id)
        p.likers = [who.get(u, ('Someone',))[0] for u in likers.get(p.pk, [])]
        p.preview = previews[p.pk]
        for c in p.preview:
            c.name = who.get(c.user_id, ('Someone',))[0]
        p.editable = viewer.can_manage(p)
        p.caption_html = caption_html(p.caption)
    return posts


def caption_html(text):
    """Caption with #hashtags as links to the feed filtered by that tag (everything else escaped)."""
    from django.utils.html import escape
    from django.utils.safestring import mark_safe
    safe = escape(text or '')
    return mark_safe(HASHTAG.sub(lambda m: f'<a class="tag" href="/moments/?tag={m.group(1)}">#{m.group(1)}</a>', safe))


def comment_rows(viewer, post):
    """Top-level comments with their replies, for the post page."""
    rows = list(visible_comments(viewer, post))
    who = names(viewer.tenant, [c.user_id for c in rows])
    top, replies = [], {}
    for c in rows:
        c.name, c.role = who.get(c.user_id, ('Someone', ''))
        c.initials, c.colour = initials(c.name), colour(c.user_id)
        c.mine = c.user_id == viewer.user.pk
        c.deletable = c.mine or viewer.can_manage(post) or viewer.manager
        (replies.setdefault(c.reply_to_id, []) if c.reply_to_id else top).append(c)
    for c in top:
        c.reply_list = replies.get(c.pk, [])
    return top


def filter_posts(viewer, qs, category='', class_id='', tag='', saved=False, author=None):
    if category in dict(Moment.CATEGORIES):
        qs = qs.filter(category=category)
    if class_id:
        qs = qs.filter(school_class_id=class_id)
    if tag:
        # (?![0-9A-Za-z_]) = end of the word; \b would mean backspace in PostgreSQL's regular expressions
        qs = qs.filter(caption__iregex=r'#' + re.escape(tag) + r'(?![0-9A-Za-z_])')
    if saved:
        qs = qs.filter(saves__user=viewer.user)
    if author:
        qs = qs.filter(author_id=author)
    return qs


def highlights(viewer):
    """The round "story" circles at the top: categories with posts, newest first, each with a cover picture."""
    out = []
    qs = viewer.posts()
    for key, label in Moment.CATEGORIES:
        latest = qs.filter(category=key).order_by('-created_at').first()
        if latest:
            cover = latest.media.exclude(thumb='').first()
            out.append({'key': key, 'label': label, 'icon': Moment.ICONS[key], 'cover': cover,
                        'at': latest.created_at})
    return sorted(out, key=lambda h: h['at'], reverse=True)



PHOTO_PURPOSE = 'School photos on the website and social media'


def photo_consent(tenant):
    """Per class, which children's parents said no to photos and how many have not answered — so a teacher can
    check before posting. {class id: {'refused': [names], 'unanswered': n, 'total': n}} ('' for the whole school);
    empty if the school does not ask for photo consent."""
    from security.models import Consent, DataPurpose
    from users.models import ENROLLED_STATUSES
    purpose = DataPurpose.objects.filter(tenant=tenant, name__iexact=PHOTO_PURPOSE, active=True,
                                         lawful_basis='CONSENT').first()
    if purpose is None:
        return {}
    answers = dict(Consent.objects.filter(tenant=tenant, purpose=purpose).values_list('student_id', 'granted'))
    out = {'': {'refused': [], 'unanswered': 0, 'total': 0}}
    for st in Student.objects.filter(tenant=tenant, status__in=ENROLLED_STATUSES).order_by('full_name') \
            .values('id', 'full_name', 'class_assigned_id'):
        for key in ('', st['class_assigned_id'] or 0):
            row = out.setdefault(key, {'refused': [], 'unanswered': 0, 'total': 0})
            row['total'] += 1
            answer = answers.get(st['id'])
            if answer is False:
                row['refused'].append(st['full_name'])
            elif answer is None:
                row['unanswered'] += 1
    return out
