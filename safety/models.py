"""
safety/models.py
────────────────
Who may collect a child and who did, one-time pickup codes, emergency roll
calls, and lost ID cards.
"""
import datetime
import uuid
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone

from tenants.managers import TenantManager, TenantModelMixin


class SafetySettings(TenantModelMixin, models.Model):
    school_ends_at = models.TimeField(default=datetime.time(14, 0),
                                      help_text='Collections before this time are early pickups.')
    pass_valid_hours = models.PositiveSmallIntegerField(
        default=6, help_text='How long a pickup code works after it is issued.')
    notify_parent_on_pickup = models.BooleanField(default=True)
    card_replacement_fee = models.DecimalField(max_digits=10, decimal_places=2,
                                               default=Decimal('1000.00'))
    phone_clock_in = models.BooleanField(default=False,
                                         help_text='Let staff clock in from their phone on school grounds.')
    school_latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    school_longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    clock_radius_m = models.PositiveIntegerField(default=150,
                                                 help_text='How close to the school a phone must be.')
    updated_at = models.DateTimeField(auto_now=True)
    objects = TenantManager()

    class Meta:
        constraints = [models.UniqueConstraint(fields=['tenant'], name='one_safety_settings_per_school')]


class PickupPass(TenantModelMixin, models.Model):
    """A one-time code a parent issues so someone else can collect their child."""
    ISSUED, USED, CANCELLED = 'ISSUED', 'USED', 'CANCELLED'
    STATUSES = [(ISSUED, 'Waiting'), (USED, 'Used'), (CANCELLED, 'Cancelled')]

    student = models.ForeignKey('users.Student', on_delete=models.CASCADE, related_name='pickup_passes')
    collector_name = models.CharField(max_length=120)
    collector_phone = models.CharField(max_length=20, blank=True)
    relationship = models.CharField(max_length=60, blank=True)
    reason = models.CharField(max_length=200, blank=True)
    code = models.CharField(max_length=6)
    valid_on = models.DateField(default=timezone.localdate)
    expires_at = models.DateTimeField()
    status = models.CharField(max_length=9, choices=STATUSES, default=ISSUED)
    failed_attempts = models.PositiveSmallIntegerField(default=0)
    issued_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                  blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    objects = TenantManager()

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Pickup pass for {self.student.full_name} — {self.collector_name}"

    @property
    def is_usable(self):
        return self.status == self.ISSUED and timezone.now() <= self.expires_at


class PickupRelease(TenantModelMixin, models.Model):
    """A child handed over at the gate: to whom, when, why, and how it was checked."""
    LIST, PASS, OVERRIDE = 'LIST', 'PASS', 'OVERRIDE'
    CHECKS = [(LIST, 'On the approved list'), (PASS, 'Pickup code'), (OVERRIDE, 'Head approved')]

    student = models.ForeignKey('users.Student', on_delete=models.CASCADE, related_name='pickups')
    collector_name = models.CharField(max_length=120)
    relationship = models.CharField(max_length=60, blank=True)
    guardian = models.ForeignKey('users.Guardian', on_delete=models.SET_NULL, null=True, blank=True,
                                 related_name='pickups')
    pickup_pass = models.ForeignKey(PickupPass, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name='releases')
    checked_by = models.CharField(max_length=8, choices=CHECKS)
    override_reason = models.CharField(max_length=255, blank=True)
    early = models.BooleanField(default=False)
    reason = models.CharField(max_length=200, blank=True)
    released_at = models.DateTimeField(default=timezone.now)
    released_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                    blank=True, related_name='+')
    parent_notified = models.BooleanField(default=False)
    objects = TenantManager()

    class Meta:
        ordering = ['-released_at']

    def __str__(self):
        return f"{self.student.full_name} collected by {self.collector_name}"


class RollCall(TenantModelMixin, models.Model):
    """An emergency or drill head count."""
    DRILL, REAL = 'DRILL', 'REAL'
    title = models.CharField(max_length=120, default='Fire drill')
    kind = models.CharField(max_length=5, choices=[(DRILL, 'Drill'), (REAL, 'Real emergency')],
                            default=DRILL)
    assembly_point = models.CharField(max_length=120, blank=True)
    started_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                   blank=True, related_name='+')
    started_at = models.DateTimeField(default=timezone.now)
    ended_at = models.DateTimeField(null=True, blank=True)
    note = models.TextField(blank=True)
    objects = TenantManager()

    class Meta:
        ordering = ['-started_at']

    def __str__(self):
        return f"{self.title} {self.started_at:%d %b %Y %H:%M}"

    @property
    def is_open(self):
        return self.ended_at is None


class RollCallEntry(TenantModelMixin, models.Model):
    SAFE, INJURED, MISSING = 'SAFE', 'INJURED', 'MISSING'
    STATUSES = [(SAFE, 'Safe'), (INJURED, 'Hurt — needs help'), (MISSING, 'Missing')]

    roll_call = models.ForeignKey(RollCall, on_delete=models.CASCADE, related_name='entries')
    student = models.ForeignKey('users.Student', on_delete=models.CASCADE, null=True, blank=True,
                                related_name='+')
    staff = models.ForeignKey('users.Staff', on_delete=models.CASCADE, null=True, blank=True,
                              related_name='+')
    status = models.CharField(max_length=8, choices=STATUSES)
    note = models.CharField(max_length=200, blank=True)
    marked_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                  blank=True, related_name='+')
    marked_at = models.DateTimeField(default=timezone.now)
    objects = TenantManager()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['roll_call', 'student'], name='one_entry_per_student',
                                    condition=models.Q(student__isnull=False)),
            models.UniqueConstraint(fields=['roll_call', 'staff'], name='one_entry_per_staff',
                                    condition=models.Q(staff__isnull=False)),
        ]


class LostCardReport(TenantModelMixin, models.Model):
    """A lost ID card: switched off at once, replaced, and the fee recorded."""
    REPORTED, REISSUED = 'REPORTED', 'REISSUED'
    card = models.ForeignKey('idcards.IDCard', on_delete=models.CASCADE, related_name='loss_reports')
    reported_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                    blank=True, related_name='+')
    note = models.CharField(max_length=200, blank=True)
    status = models.CharField(max_length=8, choices=[(REPORTED, 'Card switched off'),
                                                     (REISSUED, 'New card issued')], default=REPORTED)
    fee = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    fee_status = models.CharField(max_length=7, default='NONE',
                                  choices=[('NONE', 'No fee'), ('WALLET', 'Paid from wallet'),
                                           ('CASH', 'Paid at the office'), ('OWED', 'Owed'),
                                           ('WAIVED', 'Waived')])
    reissued_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                    blank=True, related_name='+')
    reissued_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    objects = TenantManager()

    class Meta:
        ordering = ['-created_at']


# ── Visitors ──────────────────────────────────────────────────────────────────
def _visitor_photo_path(instance, filename):
    import uuid
    return f"visitor_photos/{instance.tenant_id}/{uuid.uuid4().hex}.jpg"


def _private():
    from users.models import PrivateStorage
    return PrivateStorage()


class Visit(TenantModelMixin, models.Model):
    """Someone visiting the school: expected in advance or walking in."""
    EXPECTED, ON_SITE, LEFT, CANCELLED, DENIED = 'EXPECTED', 'ON_SITE', 'LEFT', 'CANCELLED', 'DENIED'
    STATUSES = [(EXPECTED, 'Expected'), (ON_SITE, 'On site'), (LEFT, 'Left'),
                (CANCELLED, 'Cancelled'), (DENIED, 'Turned away')]
    PURPOSES = [('MEETING', 'Meeting'), ('PARENT', 'Parent visit'), ('DELIVERY', 'Delivery'),
                ('CONTRACTOR', 'Contractor / repairs'), ('INTERVIEW', 'Interview'),
                ('OFFICIAL', 'Official / inspector'), ('OTHER', 'Other')]

    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    name = models.CharField(max_length=120)
    phone = models.CharField(max_length=20, blank=True)
    email = models.EmailField(blank=True)
    organisation = models.CharField(max_length=120, blank=True)
    purpose = models.CharField(max_length=10, choices=PURPOSES, default='MEETING')
    purpose_note = models.CharField(max_length=200, blank=True)
    host = models.ForeignKey('users.Staff', on_delete=models.SET_NULL, null=True, blank=True,
                             related_name='visits_hosted')
    student = models.ForeignKey('users.Student', on_delete=models.SET_NULL, null=True, blank=True,
                                related_name='visits')
    expected_on = models.DateField(default=timezone.localdate)
    expected_time = models.TimeField(null=True, blank=True)
    status = models.CharField(max_length=9, choices=STATUSES, default=EXPECTED)
    id_type = models.CharField(max_length=40, blank=True)
    id_number = models.CharField(max_length=40, blank=True)
    vehicle = models.CharField(max_length=20, blank=True, help_text='Plate number, if driving in.')
    photo = models.ImageField(upload_to=_visitor_photo_path, storage=_private, blank=True, null=True)
    badge_number = models.PositiveIntegerField(null=True, blank=True)
    checked_in_at = models.DateTimeField(null=True, blank=True)
    checked_out_at = models.DateTimeField(null=True, blank=True)
    checked_in_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                      blank=True, related_name='+')
    registered_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                      blank=True, related_name='+')
    note = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    objects = TenantManager()

    class Meta:
        ordering = ['-expected_on', '-created_at']

    def __str__(self):
        return f"{self.name} ({self.get_status_display()})"

    @property
    def host_name(self):
        return self.host.full_name if self.host else ''


# ── Staff leave ───────────────────────────────────────────────────────────────
class StaffLeave(TenantModelMixin, models.Model):
    TYPES = [('ANNUAL', 'Annual leave'), ('SICK', 'Sick leave'), ('MATERNITY', 'Maternity leave'),
             ('PATERNITY', 'Paternity leave'), ('STUDY', 'Study leave'),
             ('COMPASSIONATE', 'Compassionate leave'), ('OFFICIAL', 'Official duty'),
             ('UNPAID', 'Unpaid leave'), ('OTHER', 'Other')]
    PENDING, APPROVED, REJECTED, CANCELLED = 'PENDING', 'APPROVED', 'REJECTED', 'CANCELLED'
    STATUSES = [(PENDING, 'Waiting'), (APPROVED, 'Approved'), (REJECTED, 'Declined'),
                (CANCELLED, 'Cancelled')]

    staff = models.ForeignKey('users.Staff', on_delete=models.CASCADE, related_name='leaves')
    leave_type = models.CharField(max_length=13, choices=TYPES, default='ANNUAL')
    start_date = models.DateField()
    end_date = models.DateField()
    reason = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=9, choices=STATUSES, default=PENDING)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                     blank=True, related_name='+')
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                    blank=True, related_name='+')
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_note = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    objects = TenantManager()

    class Meta:
        ordering = ['-start_date']

    def __str__(self):
        return f"{self.staff.full_name}: {self.get_leave_type_display()} {self.start_date}–{self.end_date}"
