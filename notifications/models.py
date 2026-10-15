"""
notifications/models.py
───────────────────────
In-app notifications behind the bell in the top bar.

One row per event, addressed to an audience — a person, a family, the
administrators, a class, or everyone in a role — so a class announcement is
one row, not forty. Read state is kept per user (NotificationRead), and
"mark all as read" just moves the user's cleared_at mark (NotificationState).
"""
from django.conf import settings
from django.db import models

from tenants.managers import TenantModelMixin


class Notification(TenantModelMixin, models.Model):
    USER, FAMILY, ADMINS = 'USER', 'FAMILY', 'ADMINS'
    CLASS_STUDENTS, CLASS_PARENTS = 'CLASS_STU', 'CLASS_PAR'
    ALL_STAFF, ALL_STUDENTS, ALL_PARENTS = 'ALL_STAFF', 'ALL_STU', 'ALL_PAR'
    AUDIENCES = [
        (USER, 'One person'), (FAMILY, "A child's family"), (ADMINS, 'Administrators'),
        (CLASS_STUDENTS, 'Students in a class'), (CLASS_PARENTS, 'Parents of a class'),
        (ALL_STAFF, 'All staff'), (ALL_STUDENTS, 'All students'), (ALL_PARENTS, 'All parents'),
    ]
    LEVELS = [('info', 'Information'), ('success', 'Good news'), ('warning', 'Needs attention'),
              ('danger', 'Urgent')]

    audience = models.CharField(max_length=10, choices=AUDIENCES)
    recipient = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True, blank=True,
                                  related_name='notifications')
    family = models.CharField(max_length=160, blank=True, db_index=True,
                              help_text="The family key (parent email) for FAMILY notices.")
    student = models.ForeignKey('users.Student', on_delete=models.CASCADE, null=True, blank=True,
                                related_name='+', help_text='The child a family notice is about.')
    school_class = models.ForeignKey('users.Class', on_delete=models.CASCADE, null=True, blank=True,
                                     related_name='+')

    kind = models.CharField(max_length=30, db_index=True)
    title = models.CharField(max_length=160)
    body = models.CharField(max_length=300, blank=True)
    url = models.CharField(max_length=300, blank=True)
    icon = models.CharField(max_length=40, default='bell')
    level = models.CharField(max_length=8, choices=LEVELS, default='info')
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                              related_name='+', help_text='Who caused it (not notified about their own action).')
    admin_feed = models.BooleanField(default=True,
                                     help_text='Shown to administrators. Off for private messages and '
                                               'per-child routine notices that would flood the bell.')
    dedupe_key = models.CharField(max_length=120, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at', '-id']
        indexes = [models.Index(fields=['tenant', 'audience', 'created_at'])]

    def __str__(self):
        return self.title


class NotificationRead(models.Model):
    notification = models.ForeignKey(Notification, on_delete=models.CASCADE, related_name='reads')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='+')
    read_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['notification', 'user'], name='one_read_mark_per_user')]


class NotificationState(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='notification_state')
    cleared_at = models.DateTimeField(null=True, blank=True,
                                      help_text='Everything up to this moment counts as read.')
