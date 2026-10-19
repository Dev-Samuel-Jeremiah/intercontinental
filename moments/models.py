"""
moments/models.py
─────────────────
School Moments: an Instagram-style feed inside the portal. Staff post photos
and videos of classroom activities and events — for the whole school or one
class — and staff, parents and students like, comment and save them.
Everything stays inside the signed-in portal; files have no public address.
"""
import os
import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

from tenants.managers import TenantManager, TenantModelMixin


def _private_storage():
    from users.models import PrivateStorage
    return PrivateStorage()


def _upload(instance, filename):
    ext = os.path.splitext(filename)[1].lower()[:8]
    return f'moments/{instance.post.tenant_id}/{instance.post_id}/{uuid.uuid4().hex}{ext}'


class Moment(TenantModelMixin, models.Model):
    CATEGORIES = (('CLASSROOM', 'Classroom activity'), ('EVENT', 'Event'), ('SPORTS', 'Sports'),
                  ('ACHIEVEMENT', 'Achievement'), ('TRIP', 'Excursion'), ('CULTURE', 'Culture & arts'),
                  ('NEWS', 'School news'))
    ICONS = {'CLASSROOM': 'easel2-fill', 'EVENT': 'balloon-heart-fill', 'SPORTS': 'trophy-fill',
             'ACHIEVEMENT': 'award-fill', 'TRIP': 'bus-front-fill', 'CULTURE': 'palette-fill', 'NEWS': 'newspaper'}

    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='+')
    caption = models.TextField(max_length=2200, blank=True)
    category = models.CharField(max_length=12, choices=CATEGORIES, default='CLASSROOM')
    school_class = models.ForeignKey('users.Class', on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
                                     help_text='Only this class (its students and parents) and staff see it. Blank: everyone.')
    comments_on = models.BooleanField(default=True)
    pinned = models.BooleanField(default=False)
    like_count = models.PositiveIntegerField(default=0)
    comment_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    edited_at = models.DateTimeField(null=True, blank=True)
    objects = TenantManager()

    class Meta:
        ordering = ['-pinned', '-created_at']

    def __str__(self):
        return (self.caption or self.get_category_display())[:60]

    @property
    def icon(self):
        return self.ICONS.get(self.category, 'images')


class Media(models.Model):
    IMAGE, VIDEO = 'IMAGE', 'VIDEO'
    READY, PROCESSING, FAILED = 'READY', 'PROCESSING', 'FAILED'
    post = models.ForeignKey(Moment, on_delete=models.CASCADE, related_name='media')
    kind = models.CharField(max_length=5, choices=((IMAGE, 'Photo'), (VIDEO, 'Video')))
    file = models.FileField(upload_to=_upload, storage=_private_storage, max_length=255)
    thumb = models.FileField(upload_to=_upload, storage=_private_storage, max_length=255, blank=True)
    width = models.PositiveIntegerField(default=0)
    height = models.PositiveIntegerField(default=0)
    duration = models.FloatField(default=0)
    status = models.CharField(max_length=10, default=READY,
                              choices=((READY, 'Ready'), (PROCESSING, 'Processing'), (FAILED, 'Could not be prepared')))
    status_at = models.DateTimeField(default=timezone.now)
    order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ['order', 'id']

    @property
    def ratio(self):
        """Width/height for the frame, kept between Instagram's 4:5 portrait and 1.91:1 landscape."""
        if not self.width or not self.height:
            return 1
        return round(min(1.91, max(0.8, self.width / self.height)), 3)


class Like(models.Model):
    post = models.ForeignKey(Moment, on_delete=models.CASCADE, related_name='likes')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='+')
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        unique_together = ('post', 'user')


class Saved(models.Model):
    post = models.ForeignKey(Moment, on_delete=models.CASCADE, related_name='saves')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='+')
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        unique_together = ('post', 'user')


class Comment(models.Model):
    VISIBLE, HELD, HIDDEN = 'VISIBLE', 'HELD', 'HIDDEN'
    post = models.ForeignKey(Moment, on_delete=models.CASCADE, related_name='comments')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='+')
    reply_to = models.ForeignKey('self', on_delete=models.CASCADE, null=True, blank=True, related_name='replies')
    body = models.TextField(max_length=1000)
    status = models.CharField(max_length=7, default=VISIBLE,
                              choices=((VISIBLE, 'Visible'), (HELD, 'Held for review'), (HIDDEN, 'Hidden')))
    flag_reason = models.CharField(max_length=160, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['created_at', 'id']


class MomentReport(TenantModelMixin, models.Model):
    post = models.ForeignKey(Moment, on_delete=models.CASCADE, related_name='reports')
    comment = models.ForeignKey(Comment, on_delete=models.CASCADE, null=True, blank=True, related_name='reports')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='+')
    reason = models.CharField(max_length=300)
    created_at = models.DateTimeField(default=timezone.now)
    resolved = models.BooleanField(default=False)
    objects = TenantManager()

    class Meta:
        ordering = ['resolved', '-created_at']
