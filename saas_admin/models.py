"""
saas_admin/models.py
────────────────────
The platform layer that sits above schools (tenants): sample data, usage,
add-ons, coupons, referrals and agents, plan changes, white-label branding,
custom domains, health scores, support tickets, the help centre and product
tours, feature flags, exports, backups and offboarding, platform notices,
impersonation and campuses.

These rows belong to the platform, not to one school's data, so they do not
use TenantModelMixin; rows about a school point at it with a plain FK.
"""
import uuid
from decimal import Decimal

from django.conf import settings
from django.db import models

USER = settings.AUTH_USER_MODEL
ZERO = Decimal('0')
TENANT = 'tenants.Tenant'


def _private():
    from users.models import PrivateStorage
    return PrivateStorage()


def _upload(instance, filename):
    import os
    ext = os.path.splitext(filename)[1].lower()[:8]
    return f"platform/{instance.__class__.__name__.lower()}/{uuid.uuid4().hex}{ext}"


def _money(**kw):
    return models.DecimalField(max_digits=14, decimal_places=2, default=ZERO, **kw)


# ─────────────────────────────────────────────────────────────────────────────
# F422 sample data, F423 usage
# ─────────────────────────────────────────────────────────────────────────────

class SampleRecord(models.Model):
    """Remembers every row the sample-data sandbox created, so it can all be removed again."""
    tenant = models.ForeignKey(TENANT, on_delete=models.CASCADE, related_name='sample_records')
    model = models.CharField(max_length=80, help_text='app_label.ModelName')
    object_id = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)


class UsageSnapshot(models.Model):
    tenant = models.ForeignKey(TENANT, on_delete=models.CASCADE, related_name='usage_snapshots')
    date = models.DateField()
    students = models.PositiveIntegerField(default=0)
    staff = models.PositiveIntegerField(default=0)
    active_users = models.PositiveIntegerField(default=0, help_text='Signed in during the last 7 days')
    sms_sent = models.PositiveIntegerField(default=0)
    storage_mb = models.DecimalField(max_digits=10, decimal_places=1, default=ZERO)

    class Meta:
        unique_together = ('tenant', 'date')
        ordering = ['-date']


# ─────────────────────────────────────────────────────────────────────────────
# F425 add-ons, F426 coupons/referrals/credit, F427 agents, F424 plan changes
# ─────────────────────────────────────────────────────────────────────────────

class AddOn(models.Model):
    code = models.SlugField(max_length=40, unique=True)
    name = models.CharField(max_length=80)
    description = models.TextField(blank=True)
    feature = models.CharField(max_length=40, blank=True,
                               help_text='Feature code it switches on, e.g. hostel, finance, custom_domain, '
                                         'white_label')
    price_term = _money(help_text='Per term')
    price_per_student = _money(help_text='Extra per student per term')
    icon = models.CharField(max_length=40, default='bi-puzzle')
    active = models.BooleanField(default=True)
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ['sort_order', 'name']

    def __str__(self):
        return self.name


class TenantAddOn(models.Model):
    STATUSES = [('PENDING', 'Waiting for payment'), ('ACTIVE', 'Active'), ('CANCELLED', 'Cancelled')]
    tenant = models.ForeignKey(TENANT, on_delete=models.CASCADE, related_name='addons')
    addon = models.ForeignKey(AddOn, on_delete=models.PROTECT, related_name='subscribers')
    status = models.CharField(max_length=9, choices=STATUSES, default='PENDING')
    starts = models.DateField(null=True, blank=True)
    ends = models.DateField(null=True, blank=True)
    amount = _money()
    reference = models.CharField(max_length=80, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']


class Coupon(models.Model):
    code = models.CharField(max_length=30, unique=True)
    description = models.CharField(max_length=160, blank=True)
    percent_off = models.PositiveSmallIntegerField(default=0)
    amount_off = _money()
    plans = models.ManyToManyField('tenants.Plan', blank=True, related_name='+', help_text='Empty = any plan')
    max_uses = models.PositiveIntegerField(default=0, help_text='0 = unlimited')
    once_per_school = models.BooleanField(default=True)
    valid_until = models.DateField(null=True, blank=True)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.code


class CouponRedemption(models.Model):
    coupon = models.ForeignKey(Coupon, on_delete=models.CASCADE, related_name='redemptions')
    tenant = models.ForeignKey(TENANT, on_delete=models.CASCADE, related_name='coupon_redemptions')
    payment_ref = models.CharField(max_length=120, blank=True)
    discount = _money()
    at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-at']


class Agent(models.Model):
    name = models.CharField(max_length=160)
    email = models.EmailField()
    phone = models.CharField(max_length=20, blank=True)
    code = models.CharField(max_length=20, unique=True)
    commission_percent = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('15'))
    user = models.OneToOneField(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name='agent')
    bank_details = models.CharField(max_length=255, blank=True)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return f'{self.name} ({self.code})'


class Account(models.Model):
    """Platform-side facts about one school: referral code, who referred it, agent, credit."""
    tenant = models.OneToOneField(TENANT, on_delete=models.CASCADE, related_name='account')
    referral_code = models.CharField(max_length=20, unique=True)
    referred_by = models.ForeignKey(TENANT, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    referral_rewarded = models.BooleanField(default=False)
    agent = models.ForeignKey(Agent, on_delete=models.SET_NULL, null=True, blank=True, related_name='schools')
    credit = _money(help_text='Taken off the next payment')
    sandbox = models.BooleanField(default=False, help_text='Has sample data loaded')
    notes = models.TextField(blank=True, help_text='Platform team only')


class CreditEntry(models.Model):
    tenant = models.ForeignKey(TENANT, on_delete=models.CASCADE, related_name='credit_entries')
    amount = _money(help_text='Positive adds credit, negative uses it')
    reason = models.CharField(max_length=160)
    at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-at']


class Commission(models.Model):
    agent = models.ForeignKey(Agent, on_delete=models.CASCADE, related_name='commissions')
    tenant = models.ForeignKey(TENANT, on_delete=models.CASCADE, related_name='+')
    payment_ref = models.CharField(max_length=120, unique=True)
    paid_amount = _money()
    amount = _money()
    status = models.CharField(max_length=6, choices=[('UNPAID', 'Owed'), ('PAID', 'Paid')], default='UNPAID')
    paid_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']


class PaymentHook(models.Model):
    """Marks a subscription payment as already processed for coupons, credit, referrals and commission."""
    payment_ref = models.CharField(max_length=120, unique=True)
    at = models.DateTimeField(auto_now_add=True)


class PlanChange(models.Model):
    STATUSES = [('SCHEDULED', 'Scheduled'), ('APPLIED', 'Applied'), ('CANCELLED', 'Cancelled')]
    tenant = models.ForeignKey(TENANT, on_delete=models.CASCADE, related_name='plan_changes')
    from_plan = models.ForeignKey('tenants.Plan', on_delete=models.PROTECT, related_name='+')
    to_plan = models.ForeignKey('tenants.Plan', on_delete=models.PROTECT, related_name='+')
    billing_cycle = models.CharField(max_length=8)
    effective = models.DateField()
    status = models.CharField(max_length=9, choices=STATUSES, default='SCHEDULED')
    requested_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']


# ─────────────────────────────────────────────────────────────────────────────
# F428 white label, F429 custom domains
# ─────────────────────────────────────────────────────────────────────────────

class Branding(models.Model):
    tenant = models.OneToOneField(TENANT, on_delete=models.CASCADE, related_name='branding')
    brand_name = models.CharField(max_length=80, blank=True, help_text='Portal name shown under the school name')
    primary = models.CharField(max_length=7, blank=True, help_text='e.g. #1f6feb')
    accent = models.CharField(max_length=7, blank=True)
    favicon = models.ImageField(upload_to='tenant_branding/', blank=True, null=True)
    login_message = models.CharField(max_length=255, blank=True)
    email_from_name = models.CharField(max_length=80, blank=True)
    hide_powered_by = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)


class DomainRequest(models.Model):
    STATUSES = [('PENDING', 'Waiting for DNS'), ('ACTIVE', 'Live'), ('FAILED', 'Not pointing here'),
                ('REMOVED', 'Removed')]
    tenant = models.ForeignKey(TENANT, on_delete=models.CASCADE, related_name='domain_requests')
    domain = models.CharField(max_length=253)
    status = models.CharField(max_length=7, choices=STATUSES, default='PENDING')
    last_checked = models.DateTimeField(null=True, blank=True)
    error = models.CharField(max_length=255, blank=True)
    activated_at = models.DateTimeField(null=True, blank=True)
    requested_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']


# ─────────────────────────────────────────────────────────────────────────────
# F430 health, F434 feature flags
# ─────────────────────────────────────────────────────────────────────────────

class HealthScore(models.Model):
    tenant = models.ForeignKey(TENANT, on_delete=models.CASCADE, related_name='health_scores')
    date = models.DateField()
    score = models.PositiveSmallIntegerField()
    parts = models.JSONField(default=dict)

    class Meta:
        unique_together = ('tenant', 'date')
        ordering = ['-date']

    @property
    def risk(self):
        return 'Healthy' if self.score >= 70 else ('Watch' if self.score >= 40 else 'At risk')


class FeatureFlag(models.Model):
    key = models.SlugField(max_length=60, unique=True)
    description = models.CharField(max_length=255, blank=True)
    default_on = models.BooleanField(default=False)
    rollout_percent = models.PositiveSmallIntegerField(default=0, help_text='Switch on for this % of schools')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['key']

    def __str__(self):
        return self.key


class TenantFlag(models.Model):
    """A per-school override: a flag key, or 'feature:<code>' to switch a plan feature on or off."""
    tenant = models.ForeignKey(TENANT, on_delete=models.CASCADE, related_name='flags')
    key = models.CharField(max_length=60)
    enabled = models.BooleanField()
    note = models.CharField(max_length=160, blank=True)
    set_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, related_name='+')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('tenant', 'key')


# ─────────────────────────────────────────────────────────────────────────────
# F431 support, F432 help centre, F433 tours, F437 notices
# ─────────────────────────────────────────────────────────────────────────────

class Ticket(models.Model):
    CATEGORIES = [('HOW', 'How do I…?'), ('BUG', 'Something is broken'), ('BILLING', 'Billing'),
                  ('DATA', 'Data / import'), ('FEATURE', 'Feature request'), ('OTHER', 'Other')]
    STATUSES = [('OPEN', 'Open'), ('WAITING', 'Waiting for the school'), ('SOLVED', 'Solved'),
                ('CLOSED', 'Closed')]
    PRIORITIES = [('LOW', 'Low'), ('NORMAL', 'Normal'), ('HIGH', 'High'), ('URGENT', 'Urgent — school cannot work')]
    number = models.CharField(max_length=20, unique=True)
    tenant = models.ForeignKey(TENANT, on_delete=models.CASCADE, related_name='tickets')
    opened_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, related_name='+')
    subject = models.CharField(max_length=200)
    category = models.CharField(max_length=8, choices=CATEGORIES, default='HOW')
    priority = models.CharField(max_length=6, choices=PRIORITIES, default='NORMAL')
    status = models.CharField(max_length=7, choices=STATUSES, default='OPEN')
    assigned_to = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    page = models.CharField(max_length=255, blank=True, help_text='Where they were when they asked')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']


class TicketMessage(models.Model):
    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name='messages')
    author = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, related_name='+')
    from_platform = models.BooleanField(default=False)
    internal = models.BooleanField(default=False, help_text='Platform note the school does not see')
    body = models.TextField()
    attachment = models.FileField(upload_to=_upload, storage=_private, blank=True)
    at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['at']


class Article(models.Model):
    title = models.CharField(max_length=200)
    slug = models.SlugField(max_length=200, unique=True)
    category = models.CharField(max_length=60, default='Getting started')
    module = models.CharField(max_length=40, blank=True, help_text='URL prefix it helps with, e.g. finance')
    summary = models.CharField(max_length=255, blank=True)
    body = models.TextField(help_text='Plain text; blank lines start new paragraphs, lines starting "- " are '
                                      'bullets, lines starting "## " are headings')
    published = models.BooleanField(default=True)
    views = models.PositiveIntegerField(default=0)
    helpful_yes = models.PositiveIntegerField(default=0)
    helpful_no = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['category', 'title']

    def __str__(self):
        return self.title


class Tour(models.Model):
    key = models.SlugField(max_length=60, unique=True)
    title = models.CharField(max_length=120)
    path_prefix = models.CharField(max_length=120, help_text='Shown on pages whose path starts with this, e.g. '
                                                            '/library/desk/')
    steps = models.JSONField(default=list, help_text='[{"title": "...", "text": "..."}, …]')
    audience = models.CharField(max_length=8, choices=[('STAFF', 'Staff'), ('ADMIN', 'Administrators'),
                                                       ('STUDENT', 'Students'), ('ALL', 'Everyone')],
                                default='STAFF')
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ['path_prefix']

    def __str__(self):
        return self.title


class TourSeen(models.Model):
    tour = models.ForeignKey(Tour, on_delete=models.CASCADE, related_name='seen')
    user = models.ForeignKey(USER, on_delete=models.CASCADE, related_name='+')
    at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('tour', 'user')


class Notice(models.Model):
    LEVELS = [('INFO', 'Information'), ('SUCCESS', 'Good news'), ('WARNING', 'Warning'), ('DANGER', 'Outage')]
    AUDIENCES = [('ADMINS', 'School administrators'), ('STAFF', 'All staff'), ('ALL', 'Everyone signed in')]
    message = models.CharField(max_length=255)
    link = models.URLField(blank=True)
    level = models.CharField(max_length=7, choices=LEVELS, default='INFO')
    audience = models.CharField(max_length=6, choices=AUDIENCES, default='ADMINS')
    plans = models.ManyToManyField('tenants.Plan', blank=True, related_name='+', help_text='Empty = every plan')
    starts = models.DateTimeField()
    ends = models.DateTimeField(null=True, blank=True)
    dismissible = models.BooleanField(default=True)
    created_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, related_name='+')

    class Meta:
        ordering = ['-starts']


class NoticeDismissal(models.Model):
    notice = models.ForeignKey(Notice, on_delete=models.CASCADE, related_name='dismissals')
    user = models.ForeignKey(USER, on_delete=models.CASCADE, related_name='+')

    class Meta:
        unique_together = ('notice', 'user')


# ─────────────────────────────────────────────────────────────────────────────
# F435 export/offboarding, F436 backups, F439 impersonation, F440 campuses
# ─────────────────────────────────────────────────────────────────────────────

class DataExport(models.Model):
    STATUSES = [('QUEUED', 'Queued'), ('READY', 'Ready'), ('FAILED', 'Failed')]
    tenant = models.ForeignKey(TENANT, on_delete=models.CASCADE, related_name='platform_exports')
    requested_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, related_name='+')
    status = models.CharField(max_length=6, choices=STATUSES, default='QUEUED')
    file = models.FileField(upload_to=_upload, storage=_private, blank=True)
    size = models.PositiveBigIntegerField(default=0)
    tables = models.JSONField(default=dict)
    error = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']


class Offboarding(models.Model):
    STATUSES = [('REQUESTED', 'Requested'), ('SCHEDULED', 'Scheduled for deletion'), ('CANCELLED', 'Cancelled'),
                ('DELETED', 'Deleted')]
    tenant = models.ForeignKey(TENANT, on_delete=models.SET_NULL, null=True, related_name='offboardings')
    school_name = models.CharField(max_length=255)
    reason = models.TextField()
    requested_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, related_name='+')
    export = models.ForeignKey(DataExport, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    delete_after = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=9, choices=STATUSES, default='REQUESTED')
    confirmed_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']


class Backup(models.Model):
    tenant = models.ForeignKey(TENANT, on_delete=models.CASCADE, related_name='backups')
    kind = models.CharField(max_length=6, choices=[('AUTO', 'Nightly'), ('MANUAL', 'On request')], default='AUTO')
    file = models.FileField(upload_to=_upload, storage=_private, blank=True)
    size = models.PositiveBigIntegerField(default=0)
    rows = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    restored_at = models.DateTimeField(null=True, blank=True)
    restore_note = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ['-created_at']


class Impersonation(models.Model):
    admin = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, related_name='+')
    tenant = models.ForeignKey(TENANT, on_delete=models.CASCADE, related_name='impersonations')
    target = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, related_name='+')
    reason = models.CharField(max_length=255)
    token = models.UUIDField(default=uuid.uuid4, unique=True)
    token_used = models.BooleanField(default=False)
    ticket = models.ForeignKey(Ticket, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    ip = models.GenericIPAddressField(null=True, blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    entered_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-started_at']


class Campus(models.Model):
    """A section of the school — Nursery & Primary, Secondary or Darun (the Islamic section)."""
    tenant = models.ForeignKey(TENANT, on_delete=models.CASCADE, related_name='campuses')
    name = models.CharField(max_length=120)
    code = models.CharField(max_length=10, blank=True)
    address = models.CharField(max_length=255, blank=True)
    phone = models.CharField(max_length=20, blank=True)
    head = models.ForeignKey('users.Staff', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    classes = models.ManyToManyField('users.Class', blank=True, related_name='campuses')
    staff = models.ManyToManyField('users.Staff', blank=True, related_name='campuses')
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ['name']
        unique_together = ('tenant', 'name')
        verbose_name = 'section'

    def __str__(self):
        return self.name


class LandingPage(models.Model):
    """What the public home page shows: the hero background (a picture or a looping video) and the
    three campus-life photos. Anything left empty falls back to the built-in pictures."""
    tenant = models.OneToOneField(TENANT, on_delete=models.CASCADE, related_name='landing')
    hero_image = models.ImageField(upload_to='landing/', blank=True, null=True,
                                   help_text='Background picture of the top banner (also the poster while a video loads)')
    hero_video = models.FileField(upload_to='landing/', blank=True, null=True,
                                  help_text='Optional short MP4/WebM video that plays silently on a loop behind the banner')
    use_video = models.BooleanField(default=False)
    photo1 = models.ImageField(upload_to='landing/', blank=True, null=True)
    photo1_label = models.CharField(max_length=40, blank=True)
    photo1_title = models.CharField(max_length=60, blank=True)
    photo2 = models.ImageField(upload_to='landing/', blank=True, null=True)
    photo2_label = models.CharField(max_length=40, blank=True)
    photo2_title = models.CharField(max_length=60, blank=True)
    photo3 = models.ImageField(upload_to='landing/', blank=True, null=True)
    photo3_label = models.CharField(max_length=40, blank=True)
    photo3_title = models.CharField(max_length=60, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'Home page of {self.tenant}'
