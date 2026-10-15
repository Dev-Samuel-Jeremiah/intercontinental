"""
notifications/services.py
─────────────────────────
Creating notifications and working out who sees them.

Who sees what
  administrators  every school event (admin_feed), plus anything addressed to them
  teachers/staff  notices addressed to them, and staff-wide notices
  students        notices addressed to them, their class, and all students
  parents         notices about their children (the family), their children's
                  classes, and all parents
Nobody is notified about their own action (actor).
"""
from datetime import timedelta

from django.db import IntegrityError
from django.db.models import Q
from django.utils import timezone

from .models import Notification, NotificationRead, NotificationState

N = Notification
KEEP_DAYS = 90          # older notices drop out of the bell
FEED_SIZE = 15

# kind → (category, default icon). Categories drive the filter tabs on the full page.
KINDS = {
    'announcement': ('Announcements', 'megaphone-fill'),
    'exam': ('Academics', 'pencil-square'),
    'homework': ('Academics', 'journal-text'),
    'assignment': ('Academics', 'clipboard-check'),
    'submission': ('Academics', 'inbox-fill'),
    'graded': ('Academics', 'award-fill'),
    'result': ('Academics', 'trophy-fill'),
    'absence': ('Attendance', 'person-x-fill'),
    'gate': ('Attendance', 'door-open-fill'),
    'payment': ('Finance', 'cash-coin'),
    'message': ('Messages', 'chat-dots-fill'),
    'ticket': ('Messages', 'life-preserver'),
    'admission': ('Administration', 'person-plus-fill'),
    'leave': ('Administration', 'calendar2-x-fill'),
    'report': ('Administration', 'file-earmark-text-fill'),
}
CATEGORIES = sorted({c for c, _ in KINDS.values()})


# ─────────────────────────────────────────────────────────────────────────────
# Creating
# ─────────────────────────────────────────────────────────────────────────────

def family_key(student):
    from communications.connect import family_key as key
    return key(student)


def notify(tenant, kind, title, audience, *, recipient=None, student=None, school_class=None, body='',
           url='', icon='', level='info', actor=None, admin_feed=True, dedupe=''):
    """Record one notification. With `dedupe`, an identical key is only ever recorded once.
    Never raises: a notice must not break the action that caused it."""
    if tenant is None:
        return None
    try:
        if dedupe and N.objects.filter(tenant=tenant, dedupe_key=dedupe).exists():
            return None
        n = build(tenant, kind, title, audience, recipient=recipient, student=student, school_class=school_class,
                  body=body, url=url, icon=icon, level=level, actor=actor, admin_feed=admin_feed, dedupe=dedupe)
        n.save()
        return n
    except (IntegrityError, ValueError):
        import logging
        logging.getLogger(__name__).exception('Could not record a %s notification', kind)
        return None


def build(tenant, kind, title, audience, *, recipient=None, student=None, school_class=None, body='', url='',
          icon='', level='info', actor=None, admin_feed=True, dedupe=''):
    """An unsaved Notification, for notify_many()."""
    return N(tenant=tenant, kind=kind, audience=audience, recipient=recipient,
             family=family_key(student) if audience == N.FAMILY and student else '',
             student=student, school_class=school_class, title=title[:160], body=(body or '')[:300],
             url=(url or '')[:300], icon=icon or KINDS.get(kind, ('', 'bell'))[1], level=level,
             actor=actor if getattr(actor, 'pk', None) else None, admin_feed=admin_feed, dedupe_key=dedupe[:120])


def notify_many(tenant, items):
    """Save many notifications in one go (results publishing, registers), skipping any
    whose dedupe key was recorded before. Returns how many were new."""
    keys = [n.dedupe_key for n in items if n.dedupe_key]
    seen = set(N.objects.filter(tenant=tenant, dedupe_key__in=keys).values_list('dedupe_key', flat=True)) if keys else set()
    fresh, batch_keys = [], set()
    for n in items:
        if n.dedupe_key and (n.dedupe_key in seen or n.dedupe_key in batch_keys):
            continue
        batch_keys.add(n.dedupe_key)
        fresh.append(n)
    N.objects.bulk_create(fresh, batch_size=500)
    return len(fresh)


def to_user(user, kind, title, **kw):
    if user is None or not getattr(user, 'pk', None):
        return None
    return notify(kw.pop('tenant', None) or user.tenant, kind, title, N.USER, recipient=user, **kw)


def to_family(student, kind, title, **kw):
    """The parents of `student` — every parent account in the family sees it."""
    return notify(student.tenant, kind, title, N.FAMILY, student=student, **kw)


def to_student(student, kind, title, **kw):
    if not student.user_id:
        return None
    return notify(student.tenant, kind, title, N.USER, recipient=student.user, student=student, **kw)


def to_admins(tenant, kind, title, **kw):
    return notify(tenant, kind, title, N.ADMINS, **kw)


# ─────────────────────────────────────────────────────────────────────────────
# Who sees what
# ─────────────────────────────────────────────────────────────────────────────

def role_of(user, tenant):
    if not getattr(user, 'is_authenticated', False):
        return None
    if user.is_superuser:
        return 'admin'
    staff = user.staff_profiles.filter(tenant=tenant).first()
    if staff:
        return 'admin' if staff.role == 'ADMIN' else 'staff'
    if getattr(user, 'is_parent', False):
        return 'parent'
    if user.student_profiles.filter(tenant=tenant).exists():
        return 'student'
    return None


def visible_to(user, tenant):
    """Every live notification this user may see, newest first."""
    role = role_of(user, tenant)
    if role is None or tenant is None:
        return N.objects.none()
    base = N.objects.filter(tenant=tenant, created_at__gte=timezone.now() - timedelta(days=KEEP_DAYS))
    mine = Q(recipient=user)
    if role == 'admin':
        q = Q(admin_feed=True) | mine
    elif role == 'staff':
        q = mine | Q(audience=N.ALL_STAFF)
    elif role == 'student':
        student = user.student_profiles.filter(tenant=tenant).first()
        q = mine | Q(audience=N.ALL_STUDENTS)
        if student and student.class_assigned_id:
            q |= Q(audience=N.CLASS_STUDENTS, school_class_id=student.class_assigned_id)
    else:                                   # parent
        from family.services import children_of
        kids = children_of(user, tenant)
        q = mine | Q(audience=N.ALL_PARENTS)
        if kids:
            q |= Q(audience=N.FAMILY, family__in={family_key(k) for k in kids})
            classes = {k.class_assigned_id for k in kids if k.class_assigned_id}
            if classes:
                q |= Q(audience=N.CLASS_PARENTS, school_class_id__in=classes)
    return base.filter(q).exclude(actor=user).select_related('student', 'school_class', 'recipient')


def _cleared_at(user):
    state = NotificationState.objects.filter(user=user).first()
    return state.cleared_at if state else None


def unread(user, tenant):
    qs = visible_to(user, tenant).exclude(reads__user=user)
    cleared = _cleared_at(user)
    return qs.filter(created_at__gt=cleared) if cleared else qs


def mark_read(user, tenant, ids):
    ids = list(visible_to(user, tenant).filter(pk__in=ids).values_list('pk', flat=True))
    NotificationRead.objects.bulk_create([NotificationRead(notification_id=i, user=user) for i in ids],
                                         ignore_conflicts=True)
    return len(ids)


def mark_all_read(user):
    NotificationState.objects.update_or_create(user=user, defaults={'cleared_at': timezone.now()})


# ─────────────────────────────────────────────────────────────────────────────
# Showing
# ─────────────────────────────────────────────────────────────────────────────

def audience_label(n, user):
    """For administrators: who a notice was actually for."""
    if n.recipient_id == user.pk:
        return ''
    if n.audience == N.USER and n.recipient:
        return f'To {n.recipient.get_full_name() or n.recipient.username}'
    if n.audience == N.FAMILY and n.student:
        return f'To parents of {n.student.full_name}'
    if n.audience == N.CLASS_STUDENTS and n.school_class:
        return f'To {n.school_class.name} students'
    if n.audience == N.CLASS_PARENTS and n.school_class:
        return f'To {n.school_class.name} parents'
    return {N.ALL_STAFF: 'To all staff', N.ALL_STUDENTS: 'To all students',
            N.ALL_PARENTS: 'To all parents'}.get(n.audience, '')


def serialize(items, user, unread_ids):
    from django.urls import reverse
    out = []
    for n in items:
        out.append({
            'id': n.pk, 'kind': n.kind, 'category': KINDS.get(n.kind, ('General', ''))[0],
            'title': n.title, 'body': n.body, 'icon': n.icon, 'level': n.level,
            'url': reverse('notifications:open', args=[n.pk]) if n.url else '',
            'for': audience_label(n, user), 'unread': n.pk in unread_ids,
            'at': n.created_at.isoformat(),
        })
    return out
