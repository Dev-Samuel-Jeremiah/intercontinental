"""
studentlife/models.py
─────────────────────
The student experience: opt-in leaderboards, e-portfolio, career guidance,
the digital handbook, anonymous reports, wellbeing check-ins, peer tutoring,
goals and shared notes.
"""
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
    return f"studentlife/{instance.tenant_id}/{instance.__class__.__name__.lower()}/{uuid.uuid4().hex}{ext}"


class LifeSettings(TenantModelMixin, models.Model):
    leaderboards = models.BooleanField(default=True)
    notes_need_approval = models.BooleanField(default=True, help_text='A teacher checks shared notes first')
    tutoring = models.BooleanField(default=True)
    handbook_version = models.PositiveIntegerField(default=1)
    safeguarding_leads = models.ManyToManyField('users.Staff', blank=True, related_name='+',
                                                help_text='Receive anonymous reports and wellbeing alerts')

    objects = TenantManager()


class LeaderboardEntry(TenantModelMixin, models.Model):
    """A student who chose to appear on leaderboards."""
    student = models.OneToOneField('users.Student', on_delete=models.CASCADE, related_name='leaderboard')
    nickname = models.CharField(max_length=40, blank=True, help_text='Shown instead of your name if set')
    joined_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()


class PortfolioItem(TenantModelMixin, models.Model):
    KINDS = [('PROJECT', 'Project'), ('ESSAY', 'Essay or writing'), ('ART', 'Art or design'),
             ('CERT', 'Certificate'), ('COMPETITION', 'Competition'), ('SERVICE', 'Community service'),
             ('OTHER', 'Other')]
    student = models.ForeignKey('users.Student', on_delete=models.CASCADE, related_name='portfolio')
    title = models.CharField(max_length=160)
    kind = models.CharField(max_length=11, choices=KINDS, default='PROJECT')
    description = models.TextField(blank=True)
    date = models.DateField()
    file = models.FileField(upload_to=_upload, storage=_private, blank=True, null=True)
    link = models.URLField(blank=True)
    share_with_family = models.BooleanField(default=True)
    featured = models.BooleanField(default=False)
    teacher_comment = models.TextField(blank=True)
    commented_by = models.ForeignKey('users.Staff', on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        ordering = ['-featured', '-date']


class CareerProfile(TenantModelMixin, models.Model):
    student = models.OneToOneField('users.Student', on_delete=models.CASCADE, related_name='career')
    answers = models.JSONField(default=dict, help_text='Interest quiz answers')
    clusters = models.JSONField(default=list, help_text='Top interest areas from the quiz')
    chosen = models.JSONField(default=list, help_text='Careers the student is considering')
    notes = models.TextField(blank=True, help_text="The student's own thoughts")
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantManager()


class CareerNote(TenantModelMixin, models.Model):
    student = models.ForeignKey('users.Student', on_delete=models.CASCADE, related_name='career_notes')
    staff = models.ForeignKey('users.Staff', on_delete=models.SET_NULL, null=True, related_name='+')
    note = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        ordering = ['-created_at']


class HandbookSection(TenantModelMixin, models.Model):
    title = models.CharField(max_length=160)
    body = models.TextField()
    order = models.PositiveSmallIntegerField(default=0)
    published = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantManager()

    class Meta:
        ordering = ['order', 'id']


class HandbookAck(TenantModelMixin, models.Model):
    user = models.ForeignKey(USER, on_delete=models.CASCADE, related_name='+')
    version = models.PositiveIntegerField()
    acknowledged_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        unique_together = ('tenant', 'user', 'version')


class SafetyReport(TenantModelMixin, models.Model):
    KINDS = [('BULLYING', 'Bullying'), ('CYBER', 'Online bullying'), ('HARASSMENT', 'Harassment or abuse'),
             ('SAFETY', 'Something unsafe'), ('WELLBEING', 'Worried about a friend'), ('OTHER', 'Something else')]
    NEW, REVIEWING, ACTIONED, CLOSED = 'NEW', 'REVIEWING', 'ACTIONED', 'CLOSED'
    STATUSES = [(NEW, 'Received'), (REVIEWING, 'Being looked into'), (ACTIONED, 'Action taken'), (CLOSED, 'Closed')]
    reference = models.CharField(max_length=12)
    code_hash = models.CharField(max_length=64, help_text='Lets an anonymous reporter check back')
    kind = models.CharField(max_length=10, choices=KINDS, default='BULLYING')
    where = models.CharField(max_length=160, blank=True)
    when = models.CharField(max_length=120, blank=True)
    who = models.CharField(max_length=255, blank=True, help_text='Who was involved, if known')
    description = models.TextField()
    reporter = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
                                 help_text='Empty when the report is anonymous')
    status = models.CharField(max_length=9, choices=STATUSES, default=NEW)
    urgent = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantManager()

    class Meta:
        ordering = ['-created_at']
        unique_together = ('tenant', 'reference')


class ReportMessage(TenantModelMixin, models.Model):
    report = models.ForeignKey(SafetyReport, on_delete=models.CASCADE, related_name='messages')
    from_school = models.BooleanField(default=True)
    author = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    body = models.TextField()
    internal = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        ordering = ['created_at']


class Checkin(TenantModelMixin, models.Model):
    MOODS = [(5, 'Great'), (4, 'Good'), (3, 'Okay'), (2, 'Low'), (1, 'Very low')]
    student = models.ForeignKey('users.Student', on_delete=models.CASCADE, related_name='checkins')
    date = models.DateField()
    mood = models.PositiveSmallIntegerField(choices=MOODS)
    sleep = models.PositiveSmallIntegerField(null=True, blank=True, help_text='Hours of sleep')
    feelings = models.JSONField(default=list)
    note = models.CharField(max_length=500, blank=True)
    wants_talk = models.BooleanField(default=False)
    flagged = models.BooleanField(default=False)
    followed_up_by = models.ForeignKey('users.Staff', on_delete=models.SET_NULL, null=True, blank=True,
                                       related_name='+')
    followed_up_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        unique_together = ('student', 'date')
        ordering = ['-date']


class TutorOffer(TenantModelMixin, models.Model):
    student = models.ForeignKey('users.Student', on_delete=models.CASCADE, related_name='tutor_offers')
    subject = models.ForeignKey('users.Subject', on_delete=models.CASCADE, related_name='+')
    about = models.CharField(max_length=255, blank=True, help_text='Topics you can help with')
    when = models.CharField(max_length=120, blank=True, help_text='e.g. Break time, Tuesdays after school')
    active = models.BooleanField(default=True)
    approved = models.BooleanField(default=False, help_text='A teacher has approved this tutor')
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        unique_together = ('student', 'subject')


class TutorSession(TenantModelMixin, models.Model):
    REQUESTED, ACCEPTED, DONE, DECLINED, CANCELLED = 'REQUESTED', 'ACCEPTED', 'DONE', 'DECLINED', 'CANCELLED'
    STATUSES = [(REQUESTED, 'Requested'), (ACCEPTED, 'Agreed'), (DONE, 'Held'), (DECLINED, 'Declined'),
                (CANCELLED, 'Cancelled')]
    offer = models.ForeignKey(TutorOffer, on_delete=models.CASCADE, related_name='sessions')
    learner = models.ForeignKey('users.Student', on_delete=models.CASCADE, related_name='tutoring_received')
    topic = models.CharField(max_length=160)
    proposed = models.CharField(max_length=120, blank=True, help_text='When and where (at school)')
    status = models.CharField(max_length=9, choices=STATUSES, default=REQUESTED)
    rating = models.PositiveSmallIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantManager()

    class Meta:
        ordering = ['-created_at']


class Goal(TenantModelMixin, models.Model):
    CATEGORIES = [('ACADEMIC', 'Studies'), ('SKILL', 'A new skill'), ('SPORT', 'Sport and health'),
                  ('CHARACTER', 'Character and habits'), ('OTHER', 'Other')]
    ACTIVE, ACHIEVED, DROPPED = 'ACTIVE', 'ACHIEVED', 'DROPPED'
    student = models.ForeignKey('users.Student', on_delete=models.CASCADE, related_name='goals')
    title = models.CharField(max_length=160)
    category = models.CharField(max_length=9, choices=CATEGORIES, default='ACADEMIC')
    measure = models.CharField(max_length=255, blank=True, help_text='How you will know you got there')
    target_date = models.DateField(null=True, blank=True)
    progress = models.PositiveSmallIntegerField(default=0)
    status = models.CharField(max_length=8, choices=[(ACTIVE, 'Working on it'), (ACHIEVED, 'Achieved'),
                                                     (DROPPED, 'Dropped')], default=ACTIVE)
    teacher_comment = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    achieved_at = models.DateTimeField(null=True, blank=True)

    objects = TenantManager()

    class Meta:
        ordering = ['status', 'target_date', '-created_at']


class GoalUpdate(TenantModelMixin, models.Model):
    goal = models.ForeignKey(Goal, on_delete=models.CASCADE, related_name='updates')
    progress = models.PositiveSmallIntegerField()
    note = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        ordering = ['-created_at']


class SharedNote(TenantModelMixin, models.Model):
    PENDING, APPROVED, HIDDEN = 'PENDING', 'APPROVED', 'HIDDEN'
    author = models.ForeignKey('users.Student', on_delete=models.CASCADE, related_name='shared_notes')
    subject = models.ForeignKey('users.Subject', on_delete=models.CASCADE, related_name='+')
    level = models.CharField(max_length=40, help_text='Year group the notes are for, e.g. JSS 2')
    title = models.CharField(max_length=160)
    body = models.TextField(blank=True)
    file = models.FileField(upload_to=_upload, storage=_private, blank=True, null=True)
    status = models.CharField(max_length=8, choices=[(PENDING, 'Waiting for a teacher'), (APPROVED, 'Shared'),
                                                     (HIDDEN, 'Hidden')], default=PENDING)
    reviewed_by = models.ForeignKey('users.Staff', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    reports = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        ordering = ['-created_at']


class NoteLike(TenantModelMixin, models.Model):
    note = models.ForeignKey(SharedNote, on_delete=models.CASCADE, related_name='likes')
    student = models.ForeignKey('users.Student', on_delete=models.CASCADE, related_name='+')

    objects = TenantManager()

    class Meta:
        unique_together = ('note', 'student')
