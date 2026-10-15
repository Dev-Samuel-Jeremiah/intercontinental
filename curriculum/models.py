"""
curriculum/models.py
────────────────────
Departments and their heads, schemes of work, weekly lesson notes and their
review, homework, and the school calendar.
"""
from django.conf import settings
from django.db import models
from django.utils import timezone

from tenants.managers import TenantManager, TenantModelMixin


def _private():
    from users.models import PrivateStorage
    return PrivateStorage()


def _upload(instance, filename):
    import os
    import uuid
    ext = os.path.splitext(filename)[1].lower()[:8]
    return f"curriculum/{instance.tenant_id}/{instance.__class__.__name__.lower()}/{uuid.uuid4().hex}{ext}"


class Department(TenantModelMixin, models.Model):
    """Sciences, Languages, Humanities… — a group of subjects with a head."""
    name = models.CharField(max_length=80)
    hod = models.ForeignKey('users.Staff', on_delete=models.SET_NULL, null=True, blank=True,
                            related_name='departments_led', verbose_name='Head of department')
    subjects = models.ManyToManyField('users.Subject', blank=True, related_name='departments')
    description = models.CharField(max_length=255, blank=True)
    objects = TenantManager()

    class Meta:
        ordering = ['name']
        unique_together = ('tenant', 'name')

    def __str__(self):
        return self.name


class SchemeOfWork(TenantModelMixin, models.Model):
    """The plan of topics, week by week, for one subject in one class for a term."""
    subject = models.ForeignKey('users.Subject', on_delete=models.CASCADE, related_name='schemes')
    school_class = models.ForeignKey('users.Class', on_delete=models.CASCADE, related_name='schemes')
    term = models.ForeignKey('academics.Term', on_delete=models.CASCADE, related_name='schemes')
    weeks = models.PositiveSmallIntegerField(default=12)
    created_by = models.ForeignKey('users.Staff', on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='schemes_written')
    approved_by = models.ForeignKey('users.Staff', on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name='schemes_approved')
    approved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    objects = TenantManager()

    class Meta:
        unique_together = ('tenant', 'subject', 'school_class', 'term')
        ordering = ['school_class__name', 'subject__name']

    def __str__(self):
        return f"{self.subject} — {self.school_class} ({self.term})"


class SchemeTopic(TenantModelMixin, models.Model):
    scheme = models.ForeignKey(SchemeOfWork, on_delete=models.CASCADE, related_name='topics')
    week = models.PositiveSmallIntegerField()
    order = models.PositiveSmallIntegerField(default=1)
    topic = models.CharField(max_length=200)
    subtopics = models.TextField(blank=True)
    objectives = models.TextField(blank=True, help_text='By the end of the lesson students should be able to…')
    resources = models.CharField(max_length=255, blank=True)
    taught_on = models.DateField(null=True, blank=True)
    taught_by = models.ForeignKey('users.Staff', on_delete=models.SET_NULL, null=True, blank=True,
                                  related_name='+')
    objects = TenantManager()

    class Meta:
        ordering = ['week', 'order']

    def __str__(self):
        return f"Week {self.week}: {self.topic}"

    @property
    def taught(self):
        return self.taught_on is not None


class LessonNote(TenantModelMixin, models.Model):
    """A teacher's lesson note for a week, reviewed by the head of department."""
    DRAFT, SUBMITTED, APPROVED, RETURNED = 'DRAFT', 'SUBMITTED', 'APPROVED', 'RETURNED'
    STATUSES = [(DRAFT, 'Draft'), (SUBMITTED, 'Waiting for review'), (APPROVED, 'Approved'),
                (RETURNED, 'Sent back for changes')]

    teacher = models.ForeignKey('users.Staff', on_delete=models.CASCADE, related_name='lesson_notes')
    subject = models.ForeignKey('users.Subject', on_delete=models.CASCADE, related_name='lesson_notes')
    school_class = models.ForeignKey('users.Class', on_delete=models.CASCADE, related_name='lesson_notes')
    term = models.ForeignKey('academics.Term', on_delete=models.CASCADE, related_name='lesson_notes')
    week = models.PositiveSmallIntegerField()
    topic = models.ForeignKey(SchemeTopic, on_delete=models.SET_NULL, null=True, blank=True,
                              related_name='lesson_notes')
    title = models.CharField(max_length=200)
    objectives = models.TextField(blank=True)
    content = models.TextField(help_text='Introduction, presentation steps, evaluation, assignment.')
    attachment = models.FileField(upload_to=_upload, storage=_private, blank=True, null=True)
    status = models.CharField(max_length=9, choices=STATUSES, default=DRAFT)
    submitted_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey('users.Staff', on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name='lesson_notes_reviewed')
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_comment = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    objects = TenantManager()

    class Meta:
        ordering = ['-term__start_date', '-week', 'school_class__name']

    def __str__(self):
        return f"{self.subject} {self.school_class} week {self.week}: {self.title}"


class Homework(TenantModelMixin, models.Model):
    school_class = models.ForeignKey('users.Class', on_delete=models.CASCADE, related_name='homework')
    subject = models.ForeignKey('users.Subject', on_delete=models.CASCADE, related_name='homework')
    set_by = models.ForeignKey('users.Staff', on_delete=models.SET_NULL, null=True, blank=True,
                               related_name='homework_set')
    title = models.CharField(max_length=160)
    details = models.TextField(blank=True)
    set_on = models.DateField(default=timezone.localdate)
    due_on = models.DateField()
    attachment = models.FileField(upload_to=_upload, storage=_private, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    objects = TenantManager()

    class Meta:
        ordering = ['due_on', 'subject__name']

    def __str__(self):
        return f"{self.school_class} {self.subject}: {self.title}"


class CalendarEvent(TenantModelMixin, models.Model):
    KINDS = [('EXAM', 'Exams'), ('MIDTERM', 'Mid-term break'), ('HOLIDAY', 'Holiday'),
             ('PTA', 'PTA meeting'), ('SPORTS', 'Sports'), ('CULTURAL', 'Cultural day'),
             ('TRIP', 'Excursion'), ('MEETING', 'Staff meeting'), ('RESUMPTION', 'Resumption'),
             ('OTHER', 'Other')]
    AUDIENCES = [('ALL', 'Everyone'), ('STAFF', 'Staff only'), ('PARENTS', 'Parents and students')]
    COLOURS = {'EXAM': '#ef4444', 'MIDTERM': '#60a5fa', 'HOLIDAY': '#22c55e', 'PTA': '#f4d21f',
               'SPORTS': '#f59e0b', 'CULTURAL': '#a855f7', 'TRIP': '#14b8a6', 'MEETING': '#94a3b8',
               'RESUMPTION': '#ffe066', 'OTHER': '#94a3b8'}

    title = models.CharField(max_length=160)
    kind = models.CharField(max_length=10, choices=KINDS, default='OTHER')
    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True)
    audience = models.CharField(max_length=7, choices=AUDIENCES, default='ALL')
    description = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                   blank=True, related_name='+')
    # Time, place, replies and reminders (all optional)
    start_time = models.TimeField(null=True, blank=True)
    end_time = models.TimeField(null=True, blank=True)
    venue = models.CharField(max_length=120, blank=True)
    rsvp = models.BooleanField(default=False, help_text='Ask people whether they are coming')
    rsvp_deadline = models.DateField(null=True, blank=True)
    capacity = models.PositiveIntegerField(null=True, blank=True, help_text='Most people who can come, guests included')
    reminder_days = models.PositiveSmallIntegerField(default=0, help_text='Email a reminder this many days before (0 = none)')
    reminded_at = models.DateTimeField(null=True, blank=True)
    objects = TenantManager()

    class Meta:
        ordering = ['start_date']

    def __str__(self):
        return self.title

    @property
    def last_day(self):
        return self.end_date or self.start_date

    @property
    def rsvp_open(self):
        today = timezone.localdate()
        return self.rsvp and today <= (self.rsvp_deadline or self.start_date)

    @property
    def colour(self):
        return self.COLOURS.get(self.kind, '#94a3b8')


class EventRSVP(TenantModelMixin, models.Model):
    """One person's reply to an event invitation."""
    YES, MAYBE, NO = 'YES', 'MAYBE', 'NO'
    ANSWERS = [(YES, 'Coming'), (MAYBE, 'Maybe'), (NO, 'Not coming')]
    event = models.ForeignKey(CalendarEvent, on_delete=models.CASCADE, related_name='rsvps')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='event_rsvps')
    answer = models.CharField(max_length=5, choices=ANSWERS)
    guests = models.PositiveSmallIntegerField(default=0, help_text='People coming with them')
    note = models.CharField(max_length=255, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    objects = TenantManager()

    class Meta:
        unique_together = ('event', 'user')
        ordering = ['-updated_at']

    @property
    def headcount(self):
        return 1 + self.guests if self.answer == self.YES else 0


# ─────────────────────────────────────────────────────────────────────────────
# Subject streams (senior secondary)
# ─────────────────────────────────────────────────────────────────────────────

class Stream(TenantModelMixin, models.Model):
    """Science, Arts, Commercial… — compulsory subjects plus electives to choose from."""
    name = models.CharField(max_length=60)
    levels = models.CharField(max_length=120, blank=True,
                              help_text='Class levels it applies to, comma-separated, e.g. "SS 1, SS 2, SS 3". '
                                        'Empty means any class.')
    compulsory = models.ManyToManyField('users.Subject', blank=True, related_name='compulsory_in_streams')
    electives = models.ManyToManyField('users.Subject', blank=True, related_name='elective_in_streams')
    min_electives = models.PositiveSmallIntegerField(default=1)
    max_electives = models.PositiveSmallIntegerField(default=3)
    min_subjects = models.PositiveSmallIntegerField(default=8, help_text='WAEC allows 7 to 9 subjects.')
    max_subjects = models.PositiveSmallIntegerField(default=9)
    description = models.CharField(max_length=255, blank=True)
    objects = TenantManager()

    class Meta:
        ordering = ['name']
        unique_together = ('tenant', 'name')

    def __str__(self):
        return self.name

    @property
    def level_list(self):
        return [lv.strip().lower() for lv in self.levels.split(',') if lv.strip()]

    def applies_to(self, school_class):
        if not self.level_list:
            return True
        names = {(school_class.level or '').strip().lower(), school_class.name.strip().lower()}
        import re
        return any(re.match(re.escape(lv) + r'(?!\d)', n) for lv in self.level_list for n in names if n)


class StudentStream(TenantModelMixin, models.Model):
    student = models.ForeignKey('users.Student', on_delete=models.CASCADE, related_name='stream_choices')
    stream = models.ForeignKey(Stream, on_delete=models.CASCADE, related_name='students')
    electives = models.ManyToManyField('users.Subject', blank=True, related_name='+')
    chosen_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                  related_name='+')
    chosen_at = models.DateTimeField(auto_now=True)
    objects = TenantManager()

    class Meta:
        unique_together = ('tenant', 'student')

    def __str__(self):
        return f"{self.student} — {self.stream}"


# ─────────────────────────────────────────────────────────────────────────────
# Promotion
# ─────────────────────────────────────────────────────────────────────────────

class PromotionPolicy(TenantModelMixin, models.Model):
    """The school's end-of-session promotion rules."""
    pass_average = models.DecimalField(max_digits=5, decimal_places=2, default=50,
                                       help_text='Overall average needed to be promoted.')
    conditional_band = models.DecimalField(max_digits=5, decimal_places=2, default=5,
                                           help_text='Marks below the pass average that still allow '
                                                     'promotion on condition (0 turns it off).')
    subject_pass_mark = models.DecimalField(max_digits=5, decimal_places=2, default=40)
    min_subjects_passed = models.PositiveSmallIntegerField(default=0,
                                                           help_text='Subjects passed at the subject pass mark (0 = no rule).')
    must_pass = models.ManyToManyField('users.Subject', blank=True, related_name='+',
                                       help_text='e.g. Mathematics and English Language.')
    min_attendance = models.PositiveSmallIntegerField(default=0, help_text='Percent of days present (0 = no rule).')
    basis = models.CharField(max_length=10, default='SESSION',
                             choices=[('SESSION', 'Average of all terms in the session'),
                                      ('LAST', 'Last term of the session only')])
    updated_at = models.DateTimeField(auto_now=True)
    objects = TenantManager()

    def __str__(self):
        return f"Promotion policy ({self.tenant})"


class ClassProgression(TenantModelMixin, models.Model):
    """Where a class goes next year — or that it graduates."""
    from_class = models.ForeignKey('users.Class', on_delete=models.CASCADE, related_name='+')
    to_class = models.ForeignKey('users.Class', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    graduates = models.BooleanField(default=False)
    objects = TenantManager()

    class Meta:
        unique_together = ('tenant', 'from_class')


class PromotionDecision(TenantModelMixin, models.Model):
    PROMOTE, CONDITIONAL, REPEAT, GRADUATE = 'PROMOTE', 'CONDITIONAL', 'REPEAT', 'GRADUATE'
    OUTCOMES = [(PROMOTE, 'Promote'), (CONDITIONAL, 'Promote on condition'), (REPEAT, 'Repeat the class'),
                (GRADUATE, 'Graduate')]

    session = models.ForeignKey('academics.AcademicSession', on_delete=models.CASCADE, related_name='+')
    student = models.ForeignKey('users.Student', on_delete=models.CASCADE, related_name='promotion_decisions')
    from_class = models.ForeignKey('users.Class', on_delete=models.CASCADE, related_name='+')
    to_class = models.ForeignKey('users.Class', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    suggested = models.CharField(max_length=12, choices=OUTCOMES)
    outcome = models.CharField(max_length=12, choices=OUTCOMES)
    average = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    subjects_taken = models.PositiveSmallIntegerField(default=0)
    subjects_passed = models.PositiveSmallIntegerField(default=0)
    attendance_rate = models.PositiveSmallIntegerField(null=True, blank=True)
    reasons = models.TextField(blank=True)
    override_reason = models.CharField(max_length=255, blank=True)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='+')
    applied_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    objects = TenantManager()

    class Meta:
        unique_together = ('tenant', 'session', 'student')
        ordering = ['from_class__name', 'student__full_name']

    @property
    def overridden(self):
        return self.outcome != self.suggested


# ─────────────────────────────────────────────────────────────────────────────
# Cover for absent teachers
# ─────────────────────────────────────────────────────────────────────────────

class CoverAssignment(TenantModelMixin, models.Model):
    """Someone teaching (or supervising) a timetabled lesson for an absent teacher on one date."""
    slot = models.ForeignKey('timetable.TimetableSlot', on_delete=models.CASCADE, related_name='covers')
    date = models.DateField()
    absent_teacher = models.ForeignKey('users.Staff', on_delete=models.CASCADE, related_name='covered_for')
    cover_teacher = models.ForeignKey('users.Staff', on_delete=models.CASCADE, related_name='covers')
    note = models.CharField(max_length=255, blank=True, help_text='Work set for the class, where to find it…')
    assigned_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name='+')
    notified_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    objects = TenantManager()

    class Meta:
        unique_together = ('tenant', 'slot', 'date')
        ordering = ['date', 'slot__time_slot__order']


# ─────────────────────────────────────────────────────────────────────────────
# Lesson observations
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_CRITERIA = [
    'Lesson plan and preparation',
    'Clear objectives shared with students',
    'Subject knowledge',
    'Teaching methods and use of materials',
    'Student participation',
    'Classroom management and discipline',
    'Checking understanding (questions, evaluation)',
    'Time management',
    'Use of the chalkboard / whiteboard',
    'Assignment and follow-up',
]
SCALE = [(1, 'Needs improvement'), (2, 'Fair'), (3, 'Good'), (4, 'Excellent')]


class ObservationTemplate(TenantModelMixin, models.Model):
    """The school's observation rubric: one criterion per line, each scored 1–4."""
    criteria = models.TextField(default='\n'.join(DEFAULT_CRITERIA))
    objects = TenantManager()

    @property
    def criteria_list(self):
        return [c.strip() for c in self.criteria.splitlines() if c.strip()]


class LessonObservation(TenantModelMixin, models.Model):
    teacher = models.ForeignKey('users.Staff', on_delete=models.CASCADE, related_name='observations')
    observer = models.ForeignKey('users.Staff', on_delete=models.SET_NULL, null=True, blank=True,
                                 related_name='observations_made')
    observer_user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                      related_name='+')
    date = models.DateField(default=timezone.localdate)
    subject = models.ForeignKey('users.Subject', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    school_class = models.ForeignKey('users.Class', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    topic = models.CharField(max_length=200, blank=True)
    scores = models.JSONField(default=list, help_text='[{"criterion": ..., "score": 1-4, "comment": ...}]')
    strengths = models.TextField(blank=True)
    improvements = models.TextField(blank=True)
    shared = models.BooleanField(default=False)
    shared_at = models.DateTimeField(null=True, blank=True)
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    teacher_response = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    objects = TenantManager()

    class Meta:
        ordering = ['-date', '-created_at']

    @property
    def average(self):
        vals = [s['score'] for s in self.scores if s.get('score')]
        return round(sum(vals) / len(vals), 1) if vals else None

    @property
    def percent(self):
        avg = self.average
        return round(avg / 4 * 100) if avg else None


# ─────────────────────────────────────────────────────────────────────────────
# Clubs and societies
# ─────────────────────────────────────────────────────────────────────────────

class Club(TenantModelMixin, models.Model):
    DAYS = [('MON', 'Monday'), ('TUE', 'Tuesday'), ('WED', 'Wednesday'), ('THU', 'Thursday'),
            ('FRI', 'Friday'), ('SAT', 'Saturday')]
    name = models.CharField(max_length=80)
    description = models.CharField(max_length=255, blank=True)
    patrons = models.ManyToManyField('users.Staff', blank=True, related_name='clubs_led')
    meeting_day = models.CharField(max_length=3, choices=DAYS, blank=True)
    meeting_time = models.TimeField(null=True, blank=True)
    venue = models.CharField(max_length=80, blank=True)
    capacity = models.PositiveSmallIntegerField(null=True, blank=True)
    active = models.BooleanField(default=True)
    objects = TenantManager()

    class Meta:
        ordering = ['name']
        unique_together = ('tenant', 'name')

    def __str__(self):
        return self.name


class ClubMember(TenantModelMixin, models.Model):
    ROLES = [('MEMBER', 'Member'), ('PRESIDENT', 'President'), ('VICE', 'Vice president'),
             ('SECRETARY', 'Secretary'), ('TREASURER', 'Treasurer'), ('PRO', 'Public relations officer')]
    club = models.ForeignKey(Club, on_delete=models.CASCADE, related_name='members')
    student = models.ForeignKey('users.Student', on_delete=models.CASCADE, related_name='club_memberships')
    role = models.CharField(max_length=10, choices=ROLES, default='MEMBER')
    joined_on = models.DateField(default=timezone.localdate)
    objects = TenantManager()

    class Meta:
        unique_together = ('tenant', 'club', 'student')
        ordering = ['student__full_name']


class ClubMeeting(TenantModelMixin, models.Model):
    club = models.ForeignKey(Club, on_delete=models.CASCADE, related_name='meetings')
    date = models.DateField(default=timezone.localdate)
    topic = models.CharField(max_length=160, blank=True)
    notes = models.TextField(blank=True)
    recorded_by = models.ForeignKey('users.Staff', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    objects = TenantManager()

    class Meta:
        unique_together = ('tenant', 'club', 'date')
        ordering = ['-date']


class ClubAttendance(TenantModelMixin, models.Model):
    meeting = models.ForeignKey(ClubMeeting, on_delete=models.CASCADE, related_name='attendance')
    student = models.ForeignKey('users.Student', on_delete=models.CASCADE, related_name='+')
    present = models.BooleanField(default=True)
    objects = TenantManager()

    class Meta:
        unique_together = ('tenant', 'meeting', 'student')
