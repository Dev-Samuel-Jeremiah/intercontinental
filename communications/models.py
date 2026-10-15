"""
communications/models.py — Parent Communication System
============================================================
Email + SMS campaigns, delivery tracking, message templates,
scheduled sending, opt-out management, communication history.
"""

import datetime

from django.conf import settings
from django.db import models
from tenants.managers import TenantModelMixin, TenantManager

from django.utils import timezone


# ─────────────────────────────────────────────────────────────────────────────
# MESSAGE TEMPLATE  (reusable content)
# ─────────────────────────────────────────────────────────────────────────────

class MessageTemplate(TenantModelMixin, models.Model):
    CATEGORY_CHOICES = [
        ('FEE',        'Fee Reminder'),
        ('HOSTEL',     'Hostel Notice'),
        ('EXAM',       'Exam / Result'),
        ('GENERAL',    'General Announcement'),
        ('ATTENDANCE', 'Attendance Alert'),
        ('EMERGENCY',  'Emergency'),
        ('WELCOME',    'Welcome / Admission'),
        ('CUSTOM',     'Custom'),
    ]

    name        = models.CharField(max_length=200)
    category    = models.CharField(max_length=12, choices=CATEGORY_CHOICES,
                                   default='GENERAL')
    subject     = models.CharField(max_length=300,
                                   help_text='Email subject. Can use {{student_name}}, {{school_name}}, {{balance}}, etc.')
    body_email  = models.TextField(
        help_text='HTML email body. Placeholders: {{student_name}}, {{parent_name}}, '
                  '{{class_name}}, {{balance}}, {{school_name}}, {{term}}, {{session}}')
    body_sms    = models.CharField(max_length=800,
                                   help_text='SMS body (max ~5 parts = 800 chars). Same placeholders available.')
    is_active   = models.BooleanField(default=True)
    created_by  = models.ForeignKey(settings.AUTH_USER_MODEL,
                                    on_delete=models.SET_NULL,
                                    null=True, blank=True)
    created_at  = models.DateTimeField(auto_now_add=True)
    updated_at  = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['category', 'name']
        unique_together = ('tenant', 'name')

    def __str__(self):
        return f"[{self.get_category_display()}] {self.name}"


# ─────────────────────────────────────────────────────────────────────────────
# COMMUNICATION CAMPAIGN  (one send to a group of recipients)
# ─────────────────────────────────────────────────────────────────────────────

class Campaign(TenantModelMixin, models.Model):
    CHANNEL_CHOICES = [
        ('EMAIL',    'Email Only'),
        ('SMS',      'SMS Only'),
        ('BOTH',     'Email + SMS'),
    ]
    STATUS_CHOICES = [
        ('DRAFT',      'Draft'),
        ('PENDING',    'Waiting for approval'),
        ('REJECTED',   'Not approved'),
        ('SCHEDULED',  'Scheduled'),
        ('SENDING',    'Sending'),
        ('SENT',       'Sent'),
        ('FAILED',     'Failed'),
        ('CANCELLED',  'Cancelled'),
    ]
    AUDIENCE_CHOICES = [
        ('ALL',         'All Parents'),
        ('CLASS',       'Specific Class'),
        ('BOARDERS',    'Hostel Boarders Only'),
        ('DEBTORS',     'Parents with Unpaid Fees'),
        ('CUSTOM',      'Custom Selection'),
    ]

    TOPIC_CHOICES = [
        ('GENERAL',  'School news and events'),
        ('FEES',     'Fees and payments'),
        ('ACADEMIC', 'Results and academics'),
        ('URGENT',   'Safety and urgent notices'),
    ]

    title           = models.CharField(max_length=300)
    channel         = models.CharField(max_length=5,  choices=CHANNEL_CHOICES, default='EMAIL')
    topic           = models.CharField(max_length=8, choices=TOPIC_CHOICES, default='GENERAL',
                                       help_text='Parents can turn off school news; other topics always reach them.')
    audience        = models.CharField(max_length=10, choices=AUDIENCE_CHOICES, default='ALL')
    school_class    = models.ForeignKey('users.Class', on_delete=models.SET_NULL,
                                        null=True, blank=True,
                                        help_text='Only for CLASS audience')
    template        = models.ForeignKey(MessageTemplate, on_delete=models.SET_NULL,
                                        null=True, blank=True)
    subject         = models.CharField(max_length=300,
                                       help_text='Email subject (auto-filled from template if used)')
    body_email      = models.TextField(blank=True,
                                       help_text='HTML email body')
    body_sms        = models.CharField(max_length=800, blank=True,
                                       help_text='SMS message (max 800 chars)')
    status          = models.CharField(max_length=10, choices=STATUS_CHOICES, default='DRAFT')
    scheduled_at    = models.DateTimeField(null=True, blank=True,
                                           help_text='Leave blank to send immediately')
    sent_at         = models.DateTimeField(null=True, blank=True)
    created_by      = models.ForeignKey(settings.AUTH_USER_MODEL,
                                        on_delete=models.SET_NULL,
                                        null=True, blank=True)
    created_at      = models.DateTimeField(auto_now_add=True)
    updated_at      = models.DateTimeField(auto_now=True)

    # Approval (F250): messages from junior staff wait for an administrator.
    approved_by     = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                        null=True, blank=True, related_name='+')
    approved_at     = models.DateTimeField(null=True, blank=True)
    review_note     = models.CharField(max_length=255, blank=True)

    # Stats (denormalised for fast dashboard reads)
    total_recipients  = models.PositiveIntegerField(default=0)
    emails_sent       = models.PositiveIntegerField(default=0)
    emails_failed     = models.PositiveIntegerField(default=0)
    sms_sent          = models.PositiveIntegerField(default=0)
    sms_failed        = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"[{self.channel}] {self.title}"

    @property
    def success_rate(self):
        total = self.emails_sent + self.sms_sent
        total_attempts = total + self.emails_failed + self.sms_failed
        return round((total / total_attempts) * 100, 1) if total_attempts > 0 else 0


# ─────────────────────────────────────────────────────────────────────────────
# MESSAGE LOG  (one record per recipient per campaign)
# ─────────────────────────────────────────────────────────────────────────────

class MessageLog(TenantModelMixin, models.Model):
    CHANNEL_CHOICES = [
        ('EMAIL', 'Email'),
        ('SMS',   'SMS'),
    ]
    STATUS_CHOICES = [
        ('PENDING',   'Pending'),
        ('SENT',      'Sent'),
        ('DELIVERED', 'Delivered'),
        ('FAILED',    'Failed'),
        ('BOUNCED',   'Bounced'),
        ('OPTED_OUT', 'Opted Out'),
    ]

    campaign        = models.ForeignKey(Campaign, on_delete=models.CASCADE,
                                        related_name='logs')
    student         = models.ForeignKey('users.Student', on_delete=models.CASCADE,
                                        related_name='message_logs')
    channel         = models.CharField(max_length=5, choices=CHANNEL_CHOICES)
    recipient       = models.CharField(max_length=200,
                                       help_text='Email address or phone number')
    subject         = models.CharField(max_length=300, blank=True)
    body            = models.TextField(blank=True)
    status          = models.CharField(max_length=10, choices=STATUS_CHOICES,
                                       default='PENDING')
    provider_ref    = models.CharField(max_length=200, blank=True,
                                       help_text='Message ID from email/SMS provider')
    error_message   = models.TextField(blank=True)
    sent_at         = models.DateTimeField(null=True, blank=True)
    created_at      = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return (f"{self.channel} → {self.recipient} "
                f"[{self.status}] — {self.campaign.title[:40]}")


# ─────────────────────────────────────────────────────────────────────────────
# QUICK MESSAGE  (one-off direct message to a single parent)
# ─────────────────────────────────────────────────────────────────────────────

class QuickMessage(TenantModelMixin, models.Model):
    CHANNEL_CHOICES = [
        ('EMAIL', 'Email'),
        ('SMS',   'SMS'),
        ('BOTH',  'Email + SMS'),
    ]
    STATUS_CHOICES = [
        ('SENT',   'Sent'),
        ('FAILED', 'Failed'),
    ]

    student     = models.ForeignKey('users.Student', on_delete=models.CASCADE,
                                    related_name='quick_messages')
    channel     = models.CharField(max_length=5, choices=CHANNEL_CHOICES)
    subject     = models.CharField(max_length=300, blank=True)
    body        = models.TextField()
    status      = models.CharField(max_length=7, choices=STATUS_CHOICES)
    sent_by     = models.ForeignKey(settings.AUTH_USER_MODEL,
                                    on_delete=models.SET_NULL,
                                    null=True, blank=True)
    error       = models.TextField(blank=True)
    sent_at     = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-sent_at']

    def __str__(self):
        return f"Quick {self.channel} → {self.student.full_name}"


# ─────────────────────────────────────────────────────────────────────────────
# OPT-OUT  (parent opts out of SMS or email)
# ─────────────────────────────────────────────────────────────────────────────

class OptOut(TenantModelMixin, models.Model):
    CHANNEL_CHOICES = [
        ('EMAIL', 'Email'),
        ('SMS',   'SMS'),
        ('ALL',   'All Channels'),
    ]

    student     = models.ForeignKey('users.Student', on_delete=models.CASCADE,
                                    related_name='opt_outs')
    channel     = models.CharField(max_length=5, choices=CHANNEL_CHOICES)
    opted_out_at = models.DateTimeField(auto_now_add=True)
    reason      = models.TextField(blank=True)

    class Meta:
        unique_together = ('student', 'channel')

    def __str__(self):
        return f"{self.student.full_name} opted out of {self.channel}"

# ─────────────────────────────────────────────────────────────────────────────
# SETTINGS, SURVEYS, CONSENT FORMS, BIRTHDAYS, EMERGENCY BROADCASTS
# ─────────────────────────────────────────────────────────────────────────────

class CommSettings(TenantModelMixin, models.Model):
    """One row per school."""
    require_approval = models.BooleanField(
        default=True, help_text='Campaigns from staff who are not administrators wait for approval.')
    birthday_students = models.BooleanField(default=False, help_text='Greet students on their birthday.')
    birthday_staff = models.BooleanField(default=False, help_text='Greet staff on their birthday.')
    birthday_student_sms = models.CharField(
        max_length=320, default='Happy birthday, {{student_name}}! Wishing you a wonderful year ahead. '
                                '- {{school_name}}')
    birthday_staff_sms = models.CharField(
        max_length=320, default='Happy birthday, {{name}}! Thank you for all you do. - {{school_name}}')
    # Parent–teacher chat (F242)
    chat_enabled = models.BooleanField(default=True)
    office_days = models.CharField(max_length=40, default='MON,TUE,WED,THU,FRI')
    office_start = models.TimeField(default=datetime.time(8, 0))
    office_end = models.TimeField(default=datetime.time(16, 0))
    # WhatsApp (F241): try WhatsApp first for text messages, fall back to SMS
    whatsapp_enabled = models.BooleanField(default=False)
    whatsapp_template = models.CharField(max_length=80, blank=True,
                                         help_text='Approved WhatsApp template with one body variable')
    whatsapp_language = models.CharField(max_length=10, default='en')
    signage_token = models.CharField(max_length=40, blank=True)
    signage_seconds = models.PositiveSmallIntegerField(default=12, help_text='Seconds each slide stays up')

    objects = TenantManager()

    def __str__(self):
        return f'Communication settings ({self.tenant_id})'


class Survey(TenantModelMixin, models.Model):
    PARENTS, STAFF, BOTH, STUDENTS = 'PARENTS', 'STAFF', 'BOTH', 'STUDENTS'
    AUDIENCES = [(PARENTS, 'Parents'), (STAFF, 'Staff'), (BOTH, 'Parents and staff'), (STUDENTS, 'Students')]
    title = models.CharField(max_length=160)
    description = models.TextField(blank=True)
    audience = models.CharField(max_length=8, choices=AUDIENCES, default=PARENTS)
    anonymous = models.BooleanField(default=True, help_text="Names are not shown with the answers")
    questions = models.JSONField(default=list, help_text='[{"text": "...", "kind": "CHOICE", "options": [...]}]')
    closes = models.DateField()
    published = models.BooleanField(default=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.title


class SurveyResponse(TenantModelMixin, models.Model):
    survey = models.ForeignKey(Survey, on_delete=models.CASCADE, related_name='responses')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='+')
    answers = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        unique_together = ('survey', 'user')


class ConsentForm(TenantModelMixin, models.Model):
    title = models.CharField(max_length=160)
    details = models.TextField(help_text='What parents are agreeing to: dates, place, transport, cost, what to bring')
    classes = models.ManyToManyField('users.Class', blank=True, related_name='+',
                                     help_text='Leave empty for the whole school')
    event = models.ForeignKey('curriculum.CalendarEvent', on_delete=models.SET_NULL, null=True, blank=True,
                              related_name='+')
    cost = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    deadline = models.DateField()
    closed = models.BooleanField(default=False)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.title


class ConsentResponse(TenantModelMixin, models.Model):
    YES, NO = 'YES', 'NO'
    form = models.ForeignKey(ConsentForm, on_delete=models.CASCADE, related_name='responses')
    student = models.ForeignKey('users.Student', on_delete=models.CASCADE, related_name='consent_responses')
    answer = models.CharField(max_length=3, choices=[(YES, 'I give consent'), (NO, 'I do not give consent')])
    signed_name = models.CharField(max_length=160, help_text='Typed full name, as a signature')
    signed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                  related_name='+')
    ip_address = models.CharField(max_length=45, blank=True)
    note = models.CharField(max_length=255, blank=True, help_text='Medical or other information for the trip')
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        unique_together = ('form', 'student')


class BirthdayGreeting(TenantModelMixin, models.Model):
    """One greeting per person per year, so a re-run never repeats it."""
    person = models.CharField(max_length=20, help_text='student:<id> or staff:<id>')
    year = models.PositiveSmallIntegerField()
    sent_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        unique_together = ('tenant', 'person', 'year')


class EmergencyBroadcast(TenantModelMixin, models.Model):
    subject = models.CharField(max_length=160)
    message = models.TextField()
    to_parents = models.BooleanField(default=True)
    to_staff = models.BooleanField(default=True)
    sms_sent = models.PositiveIntegerField(default=0)
    sms_failed = models.PositiveIntegerField(default=0)
    emails_sent = models.PositiveIntegerField(default=0)
    emails_failed = models.PositiveIntegerField(default=0)
    sent_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    objects = TenantManager()

    class Meta:
        ordering = ['-created_at']


# ─────────────────────────────────────────────────────────────────────────────
# PART 2: languages, preferences, chat, read reminders, SMS units, inbox
# ─────────────────────────────────────────────────────────────────────────────

LANGUAGES = [('en', 'English'), ('ha', 'Hausa'), ('yo', 'Yoruba'), ('ig', 'Igbo'), ('pcm', 'Nigerian Pidgin'),
             ('fr', 'French')]


class MessageTemplateTranslation(TenantModelMixin, models.Model):
    template = models.ForeignKey(MessageTemplate, on_delete=models.CASCADE, related_name='translations')
    language = models.CharField(max_length=4, choices=LANGUAGES)
    subject = models.CharField(max_length=300, blank=True)
    body_email = models.TextField(blank=True)
    body_sms = models.CharField(max_length=800, blank=True)
    ai_drafted = models.BooleanField(default=False)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='+')
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantManager()

    class Meta:
        unique_together = ('template', 'language')


class FamilyPreference(TenantModelMixin, models.Model):
    """What a family wants, keyed by the parent email shared by siblings (or student:<id>)."""
    OPTIONAL_TOPICS = [('GENERAL', 'School news and events'), ('SURVEYS', 'Surveys and polls'),
                       ('BIRTHDAY', 'Birthday greetings'), ('DIGEST', 'Daily summary of the school day')]
    key = models.CharField(max_length=160)
    language = models.CharField(max_length=4, choices=LANGUAGES, default='en')
    topics_off = models.JSONField(default=list)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='+')

    objects = TenantManager()

    class Meta:
        unique_together = ('tenant', 'key')


class Conversation(TenantModelMixin, models.Model):
    student = models.ForeignKey('users.Student', on_delete=models.CASCADE, related_name='conversations')
    teacher = models.ForeignKey('users.Staff', on_delete=models.CASCADE, related_name='conversations')
    parent = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='+')
    subject = models.CharField(max_length=160)
    closed = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    last_at = models.DateTimeField(default=timezone.now)

    objects = TenantManager()

    class Meta:
        ordering = ['-last_at']


class ChatMessage(TenantModelMixin, models.Model):
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name='messages')
    sender = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='+')
    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    read_at = models.DateTimeField(null=True, blank=True)
    hidden = models.BooleanField(default=False, help_text='Hidden by an administrator')
    hidden_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                  related_name='+')
    flagged = models.BooleanField(default=False)
    flag_reason = models.CharField(max_length=255, blank=True)

    objects = TenantManager()

    class Meta:
        ordering = ['created_at']


class AnnouncementReminder(TenantModelMixin, models.Model):
    announcement = models.ForeignKey('announcement.Announcement', on_delete=models.CASCADE, related_name='reminders')
    families = models.PositiveIntegerField(default=0)
    sent_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='+')
    sent_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        ordering = ['-sent_at']


class SmsWallet(TenantModelMixin, models.Model):
    """SMS units a school has bought (F246). One per school."""
    balance = models.IntegerField(default=0)
    used_total = models.PositiveIntegerField(default=0)
    low_alert_sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        constraints = [models.UniqueConstraint(fields=['tenant'], name='one_sms_wallet_per_school')]


class SmsUsageDay(TenantModelMixin, models.Model):
    date = models.DateField()
    units = models.PositiveIntegerField(default=0)
    messages = models.PositiveIntegerField(default=0)

    objects = TenantManager()

    class Meta:
        unique_together = ('tenant', 'date')
        ordering = ['-date']


class SmsTopUp(TenantModelMixin, models.Model):
    PENDING, PAID, FAILED = 'PENDING', 'PAID', 'FAILED'
    units = models.PositiveIntegerField()
    amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    reference = models.CharField(max_length=80, unique=True)
    kind = models.CharField(max_length=8, choices=[('PURCHASE', 'Bought'), ('GRANT', 'Free units')],
                            default='PURCHASE')
    status = models.CharField(max_length=7, choices=[(PENDING, 'Waiting for payment'), (PAID, 'Paid'),
                                                     (FAILED, 'Failed')], default=PENDING)
    paid_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        ordering = ['-created_at']


class InboundMessage(TenantModelMixin, models.Model):
    """A reply that came in by SMS or WhatsApp (F258)."""
    channel = models.CharField(max_length=3, choices=[('SMS', 'SMS'), ('WA', 'WhatsApp')])
    from_number = models.CharField(max_length=30)
    body = models.TextField()
    provider_id = models.CharField(max_length=120, blank=True)
    student = models.ForeignKey('users.Student', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    received_at = models.DateTimeField(auto_now_add=True)
    handled = models.BooleanField(default=False)
    handled_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='+')
    handled_at = models.DateTimeField(null=True, blank=True)

    objects = TenantManager()

    class Meta:
        ordering = ['-received_at']


class InboxReply(TenantModelMixin, models.Model):
    message = models.ForeignKey(InboundMessage, on_delete=models.CASCADE, related_name='replies')
    body = models.TextField()
    ok = models.BooleanField(default=False)
    error = models.CharField(max_length=255, blank=True)
    sent_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='+')
    sent_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        ordering = ['sent_at']
