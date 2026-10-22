"""
security/models.py
──────────────────
Security, privacy and compliance.

Sign-in protection: two-factor sign-in (F442), failed sign-ins and lockout
(F446), known devices for new-device alerts (F445), signed-in sessions that
can be revoked (F456) and password history for the password policy (F447).

Access: custom roles that set the existing staff permission switches (F443)
and one audit trail for sensitive actions across modules (F444).

Privacy (Nigeria Data Protection Act 2023): purposes with their lawful basis
and parental consent (F448), the school's privacy notice and Data Protection
Officer (F449), retention rules (F450), data subject requests (F451) and
personal data breaches with the 72-hour NDPC deadline (F452).

Platform: vulnerability reports from the public and penetration tests (F460).
"""
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone

from tenants.managers import TenantManager, TenantModelMixin

USER = settings.AUTH_USER_MODEL


def _private():
    from users.models import PrivateStorage
    return PrivateStorage()


def _upload(instance, filename):
    import os
    import uuid
    ext = os.path.splitext(filename)[1].lower()[:8]
    return f"security/{instance.__class__.__name__.lower()}/{uuid.uuid4().hex}{ext}"


# ─────────────────────────────────────────────────────────────────────────────
# School settings
# ─────────────────────────────────────────────────────────────────────────────

class SecuritySettings(TenantModelMixin, models.Model):
    TWO_FACTOR = [('NONE', 'Optional for everyone'), ('ADMINS', 'Required for administrators'),
                  ('STAFF', 'Required for all staff')]
    require_2fa = models.CharField(max_length=6, choices=TWO_FACTOR, default='ADMINS')
    min_length = models.PositiveSmallIntegerField(default=8, help_text='Staff passwords: fewest characters')
    require_mix = models.BooleanField(default=False, help_text='Letters and numbers')
    require_symbol = models.BooleanField(default=False)
    expiry_days = models.PositiveSmallIntegerField(default=0, help_text='0 = passwords never expire')
    history = models.PositiveSmallIntegerField(default=0, help_text='Cannot reuse the last N passwords')
    lockout_attempts = models.PositiveSmallIntegerField(default=5)
    lockout_minutes = models.PositiveSmallIntegerField(default=15)
    alert_new_device = models.BooleanField(default=True)
    idle_minutes = models.PositiveSmallIntegerField(default=0, help_text='Sign staff out after this many idle '
                                                                       'minutes (0 = off)')
    notify_admins_lockout = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantManager()


# ─────────────────────────────────────────────────────────────────────────────
# Sign-in protection
# ─────────────────────────────────────────────────────────────────────────────

class TwoFactor(models.Model):
    METHODS = [('TOTP', 'Authenticator app'), ('EMAIL', 'Code by email'), ('SMS', 'Code by SMS')]
    user = models.OneToOneField(USER, on_delete=models.CASCADE, related_name='two_factor')
    method = models.CharField(max_length=5, choices=METHODS, default='TOTP')
    secret = models.CharField(max_length=64, blank=True)
    phone = models.CharField(max_length=20, blank=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    last_step = models.BigIntegerField(default=0, help_text='Stops an authenticator code being used twice')
    backup_codes = models.JSONField(default=list, blank=True, help_text='Hashed one-time codes')
    last_used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    @property
    def active(self):
        return self.confirmed_at is not None


class LoginFailure(models.Model):
    host = models.CharField(max_length=120)
    identity = models.CharField(max_length=150)
    ip = models.GenericIPAddressField(null=True, blank=True)
    path = models.CharField(max_length=60, blank=True)
    at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-at']


class KnownDevice(models.Model):
    user = models.ForeignKey(USER, on_delete=models.CASCADE, related_name='known_devices')
    token_hash = models.CharField(max_length=64)
    label = models.CharField(max_length=120, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    first_seen = models.DateTimeField(auto_now_add=True)
    last_seen = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-last_seen']
        unique_together = ('user', 'token_hash')


class UserSession(models.Model):
    user = models.ForeignKey(USER, on_delete=models.CASCADE, related_name='security_sessions')
    tenant = models.ForeignKey('tenants.Tenant', on_delete=models.CASCADE, null=True, blank=True,
                               related_name='security_sessions')
    session_key = models.CharField(max_length=64, unique=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_seen = models.DateTimeField(default=timezone.now)
    ended_at = models.DateTimeField(null=True, blank=True)
    ended_reason = models.CharField(max_length=40, blank=True)

    class Meta:
        ordering = ['-last_seen']


class PasswordHistory(models.Model):
    user = models.ForeignKey(USER, on_delete=models.CASCADE, related_name='password_history')
    password = models.CharField(max_length=128)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']


# ─────────────────────────────────────────────────────────────────────────────
# Access: roles and the audit trail
# ─────────────────────────────────────────────────────────────────────────────

class Role(TenantModelMixin, models.Model):
    name = models.CharField(max_length=80)
    description = models.CharField(max_length=255, blank=True)
    base_role = models.CharField(max_length=20, blank=True, help_text='Staff role to set (Teacher, Account Officer…)')
    permissions = models.JSONField(default=list, blank=True)
    created_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantManager()

    class Meta:
        ordering = ['name']
        unique_together = ('tenant', 'name')

    def __str__(self):
        return self.name


class RoleAssignment(TenantModelMixin, models.Model):
    staff = models.OneToOneField('users.Staff', on_delete=models.CASCADE, related_name='custom_role')
    role = models.ForeignKey(Role, on_delete=models.CASCADE, related_name='assignments')
    assigned_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    assigned_at = models.DateTimeField(auto_now=True)

    objects = TenantManager()


class AuditEvent(models.Model):
    CATEGORIES = [('AUTH', 'Sign-in'), ('ACCESS', 'Access & permissions'), ('DATA', 'Records'),
                  ('RESULTS', 'Results & exams'), ('MONEY', 'Money'), ('PRIVACY', 'Privacy'), ('ADMIN', 'Settings')]
    tenant = models.ForeignKey('tenants.Tenant', on_delete=models.CASCADE, null=True, blank=True,
                               related_name='audit_events')
    user = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name='audit_events')
    username = models.CharField(max_length=150, blank=True)
    category = models.CharField(max_length=7, choices=CATEGORIES, default='DATA')
    action = models.CharField(max_length=30)
    object_type = models.CharField(max_length=60, blank=True)
    object_id = models.CharField(max_length=64, blank=True)
    summary = models.CharField(max_length=500)
    changes = models.JSONField(default=dict, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=200, blank=True)
    at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-at', '-id']


# ─────────────────────────────────────────────────────────────────────────────
# Privacy (NDPA)
# ─────────────────────────────────────────────────────────────────────────────

class PrivacyNotice(TenantModelMixin, models.Model):
    controller = models.CharField(max_length=200, blank=True, help_text='Legal name of the school')
    dpo_name = models.CharField('Data Protection Officer', max_length=150, blank=True)
    dpo_email = models.EmailField(blank=True)
    dpo_phone = models.CharField(max_length=20, blank=True)
    dpo_address = models.CharField(max_length=255, blank=True)
    dpco = models.CharField('Data Protection Compliance Organisation', max_length=150, blank=True,
                            help_text='The NDPC-licensed DPCO that audits the school, if any')
    notice = models.TextField(blank=True, help_text='## headings, - bullets, blank line between paragraphs')
    published = models.BooleanField(default=True)
    updated_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantManager()


class DataPurpose(TenantModelMixin, models.Model):
    BASES = [('CONSENT', 'Consent'), ('CONTRACT', 'Contract (the school place)'),
             ('LEGAL', 'Legal obligation'), ('VITAL', 'Vital interests'), ('PUBLIC', 'Public interest'),
             ('LEGITIMATE', 'Legitimate interests')]
    name = models.CharField(max_length=120)
    description = models.CharField(max_length=500, blank=True)
    lawful_basis = models.CharField(max_length=10, choices=BASES, default='CONSENT')
    data_used = models.CharField(max_length=255, blank=True, help_text='e.g. Photo, name, class')
    active = models.BooleanField(default=True)
    sort_order = models.PositiveSmallIntegerField(default=0)

    objects = TenantManager()

    class Meta:
        ordering = ['sort_order', 'name']

    def __str__(self):
        return self.name

    @property
    def needs_consent(self):
        return self.lawful_basis == 'CONSENT'


class Consent(TenantModelMixin, models.Model):
    METHODS = [('PORTAL', 'Parent portal'), ('PAPER', 'Signed form'), ('EMAIL', 'Email'), ('VERBAL', 'In person')]
    student = models.ForeignKey('users.Student', on_delete=models.CASCADE, related_name='consents')
    purpose = models.ForeignKey(DataPurpose, on_delete=models.CASCADE, related_name='consents')
    granted = models.BooleanField(default=False)
    given_by = models.CharField(max_length=150, blank=True)
    relationship = models.CharField(max_length=60, blank=True)
    method = models.CharField(max_length=6, choices=METHODS, default='PORTAL')
    recorded_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    updated_at = models.DateTimeField(auto_now=True)
    history = models.JSONField(default=list, blank=True)

    objects = TenantManager()

    class Meta:
        unique_together = ('student', 'purpose')


class DataRequest(TenantModelMixin, models.Model):
    KINDS = [('ACCESS', 'See a copy of the data'), ('CORRECTION', 'Correct the data'),
             ('DELETION', 'Delete the data'), ('RESTRICTION', 'Limit how it is used'),
             ('OBJECTION', 'Object to a use'), ('PORTABILITY', 'Move the data elsewhere')]
    STATUSES = [('NEW', 'New'), ('VERIFYING', 'Checking identity'), ('IN_PROGRESS', 'In progress'),
                ('DONE', 'Completed'), ('REFUSED', 'Refused')]
    reference = models.CharField(max_length=20, unique=True)
    kind = models.CharField(max_length=11, choices=KINDS, default='ACCESS')
    requester_name = models.CharField(max_length=150)
    requester_email = models.EmailField(blank=True)
    requester_phone = models.CharField(max_length=20, blank=True)
    relationship = models.CharField(max_length=60, blank=True, help_text='Parent, guardian, former student, staff…')
    student = models.ForeignKey('users.Student', on_delete=models.SET_NULL, null=True, blank=True,
                                related_name='data_requests')
    details = models.TextField()
    status = models.CharField(max_length=11, choices=STATUSES, default='NEW')
    identity_checked = models.BooleanField(default=False)
    due_on = models.DateField()
    response = models.TextField(blank=True)
    export = models.FileField(upload_to=_upload, storage=_private, blank=True)
    handled_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    submitted_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    notes = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    objects = TenantManager()

    class Meta:
        ordering = ['-created_at']

    @property
    def open(self):
        return self.status not in ('DONE', 'REFUSED')

    @property
    def overdue(self):
        return self.open and self.due_on < timezone.localdate()

    @property
    def days_left(self):
        return (self.due_on - timezone.localdate()).days


class Breach(TenantModelMixin, models.Model):
    SEVERITY = [('LOW', 'Low — unlikely to harm anyone'), ('MEDIUM', 'Medium'), ('HIGH', 'High — likely to harm people')]
    STATUSES = [('OPEN', 'Open'), ('CONTAINED', 'Contained'), ('REPORTED', 'Reported to NDPC'), ('CLOSED', 'Closed')]
    reference = models.CharField(max_length=20, unique=True)
    title = models.CharField(max_length=200)
    discovered_at = models.DateTimeField()
    occurred_at = models.DateTimeField(null=True, blank=True)
    description = models.TextField()
    data_types = models.CharField(max_length=255, blank=True, help_text='Names, results, medical notes…')
    people_affected = models.PositiveIntegerField(default=0)
    children_involved = models.BooleanField(default=True)
    severity = models.CharField(max_length=6, choices=SEVERITY, default='MEDIUM')
    containment = models.TextField(blank=True, help_text='What was done to stop it and limit harm')
    status = models.CharField(max_length=9, choices=STATUSES, default='OPEN')
    contained_at = models.DateTimeField(null=True, blank=True)
    reported_at = models.DateTimeField(null=True, blank=True)
    ndpc_reference = models.CharField(max_length=80, blank=True)
    people_notified_at = models.DateTimeField(null=True, blank=True)
    not_reported_reason = models.TextField(blank=True, help_text='Why it did not need reporting, if so')
    created_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    objects = TenantManager()

    class Meta:
        ordering = ['-discovered_at']

    @property
    def deadline(self):
        return self.discovered_at + timedelta(hours=72)

    @property
    def hours_left(self):
        return round((self.deadline - timezone.now()).total_seconds() / 3600, 1)


class BreachAction(TenantModelMixin, models.Model):
    breach = models.ForeignKey(Breach, on_delete=models.CASCADE, related_name='actions')
    text = models.TextField()
    by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        ordering = ['at']


class RetentionRule(TenantModelMixin, models.Model):
    kind = models.CharField(max_length=30)
    enabled = models.BooleanField(default=False)
    days = models.PositiveIntegerField(default=365)
    last_run = models.DateTimeField(null=True, blank=True)
    last_count = models.PositiveIntegerField(default=0)

    objects = TenantManager()

    class Meta:
        unique_together = ('tenant', 'kind')


# ─────────────────────────────────────────────────────────────────────────────
# Platform: disclosure and penetration tests
# ─────────────────────────────────────────────────────────────────────────────

class VulnerabilityReport(models.Model):
    STATUSES = [('NEW', 'New'), ('TRIAGED', 'Confirmed'), ('FIXED', 'Fixed'), ('REJECTED', 'Not a problem'),
                ('DUPLICATE', 'Duplicate')]
    SEVERITY = [('LOW', 'Low'), ('MEDIUM', 'Medium'), ('HIGH', 'High'), ('CRITICAL', 'Critical')]
    reference = models.CharField(max_length=20, unique=True)
    reporter_name = models.CharField(max_length=150)
    email = models.EmailField()
    title = models.CharField(max_length=200)
    url = models.CharField(max_length=300, blank=True)
    details = models.TextField()
    severity = models.CharField(max_length=8, choices=SEVERITY, default='MEDIUM')
    status = models.CharField(max_length=9, choices=STATUSES, default='NEW')
    reward = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']


class PenTest(models.Model):
    STATUSES = [('PLANNED', 'Planned'), ('RUNNING', 'Running'), ('REPORTED', 'Report received'),
                ('CLOSED', 'Findings fixed')]
    title = models.CharField(max_length=160)
    vendor = models.CharField(max_length=160, blank=True)
    scope = models.TextField(blank=True)
    starts = models.DateField()
    ends = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=8, choices=STATUSES, default='PLANNED')
    high = models.PositiveSmallIntegerField(default=0)
    medium = models.PositiveSmallIntegerField(default=0)
    low = models.PositiveSmallIntegerField(default=0)
    fixed = models.PositiveSmallIntegerField(default=0)
    report = models.FileField(upload_to=_upload, storage=_private, blank=True)
    next_due = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-starts']


# ─────────────────────────────────────────────────────────────────────────────
# Platform: backups (daily database snapshots of the whole system)
# ─────────────────────────────────────────────────────────────────────────────

class SystemBackup(models.Model):
    """One run of the database (or files) backup: when, how big, whether it passed its checks."""
    TRIGGERS = [('SCHEDULE', 'Scheduled (cron / timer)'), ('AUTO', 'Automatic daily run'), ('MANUAL', 'Started by an administrator'),
                ('UPDATE', 'Before an update')]
    kind = models.CharField(max_length=5, choices=[('DB', 'Database'), ('FILES', 'Uploaded files')], default='DB')
    trigger = models.CharField(max_length=8, choices=TRIGGERS, default='SCHEDULE')
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    ok = models.BooleanField(default=False)
    file_name = models.CharField(max_length=120, blank=True)
    size = models.PositiveBigIntegerField(default=0)
    integrity = models.CharField(max_length=120, blank=True, help_text='"ok", or what the check found')
    offsite = models.CharField(max_length=200, blank=True, help_text='Where the off-site copy went, or why it did not')
    error = models.TextField(blank=True)
    started_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')

    class Meta:
        ordering = ['-started_at']

    @property
    def seconds(self):
        return (self.finished_at - self.started_at).total_seconds() if self.finished_at else None


# ─────────────────────────────────────────────────────────────────────────────
# Password help: for people with no email address on their account
# ─────────────────────────────────────────────────────────────────────────────

class PasswordHelpRequest(TenantModelMixin, models.Model):
    """Someone who cannot reset their password by email asks the school office.
    An administrator checks who they are, then gives them a temporary password."""
    KINDS = [('STAFF', 'Staff member'), ('STUDENT', 'Student'), ('PARENT', 'Parent')]
    STATUSES = [('OPEN', 'Waiting'), ('DONE', 'Password reset'), ('DECLINED', 'Declined')]
    kind = models.CharField(max_length=7, choices=KINDS)
    identifier = models.CharField(max_length=150, help_text='Staff username, or the admission number')
    name = models.CharField(max_length=120)
    phone = models.CharField(max_length=30, blank=True)
    note = models.CharField(max_length=500, blank=True)
    user = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
                             help_text='The staff or student account it matched')
    student = models.ForeignKey('users.Student', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    status = models.CharField(max_length=8, choices=STATUSES, default='OPEN')
    from_desk = models.BooleanField(default=False, help_text='Reset at the office, not asked for online')
    ip = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    handled_at = models.DateTimeField(null=True, blank=True)
    handled_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')

    objects = TenantManager()

    class Meta:
        ordering = ['-created_at']

    @property
    def matched(self):
        return self.user_id is not None if self.kind == 'STAFF' else self.student_id is not None
