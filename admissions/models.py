"""
admissions/models.py
────────────────────
The admissions funnel: a parent applies on the school's public page, staff move
the application through screening, exam, interview and an offer, and an accepted
applicant becomes a Student in one click.

Everything here is per school (TenantModelMixin + TenantManager).
"""
from django.db import models

from tenants.managers import TenantManager, TenantModelMixin
from security.media import signed_storage


class AdmissionSettings(TenantModelMixin, models.Model):
    """One row per school: how its admissions behave."""

    applications_open = models.BooleanField(
        default=False, help_text="Show the public application form for this school.")
    session = models.ForeignKey(
        'academics.AcademicSession', on_delete=models.SET_NULL, null=True, blank=True,
        help_text="Session applicants are applying for.")
    application_fee = models.DecimalField(
        max_digits=10, decimal_places=2, default=0,
        help_text="Charged before an application is submitted. 0 means free.")

    # Admission numbers, e.g. TIT/2026/0042
    number_format = models.CharField(
        max_length=60, default='{prefix}/{year}/{seq:04d}',
        help_text="Tokens: {prefix} {year} {short_year} {class} {seq}. "
                  "Example: {prefix}/{year}/{seq:04d} → TIT/2026/0042")
    number_prefix = models.CharField(max_length=20, blank=True,
                                     help_text="Leave blank to use the school's initials.")
    next_number = models.PositiveIntegerField(default=1)

    require_documents = models.BooleanField(
        default=False, help_text="Applicants must attach at least one document.")
    instructions = models.TextField(
        blank=True, help_text="Shown at the top of the public application form.")

    objects = TenantManager()

    class Meta:
        verbose_name = 'Admission settings'
        verbose_name_plural = 'Admission settings'

    def __str__(self):
        return f"Admission settings — {self.tenant}"


class AdmissionQuota(TenantModelMixin, models.Model):
    """Seats available in a class, so a school can stop over-offering."""

    school_class = models.ForeignKey('users.Class', on_delete=models.CASCADE,
                                     related_name='admission_quotas')
    seats = models.PositiveIntegerField(default=0, help_text="0 means no limit.")

    objects = TenantManager()

    class Meta:
        unique_together = ('tenant', 'school_class')

    def __str__(self):
        return f"{self.school_class} — {self.seats} seats"


class Application(TenantModelMixin, models.Model):
    """One applicant, moving through the admissions pipeline."""

    NEW = 'NEW'
    SCREENING = 'SCREENING'
    EXAM = 'EXAM'
    INTERVIEW = 'INTERVIEW'
    OFFERED = 'OFFERED'
    ACCEPTED = 'ACCEPTED'
    ENROLLED = 'ENROLLED'
    WAITLIST = 'WAITLIST'
    REJECTED = 'REJECTED'
    WITHDRAWN = 'WITHDRAWN'

    STATUS_CHOICES = [
        (NEW, 'Applied'),
        (SCREENING, 'Screening'),
        (EXAM, 'Entrance exam'),
        (INTERVIEW, 'Interview'),
        (OFFERED, 'Offer sent'),
        (ACCEPTED, 'Offer accepted'),
        (ENROLLED, 'Enrolled'),
        (WAITLIST, 'Waitlisted'),
        (REJECTED, 'Not offered'),
        (WITHDRAWN, 'Withdrawn'),
    ]
    #: Stages shown as columns on the pipeline board, in order.
    PIPELINE = [NEW, SCREENING, EXAM, INTERVIEW, OFFERED, ACCEPTED, ENROLLED]
    #: Statuses that no longer need action.
    CLOSED = [ENROLLED, REJECTED, WITHDRAWN]

    GENDER_CHOICES = [('Male', 'Male'), ('Female', 'Female')]
    SOURCE_CHOICES = [
        ('WORD_OF_MOUTH', 'Friend or family'), ('SOCIAL', 'Social media'),
        ('WEBSITE', 'School website'), ('RADIO', 'Radio or TV'),
        ('BANNER', 'Banner or flyer'), ('EVENT', 'School event'),
        ('OTHER', 'Somewhere else'),
    ]

    reference = models.CharField(max_length=20, db_index=True,
                                 help_text="Quoted by the parent when checking status.")

    # The child
    full_name = models.CharField(max_length=255)
    gender = models.CharField(max_length=10, choices=GENDER_CHOICES, blank=True)
    date_of_birth = models.DateField(null=True, blank=True)
    passport = models.ImageField(upload_to='admissions/passports/', storage=signed_storage, blank=True, null=True)
    applying_for = models.ForeignKey('users.Class', on_delete=models.SET_NULL, null=True,
                                     blank=True, related_name='applications')
    session = models.ForeignKey('academics.AcademicSession', on_delete=models.SET_NULL,
                                null=True, blank=True, related_name='applications')
    previous_school = models.CharField(max_length=255, blank=True)

    # The parent or guardian
    parent_name = models.CharField(max_length=255)
    parent_phone = models.CharField(max_length=20)
    parent_email = models.EmailField(blank=True)
    relationship = models.CharField(max_length=40, blank=True,
                                    help_text="Mother, father, guardian…")
    address = models.TextField(blank=True)

    # Where the family heard about the school
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES, blank=True)
    source_detail = models.CharField(max_length=120, blank=True)

    # Entrance exam / interview scheduling
    exam_date = models.DateTimeField(null=True, blank=True,
                                     help_text="When the applicant should come in.")
    exam_venue = models.CharField(max_length=160, blank=True)
    exam_notified_at = models.DateTimeField(null=True, blank=True)

    # Progress
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=NEW, db_index=True)
    exam_score = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    interview_score = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    decision_note = models.TextField(blank=True)
    offer_expires_on = models.DateField(null=True, blank=True)

    # Application fee
    fee_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    fee_paid = models.BooleanField(default=False)
    fee_reference = models.CharField(max_length=80, blank=True)
    fee_paid_at = models.DateTimeField(null=True, blank=True)

    # Offer letter
    offer_sent_at = models.DateTimeField(null=True, blank=True)

    # Links discovered or created along the way
    sibling_of = models.ForeignKey('users.Student', on_delete=models.SET_NULL, null=True,
                                   blank=True, related_name='sibling_applications',
                                   help_text="Matched by guardian phone or email.")
    student = models.OneToOneField('users.Student', on_delete=models.SET_NULL, null=True,
                                   blank=True, related_name='application',
                                   help_text="Created when the applicant is enrolled.")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantManager()

    class Meta:
        ordering = ['-created_at']
        unique_together = ('tenant', 'reference')
        indexes = [models.Index(fields=['tenant', 'status'])]

    def __str__(self):
        return f"{self.reference} — {self.full_name}"

    @property
    def is_closed(self):
        return self.status in self.CLOSED

    @property
    def total_score(self):
        scores = [s for s in (self.exam_score, self.interview_score) if s is not None]
        return sum(scores) if scores else None

    @property
    def age(self):
        if not self.date_of_birth:
            return None
        from datetime import date
        today = date.today()
        return today.year - self.date_of_birth.year - (
            (today.month, today.day) < (self.date_of_birth.month, self.date_of_birth.day))


class ApplicationDocument(TenantModelMixin, models.Model):
    """Birth certificate, last report card, passport photo…"""

    KIND_CHOICES = [
        ('BIRTH_CERT', 'Birth certificate'),
        ('LAST_RESULT', 'Last school result'),
        ('TRANSFER', 'Transfer certificate'),
        ('PASSPORT', 'Passport photograph'),
        ('OTHER', 'Other document'),
    ]

    application = models.ForeignKey(Application, on_delete=models.CASCADE,
                                    related_name='documents')
    kind = models.CharField(max_length=20, choices=KIND_CHOICES, default='OTHER')
    file = models.FileField(upload_to='admissions/documents/', storage=signed_storage)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    def __str__(self):
        return f"{self.get_kind_display()} — {self.application.reference}"


class ApplicationEvent(TenantModelMixin, models.Model):
    """An audit trail: every status change and note, with who did it."""

    application = models.ForeignKey(Application, on_delete=models.CASCADE,
                                    related_name='events')
    from_status = models.CharField(max_length=12, blank=True)
    to_status = models.CharField(max_length=12, blank=True)
    note = models.TextField(blank=True)
    actor = models.ForeignKey('users.User', on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.application.reference}: {self.from_status or '—'} → {self.to_status or 'note'}"
