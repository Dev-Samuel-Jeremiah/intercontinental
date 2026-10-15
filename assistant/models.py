"""
assistant/models.py
───────────────────
School AI settings (which provider each feature uses and a monthly
allowance), a log of every AI call for the usage dashboard, the review
queue for AI text that will reach parents, and approved translations of
announcements.
"""
from django.conf import settings
from django.db import models

from tenants.managers import TenantManager, TenantModelMixin

PROVIDERS = [('openai', 'OpenAI'), ('gemini', 'Google Gemini'), ('claude', 'Anthropic Claude')]
FEATURES = [
    ('lesson_note', 'Lesson note drafts'),
    ('questions', 'Question generator'),
    ('parent_message', 'Parent message drafts'),
    ('translation', 'Translating notices'),
    ('report_comments', 'Report card comments'),
    ('admin_query', 'Questions about school data'),
    ('early_warning', 'At-risk explanations'),
    ('study_plan', 'Study plans'),
    ('essay_feedback', 'Essay feedback'),
    ('tutor', 'Course tutor'),
    ('safety', 'Content safety checks'),
]


class AISettings(TenantModelMixin, models.Model):
    enabled = models.BooleanField(default=True)
    default_provider = models.CharField(max_length=10, choices=PROVIDERS, blank=True,
                                        help_text='Blank = the platform default')
    feature_providers = models.JSONField(default=dict, blank=True, help_text='{feature: provider}')
    monthly_tokens = models.PositiveIntegerField(null=True, blank=True,
                                                 help_text='Blank = the platform allowance')
    second_reviewer = models.BooleanField(
        default=False, help_text='AI messages to parents must be approved by someone other than the writer')
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantManager()

    class Meta:
        verbose_name_plural = 'AI settings'
        constraints = [models.UniqueConstraint(fields=['tenant'], name='one_ai_settings_per_school')]


class AIUsage(TenantModelMixin, models.Model):
    feature = models.CharField(max_length=20)
    provider = models.CharField(max_length=10)
    model = models.CharField(max_length=60, blank=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                             related_name='+')
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    ok = models.BooleanField(default=True)
    error = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    objects = TenantManager()

    class Meta:
        ordering = ['-created_at']

    @property
    def tokens(self):
        return self.input_tokens + self.output_tokens


class AIDraft(TenantModelMixin, models.Model):
    """AI-written text that will reach parents. Nothing is sent until a staff member approves it."""
    PENDING, SENT, REJECTED = 'PENDING', 'SENT', 'REJECTED'
    STATUSES = [(PENDING, 'Waiting for review'), (SENT, 'Approved and sent'), (REJECTED, 'Rejected')]
    CHANNELS = [('EMAIL', 'Email'), ('SMS', 'SMS'), ('BOTH', 'Email and SMS')]
    PURPOSES = [('FEES', 'Fees'), ('DISCIPLINE', 'Behaviour'), ('ATTENDANCE', 'Attendance'),
                ('ACADEMIC', 'Schoolwork'), ('HEALTH', 'Wellbeing'), ('PRAISE', 'Praise'), ('OTHER', 'Other')]
    student = models.ForeignKey('users.Student', on_delete=models.CASCADE, related_name='ai_drafts')
    purpose = models.CharField(max_length=10, choices=PURPOSES, default='OTHER')
    language = models.CharField(max_length=10, default='en')
    channel = models.CharField(max_length=5, choices=CHANNELS, default='EMAIL')
    brief = models.TextField(help_text='What the writer asked for')
    subject = models.CharField(max_length=200)
    body = models.TextField()
    status = models.CharField(max_length=8, choices=STATUSES, default=PENDING)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                   related_name='+')
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name='+')
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_note = models.CharField(max_length=255, blank=True)
    send_error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        ordering = ['-created_at']


class AnnouncementTranslation(TenantModelMixin, models.Model):
    LANGUAGES = [('ha', 'Hausa'), ('yo', 'Yoruba'), ('ig', 'Igbo'), ('pcm', 'Nigerian Pidgin'), ('fr', 'French')]
    announcement = models.ForeignKey('announcement.Announcement', on_delete=models.CASCADE,
                                     related_name='translations')
    language = models.CharField(max_length=4, choices=LANGUAGES)
    title = models.CharField(max_length=255)
    message = models.TextField()
    approved = models.BooleanField(default=False)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name='+')
    approved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        unique_together = ('announcement', 'language')
        ordering = ['language']


class TutorSettings(TenantModelMixin, models.Model):
    """The teacher's limits for the AI tutor in one course."""
    course = models.OneToOneField('classroom.Course', on_delete=models.CASCADE, related_name='tutor_settings')
    enabled = models.BooleanField(default=False)
    daily_limit = models.PositiveSmallIntegerField(default=20, help_text='Questions per student per day')
    instructions = models.TextField(blank=True, help_text='e.g. "Don\'t solve Assignment 3; give hints only."')
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantManager()


class TutorMessage(TenantModelMixin, models.Model):
    USER, TUTOR = 'user', 'assistant'
    course = models.ForeignKey('classroom.Course', on_delete=models.CASCADE, related_name='tutor_messages')
    student = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='tutor_messages')
    role = models.CharField(max_length=9, choices=[(USER, 'Student'), (TUTOR, 'Tutor')])
    text = models.TextField()
    held = models.BooleanField(default=False, help_text='Stopped by the safety check; the teacher was told')
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        ordering = ['created_at', 'id']


class EssayFeedback(TenantModelMixin, models.Model):
    student = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='essay_feedback')
    assignment = models.ForeignKey('classroom.Assignment', on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='+')
    title = models.CharField(max_length=200, blank=True)
    text = models.TextField()
    feedback = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        ordering = ['-created_at']


class StudyPlan(TenantModelMixin, models.Model):
    student = models.ForeignKey('users.Student', on_delete=models.CASCADE, related_name='study_plans')
    plan = models.JSONField(default=dict)
    focus = models.JSONField(default=list, help_text='The weak topics and subjects it was built from')
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                     related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        ordering = ['-created_at']


class AdminQuery(TenantModelMixin, models.Model):
    """A plain-English question an administrator asked, and which safe report answered it."""
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='+')
    question = models.CharField(max_length=500)
    tool = models.CharField(max_length=40, blank=True)
    params = models.JSONField(default=dict)
    rows = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        ordering = ['-created_at']
