"""
idcards/models.py — tenant-scoped
==================================
Digital + printable ID cards (with QR codes) for students and staff, plus
the two attendance workflows they drive:

  1. Gate attendance  — a student's personal QR (on their printed ID card)
     is scanned by a staff member at the gate every morning/closing time.
     Each scan is logged (GateScanLog) and the parent gets an email.

  2. Staff clock in/out — ONE shared QR code is printed and stuck at the
     reception desk. Staff scan that same code with their own phone
     (already logged in) to clock in/out. Each scan is logged
     (StaffClockLog) instead of a manual attendance register.
"""
import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

from tenants.managers import TenantModelMixin, TenantManager


class IDCard(TenantModelMixin, models.Model):
    """One ID card per Student or per Staff member. The `card_uid` is the
    secret embedded in the printed QR code — nobody can guess it, and it
    never changes unless the card is reissued."""

    CARD_TYPE_CHOICES = [
        ('STUDENT', 'Student'),
        ('STAFF', 'Staff'),
    ]

    card_type   = models.CharField(max_length=10, choices=CARD_TYPE_CHOICES)
    student     = models.OneToOneField('users.Student', on_delete=models.CASCADE,
                                        null=True, blank=True, related_name='id_card')
    staff       = models.OneToOneField('users.Staff', on_delete=models.CASCADE,
                                        null=True, blank=True, related_name='id_card')
    card_uid    = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    card_number = models.CharField(max_length=30, blank=True,
                                    help_text="Human-readable card number printed on the card")
    is_active   = models.BooleanField(default=True)
    issued_at   = models.DateTimeField(auto_now_add=True)
    reissued_at = models.DateTimeField(null=True, blank=True)

    objects = TenantManager()

    class Meta:
        ordering = ['-issued_at']

    def __str__(self):
        return f"ID Card ({self.get_card_type_display()}) — {self.holder_name}"

    @property
    def holder(self):
        return self.student if self.card_type == 'STUDENT' else self.staff

    @property
    def holder_name(self):
        h = self.holder
        return h.full_name if h else '—'

    @property
    def holder_photo(self):
        if self.card_type == 'STUDENT' and self.student and self.student.passport:
            return self.student.passport
        return None

    def reissue(self):
        """Invalidate the old QR by rolling a fresh uid (old printed card stops working)."""
        self.card_uid = uuid.uuid4()
        self.reissued_at = timezone.now()
        self.save(update_fields=['card_uid', 'reissued_at'])


GATE_DIRECTION_CHOICES = [
    ('IN', 'Arrived'),
    ('OUT', 'Departed'),
]


class GateScanLog(TenantModelMixin, models.Model):
    """One row per gate scan of a student's ID card."""

    student      = models.ForeignKey('users.Student', on_delete=models.CASCADE,
                                      related_name='gate_scans')
    card         = models.ForeignKey(IDCard, on_delete=models.SET_NULL, null=True, blank=True,
                                      related_name='gate_scans')
    direction    = models.CharField(max_length=3, choices=GATE_DIRECTION_CHOICES)
    scanned_at   = models.DateTimeField(auto_now_add=True)
    scanned_by   = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                      null=True, blank=True, related_name='gate_scans_operated')
    parent_notified      = models.BooleanField(default=False)
    notify_error         = models.CharField(max_length=255, blank=True)

    objects = TenantManager()

    class Meta:
        ordering = ['-scanned_at']

    def __str__(self):
        return f"{self.student.full_name} — {self.get_direction_display()} @ {self.scanned_at:%Y-%m-%d %H:%M}"


class ReceptionStation(TenantModelMixin, models.Model):
    """A single shared QR code, printed and posted at reception, that any
    staff member scans (with their own device) to clock in/out."""

    name         = models.CharField(max_length=100, default='Main Reception')
    station_uid  = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    is_active    = models.BooleanField(default=True)
    created_at   = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    def __str__(self):
        return self.name

    @classmethod
    def get_or_create_default(cls, tenant):
        station = cls.objects.for_tenant(tenant).filter(is_active=True).first()
        if station:
            return station
        return cls.objects.create(tenant=tenant, name='Main Reception')


class StaffClockLog(TenantModelMixin, models.Model):
    """One row per staff clock-in/out scan at the reception QR."""

    staff       = models.ForeignKey('users.Staff', on_delete=models.CASCADE,
                                     related_name='clock_logs')
    station     = models.ForeignKey(ReceptionStation, on_delete=models.SET_NULL,
                                     null=True, blank=True, related_name='clock_logs')
    direction   = models.CharField(max_length=3, choices=GATE_DIRECTION_CHOICES)
    scanned_at  = models.DateTimeField(auto_now_add=True)
    method      = models.CharField(max_length=5, default='QR',
                                   choices=[('QR', 'Reception QR'), ('PHONE', 'Phone, on school grounds')])
    latitude    = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude   = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    distance_m  = models.PositiveIntegerField(null=True, blank=True,
                                              help_text='How far from the school the phone reported.')

    objects = TenantManager()

    class Meta:
        ordering = ['-scanned_at']

    def __str__(self):
        return f"{self.staff.full_name} — {self.get_direction_display()} @ {self.scanned_at:%Y-%m-%d %H:%M}"
