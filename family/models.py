"""
family/models.py
────────────────
The parent experience: daily digests, appointments, the PTA, volunteering,
feedback and complaints, password-free sign-in and satisfaction scores.
"""
import datetime
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone

from tenants.managers import TenantManager, TenantModelMixin

USER = settings.AUTH_USER_MODEL


class FamilySettings(TenantModelMixin, models.Model):
    digest_enabled = models.BooleanField(default=False, help_text='Email families a summary of each school day')
    satisfaction_enabled = models.BooleanField(default=True, help_text='Ask parents for a 0–10 score each term')
    complaint_days = models.PositiveSmallIntegerField(default=5, help_text='Target days to resolve a complaint')
    magic_login = models.BooleanField(default=True, help_text='Parents may sign in with an emailed link or SMS code')

    objects = TenantManager()


class DigestLog(TenantModelMixin, models.Model):
    family = models.CharField(max_length=160)
    date = models.DateField()
    sent_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        unique_together = ('tenant', 'family', 'date')


DAYS = [('MON', 'Monday'), ('TUE', 'Tuesday'), ('WED', 'Wednesday'), ('THU', 'Thursday'), ('FRI', 'Friday'),
        ('SAT', 'Saturday')]
MODES = [('IN_PERSON', 'At school'), ('PHONE', 'Phone call'), ('VIDEO', 'Video call')]


class Availability(TenantModelMixin, models.Model):
    """When a member of staff can meet parents, every week."""
    staff = models.ForeignKey('users.Staff', on_delete=models.CASCADE, related_name='availability')
    day = models.CharField(max_length=3, choices=DAYS)
    start = models.TimeField()
    end = models.TimeField()
    slot_minutes = models.PositiveSmallIntegerField(default=15)
    location = models.CharField(max_length=120, blank=True)

    objects = TenantManager()

    class Meta:
        ordering = ['staff__full_name', 'day', 'start']


class Appointment(TenantModelMixin, models.Model):
    BOOKED, CANCELLED, DONE = 'BOOKED', 'CANCELLED', 'DONE'
    staff = models.ForeignKey('users.Staff', on_delete=models.CASCADE, related_name='appointments')
    parent = models.ForeignKey(USER, on_delete=models.CASCADE, related_name='+')
    student = models.ForeignKey('users.Student', on_delete=models.CASCADE, related_name='appointments')
    date = models.DateField()
    start = models.TimeField()
    end = models.TimeField()
    mode = models.CharField(max_length=9, choices=MODES, default='IN_PERSON')
    reason = models.CharField(max_length=255)
    status = models.CharField(max_length=9, choices=[(BOOKED, 'Booked'), (CANCELLED, 'Cancelled'), (DONE, 'Held')],
                              default=BOOKED)
    note = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        ordering = ['date', 'start']


class PtaExecutive(TenantModelMixin, models.Model):
    name = models.CharField(max_length=120)
    role = models.CharField(max_length=80)
    phone = models.CharField(max_length=20, blank=True)
    email = models.EmailField(blank=True)
    order = models.PositiveSmallIntegerField(default=0)

    objects = TenantManager()

    class Meta:
        ordering = ['order', 'id']


class PtaMeeting(TenantModelMixin, models.Model):
    title = models.CharField(max_length=160)
    date = models.DateField()
    time = models.TimeField(default=datetime.time(10, 0))
    venue = models.CharField(max_length=160, blank=True)
    agenda = models.TextField(blank=True)
    minutes = models.TextField(blank=True)
    minutes_published = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        ordering = ['-date']


class PtaLevy(TenantModelMixin, models.Model):
    title = models.CharField(max_length=160)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    due_date = models.DateField(null=True, blank=True)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        ordering = ['-created_at']


class PtaPayment(TenantModelMixin, models.Model):
    """One per family per levy (the PTA levy is per family, not per child)."""
    levy = models.ForeignKey(PtaLevy, on_delete=models.CASCADE, related_name='payments')
    family = models.CharField(max_length=160)
    student = models.ForeignKey('users.Student', on_delete=models.SET_NULL, null=True, related_name='+')
    amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    method = models.CharField(max_length=8, choices=[('WALLET', 'Family wallet'), ('CASH', 'Cash'),
                                                     ('TRANSFER', 'Bank transfer'), ('WAIVED', 'Waived')])
    transaction = models.ForeignKey('finance.Transaction', on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name='+')
    received_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        unique_together = ('levy', 'family')


class Opportunity(TenantModelMixin, models.Model):
    title = models.CharField(max_length=160)
    details = models.TextField(blank=True)
    date = models.DateField()
    start_time = models.TimeField(null=True, blank=True)
    end_time = models.TimeField(null=True, blank=True)
    places = models.PositiveSmallIntegerField(null=True, blank=True, help_text='Empty = no limit')
    cancelled = models.BooleanField(default=False)
    created_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        ordering = ['date']


class Signup(TenantModelMixin, models.Model):
    opportunity = models.ForeignKey(Opportunity, on_delete=models.CASCADE, related_name='signups')
    user = models.ForeignKey(USER, on_delete=models.CASCADE, related_name='+')
    name = models.CharField(max_length=120)
    phone = models.CharField(max_length=20, blank=True)
    note = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        unique_together = ('opportunity', 'user')
        ordering = ['created_at']


class Ticket(TenantModelMixin, models.Model):
    KINDS = [('COMPLAINT', 'Complaint'), ('FEEDBACK', 'Feedback'), ('SUGGESTION', 'Suggestion'),
             ('COMPLIMENT', 'Compliment')]
    CATEGORIES = [('ACADEMIC', 'Teaching and learning'), ('FEES', 'Fees and payments'), ('WELFARE', 'Welfare and safety'),
                  ('TRANSPORT', 'Transport'), ('FACILITIES', 'Facilities'), ('STAFF', 'A member of staff'),
                  ('FOOD', 'Food and canteen'), ('OTHER', 'Something else')]
    OPEN, PROGRESS, RESOLVED, CLOSED = 'OPEN', 'PROGRESS', 'RESOLVED', 'CLOSED'
    STATUSES = [(OPEN, 'New'), (PROGRESS, 'Being looked at'), (RESOLVED, 'Resolved'), (CLOSED, 'Closed')]
    reference = models.CharField(max_length=20)
    parent = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, related_name='+')
    student = models.ForeignKey('users.Student', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    kind = models.CharField(max_length=10, choices=KINDS, default='COMPLAINT')
    category = models.CharField(max_length=10, choices=CATEGORIES, default='OTHER')
    subject = models.CharField(max_length=160)
    body = models.TextField()
    status = models.CharField(max_length=8, choices=STATUSES, default=OPEN)
    assigned_to = models.ForeignKey('users.Staff', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    rating = models.PositiveSmallIntegerField(null=True, blank=True)
    rating_comment = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    objects = TenantManager()

    class Meta:
        ordering = ['-created_at']
        unique_together = ('tenant', 'reference')


class TicketMessage(TenantModelMixin, models.Model):
    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name='messages')
    author = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, related_name='+')
    body = models.TextField()
    internal = models.BooleanField(default=False, help_text='Staff note, not shown to the parent')
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        ordering = ['created_at']


class LoginToken(TenantModelMixin, models.Model):
    """A one-time sign-in link (email) or code (SMS) for a parent."""
    token_hash = models.CharField(max_length=64, db_index=True)
    student = models.ForeignKey('users.Student', on_delete=models.CASCADE, related_name='+')
    channel = models.CharField(max_length=5, choices=[('EMAIL', 'Email'), ('SMS', 'SMS')])
    sent_to = models.CharField(max_length=160)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)

    objects = TenantManager()

    @property
    def live(self):
        return not self.used_at and self.expires_at > timezone.now() and self.attempts < 5


class SatisfactionResponse(TenantModelMixin, models.Model):
    user = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, related_name='+')
    family = models.CharField(max_length=160)
    period = models.CharField(max_length=10, help_text='e.g. 2026-T1')
    score = models.PositiveSmallIntegerField()
    comment = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        unique_together = ('tenant', 'family', 'period')
        ordering = ['-created_at']
