"""
hr/models.py
────────────
Staff records for the directory, onboarding checklists, contracts,
appraisals, training, disciplinary cases, exits, and recruitment
(vacancies and applications).
"""
from decimal import Decimal

from django.conf import settings
from django.db import models

from tenants.managers import TenantManager, TenantModelMixin


def _private():
    from users.models import PrivateStorage
    return PrivateStorage()


def _upload(instance, filename):
    import os
    import uuid
    ext = os.path.splitext(filename)[1].lower()[:8]
    return f"hr/{instance.tenant_id}/{instance.__class__.__name__.lower()}/{uuid.uuid4().hex}{ext}"


class StaffRecord(TenantModelMixin, models.Model):
    staff = models.OneToOneField('users.Staff', on_delete=models.CASCADE, related_name='record')
    photo = models.FileField(upload_to=_upload, storage=_private, blank=True, null=True)
    job_title = models.CharField(max_length=100, blank=True)
    department = models.ForeignKey('curriculum.Department', on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='+')
    phone = models.CharField(max_length=20, blank=True)
    extension = models.CharField(max_length=10, blank=True)
    date_joined = models.DateField(null=True, blank=True)
    date_of_birth = models.DateField(null=True, blank=True, help_text='Used for birthday greetings')
    emergency_name = models.CharField(max_length=120, blank=True)
    emergency_phone = models.CharField(max_length=20, blank=True)
    address = models.CharField(max_length=255, blank=True)
    left_on = models.DateField(null=True, blank=True)

    objects = TenantManager()


class OnboardingTask(TenantModelMixin, models.Model):
    """One line of the school's new-staff checklist."""
    title = models.CharField(max_length=120)
    order = models.PositiveSmallIntegerField(default=0)
    needs_file = models.BooleanField(default=False, help_text='A document must be uploaded to tick it off')
    active = models.BooleanField(default=True)

    objects = TenantManager()

    class Meta:
        ordering = ['order', 'id']


class OnboardingStep(TenantModelMixin, models.Model):
    staff = models.ForeignKey('users.Staff', on_delete=models.CASCADE, related_name='onboarding')
    task = models.ForeignKey(OnboardingTask, on_delete=models.CASCADE, related_name='steps')
    done = models.BooleanField(default=False)
    done_at = models.DateTimeField(null=True, blank=True)
    done_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                related_name='+')
    file = models.FileField(upload_to=_upload, storage=_private, blank=True, null=True)
    note = models.CharField(max_length=200, blank=True)

    objects = TenantManager()

    class Meta:
        unique_together = ('staff', 'task')


class Contract(TenantModelMixin, models.Model):
    KINDS = [('PERMANENT', 'Permanent'), ('FIXED', 'Fixed term'), ('PROBATION', 'Probation'),
             ('PART_TIME', 'Part-time'), ('CONSULTANT', 'Consultant / NYSC')]
    staff = models.ForeignKey('users.Staff', on_delete=models.CASCADE, related_name='contracts')
    kind = models.CharField(max_length=10, choices=KINDS, default='PERMANENT')
    start = models.DateField()
    end = models.DateField(null=True, blank=True, help_text='Leave empty for a permanent contract')
    file = models.FileField(upload_to=_upload, storage=_private, blank=True, null=True)
    note = models.CharField(max_length=200, blank=True)
    reminded_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        ordering = ['-start']


class AppraisalCycle(TenantModelMixin, models.Model):
    title = models.CharField(max_length=120)
    opens = models.DateField()
    closes = models.DateField()
    criteria = models.JSONField(default=list, help_text='[{"name": "...", "weight": 20}]')
    open = models.BooleanField(default=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        ordering = ['-opens']


class Appraisal(TenantModelMixin, models.Model):
    SELF, REVIEW, DONE = 'SELF', 'REVIEW', 'DONE'
    STATUSES = [(SELF, 'Self-review'), (REVIEW, 'With the reviewer'), (DONE, 'Complete')]
    cycle = models.ForeignKey(AppraisalCycle, on_delete=models.CASCADE, related_name='appraisals')
    staff = models.ForeignKey('users.Staff', on_delete=models.CASCADE, related_name='appraisals')
    reviewer = models.ForeignKey('users.Staff', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    self_scores = models.JSONField(default=dict)
    self_comment = models.TextField(blank=True)
    scores = models.JSONField(default=dict)
    reviewer_comment = models.TextField(blank=True)
    overall = models.DecimalField(max_digits=5, decimal_places=1, null=True, blank=True)
    status = models.CharField(max_length=6, choices=STATUSES, default=SELF)
    submitted_at = models.DateTimeField(null=True, blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)

    objects = TenantManager()

    class Meta:
        unique_together = ('cycle', 'staff')
        ordering = ['staff__full_name']


class TrainingRecord(TenantModelMixin, models.Model):
    staff = models.ForeignKey('users.Staff', on_delete=models.CASCADE, related_name='training')
    title = models.CharField(max_length=160)
    provider = models.CharField(max_length=120, blank=True)
    date = models.DateField()
    hours = models.DecimalField(max_digits=6, decimal_places=1, default=Decimal('0'))
    cost = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    certificate = models.FileField(upload_to=_upload, storage=_private, blank=True, null=True)
    added_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        ordering = ['-date']


class DisciplinaryCase(TenantModelMixin, models.Model):
    KINDS = [('QUERY', 'Query'), ('WARNING', 'Warning letter'), ('SUSPENSION', 'Suspension'), ('OTHER', 'Other')]
    OUTCOMES = [('NO_ACTION', 'No further action'), ('WARNING', 'Warning'), ('FINAL', 'Final warning'),
                ('SUSPENSION', 'Suspension'), ('DISMISSAL', 'Dismissal')]
    staff = models.ForeignKey('users.Staff', on_delete=models.CASCADE, related_name='disciplinary')
    kind = models.CharField(max_length=10, choices=KINDS, default='QUERY')
    date = models.DateField()
    allegation = models.TextField()
    respond_by = models.DateField(null=True, blank=True)
    response = models.TextField(blank=True)
    responded_at = models.DateTimeField(null=True, blank=True)
    outcome = models.CharField(max_length=10, choices=OUTCOMES, blank=True)
    outcome_note = models.TextField(blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    issued_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        ordering = ['-date', '-id']


class ExitCase(TenantModelMixin, models.Model):
    KINDS = [('RESIGNATION', 'Resignation'), ('RETIREMENT', 'Retirement'), ('END', 'End of contract'),
             ('DISMISSAL', 'Dismissal'), ('OTHER', 'Other')]
    staff = models.ForeignKey('users.Staff', on_delete=models.CASCADE, related_name='exits')
    kind = models.CharField(max_length=11, choices=KINDS, default='RESIGNATION')
    notice_date = models.DateField()
    last_day = models.DateField()
    reason = models.CharField(max_length=255, blank=True)
    handover = models.JSONField(default=list, help_text='[{"item": "...", "done": false}]')
    other_amounts = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0'),
                                        help_text='Anything else owed to them (or minus, owed by them)')
    settlement = models.JSONField(default=dict)
    completed_at = models.DateTimeField(null=True, blank=True)
    completed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        ordering = ['-last_day']


class Vacancy(TenantModelMixin, models.Model):
    title = models.CharField(max_length=160)
    department = models.CharField(max_length=80, blank=True)
    description = models.TextField()
    requirements = models.TextField(blank=True)
    closing_date = models.DateField()
    published = models.BooleanField(default=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        ordering = ['-created_at']


class JobApplication(TenantModelMixin, models.Model):
    STATUSES = [('NEW', 'New'), ('SHORTLISTED', 'Shortlisted'), ('INTERVIEW', 'Interview booked'),
                ('OFFERED', 'Offered'), ('HIRED', 'Hired'), ('REJECTED', 'Not successful')]
    vacancy = models.ForeignKey(Vacancy, on_delete=models.CASCADE, related_name='applications')
    full_name = models.CharField(max_length=160)
    email = models.EmailField()
    phone = models.CharField(max_length=20)
    cv = models.FileField(upload_to=_upload, storage=_private)
    cover_letter = models.TextField(blank=True)
    status = models.CharField(max_length=11, choices=STATUSES, default='NEW')
    interview_at = models.DateTimeField(null=True, blank=True)
    interview_venue = models.CharField(max_length=160, blank=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        ordering = ['-created_at']


class StaffReport(TenantModelMixin, models.Model):
    """A staff member's daily or weekly work report. Drafts are private; once
    submitted an administrator reads it, can leave feedback, and marks it reviewed
    (after which it can no longer be edited)."""
    DAILY, WEEKLY = 'DAILY', 'WEEKLY'
    PERIODS = [(DAILY, 'Daily'), (WEEKLY, 'Weekly')]
    DRAFT, SUBMITTED, REVIEWED = 'DRAFT', 'SUBMITTED', 'REVIEWED'
    STATUSES = [(DRAFT, 'Draft'), (SUBMITTED, 'Submitted'), (REVIEWED, 'Reviewed')]

    staff = models.ForeignKey('users.Staff', on_delete=models.CASCADE, related_name='work_reports')
    period = models.CharField(max_length=6, choices=PERIODS)
    period_start = models.DateField(help_text='The day, or the Monday of the week.')
    period_end = models.DateField()
    work_done = models.TextField('What I did')
    highlights = models.TextField('Highlights and achievements', blank=True)
    challenges = models.TextField(blank=True)
    support_needed = models.TextField('Support I need', blank=True)
    next_plan = models.TextField('Plan for the next period', blank=True)
    attachment = models.FileField(upload_to=_upload, storage=_private, blank=True, null=True)
    status = models.CharField(max_length=9, choices=STATUSES, default=DRAFT)
    submitted_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name='+')
    reviewed_at = models.DateTimeField(null=True, blank=True)
    feedback = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantManager()

    class Meta:
        ordering = ['-period_start', 'period', 'staff__full_name']
        constraints = [models.UniqueConstraint(fields=['staff', 'period', 'period_start'],
                                               name='one_report_per_staff_per_period')]

    def __str__(self):
        return f'{self.get_period_display()} report — {self.staff.full_name}, {self.period_start:%d %b %Y}'

    @property
    def due(self):
        """The last day it counts as on time: the next day for a daily report, the
        Monday after for a weekly one."""
        from datetime import timedelta
        return self.period_end + timedelta(days=1 if self.period == self.DAILY else 3)

    @property
    def late(self):
        from django.utils import timezone
        return bool(self.submitted_at) and timezone.localtime(self.submitted_at).date() > self.due

    @property
    def editable(self):
        return self.status != self.REVIEWED
