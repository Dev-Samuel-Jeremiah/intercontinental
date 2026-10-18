"""
staffchat/models.py
───────────────────
Staff chat, WhatsApp style: the school's community rooms (All Staff, and
Staff Announcements where only administrators post), groups any member of
staff can start, one-to-one chats, and complaints — a private thread between
one member of staff and management, optionally anonymous.
"""
import os
import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from tenants.managers import TenantManager, TenantModelMixin


def _private_storage():
    from users.models import PrivateStorage
    return PrivateStorage()


def _upload(instance, filename):
    ext = os.path.splitext(filename)[1].lower()[:8]
    return f'staffchat/{instance.room.tenant_id}/{instance.room_id}/{uuid.uuid4().hex}{ext}'


class ChatRoom(TenantModelMixin, models.Model):
    GROUP, DIRECT, COMPLAINT = 'GROUP', 'DIRECT', 'COMPLAINT'
    KINDS = ((GROUP, 'Group'), (DIRECT, 'Direct chat'), (COMPLAINT, 'Complaint'))
    # The two community rooms every member of staff belongs to
    ALL_STAFF, ANNOUNCEMENTS = 'ALL_STAFF', 'ANNOUNCEMENTS'
    COMMUNITY = ((ALL_STAFF, 'All Staff'), (ANNOUNCEMENTS, 'Staff Announcements'))
    CATEGORIES = (('WORKPLACE', 'Workplace'), ('PAY', 'Pay & allowances'), ('FACILITIES', 'Facilities & equipment'),
                  ('CONDUCT', 'Conduct of a colleague'), ('STUDENTS', 'Students & parents'),
                  ('WORKLOAD', 'Workload & timetable'), ('OTHER', 'Something else'))
    STATUSES = (('OPEN', 'Open'), ('REVIEW', 'Under review'), ('RESOLVED', 'Resolved'))

    kind = models.CharField(max_length=10, choices=KINDS, default=GROUP)
    community = models.CharField(max_length=14, choices=COMMUNITY, blank=True)
    name = models.CharField(max_length=80, blank=True)
    description = models.CharField(max_length=300, blank=True)
    only_admins_post = models.BooleanField(default=False, help_text='Only group admins can send messages.')
    direct_key = models.CharField(max_length=40, blank=True, help_text='"<user id>-<user id>" for a direct chat.')
    category = models.CharField(max_length=12, choices=CATEGORIES, blank=True)
    status = models.CharField(max_length=10, choices=STATUSES, blank=True)
    anonymous = models.BooleanField(default=False, help_text="Management sees the complainant's messages without a name.")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='+')
    created_at = models.DateTimeField(default=timezone.now)
    last_message_at = models.DateTimeField(null=True, blank=True)
    objects = TenantManager()

    class Meta:
        ordering = ['-last_message_at', '-id']
        constraints = [
            models.UniqueConstraint(fields=['tenant', 'direct_key'], condition=~Q(direct_key=''),
                                    name='staffchat_one_direct_room'),
            models.UniqueConstraint(fields=['tenant', 'community'], condition=~Q(community=''),
                                    name='staffchat_one_community_room'),
        ]

    def __str__(self):
        return self.name or f'{self.get_kind_display()} {self.pk}'


class Member(models.Model):
    MEMBER, ADMIN = 'MEMBER', 'ADMIN'
    room = models.ForeignKey(ChatRoom, on_delete=models.CASCADE, related_name='members')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='chat_memberships')
    role = models.CharField(max_length=6, choices=((MEMBER, 'Member'), (ADMIN, 'Admin')), default=MEMBER)
    last_read_id = models.BigIntegerField(default=0)
    typing_at = models.DateTimeField(null=True, blank=True)
    joined_at = models.DateTimeField(default=timezone.now)

    class Meta:
        unique_together = ('room', 'user')


class Message(models.Model):
    TEXT, SYSTEM = 'TEXT', 'SYSTEM'
    room = models.ForeignKey(ChatRoom, on_delete=models.CASCADE, related_name='messages')
    sender = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                               related_name='+')
    kind = models.CharField(max_length=6, choices=((TEXT, 'Message'), (SYSTEM, 'Event')), default=TEXT)
    body = models.TextField(blank=True)
    reply_to = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    attachment = models.FileField(upload_to=_upload, storage=_private_storage, blank=True)
    attachment_name = models.CharField(max_length=160, blank=True)
    attachment_size = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(default=timezone.now)
    edited_at = models.DateTimeField(null=True, blank=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    # Bumped on every edit, deletion or reaction, so open chats pick the change up
    updated_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ['id']
        indexes = [models.Index(fields=['room', 'id'])]


class Reaction(models.Model):
    message = models.ForeignKey(Message, on_delete=models.CASCADE, related_name='reactions')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='+')
    emoji = models.CharField(max_length=16)

    class Meta:
        unique_together = ('message', 'user')     # one reaction each, as on WhatsApp


class Presence(TenantModelMixin, models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='+')
    last_seen = models.DateTimeField(default=timezone.now)
    objects = TenantManager()

    class Meta:
        unique_together = ('tenant', 'user')
