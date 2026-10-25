"""
hostel/models.py — Hostel Management System
=================================================
Full boarding school ERP:
  Hostel → Floor → Room → Bed → BoarderProfile
  TermBilling (hostel fees per term)
  CheckIn / CheckOut history
  MealPlan + MealAttendance
  VisitorLog
  IncidentReport
  HostelNotice
"""

import uuid
from decimal import Decimal

from django.conf import settings
from django.db import models
from tenants.managers import TenantModelMixin, TenantManager
from security.crypto import EncryptedTextField

from django.utils import timezone

from users.models import Student
from academics.models import AcademicSession, Term


# ─────────────────────────────────────────────────────────────────────────────
# HOSTEL BUILDING
# ─────────────────────────────────────────────────────────────────────────────

class Hostel(TenantModelMixin, models.Model):
    GENDER_CHOICES = [
        ('MALE',   'Male (Boys)'),
        ('FEMALE', 'Female (Girls)'),
        ('MIXED',  'Mixed'),
    ]

    name        = models.CharField(max_length=100)
    gender      = models.CharField(max_length=6, choices=GENDER_CHOICES)
    description = models.TextField(blank=True)
    warden      = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='hostel_warden_of',
        help_text='Staff member responsible for this hostel'
    )
    address     = models.CharField(max_length=300, blank=True,
                                   help_text='Physical location / block description')
    capacity    = models.PositiveIntegerField(default=0,
                                              help_text='Total beds (auto-calculated from rooms)')
    is_active   = models.BooleanField(default=True)
    created_at  = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']
        unique_together = ('tenant', 'name')

    def __str__(self):
        return f"{self.name} ({self.get_gender_display()})"

    @property
    def total_beds(self):
        return sum(r.capacity for r in self.rooms.filter(is_active=True))

    @property
    def occupied_beds(self):
        return Bed.objects.filter(
            room__hostel=self,
            room__is_active=True,
            status='OCCUPIED'
        ).count()

    @property
    def available_beds(self):
        return self.total_beds - self.occupied_beds

    @property
    def occupancy_rate(self):
        total = self.total_beds
        return round((self.occupied_beds / total) * 100, 1) if total > 0 else 0


# ─────────────────────────────────────────────────────────────────────────────
# FLOOR
# ─────────────────────────────────────────────────────────────────────────────

class Floor(TenantModelMixin, models.Model):
    hostel      = models.ForeignKey(Hostel, on_delete=models.CASCADE,
                                    related_name='floors')
    name        = models.CharField(max_length=50,
                                   help_text='e.g. Ground Floor, 1st Floor')
    floor_number = models.PositiveIntegerField(default=0)
    description  = models.TextField(blank=True)

    class Meta:
        ordering = ['hostel', 'floor_number']
        unique_together = ('hostel', 'floor_number')

    def __str__(self):
        return f"{self.hostel.name} — {self.name}"


# ─────────────────────────────────────────────────────────────────────────────
# ROOM
# ─────────────────────────────────────────────────────────────────────────────

class Room(TenantModelMixin, models.Model):
    ROOM_TYPE_CHOICES = [
        ('DORMITORY', 'Dormitory (Shared)'),
        ('DOUBLE',    'Double Room'),
        ('SINGLE',    'Single Room'),
        ('SUITE',     'Suite'),
    ]
    STATUS_CHOICES = [
        ('AVAILABLE',    'Available'),
        ('FULL',         'Full'),
        ('MAINTENANCE',  'Under Maintenance'),
        ('CLOSED',       'Closed'),
    ]

    hostel      = models.ForeignKey(Hostel, on_delete=models.CASCADE,
                                    related_name='rooms')
    floor       = models.ForeignKey(Floor, on_delete=models.SET_NULL,
                                    null=True, blank=True, related_name='rooms')
    room_number = models.CharField(max_length=20)
    room_type   = models.CharField(max_length=12, choices=ROOM_TYPE_CHOICES,
                                   default='DORMITORY')
    capacity    = models.PositiveIntegerField(default=4,
                                              help_text='Maximum number of beds')
    status      = models.CharField(max_length=12, choices=STATUS_CHOICES,
                                   default='AVAILABLE')
    description = models.TextField(blank=True)
    has_ac      = models.BooleanField(default=False, verbose_name='Has A/C')
    has_bathroom = models.BooleanField(default=False, verbose_name='Has Attached Bathroom')
    is_active   = models.BooleanField(default=True)

    class Meta:
        ordering = ['hostel', 'room_number']
        unique_together = ('hostel', 'room_number')

    def __str__(self):
        return f"Room {self.room_number} — {self.hostel.name}"

    @property
    def occupied_count(self):
        return self.beds.filter(status='OCCUPIED').count()

    @property
    def available_count(self):
        return self.beds.filter(status='AVAILABLE').count()

    @property
    def is_full(self):
        return self.occupied_count >= self.capacity


# ─────────────────────────────────────────────────────────────────────────────
# BED
# ─────────────────────────────────────────────────────────────────────────────

class Bed(TenantModelMixin, models.Model):
    STATUS_CHOICES = [
        ('AVAILABLE',   'Available'),
        ('OCCUPIED',    'Occupied'),
        ('RESERVED',    'Reserved'),
        ('MAINTENANCE', 'Under Maintenance'),
    ]

    room        = models.ForeignKey(Room, on_delete=models.CASCADE,
                                    related_name='beds')
    bed_number  = models.CharField(max_length=20,
                                   help_text='e.g. B1, Top Bunk, Bottom Bunk')
    status      = models.CharField(max_length=12, choices=STATUS_CHOICES,
                                   default='AVAILABLE')
    note        = models.TextField(blank=True)

    class Meta:
        ordering = ['room', 'bed_number']
        unique_together = ('room', 'bed_number')

    def __str__(self):
        return f"Bed {self.bed_number} — {self.room}"

    @property
    def current_occupant(self):
        return self.boarder_profiles.filter(
            status='ACTIVE'
        ).select_related('student').first()


# ─────────────────────────────────────────────────────────────────────────────
# BOARDER PROFILE  (the central record linking Student → Bed)
# ─────────────────────────────────────────────────────────────────────────────

class BoarderProfile(TenantModelMixin, models.Model):
    STATUS_CHOICES = [
        ('ACTIVE',     'Currently Boarding'),
        ('CHECKED_OUT','Checked Out'),
        ('SUSPENDED',  'Suspended'),
        ('EXEAT',      'On Exeat / Leave'),
    ]
    STUDENT_TYPE = [
        ('BOARDER', 'Full Boarder'),
        ('DAY',     'Day Student'),
        ('WEEKLY',  'Weekly Boarder'),
    ]

    student         = models.OneToOneField(Student, on_delete=models.CASCADE,
                                           related_name='boarder_profile')
    student_type    = models.CharField(max_length=8, choices=STUDENT_TYPE,
                                       default='DAY')
    bed             = models.ForeignKey(Bed, on_delete=models.SET_NULL,
                                        null=True, blank=True,
                                        related_name='boarder_profiles')
    session         = models.ForeignKey(AcademicSession, on_delete=models.SET_NULL,
                                        null=True, blank=True)
    status          = models.CharField(max_length=12, choices=STATUS_CHOICES,
                                       default='ACTIVE')
    # Emergency contact (may differ from parent)
    emergency_name  = models.CharField(max_length=200, blank=True)
    emergency_phone = models.CharField(max_length=20,  blank=True)
    emergency_rel   = models.CharField(max_length=50,  blank=True,
                                       verbose_name='Relationship')
    # Medical
    medical_conditions = EncryptedTextField(blank=True,
                                          verbose_name='Medical Conditions / Allergies')
    DIETS = [('NONE', 'No special diet'), ('VEGETARIAN', 'Vegetarian'), ('HALAL', 'Halal only'),
             ('NO_PORK', 'No pork'), ('ALLERGY', 'Food allergy'), ('MEDICAL', 'Medical diet'), ('OTHER', 'Other')]
    diet            = models.CharField(max_length=10, choices=DIETS, default='NONE')
    diet_note       = models.CharField(max_length=255, blank=True, help_text='e.g. allergic to groundnuts')
    doctor_name     = models.CharField(max_length=200, blank=True)
    doctor_phone    = models.CharField(max_length=20,  blank=True)
    blood_group     = models.CharField(max_length=5,   blank=True)
    # Misc
    joined_date     = models.DateField(default=timezone.now)
    note            = models.TextField(blank=True)
    created_at      = models.DateTimeField(auto_now_add=True)
    updated_at      = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['student__full_name']

    def __str__(self):
        return f"{self.student.full_name} [{self.get_student_type_display()}]"

    @property
    def is_boarder(self):
        return self.student_type in ('BOARDER', 'WEEKLY')

    @property
    def hostel(self):
        return self.bed.room.hostel if self.bed else None

    @property
    def room(self):
        return self.bed.room if self.bed else None


# ─────────────────────────────────────────────────────────────────────────────
# TERM BILLING  (hostel fee per term — integrates with finance)
# ─────────────────────────────────────────────────────────────────────────────

class HostelTermBilling(TenantModelMixin, models.Model):
    """Per-student hostel fee record for a given term."""
    STATUS_CHOICES = [
        ('PAID',    'Paid'),
        ('PARTIAL', 'Partial'),
        ('UNPAID',  'Unpaid'),
        ('WAIVED',  'Waived'),
    ]

    boarder     = models.ForeignKey(BoarderProfile, on_delete=models.CASCADE,
                                    related_name='term_billings')
    term        = models.ForeignKey(Term, on_delete=models.CASCADE)
    session     = models.ForeignKey(AcademicSession, on_delete=models.CASCADE)
    # Fee components
    boarding_fee  = models.DecimalField(max_digits=14, decimal_places=2,
                                        default=Decimal('0.00'),
                                        help_text='Base hostel/accommodation fee')
    meal_fee      = models.DecimalField(max_digits=14, decimal_places=2,
                                        default=Decimal('0.00'))
    laundry_fee   = models.DecimalField(max_digits=14, decimal_places=2,
                                        default=Decimal('0.00'))
    other_fee     = models.DecimalField(max_digits=14, decimal_places=2,
                                        default=Decimal('0.00'))
    total_fee     = models.DecimalField(max_digits=14, decimal_places=2,
                                        default=Decimal('0.00'))
    amount_paid   = models.DecimalField(max_digits=14, decimal_places=2,
                                        default=Decimal('0.00'))
    status        = models.CharField(max_length=8, choices=STATUS_CHOICES,
                                     default='UNPAID')
    # Link to finance wallet transaction
    finance_transaction = models.ForeignKey(
        'finance.Transaction', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='hostel_billings'
    )
    waived_by   = models.ForeignKey(settings.AUTH_USER_MODEL,
                                    on_delete=models.SET_NULL,
                                    null=True, blank=True)
    note        = models.TextField(blank=True)
    created_at  = models.DateTimeField(auto_now_add=True)
    updated_at  = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-session__name', 'term__name']
        unique_together = ('boarder', 'term', 'session')

    @property
    def balance_due(self):
        return max(Decimal('0'), self.total_fee - self.amount_paid)

    def compute_total(self):
        self.total_fee = (
            self.boarding_fee + self.meal_fee +
            self.laundry_fee + self.other_fee
        )
        return self.total_fee

    def __str__(self):
        return (f"{self.boarder.student.full_name} — "
                f"{self.term} / {self.session}  ₦{self.total_fee:,.2f}")


# ─────────────────────────────────────────────────────────────────────────────
# HOSTEL FEE STRUCTURE  (admin sets standard fee amounts per hostel/term)
# ─────────────────────────────────────────────────────────────────────────────

class HostelFeeStructure(TenantModelMixin, models.Model):
    hostel        = models.ForeignKey(Hostel, on_delete=models.CASCADE,
                                      related_name='fee_structures',
                                      null=True, blank=True,
                                      help_text='Leave blank for all hostels')
    term          = models.ForeignKey(Term, on_delete=models.SET_NULL,
                                      null=True, blank=True)
    session       = models.ForeignKey(AcademicSession, on_delete=models.SET_NULL,
                                      null=True, blank=True)
    boarding_fee  = models.DecimalField(max_digits=14, decimal_places=2,
                                        default=Decimal('0.00'))
    meal_fee      = models.DecimalField(max_digits=14, decimal_places=2,
                                        default=Decimal('0.00'))
    laundry_fee   = models.DecimalField(max_digits=14, decimal_places=2,
                                        default=Decimal('0.00'))
    other_fee     = models.DecimalField(max_digits=14, decimal_places=2,
                                        default=Decimal('0.00'))
    description   = models.TextField(blank=True)
    is_active     = models.BooleanField(default=True)
    created_at    = models.DateTimeField(auto_now_add=True)

    @property
    def total(self):
        return self.boarding_fee + self.meal_fee + self.laundry_fee + self.other_fee

    def __str__(self):
        hostel_name = self.hostel.name if self.hostel else 'All Hostels'
        return f"{hostel_name} — {self.term or 'All Terms'}  ₦{self.total:,.2f}"


# ─────────────────────────────────────────────────────────────────────────────
# CHECK-IN / CHECK-OUT  (movement log)
# ─────────────────────────────────────────────────────────────────────────────

class CheckInOut(TenantModelMixin, models.Model):
    TYPE_CHOICES = [
        ('CHECK_IN',    'Check In'),
        ('CHECK_OUT',   'Check Out'),
        ('EXEAT',       'Exeat / Leave'),
        ('RETURN',      'Return from Leave'),
        ('SUSPENSION',  'Suspension'),
    ]

    boarder     = models.ForeignKey(BoarderProfile, on_delete=models.CASCADE,
                                    related_name='movements')
    movement_type = models.CharField(max_length=12, choices=TYPE_CHOICES)
    datetime    = models.DateTimeField(default=timezone.now)
    expected_return = models.DateTimeField(null=True, blank=True,
                                           help_text='For exeat / leave only')
    actual_return   = models.DateTimeField(null=True, blank=True)
    reason      = models.TextField(blank=True)
    authorized_by = models.ForeignKey(settings.AUTH_USER_MODEL,
                                      on_delete=models.SET_NULL,
                                      null=True, blank=True,
                                      related_name='hostel_authorizations')
    parent_consent = models.BooleanField(default=False,
                                         help_text='Parent confirmed this movement')
    note        = models.TextField(blank=True)
    created_at  = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-datetime']

    def __str__(self):
        return (f"{self.boarder.student.full_name} — "
                f"{self.get_movement_type_display()} "
                f"{self.datetime:%d %b %Y %-I:%M %p}")

    @property
    def is_overdue(self):
        if self.movement_type == 'EXEAT' and self.expected_return:
            return (not self.actual_return and
                    timezone.now() > self.expected_return)
        return False


# ─────────────────────────────────────────────────────────────────────────────
# MEAL PLAN
# ─────────────────────────────────────────────────────────────────────────────

class MealPlan(TenantModelMixin, models.Model):
    """Defines daily meal schedule for a hostel."""
    MEAL_TYPE = [
        ('BREAKFAST', 'Breakfast'),
        ('LUNCH',     'Lunch'),
        ('DINNER',    'Dinner'),
        ('SNACK',     'Snack'),
    ]
    DAY_CHOICES = [
        (0, 'Monday'), (1, 'Tuesday'), (2, 'Wednesday'),
        (3, 'Thursday'), (4, 'Friday'), (5, 'Saturday'), (6, 'Sunday'),
    ]

    hostel      = models.ForeignKey(Hostel, on_delete=models.CASCADE,
                                    related_name='meal_plans')
    meal_type   = models.CharField(max_length=10, choices=MEAL_TYPE)
    day_of_week = models.IntegerField(choices=DAY_CHOICES)
    menu        = models.CharField(max_length=300, help_text='What is served')
    time        = models.TimeField(help_text='Meal serving time')
    is_active   = models.BooleanField(default=True)

    class Meta:
        ordering = ['day_of_week', 'time']
        unique_together = ('hostel', 'meal_type', 'day_of_week')

    def __str__(self):
        return (f"{self.hostel.name} — "
                f"{self.get_day_of_week_display()} {self.get_meal_type_display()}: {self.menu}")


class MealAttendance(TenantModelMixin, models.Model):
    """Daily record of boarders present at each meal."""
    boarder     = models.ForeignKey(BoarderProfile, on_delete=models.CASCADE,
                                    related_name='meal_attendance')
    meal_plan   = models.ForeignKey(MealPlan, on_delete=models.CASCADE,
                                    related_name='attendance')
    date        = models.DateField(default=timezone.now)
    present     = models.BooleanField(default=True)
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL,
                                    on_delete=models.SET_NULL,
                                    null=True, blank=True)
    created_at  = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-date']
        unique_together = ('boarder', 'meal_plan', 'date')

    def __str__(self):
        status = 'Present' if self.present else 'Absent'
        return (f"{self.boarder.student.full_name} — "
                f"{self.meal_plan.get_meal_type_display()} "
                f"{self.date}  [{status}]")


# ─────────────────────────────────────────────────────────────────────────────
# VISITOR LOG
# ─────────────────────────────────────────────────────────────────────────────

class VisitorLog(TenantModelMixin, models.Model):
    STATUS_CHOICES = [
        ('PENDING',   'Pending'),
        ('APPROVED',  'Approved'),
        ('DENIED',    'Denied'),
        ('COMPLETED', 'Visit Completed'),
    ]
    RELATION_CHOICES = [
        ('PARENT',   'Parent'),
        ('GUARDIAN', 'Guardian'),
        ('SIBLING',  'Sibling'),
        ('RELATIVE', 'Other Relative'),
        ('FRIEND',   'Friend'),
        ('OTHER',    'Other'),
    ]

    boarder         = models.ForeignKey(BoarderProfile, on_delete=models.CASCADE,
                                        related_name='visitors')
    visitor_name    = models.CharField(max_length=200)
    visitor_phone   = models.CharField(max_length=20)
    relationship    = models.CharField(max_length=10, choices=RELATION_CHOICES,
                                       default='PARENT')
    purpose         = models.CharField(max_length=300)
    id_type         = models.CharField(max_length=50, blank=True,
                                       help_text='Type of ID presented')
    id_number       = models.CharField(max_length=50, blank=True)
    visit_date      = models.DateField(default=timezone.now)
    time_in         = models.TimeField(null=True, blank=True)
    time_out        = models.TimeField(null=True, blank=True)
    status          = models.CharField(max_length=10, choices=STATUS_CHOICES,
                                       default='PENDING')
    approved_by     = models.ForeignKey(settings.AUTH_USER_MODEL,
                                        on_delete=models.SET_NULL,
                                        null=True, blank=True)
    note            = models.TextField(blank=True)
    created_at      = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-visit_date', '-created_at']

    def __str__(self):
        return (f"{self.visitor_name} visiting "
                f"{self.boarder.student.full_name} — {self.visit_date}")


# ─────────────────────────────────────────────────────────────────────────────
# INCIDENT REPORT
# ─────────────────────────────────────────────────────────────────────────────

class IncidentReport(TenantModelMixin, models.Model):
    SEVERITY_CHOICES = [
        ('LOW',      'Low — Minor Issue'),
        ('MEDIUM',   'Medium — Requires Attention'),
        ('HIGH',     'High — Serious'),
        ('CRITICAL', 'Critical — Immediate Action Required'),
    ]
    STATUS_CHOICES = [
        ('OPEN',     'Open'),
        ('UNDER_REVIEW', 'Under Review'),
        ('RESOLVED', 'Resolved'),
        ('CLOSED',   'Closed'),
    ]
    TYPE_CHOICES = [
        ('DISCIPLINARY', 'Disciplinary'),
        ('MEDICAL',      'Medical'),
        ('PROPERTY',     'Property Damage'),
        ('THEFT',        'Theft'),
        ('BULLYING',     'Bullying'),
        ('SAFETY',       'Safety Concern'),
        ('OTHER',        'Other'),
    ]

    boarder         = models.ForeignKey(BoarderProfile, on_delete=models.CASCADE,
                                        related_name='incidents')
    incident_type   = models.CharField(max_length=14, choices=TYPE_CHOICES)
    severity        = models.CharField(max_length=8,  choices=SEVERITY_CHOICES,
                                       default='LOW')
    title           = models.CharField(max_length=200)
    description     = models.TextField()
    incident_date   = models.DateTimeField(default=timezone.now)
    location        = models.CharField(max_length=100, blank=True)
    reported_by     = models.ForeignKey(settings.AUTH_USER_MODEL,
                                        on_delete=models.SET_NULL,
                                        null=True, blank=True,
                                        related_name='hostel_incidents_reported')
    action_taken    = models.TextField(blank=True)
    status          = models.CharField(max_length=14, choices=STATUS_CHOICES,
                                       default='OPEN')
    resolved_by     = models.ForeignKey(settings.AUTH_USER_MODEL,
                                        on_delete=models.SET_NULL,
                                        null=True, blank=True,
                                        related_name='hostel_incidents_resolved')
    resolved_at     = models.DateTimeField(null=True, blank=True)
    created_at      = models.DateTimeField(auto_now_add=True)
    updated_at      = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-incident_date']

    def __str__(self):
        return f"[{self.severity}] {self.title} — {self.boarder.student.full_name}"


# ─────────────────────────────────────────────────────────────────────────────
# HOSTEL NOTICE BOARD
# ─────────────────────────────────────────────────────────────────────────────

class HostelNotice(TenantModelMixin, models.Model):
    PRIORITY_CHOICES = [
        ('NORMAL',  'Normal'),
        ('URGENT',  'Urgent'),
        ('CRITICAL','Critical'),
    ]

    hostel      = models.ForeignKey(Hostel, on_delete=models.CASCADE,
                                    related_name='notices',
                                    null=True, blank=True,
                                    help_text='Leave blank for all hostels')
    title       = models.CharField(max_length=200)
    content     = models.TextField()
    priority    = models.CharField(max_length=8, choices=PRIORITY_CHOICES,
                                   default='NORMAL')
    posted_by   = models.ForeignKey(settings.AUTH_USER_MODEL,
                                    on_delete=models.SET_NULL,
                                    null=True, blank=True)
    is_active   = models.BooleanField(default=True)
    expires_on  = models.DateField(null=True, blank=True)
    created_at  = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"[{self.priority}] {self.title}"

    @property
    def is_expired(self):
        return self.expires_on and timezone.now().date() > self.expires_on


# ─────────────────────────────────────────────────────────────────────────────
# BOARDING LIFE: roll calls, exeat requests, visiting days, inspections,
# sick bay, room changes and housemaster reports
# ─────────────────────────────────────────────────────────────────────────────

class RollCall(TenantModelMixin, models.Model):
    # Own reverse name: another app has a model with the same name, and both
    # can't be Tenant.rollcall_set.
    tenant = models.ForeignKey('tenants.Tenant', on_delete=models.CASCADE, db_index=True,
                               null=True, blank=True, related_name='hostel_roll_calls')
    KINDS = [('MORNING', 'Morning roll call'), ('NIGHT', 'Night roll call'), ('PREP', 'Prep (study)'),
             ('LIGHTS', 'Lights-out check'), ('OTHER', 'Other')]
    hostel = models.ForeignKey(Hostel, on_delete=models.CASCADE, related_name='roll_calls')
    kind = models.CharField(max_length=7, choices=KINDS, default='NIGHT')
    date = models.DateField(default=timezone.localdate)
    taken_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='+')
    note = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        unique_together = ('hostel', 'kind', 'date')
        ordering = ['-date', 'kind']


class RollCallMark(TenantModelMixin, models.Model):
    STATUSES = [('PRESENT', 'Present'), ('ABSENT', 'Absent'), ('LATE', 'Late'), ('EXEAT', 'On exeat'),
                ('SICK', 'In sick bay'), ('EXCUSED', 'Excused')]
    roll_call = models.ForeignKey(RollCall, on_delete=models.CASCADE, related_name='marks')
    boarder = models.ForeignKey(BoarderProfile, on_delete=models.CASCADE, related_name='roll_marks')
    status = models.CharField(max_length=7, choices=STATUSES, default='PRESENT')
    note = models.CharField(max_length=160, blank=True)

    objects = TenantManager()

    class Meta:
        unique_together = ('roll_call', 'boarder')


class ExeatRequest(TenantModelMixin, models.Model):
    PENDING_PARENT, PENDING_SCHOOL, APPROVED, DECLINED, OUT, RETURNED, CANCELLED = (
        'PARENT', 'SCHOOL', 'APPROVED', 'DECLINED', 'OUT', 'RETURNED', 'CANCELLED')
    STATUSES = [(PENDING_PARENT, 'Waiting for the parent'), (PENDING_SCHOOL, 'Waiting for the housemaster'),
                (APPROVED, 'Approved'), (DECLINED, 'Declined'), (OUT, 'Signed out'), (RETURNED, 'Back in school'),
                (CANCELLED, 'Cancelled')]
    boarder = models.ForeignKey(BoarderProfile, on_delete=models.CASCADE, related_name='exeat_requests')
    leave_at = models.DateTimeField()
    return_by = models.DateTimeField()
    reason = models.CharField(max_length=255)
    pickup_person = models.CharField(max_length=160, help_text='Who collects the child')
    pickup_phone = models.CharField(max_length=20, blank=True)
    status = models.CharField(max_length=9, choices=STATUSES, default=PENDING_SCHOOL)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                     related_name='+')
    parent_approved_at = models.DateTimeField(null=True, blank=True)
    parent_approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                           blank=True, related_name='+')
    school_decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                          blank=True, related_name='+')
    school_note = models.CharField(max_length=255, blank=True)
    movement = models.ForeignKey(CheckInOut, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        ordering = ['-leave_at']


class VisitingDay(TenantModelMixin, models.Model):
    date = models.DateField()
    start = models.TimeField()
    end = models.TimeField()
    slot_minutes = models.PositiveSmallIntegerField(default=60)
    families_per_slot = models.PositiveSmallIntegerField(default=20)
    notes = models.TextField(blank=True, help_text='Rules, what to bring, where to park')
    open = models.BooleanField(default=True)

    objects = TenantManager()

    class Meta:
        ordering = ['date']


class VisitBooking(TenantModelMixin, models.Model):
    day = models.ForeignKey(VisitingDay, on_delete=models.CASCADE, related_name='bookings')
    boarder = models.ForeignKey(BoarderProfile, on_delete=models.CASCADE, related_name='visit_bookings')
    parent = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='+')
    slot = models.TimeField()
    visitors = models.PositiveSmallIntegerField(default=1)
    names = models.CharField(max_length=255, help_text='Names of the visitors')
    arrived_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        unique_together = ('day', 'boarder')
        ordering = ['slot']


class Inspection(TenantModelMixin, models.Model):
    room = models.ForeignKey(Room, on_delete=models.CASCADE, related_name='inspections')
    date = models.DateField(default=timezone.localdate)
    inspector = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='+')
    scores = models.JSONField(default=dict, help_text='{"Cleanliness": 4, ...} each out of 5')
    total = models.DecimalField(max_digits=5, decimal_places=1, default=0, help_text='Percent')
    note = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        ordering = ['-date', '-id']


class SickBayVisit(TenantModelMixin, models.Model):
    OUTCOMES = [('', 'Still in sick bay'), ('DORM', 'Back to the dormitory/class'), ('HOME', 'Sent home'),
                ('HOSPITAL', 'Referred to hospital')]
    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name='sick_bay_visits')
    admitted_at = models.DateTimeField(default=timezone.now)
    complaint = models.CharField(max_length=255)
    temperature = models.DecimalField(max_digits=4, decimal_places=1, null=True, blank=True)
    treatment = models.TextField(blank=True)
    discharged_at = models.DateTimeField(null=True, blank=True)
    outcome = models.CharField(max_length=8, choices=OUTCOMES, blank=True)
    parent_told = models.BooleanField(default=False)
    nurse = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='+')

    objects = TenantManager()

    class Meta:
        ordering = ['-admitted_at']


class RoomChangeRequest(TenantModelMixin, models.Model):
    PENDING, APPROVED, DECLINED = 'PENDING', 'APPROVED', 'DECLINED'
    boarder = models.ForeignKey(BoarderProfile, on_delete=models.CASCADE, related_name='room_requests')
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                     related_name='+')
    reason = models.TextField()
    preferred_room = models.ForeignKey(Room, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    status = models.CharField(max_length=8, choices=[(PENDING, 'Waiting'), (APPROVED, 'Approved'),
                                                     (DECLINED, 'Declined')], default=PENDING)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='+')
    note = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        ordering = ['-created_at']


class BoardingReport(TenantModelMixin, models.Model):
    RATINGS = [(5, 'Excellent'), (4, 'Very good'), (3, 'Good'), (2, 'Fair'), (1, 'Needs improvement')]
    boarder = models.ForeignKey(BoarderProfile, on_delete=models.CASCADE, related_name='reports')
    term = models.ForeignKey(Term, on_delete=models.CASCADE, related_name='+')
    conduct = models.PositiveSmallIntegerField(choices=RATINGS, default=3)
    neatness = models.PositiveSmallIntegerField(choices=RATINGS, default=3)
    relationships = models.PositiveSmallIntegerField(choices=RATINGS, default=3)
    comment = models.TextField(blank=True)
    written_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='+')
    published = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantManager()

    class Meta:
        unique_together = ('boarder', 'term')
