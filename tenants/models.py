"""
tenants/models.py
─────────────────
Core school models.

This is a dedicated single-school installation: there is exactly one Tenant
(the school) and every record in every other app belongs to it through its
`tenant` FK. TenantMiddleware binds that school to every request as
request.tenant. Plan, Subscription and SchoolRegistration are legacy tables
kept only so existing migrations and data stay intact; nothing uses them.
"""

import re
import uuid
from django.db import models
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.utils import timezone


def _slug_validator(value):
    if not re.match(r'^[a-z0-9][a-z0-9\-]{1,48}[a-z0-9]$', value):
        raise ValidationError(
            "Subdomain must be 3–50 lowercase letters, digits or hyphens, "
            "and must not start or end with a hyphen."
        )


RESERVED_SLUGS = {
    'www', 'api', 'admin', 'app', 'mail', 'smtp', 'portal',
    'static', 'media', 'cdn', 'status', 'billing', 'support',
    'help', 'docs', 'demo', 'test', 'staging', 'dev',
}


# ─────────────────────────────────────────────────────────────────────────────
# Plan
# ─────────────────────────────────────────────────────────────────────────────
class Plan(models.Model):
    """
    A subscription plan defines which modules are unlocked and how many
    students / staff accounts are permitted.
    """
    BILLING_CYCLE = [
        ('MONTHLY',  'Monthly'),
        ('ANNUAL',   'Annual'),
        ('LIFETIME', 'Lifetime / One-off'),
    ]

    # Identity
    name        = models.CharField(max_length=80, unique=True)   # e.g. "Starter", "Growth", "Enterprise"
    code        = models.SlugField(max_length=40, unique=True)   # e.g. "starter", "growth"
    description = models.TextField(blank=True)
    is_active   = models.BooleanField(default=True)
    is_public   = models.BooleanField(default=True,
                    help_text="Visible on public pricing page")
    sort_order  = models.PositiveSmallIntegerField(default=0)

    # Pricing model
    # price_per_student_term: the core pricing unit — charged per student per term
    # price_per_student_session: = price_per_student_term * 3 (3 terms per session)
    # The flat monthly/annual prices are kept as fallback / legacy
    price_per_student_term = models.DecimalField(
        max_digits=10, decimal_places=2, default=0,
        help_text="Price per student per term (e.g. 2000 for Starter)"
    )
    price_monthly = models.DecimalField(max_digits=12, decimal_places=2, default=0,
                        help_text="Flat monthly price (used if price_per_student_term=0)")
    price_annual  = models.DecimalField(max_digits=12, decimal_places=2, default=0,
                        help_text="Flat annual price (discount already applied)")
    terms_per_session = models.PositiveSmallIntegerField(default=3,
                        help_text="Number of terms per session (usually 3)")
    currency      = models.CharField(max_length=3, default='NGN')

    # Hard limits  (0 = unlimited)
    max_students   = models.PositiveIntegerField(default=200)
    max_staff      = models.PositiveIntegerField(default=20)
    max_classes    = models.PositiveIntegerField(default=10)

    # Feature flags — one per module
    feature_academics     = models.BooleanField(default=True)
    feature_cbt           = models.BooleanField(default=False)
    feature_results       = models.BooleanField(default=True)
    feature_finance       = models.BooleanField(default=False)
    feature_payroll       = models.BooleanField(default=False)
    feature_hostel        = models.BooleanField(default=False)
    feature_communications= models.BooleanField(default=False)
    feature_inventory     = models.BooleanField(default=False)
    feature_timetable     = models.BooleanField(default=False)
    feature_announcements = models.BooleanField(default=True)
    feature_parent_portal = models.BooleanField(default=True)
    feature_api_access    = models.BooleanField(default=False)
    feature_custom_domain = models.BooleanField(default=False)

    # Branding
    highlight_label = models.CharField(max_length=40, blank=True,
                        help_text="e.g. 'Most Popular'")
    highlight_color = models.CharField(max_length=7, blank=True, default='#d4a843',
                        help_text="Hex colour for the highlight badge")

    class Meta:
        ordering = ['sort_order', 'price_monthly']

    def __str__(self):
        return f"{self.name} (₦{self.price_monthly:,.0f}/mo)"

    @property
    def annual_monthly_equivalent(self):
        """Monthly cost if billed annually."""
        if self.price_annual and self.price_annual > 0:
            return round(self.price_annual / 12, 2)
        return self.price_monthly

    @property
    def monthly_equivalent(self):
        """Alias used by templates."""
        return self.annual_monthly_equivalent

    @property
    def price_per_student_session(self):
        """Price per student per session = term price × number of terms."""
        return self.price_per_student_term * self.terms_per_session

    def calculate_term_cost(self, student_count):
        """Total cost for one term given number of students."""
        if self.price_per_student_term and student_count:
            return self.price_per_student_term * student_count
        return self.price_monthly

    def calculate_session_cost(self, student_count):
        """Total cost for one full session (all terms)."""
        if self.price_per_student_term and student_count:
            return self.price_per_student_term * student_count * self.terms_per_session
        return self.price_annual

    @property
    def annual_savings(self):
        if self.price_monthly and self.price_annual:
            return round((self.price_monthly * 12) - self.price_annual, 2)
        return 0

    def has_feature(self, feature_code: str) -> bool:
        return getattr(self, f'feature_{feature_code}', False)

    @property
    def features_list(self):
        mapping = [
            ('academics',      'Academic Management',    'bi-mortarboard-fill'),
            ('cbt',            'Computer-Based Testing', 'bi-pencil-square'),
            ('results',        'Results & Transcripts',  'bi-clipboard-data-fill'),
            ('finance',        'Finance & Fee Wallet',   'bi-cash-coin'),
            ('payroll',        'Staff Payroll',          'bi-briefcase-fill'),
            ('hostel',         'Hostel Management',      'bi-house-fill'),
            ('communications', 'Email & SMS Campaigns',  'bi-chat-dots-fill'),
            ('inventory',      'Inventory & Assets',     'bi-box-seam-fill'),
            ('timetable',      'Timetable Builder',      'bi-grid-3x3-gap-fill'),
            ('announcements',  'Announcements',          'bi-megaphone-fill'),
            ('parent_portal',  'Parent Portal',          'bi-people-fill'),
            ('api_access',     'API Access',             'bi-code-slash'),
            ('custom_domain',  'Custom Domain',          'bi-globe2'),
        ]
        return [
            {'code': code, 'label': label, 'icon': icon,
             'included': self.has_feature(code)}
            for code, label, icon in mapping
        ]


# ─────────────────────────────────────────────────────────────────────────────
# Tenant (School)
# ─────────────────────────────────────────────────────────────────────────────
class Tenant(models.Model):
    STATUS_CHOICES = [
        ('PENDING',   'Pending Activation'),   # registered, not yet approved
        ('TRIAL',     'Free Trial'),
        ('ACTIVE',    'Active'),
        ('SUSPENDED', 'Suspended'),
        ('CANCELLED', 'Cancelled'),
    ]

    SCHOOL_TYPE_CHOICES = [
        ('nursery',    'Nursery / Primary School'),
        ('secondary',  'Secondary / High School'),
        ('tertiary',   'Tertiary / Polytechnic'),
        ('vocational', 'Vocational / Technical'),
    ]

    # FIX #1: Added TERM and SESSION so that per-student billing cycles
    # pass model validation. Previously only MONTHLY/ANNUAL were here,
    # which caused a ValidationError — and a silent failure — whenever a
    # school registered on a TERM billing cycle.
    BILLING_CYCLE_CHOICES = [
        ('TERM',     'Per Term'),
        ('SESSION',  'Per Session (3 Terms)'),
        ('MONTHLY',  'Monthly'),
        ('ANNUAL',   'Annual'),
    ]

    # Identity
    id           = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name         = models.CharField(max_length=255,
                    help_text="Official school name, e.g. Sunshine Academy")
    subdomain    = models.CharField(max_length=50, unique=True,
                    validators=[_slug_validator],
                    help_text="Unique subdomain slug, e.g. 'sunshine'")
    school_type  = models.CharField(max_length=20, choices=SCHOOL_TYPE_CHOICES,
                    default='secondary')
    custom_domain= models.CharField(max_length=253, blank=True,
                    help_text="Custom domain if on Enterprise plan, e.g. portal.sunshineacademy.edu.ng")

    # Contact
    email        = models.EmailField(unique=True,
                    help_text="Primary contact / billing email")
    phone        = models.CharField(max_length=20, blank=True)
    address      = models.TextField(blank=True)
    state        = models.CharField(max_length=60, blank=True)
    country      = models.CharField(max_length=60, default='Nigeria')
    logo         = models.ImageField(upload_to='tenant_logos/', blank=True, null=True)
    motto        = models.CharField(max_length=255, blank=True,
                    help_text="School motto — shown under the logo on result "
                              "sheets and letterheads.")

    # Owner / registrant contact info (stored directly — no platform User required initially)
    owner_name   = models.CharField(max_length=150, blank=True,
                    help_text="Name of the person who registered the school")
    owner_email  = models.EmailField(blank=True,
                    help_text="Email of the registrant (may differ from school email)")

    # Billing owner (platform User account, linked after approval)
    # NOTE: was on_delete=PROTECT — but User.tenant is CASCADE, so deleting a
    # Tenant cascades to delete its owner User, which was then blocked by
    # this very same PROTECT FK (the tenant protecting its own owner from
    # deletion). That circular reference made every tenant undeletable.
    # SET_NULL breaks the cycle: the owner user gets deleted along with the
    # tenant (via User.tenant CASCADE), and this field just becomes null.
    owner        = models.ForeignKey(
                    'users.User', on_delete=models.SET_NULL,
                    related_name='owned_tenants', null=True, blank=True,
                    help_text="Platform-level owner account (not a school staff)")

    # Subscription
    plan          = models.ForeignKey(Plan, on_delete=models.PROTECT,
                     null=True, blank=True, related_name='tenants')
    billing_cycle = models.CharField(max_length=8, choices=BILLING_CYCLE_CHOICES,
                     default='TERM')
    status        = models.CharField(max_length=12, choices=STATUS_CHOICES,
                     default='PENDING')
    trial_ends    = models.DateField(null=True, blank=True)

    # Timestamps
    created_at   = models.DateTimeField(auto_now_add=True)
    updated_at   = models.DateTimeField(auto_now=True)
    activated_at = models.DateTimeField(null=True, blank=True)

    # School-specific config (overrides platform defaults)
    timezone     = models.CharField(max_length=60, default='Africa/Lagos')
    currency     = models.CharField(max_length=3, default='NGN')
    term_system  = models.CharField(max_length=20, default='trimester',
                    choices=[('trimester','3 Terms'),('semester','2 Semesters')])

    # Paystack sub-account for split payments (optional, future)
    paystack_subaccount_code = models.CharField(max_length=80, blank=True)

    # Expiry-reminder tracking (trial) — prevents the daily check_subscriptions
    # command from emailing the same reminder more than once.
    trial_reminder_7d_sent = models.BooleanField(default=False)
    trial_reminder_3d_sent = models.BooleanField(default=False)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.name

    def clean(self):
        if self.subdomain in RESERVED_SLUGS:
            raise ValidationError(
                f"'{self.subdomain}' is a reserved subdomain and cannot be used.")

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    @property
    def portal_url(self):
        """The school portal's address, e.g. https://portal.example.edu.ng"""
        from django.conf import settings
        url = getattr(settings, 'SITE_URL', '')
        if url:
            return url.rstrip('/')
        root = getattr(settings, 'ROOT_DOMAIN', 'localhost')
        scheme = 'http' if root in ('localhost', '127.0.0.1') else 'https'
        port = getattr(settings, 'DEV_PORT', '')
        return f"{scheme}://{root}{':' + port if port else ''}"

    # A dedicated install has no trial, no subscription and no plan limits.
    is_on_trial = False
    trial_expired = False

    def days_until_trial_ends(self):
        return None

    @property
    def is_accessible(self):
        return True

    def has_feature(self, feature_code: str) -> bool:
        """Every module is included in this installation."""
        return True

    def activate(self):
        self.status = 'ACTIVE'
        self.activated_at = timezone.now()
        self.save(update_fields=['status', 'activated_at', 'updated_at'])

    def suspend(self):
        self.status = 'SUSPENDED'
        self.save(update_fields=['status', 'updated_at'])


# ─────────────────────────────────────────────────────────────────────────────
# Subscription (billing record per cycle)
# ─────────────────────────────────────────────────────────────────────────────
class Subscription(models.Model):
    BILLING_CYCLE = [
        ('TERM',     'Per Term'),
        ('SESSION',  'Per Session (3 Terms)'),
        ('MONTHLY',  'Monthly'),
        ('ANNUAL',   'Annual'),
    ]
    STATUS = [
        ('ACTIVE',    'Active'),
        ('PAST_DUE',  'Past Due'),
        ('CANCELLED', 'Cancelled'),
        ('EXPIRED',   'Expired'),
    ]

    tenant        = models.ForeignKey(Tenant, on_delete=models.CASCADE,
                     related_name='subscriptions')
    plan          = models.ForeignKey(Plan, on_delete=models.PROTECT)
    billing_cycle = models.CharField(max_length=8, choices=BILLING_CYCLE,
                     default='TERM')
    status        = models.CharField(max_length=10, choices=STATUS, default='ACTIVE')

    # For per-student billing
    student_count = models.PositiveIntegerField(default=0,
                     help_text="Number of students billed for this period")
    price_per_student = models.DecimalField(max_digits=10, decimal_places=2, default=0,
                         help_text="Rate per student at time of billing")

    amount        = models.DecimalField(max_digits=12, decimal_places=2)
    currency      = models.CharField(max_length=3, default='NGN')

    starts_at     = models.DateField()
    ends_at       = models.DateField()
    renews_at     = models.DateField(null=True, blank=True)
    cancelled_at  = models.DateTimeField(null=True, blank=True)

    # Paystack reference
    paystack_ref          = models.CharField(max_length=100, blank=True)
    paystack_subscription_code = models.CharField(max_length=100, blank=True)

    created_at    = models.DateTimeField(auto_now_add=True)

    # Expiry-reminder tracking — prevents the daily check_subscriptions
    # command from emailing the same reminder / expiry notice more than once.
    reminder_7d_sent     = models.BooleanField(default=False)
    reminder_3d_sent     = models.BooleanField(default=False)
    expired_notice_sent  = models.BooleanField(default=False)

    class Meta:
        ordering = ['-starts_at']

    def __str__(self):
        return (f"{self.tenant.name} — {self.plan.name} "
                f"({self.billing_cycle}) {self.starts_at}→{self.ends_at}")

    @property
    def is_active(self):
        return (self.status == 'ACTIVE' and
                self.starts_at <= timezone.now().date() <= self.ends_at)

    @property
    def days_remaining(self):
        delta = self.ends_at - timezone.now().date()
        return max(delta.days, 0)


# ─────────────────────────────────────────────────────────────────────────────
# Payment (each Paystack charge)
# ─────────────────────────────────────────────────────────────────────────────
class SubscriptionPayment(models.Model):
    STATUS = [
        ('PENDING',  'Pending'),
        ('SUCCESS',  'Successful'),
        ('FAILED',   'Failed'),
        ('REFUNDED', 'Refunded'),
    ]

    id            = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant        = models.ForeignKey(Tenant, on_delete=models.CASCADE,
                     related_name='payments')
    subscription  = models.ForeignKey(Subscription, on_delete=models.SET_NULL,
                     null=True, blank=True, related_name='payments')
    plan          = models.ForeignKey(Plan, on_delete=models.PROTECT)

    amount        = models.DecimalField(max_digits=12, decimal_places=2)
    currency      = models.CharField(max_length=3, default='NGN')
    status        = models.CharField(max_length=10, choices=STATUS, default='PENDING')

    paystack_ref  = models.CharField(max_length=120, unique=True)
    paystack_data = models.JSONField(default=dict, blank=True)   # raw Paystack webhook payload

    paid_at       = models.DateTimeField(null=True, blank=True)
    created_at    = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.tenant.name} — ₦{self.amount:,.2f} [{self.status}]"


# ─────────────────────────────────────────────────────────────────────────────
# School Registration Request (before Tenant is created)
# ─────────────────────────────────────────────────────────────────────────────
class SchoolRegistration(models.Model):
    STATUS = [
        ('PENDING',   'Pending Review'),
        ('APPROVED',  'Approved'),
        ('REJECTED',  'Rejected'),
    ]

    SCHOOL_TYPE_CHOICES = [
        ('nursery',    'Nursery / Primary School'),
        ('secondary',  'Secondary / High School'),
        ('tertiary',   'Tertiary / Polytechnic'),
        ('vocational', 'Vocational / Technical'),
    ]

    # FIX #1 (continued): SchoolRegistration also needs TERM and SESSION
    # so that desired_billing='TERM' passes validation and is stored correctly.
    BILLING_CYCLE_CHOICES = [
        ('TERM',     'Per Term'),
        ('SESSION',  'Per Session (3 Terms)'),
        ('MONTHLY',  'Monthly'),
        ('ANNUAL',   'Annual'),
    ]

    SOURCE_CHOICES = [
        ('website',  'Main Website'),
        ('founding', 'Founding Schools Campaign'),
    ]

    # Basic info
    source         = models.CharField(max_length=20, choices=SOURCE_CHOICES,
                      default='website',
                      help_text="Which funnel this registration came from")
    school_name    = models.CharField(max_length=255)
    school_type    = models.CharField(max_length=20, choices=SCHOOL_TYPE_CHOICES,
                      default='secondary')
    subdomain      = models.CharField(max_length=50, validators=[_slug_validator])
    contact_name   = models.CharField(max_length=150)
    contact_email  = models.EmailField(unique=True)
    contact_phone  = models.CharField(max_length=20)
    school_address = models.TextField(blank=True)
    state          = models.CharField(max_length=60, blank=True)

    # What they want
    desired_plan    = models.ForeignKey(Plan, on_delete=models.SET_NULL,
                       null=True, blank=True)
    desired_billing = models.CharField(max_length=8, choices=BILLING_CYCLE_CHOICES,
                       default='TERM')
    student_count   = models.PositiveIntegerField(default=0,
                       help_text="Approximate number of students")
    message         = models.TextField(blank=True,
                       help_text="Anything else you'd like us to know")

    # Processing
    status         = models.CharField(max_length=10, choices=STATUS, default='PENDING')
    reviewed_by    = models.ForeignKey('users.User', on_delete=models.SET_NULL,
                      null=True, blank=True, related_name='reviewed_registrations')
    reviewed_at    = models.DateTimeField(null=True, blank=True)
    rejection_note = models.TextField(blank=True)
    tenant         = models.OneToOneField(Tenant, on_delete=models.SET_NULL,
                      null=True, blank=True, related_name='registration')

    created_at     = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.school_name} [{self.status}]"