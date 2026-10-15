from django.conf import settings
from django.db import models
from tenants.managers import TenantModelMixin, TenantManager

from django.utils import timezone
from users.models import Class, TERM_OPTIONS

class Announcement(TenantModelMixin, models.Model):
    title = models.CharField(max_length=255)
    message = models.TextField()

    school_class = models.ForeignKey(
        Class,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='announcements'
    )

    term = models.CharField(
        max_length=20,
        choices=TERM_OPTIONS,
        blank=True,
        null=True
    )

    session = models.CharField(
        max_length=20,
        blank=True,
        null=True
    )

    audience = models.CharField(
        max_length=10,
        choices=(
            ('students', 'Students'),
            ('parents', 'Parents'),
            ('both', 'Students & Parents'),
        ),
        default='both'
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )

    published = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    published_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)

    # ── Email / SMS notification fields ───────────────────────────────────────
    notify_parents = models.BooleanField(
        default=False,
        help_text='Send email/SMS to parents when this announcement is published'
    )
    notify_channel = models.CharField(
        max_length=5,
        choices=[('EMAIL', 'Email Only'), ('SMS', 'SMS Only'), ('BOTH', 'Email + SMS')],
        default='BOTH',
        help_text='Which channel to use for parent notifications'
    )

    class Meta:
        ordering = ['-published_at', '-created_at']

    def publish(self):
        self.published = True
        self.published_at = timezone.now()
        self.save()

    def is_active(self):
        if self.expires_at:
            return self.expires_at >= timezone.now()
        return True

    def __str__(self):
        return self.title


# announcements/models.py
from django.conf import settings
from django.db import models

class AnnouncementRead(TenantModelMixin, models.Model):
    announcement = models.ForeignKey(
        Announcement,
        on_delete=models.CASCADE,
        related_name='reads'
    )
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    read_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('announcement', 'user')




from django.db import models
from django.conf import settings

class AnnouncementReaction(TenantModelMixin, models.Model):
    announcement = models.ForeignKey('Announcement', on_delete=models.CASCADE, related_name='reactions')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    liked = models.BooleanField(default=False)  # You can expand to multiple reactions later
    reacted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('announcement', 'user')