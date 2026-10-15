"""
teaching/models.py
──────────────────
Teacher productivity: gradebook, behaviour points, comment bank, resource
library, task list, duty roster and handover notes.
"""
from decimal import Decimal

from django.conf import settings
from django.db import models

from tenants.managers import TenantManager, TenantModelMixin

USER = settings.AUTH_USER_MODEL


def _private():
    from users.models import PrivateStorage
    return PrivateStorage()


def _upload(instance, filename):
    import os
    import uuid
    ext = os.path.splitext(filename)[1].lower()[:8]
    return f"teaching/{instance.tenant_id}/{instance.__class__.__name__.lower()}/{uuid.uuid4().hex}{ext}"


# ── F304 Gradebook ──────────────────────────────────────────────────────────

CATEGORIES = [('CLASSWORK', 'Classwork'), ('HOMEWORK', 'Homework'), ('TEST', 'Tests'), ('PROJECT', 'Projects'),
              ('PRACTICAL', 'Practicals')]


class Gradebook(TenantModelMixin, models.Model):
    """One per class, subject and term, with category weights and where each category goes on the report card."""
    school_class = models.ForeignKey('users.Class', on_delete=models.CASCADE, related_name='+')
    subject = models.ForeignKey('users.Subject', on_delete=models.CASCADE, related_name='+')
    term = models.ForeignKey('academics.Term', on_delete=models.CASCADE, related_name='+')
    teacher = models.ForeignKey('users.Staff', on_delete=models.SET_NULL, null=True, related_name='gradebooks')
    weights = models.JSONField(default=dict, help_text='{"TEST": 50, "HOMEWORK": 30, ...}')
    report_columns = models.JSONField(default=dict, help_text='{"TEST": "ca1", "HOMEWORK": "ca2"}')
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        unique_together = ('school_class', 'subject', 'term')


class GradeItem(TenantModelMixin, models.Model):
    gradebook = models.ForeignKey(Gradebook, on_delete=models.CASCADE, related_name='items')
    title = models.CharField(max_length=120)
    category = models.CharField(max_length=9, choices=CATEGORIES, default='CLASSWORK')
    max_score = models.DecimalField(max_digits=6, decimal_places=1, default=Decimal('10'))
    date = models.DateField()
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        ordering = ['date', 'id']


class GradeMark(TenantModelMixin, models.Model):
    item = models.ForeignKey(GradeItem, on_delete=models.CASCADE, related_name='marks')
    student = models.ForeignKey('users.Student', on_delete=models.CASCADE, related_name='+')
    score = models.DecimalField(max_digits=6, decimal_places=1, null=True, blank=True)
    excused = models.BooleanField(default=False, help_text='Not counted (absent with a reason)')

    objects = TenantManager()

    class Meta:
        unique_together = ('item', 'student')


# ── F305 Behaviour points ───────────────────────────────────────────────────

class BehaviourEntry(TenantModelMixin, models.Model):
    POSITIVE = [('KIND', 'Kindness'), ('EFFORT', 'Great effort'), ('HELP', 'Helping others'),
                ('LEAD', 'Leadership'), ('PROGRESS', 'Big improvement'), ('HOMEWORK', 'Excellent homework')]
    NEGATIVE = [('LATE', 'Late to class'), ('NOHW', 'Homework not done'), ('DISRUPT', 'Disrupting the class'),
                ('UNIFORM', 'Uniform'), ('PHONE', 'Phone in class'), ('RUDE', 'Disrespect')]
    student = models.ForeignKey('users.Student', on_delete=models.CASCADE, related_name='behaviour')
    staff = models.ForeignKey('users.Staff', on_delete=models.SET_NULL, null=True, related_name='+')
    category = models.CharField(max_length=10, choices=POSITIVE + NEGATIVE)
    points = models.SmallIntegerField()
    note = models.CharField(max_length=255, blank=True)
    date = models.DateField()
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        ordering = ['-date', '-id']


# ── F308 Comment bank ───────────────────────────────────────────────────────

class CommentTemplate(TenantModelMixin, models.Model):
    BANDS = [('EXCELLENT', 'Excellent'), ('GOOD', 'Good'), ('FAIR', 'Fair'), ('WEAK', 'Needs to work harder'),
             ('CONDUCT', 'Conduct')]
    staff = models.ForeignKey('users.Staff', on_delete=models.CASCADE, null=True, blank=True, related_name='+',
                              help_text='Empty = shared with the whole school')
    band = models.CharField(max_length=9, choices=BANDS, default='GOOD')
    text = models.CharField(max_length=255, help_text='Use {first} for the first name')
    uses = models.PositiveIntegerField(default=0)

    objects = TenantManager()

    class Meta:
        ordering = ['band', '-uses', 'id']


# ── F309 Resource library ───────────────────────────────────────────────────

class Resource(TenantModelMixin, models.Model):
    # Own reverse name: another app has a model with the same name, and both
    # can't be Tenant.resource_set.
    tenant = models.ForeignKey('tenants.Tenant', on_delete=models.CASCADE, db_index=True,
                               null=True, blank=True, related_name='teaching_resources')
    KINDS = [('WORKSHEET', 'Worksheet'), ('SLIDES', 'Slides'), ('NOTES', 'Lesson notes'), ('PAST', 'Past questions'),
             ('VIDEO', 'Video'), ('LINK', 'Website'), ('OTHER', 'Other')]
    title = models.CharField(max_length=160)
    subject = models.ForeignKey('users.Subject', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    level = models.CharField(max_length=40, blank=True, help_text='e.g. JSS 1, SS 2')
    kind = models.CharField(max_length=9, choices=KINDS, default='WORKSHEET')
    description = models.TextField(blank=True)
    file = models.FileField(upload_to=_upload, storage=_private, blank=True, null=True)
    link = models.URLField(blank=True)
    shared = models.BooleanField(default=True, help_text='Other teachers can find it')
    uploaded_by = models.ForeignKey('users.Staff', on_delete=models.SET_NULL, null=True, related_name='+')
    downloads = models.PositiveIntegerField(default=0)
    saved_by = models.ManyToManyField('users.Staff', blank=True, related_name='saved_resources')
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        ordering = ['-created_at']


# ── F311 Task list ──────────────────────────────────────────────────────────

class TeacherTask(TenantModelMixin, models.Model):
    staff = models.ForeignKey('users.Staff', on_delete=models.CASCADE, related_name='tasks')
    title = models.CharField(max_length=200)
    notes = models.TextField(blank=True)
    due_date = models.DateField(null=True, blank=True)
    priority = models.CharField(max_length=6, choices=[('HIGH', 'High'), ('NORMAL', 'Normal'), ('LOW', 'Low')],
                                default='NORMAL')
    done = models.BooleanField(default=False)
    done_at = models.DateTimeField(null=True, blank=True)
    assigned_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        ordering = ['done', 'due_date', '-created_at']


# ── F312 Duty roster ────────────────────────────────────────────────────────

class DutyPost(TenantModelMixin, models.Model):
    name = models.CharField(max_length=100, help_text='e.g. Gate (morning), Dining hall, Break — field')
    location = models.CharField(max_length=100, blank=True)
    start_time = models.TimeField()
    end_time = models.TimeField()
    staff_needed = models.PositiveSmallIntegerField(default=1)
    active = models.BooleanField(default=True)

    objects = TenantManager()

    class Meta:
        ordering = ['start_time', 'name']


class DutyAssignment(TenantModelMixin, models.Model):
    post = models.ForeignKey(DutyPost, on_delete=models.CASCADE, related_name='assignments')
    staff = models.ForeignKey('users.Staff', on_delete=models.CASCADE, related_name='duties')
    date = models.DateField()
    reminded = models.BooleanField(default=False)

    objects = TenantManager()

    class Meta:
        unique_together = ('post', 'staff', 'date')
        ordering = ['date', 'post__start_time']


# ── F313 Handover notes ─────────────────────────────────────────────────────

class HandoverNote(TenantModelMixin, models.Model):
    KINDS = [('COVER', 'Cover while I am away'), ('CLASS', 'New form teacher / end of year'),
             ('SUBJECT', 'Taking over a subject'), ('OTHER', 'Other')]
    kind = models.CharField(max_length=7, choices=KINDS, default='COVER')
    from_staff = models.ForeignKey('users.Staff', on_delete=models.CASCADE, related_name='handovers_given')
    to_staff = models.ForeignKey('users.Staff', on_delete=models.CASCADE, related_name='handovers_received')
    school_class = models.ForeignKey('users.Class', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    subject = models.ForeignKey('users.Subject', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    start = models.DateField(null=True, blank=True)
    end = models.DateField(null=True, blank=True)
    covered = models.TextField(blank=True, help_text='What has been taught or done')
    next_steps = models.TextField(blank=True, help_text='What comes next')
    students = models.TextField(blank=True, help_text='Students to watch, needs, behaviour')
    other = models.TextField(blank=True)
    file = models.FileField(upload_to=_upload, storage=_private, blank=True, null=True)
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        ordering = ['-created_at']
