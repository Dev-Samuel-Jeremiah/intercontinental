"""
schoolai/models.py
──────────────────
School intelligence: the school's long-term memory and what it learns from it.

  Memory     Snapshot — the school's vital signs, every day, kept for years.
             FeatureRow — each student's term in numbers (results, attendance,
             practice, labs, health, fees) and, once the next term is done,
             what actually happened next. This is the training data; it grows
             every term.
  Learning   ModelVersion — every model the school has trained, with its
             accuracy on the most recent term. A new version only replaces the
             active one if it does better (champion / challenger).
             Prediction — the active models' view of each student, with the
             reasons behind it. Staff only; never shown to students or parents.
             LearningRun — the nightly learning log.
  Knowledge  KnowledgeItem — what the school knows (announcements, calendar,
             fees, policies, FAQs, and answers staff have taught it).
             Question — every question asked, how it was answered, whether it
             helped. Unanswered and unhelpful ones go to administrators to
             teach, and the answers become knowledge.
"""
from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from tenants.managers import TenantManager, TenantModelMixin

USER = settings.AUTH_USER_MODEL


class Snapshot(TenantModelMixin, models.Model):
    date = models.DateField(default=timezone.localdate)
    metrics = models.JSONField(default=dict)
    objects = TenantManager()

    class Meta:
        ordering = ['-date']
        unique_together = ('tenant', 'date')


class FeatureRow(TenantModelMixin, models.Model):
    student = models.ForeignKey('users.Student', on_delete=models.CASCADE, related_name='ai_rows')
    term = models.ForeignKey('academics.Term', on_delete=models.CASCADE, related_name='+')
    order = models.PositiveIntegerField(default=0, help_text='Chronological position of the term')
    features = models.JSONField(default=dict)
    labels = models.JSONField(default=dict, blank=True, help_text='What happened the next term (empty until known)')
    updated_at = models.DateTimeField(auto_now=True)
    objects = TenantManager()

    class Meta:
        unique_together = ('tenant', 'student', 'term')
        indexes = [models.Index(fields=['tenant', 'order'])]


class ModelVersion(TenantModelMixin, models.Model):
    KINDS = [('NEXT_AVG', 'Next-term average'), ('FAIL_RISK', 'Risk of falling below the pass mark'),
             ('ENROLMENT', 'Enrolment forecast')]
    STATUSES = [('ACTIVE', 'Active'), ('RETIRED', 'Retired'), ('REJECTED', 'Not better — kept the active one')]
    kind = models.CharField(max_length=10, choices=KINDS)
    version = models.PositiveIntegerField()
    status = models.CharField(max_length=8, choices=STATUSES, default='REJECTED')
    trained_at = models.DateTimeField(auto_now_add=True)
    samples = models.PositiveIntegerField(default=0)
    terms = models.PositiveIntegerField(default=0)
    metrics = models.JSONField(default=dict)
    params = models.JSONField(default=dict)
    note = models.CharField(max_length=255, blank=True)
    objects = TenantManager()

    class Meta:
        ordering = ['-trained_at', '-id']
        unique_together = ('tenant', 'kind', 'version')


class Prediction(TenantModelMixin, models.Model):
    BANDS = [('HIGH', 'High'), ('MEDIUM', 'Medium'), ('LOW', 'Low')]
    student = models.ForeignKey('users.Student', on_delete=models.CASCADE, related_name='ai_predictions')
    kind = models.CharField(max_length=10, choices=ModelVersion.KINDS)
    model = models.ForeignKey(ModelVersion, on_delete=models.CASCADE, related_name='predictions')
    term = models.ForeignKey('academics.Term', on_delete=models.SET_NULL, null=True, related_name='+')
    value = models.FloatField()
    band = models.CharField(max_length=6, choices=BANDS, blank=True)
    reasons = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now=True)
    objects = TenantManager()

    class Meta:
        unique_together = ('tenant', 'student', 'kind')


class LearningRun(TenantModelMixin, models.Model):
    TRIGGERS = [('SCHEDULE', 'Nightly'), ('AUTO', 'Automatic catch-up'), ('MANUAL', 'Started by an administrator')]
    trigger = models.CharField(max_length=8, choices=TRIGGERS, default='SCHEDULE')
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    ok = models.BooleanField(default=False)
    steps = models.JSONField(default=list)
    error = models.TextField(blank=True)
    started_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    objects = TenantManager()

    class Meta:
        ordering = ['-started_at']


class KnowledgeItem(TenantModelMixin, models.Model):
    SOURCES = [('MANUAL', 'Written by staff'), ('ANSWER', 'Taught from a question'), ('ANNOUNCEMENT', 'Announcement'),
               ('CALENDAR', 'Calendar'), ('FEES', 'Fees'), ('SCHOOL', 'School facts'), ('POLICY', 'Policy'),
               ('HELP', 'Help article')]
    AUDIENCES = [('ALL', 'Everyone'), ('STAFF', 'Staff only'), ('FAMILY', 'Students and parents')]
    title = models.CharField(max_length=200)
    body = models.TextField()
    source = models.CharField(max_length=12, choices=SOURCES, default='MANUAL')
    source_key = models.CharField(max_length=60, blank=True, help_text='e.g. announcement:12 — kept in step automatically')
    audience = models.CharField(max_length=6, choices=AUDIENCES, default='ALL')
    keywords = models.CharField(max_length=255, blank=True, help_text='Other words people might use')
    active = models.BooleanField(default=True)
    helpful = models.PositiveIntegerField(default=0)
    unhelpful = models.PositiveIntegerField(default=0)
    uses = models.PositiveIntegerField(default=0)
    created_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    objects = TenantManager()

    class Meta:
        ordering = ['-updated_at']
        constraints = [models.UniqueConstraint(fields=['tenant', 'source_key'], condition=~Q(source_key=''),
                                               name='schoolai_one_item_per_source')]

    def __str__(self):
        return self.title


class AskedQuestion(TenantModelMixin, models.Model):
    STATUSES = [('ANSWERED', 'Answered'), ('UNSURE', 'Waiting for the school'), ('RESOLVED', 'Taught')]
    user = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    audience = models.CharField(max_length=6, choices=KnowledgeItem.AUDIENCES, default='ALL')
    text = models.CharField(max_length=500)
    answer = models.TextField(blank=True)
    sources = models.JSONField(default=list, blank=True)
    confidence = models.FloatField(default=0)
    used_ai = models.BooleanField(default=False)
    status = models.CharField(max_length=8, choices=STATUSES, default='ANSWERED')
    rating = models.SmallIntegerField(default=0, help_text='1 helpful, -1 not helpful, 0 not rated')
    resolved_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    resolved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    objects = TenantManager()

    class Meta:
        ordering = ['-created_at']
