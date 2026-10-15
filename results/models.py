from decimal import Decimal

from django.db import models

from academics.models import AcademicSession, Term
from users.models import Class, Student, Subject
from tenants.managers import TenantModelMixin, TenantManager
from security.media import signed_storage


# ---------------------------------------------------------------------------
# Grading
# ---------------------------------------------------------------------------

class Grading(TenantModelMixin, models.Model):
    """
    Maps a minimum score to a grade letter and description.
    Example: grade='A', min_score=70, description='Excellent'
    """
    grade       = models.CharField(max_length=2)
    min_score   = models.DecimalField(max_digits=5, decimal_places=2)
    description = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering        = ['-min_score']
        unique_together = ('tenant', 'grade', 'min_score')

    def __str__(self):
        return f"{self.grade} ({self.min_score}+)"

    def includes(self, score: float) -> bool:
        """Return True if *score* falls into this grade band."""
        return Decimal(str(score)) >= self.min_score


# ---------------------------------------------------------------------------
# Term Result
# ---------------------------------------------------------------------------

class TermResult(TenantModelMixin, models.Model):
    student        = models.ForeignKey(Student,         on_delete=models.CASCADE)
    class_assigned = models.ForeignKey(Class,           on_delete=models.CASCADE)
    subject        = models.ForeignKey(Subject,         on_delete=models.CASCADE)
    session        = models.ForeignKey(AcademicSession, on_delete=models.CASCADE)
    term           = models.ForeignKey(Term,            on_delete=models.CASCADE)

    # Continuous Assessment
    ca1_score   = models.FloatField(default=0)   # TRT
    ca2_score   = models.FloatField(default=0)   # Home Work
    ca3_score   = models.FloatField(default=0)   # Class Participation
    essay_score = models.FloatField(default=0)

    # Exam score (70%)
    exam_score     = models.FloatField(default=0)
    raw_exam_score = models.FloatField(default=0, null=True, blank=True)  # locked CBT score

    # Computed — written by save()
    total_score = models.FloatField(default=0)
    grade       = models.CharField(max_length=5,   blank=True, null=True)  # 'ABS' needs 3
    remark      = models.CharField(max_length=100, blank=True, null=True)

    published = models.BooleanField(default=False)

    # Set True when the student does NOT offer this subject this term.
    # Scores are zeroed, grade becomes 'ABS', and this row is excluded
    # from class averages, highest/lowest, and position calculations.
    is_not_offering = models.BooleanField(default=False)

    class Meta:
        unique_together = ('student', 'subject', 'term', 'session')

    def calculate_total(self) -> float:
        if self.is_not_offering:
            return 0
        return (
            (self.ca1_score   or 0) +
            (self.ca2_score   or 0) +
            (self.ca3_score   or 0) +
            (self.essay_score or 0) +
            (self.exam_score  or 0)
        )

    def calculate_grade_and_remark(self):
        if self.is_not_offering:
            return 'ABS', 'Not Offering'
        grading = (
            Grading.objects
            .filter(tenant_id=self.tenant_id, min_score__lte=Decimal(str(self.total_score)))
            .order_by('-min_score')
            .first()
        )
        if grading:
            return grading.grade, grading.description or grading.grade
        return None, None

    def save(self, *args, **kwargs):
        if self.is_not_offering:
            # Zero out all scores — never store real scores for non-offering students
            self.ca1_score = self.ca2_score = self.ca3_score = 0
            self.essay_score = self.exam_score = 0
        self.total_score = self.calculate_total()
        self.grade, self.remark = self.calculate_grade_and_remark()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.student} - {self.subject} ({self.term})"


# ---------------------------------------------------------------------------
# Session Result  (annual summary across all three terms)
# ---------------------------------------------------------------------------

class SessionResult(TenantModelMixin, models.Model):
    student        = models.ForeignKey(Student,         on_delete=models.CASCADE)
    class_assigned = models.ForeignKey(Class,           on_delete=models.CASCADE)
    subject        = models.ForeignKey(Subject,         on_delete=models.CASCADE)
    session        = models.ForeignKey(AcademicSession, on_delete=models.CASCADE)

    first_term    = models.FloatField(default=0)
    second_term   = models.FloatField(default=0)
    third_term    = models.FloatField(default=0)
    total_score   = models.FloatField(default=0)
    average_score = models.FloatField(default=0)

    class Meta:
        unique_together = ('student', 'subject', 'session')

    def calculate(self):
        self.total_score   = (self.first_term or 0) + (self.second_term or 0) + (self.third_term or 0)
        self.average_score = self.total_score / 3

    def save(self, *args, **kwargs):
        self.calculate()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.student} - {self.subject} ({self.session})"


# ---------------------------------------------------------------------------
# Remarks
# ---------------------------------------------------------------------------

class TeacherRemark(TenantModelMixin, models.Model):
    """Class teacher's end-of-term comment on a student."""
    student = models.ForeignKey(Student,         on_delete=models.CASCADE)
    term    = models.ForeignKey(Term,            on_delete=models.CASCADE, blank=True, null=True)
    session = models.ForeignKey(AcademicSession, on_delete=models.CASCADE)
    remark  = models.CharField(max_length=255, blank=True, default='')

    class Meta:
        unique_together = ('student', 'term', 'session')

    def __str__(self):
        return f"Teacher Remark - {self.student} ({self.term} / {self.session})"


class HosRemark(TenantModelMixin, models.Model):
    """Head of School remark on a student's term result."""
    student    = models.ForeignKey(Student,         on_delete=models.CASCADE, related_name='hos_remarks')
    term       = models.ForeignKey(Term,            on_delete=models.CASCADE, blank=True, null=True)
    session    = models.ForeignKey(AcademicSession, on_delete=models.CASCADE)
    remark     = models.TextField(blank=True, default='')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('student', 'term', 'session')

    def __str__(self):
        return f"HOS Remark - {self.student} ({self.term} / {self.session})"


class SubjectTeacherRemark(TenantModelMixin, models.Model):
    """Per-subject remark written by the subject teacher."""
    student    = models.ForeignKey(Student,         on_delete=models.CASCADE)
    subject    = models.ForeignKey(Subject,         on_delete=models.CASCADE)
    term       = models.ForeignKey(Term,            on_delete=models.CASCADE)
    session    = models.ForeignKey(AcademicSession, on_delete=models.CASCADE)
    remark     = models.TextField(blank=True, default='')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('student', 'subject', 'term', 'session')

    def __str__(self):
        return f"Subject Remark - {self.student} / {self.subject} ({self.term})"


# ---------------------------------------------------------------------------
# Skills & Behaviour
# ---------------------------------------------------------------------------

class Skill(TenantModelMixin, models.Model):
    CATEGORY_CHOICES = [
        ('skill',     'Skill'),
        ('behaviour', 'Behaviour'),
    ]
    name     = models.CharField(max_length=100)
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES)

    def __str__(self):
        return f"{self.name} ({self.get_category_display()})"


class SkillAssessment(TenantModelMixin, models.Model):
    student = models.ForeignKey(Student,         on_delete=models.CASCADE)
    term    = models.ForeignKey(Term,            on_delete=models.CASCADE)
    session = models.ForeignKey(AcademicSession, on_delete=models.CASCADE)
    skill   = models.ForeignKey(Skill,           on_delete=models.CASCADE)
    score   = models.PositiveSmallIntegerField(choices=[(i, i) for i in range(1, 6)])

    class Meta:
        unique_together = ('student', 'term', 'session', 'skill')

    def __str__(self):
        return f"{self.student} - {self.skill.name}: {self.score}"


# ---------------------------------------------------------------------------
# Head Teacher Signature
# ---------------------------------------------------------------------------

class HeadTeacherSignature(TenantModelMixin, models.Model):
    signature = models.ImageField(upload_to='head_teacher_signature/', storage=signed_storage)

    def __str__(self):
        return 'Head Teacher Signature'


# ---------------------------------------------------------------------------
# Result Sheet Layout — lets each school choose what shows on their
# printed/rendered result sheet (Settings → Result Sheet Layout).
# ---------------------------------------------------------------------------

class ResultSheetSettings(TenantModelMixin, models.Model):
    THEME_CHOICES = [
        ('classic_gold',      'Classic Gold'),
        ('royal_navy',        'Royal Navy'),
        ('emerald_prestige',  'Emerald Prestige'),
        ('burgundy_elegance', 'Burgundy Elegance'),
        ('sapphire_scholar',  'Sapphire Scholar'),
        ('rose_quartz',       'Rose Quartz'),
        ('midnight_platinum', 'Midnight Platinum'),
        ('forest_academia',   'Forest Academia'),
        ('coral_sunrise',     'Coral Sunrise'),
        ('slate_minimal',     'Slate Minimal'),
    ]

    theme = models.CharField(max_length=30, choices=THEME_CHOICES, default='classic_gold')

    show_ca_breakdown = models.BooleanField(
        default=True,
        help_text="Show each CA column separately. "
                  "If off, only the combined CA total is shown.",
    )
    show_class_average = models.BooleanField(default=True)
    show_position = models.BooleanField(default=False, help_text='Show the position in class on report cards.')
    require_approval = models.BooleanField(default=False, help_text='Subject sheets are reviewed and each class '
                                                                    'approved before results can be published.')
    fee_gate = models.BooleanField(default=False, help_text="Hide a child's results until enough of the term's fees are paid.")
    fee_gate_percent = models.PositiveSmallIntegerField(default=100)
    require_pin = models.BooleanField(default=False, help_text='Parents and students need a result PIN to view results.')
    pin_uses = models.PositiveSmallIntegerField(default=5)
    notify_on_publish = models.BooleanField(default=True, help_text='Text and email families when results are published.')
    show_subject_position = models.BooleanField(default=False, help_text='Show the position in each subject.')
    show_skills_section = models.BooleanField(default=True)
    show_behaviour_section = models.BooleanField(default=True)
    footer_note = models.CharField(
        max_length=255, blank=True, default='',
        help_text="Optional line printed at the bottom of every result sheet.",
    )

    def __str__(self):
        return f"Result Sheet Settings ({self.tenant})"
    



# ---------------------------------------------------------------------------
# Manual Term Attendance  (days present per student per term)
# ---------------------------------------------------------------------------

class StudentTermAttendance(TenantModelMixin, models.Model):
    """
    Stores how many days a student was present in a given term.
    This overrides the automatic day-count from the Attendance log
    when the result sheet is rendered.
    """
    student = models.ForeignKey(Student,         on_delete=models.CASCADE, related_name='term_attendances')
    term    = models.ForeignKey(Term,            on_delete=models.CASCADE)
    session = models.ForeignKey(AcademicSession, on_delete=models.CASCADE)
    days_present = models.PositiveSmallIntegerField(default=0)

    class Meta:
        unique_together = ('student', 'term', 'session')
        ordering = ['student__user__last_name', 'student__user__first_name']

    def __str__(self):
        return f"{self.student.full_name} — {self.term} / {self.session}: {self.days_present} days"

# ---------------------------------------------------------------------------
# AI-Generated Student Comments
# ---------------------------------------------------------------------------

class AIStudentComment(TenantModelMixin, models.Model):
    """
    Stores the AI-generated end-of-term comments for a student.

    One record per student per term per session (unique_together).
    Comments are generated via the Anthropic Claude API and can be
    re-generated at any time without affecting the TermResult records.

    Fields:
        teacher_comment  — class teacher's remark (2–3 sentences)
        hos_comment      — Head of School's formal remark (1–2 sentences)
        overall_summary  — one-line holistic summary for the report card header

    generation_ok=False means the last generation attempt failed; check
    error_message for details. The comment fields will be empty in that case.
    """
    student = models.ForeignKey(
        'users.Student', on_delete=models.CASCADE, related_name='ai_comments'
    )
    term    = models.ForeignKey('academics.Term',            on_delete=models.CASCADE)
    session = models.ForeignKey('academics.AcademicSession', on_delete=models.CASCADE)

    # Generated comment fields
    teacher_comment = models.TextField(blank=True, default='')
    hos_comment     = models.TextField(blank=True, default='')
    overall_summary = models.TextField(blank=True, default='')

    # Snapshot used for generation (informational / re-generation detection)
    overall_percentage = models.FloatField(default=0)
    total_subjects     = models.PositiveSmallIntegerField(default=0)

    # Generation metadata
    generated_at  = models.DateTimeField(default=None, null=True, blank=True)
    model_used    = models.CharField(max_length=60, default='claude-sonnet-4-20250514')
    generation_ok = models.BooleanField(default=False)
    error_message = models.TextField(blank=True, default='')

    class Meta:
        unique_together = ('student', 'term', 'session')
        ordering        = ['-generated_at']

    def __str__(self):
        status = '✓' if self.generation_ok else '✗'
        return (
            f"[{status}] AI Comment — {self.student.full_name} "
            f"({self.term} / {self.session})"
        )

# ---------------------------------------------------------------------------
# Continuous assessment template  (what each score column is, and out of what)
# ---------------------------------------------------------------------------

class AssessmentScheme(TenantModelMixin, models.Model):
    """
    The school's CA structure, e.g. "1st test 20 + 2nd test 10 + assignment 10
    + exam 60". Scores live in TermResult's five columns; this names them,
    sets their maximum marks, and switches unused ones off (maximum 0).
    """
    COLUMNS = ('ca1', 'ca2', 'ca3', 'essay', 'exam')
    DEFAULTS = {'ca1': ('TRT', 10), 'ca2': ('Homework', 10), 'ca3': ('Class Part.', 10),
                'essay': ('Essay', 0), 'exam': ('Exam', 70)}

    ca1_label = models.CharField(max_length=24, default='TRT')
    ca1_max = models.PositiveSmallIntegerField(default=10)
    ca2_label = models.CharField(max_length=24, default='Homework')
    ca2_max = models.PositiveSmallIntegerField(default=10)
    ca3_label = models.CharField(max_length=24, default='Class Part.')
    ca3_max = models.PositiveSmallIntegerField(default=10)
    essay_label = models.CharField(max_length=24, default='Essay')
    essay_max = models.PositiveSmallIntegerField(default=0)
    exam_label = models.CharField(max_length=24, default='Exam')
    exam_max = models.PositiveSmallIntegerField(default=70)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantManager()

    def __str__(self):
        return f"Assessment scheme ({self.tenant})"

    @classmethod
    def for_tenant(cls, tenant):
        """The school's scheme, or an unsaved default one."""
        if tenant is None:
            return cls()
        return cls.objects.filter(tenant=tenant).first() or cls(tenant=tenant)

    @property
    def columns(self):
        """[{key, field, label, max}] for every column in use, in order."""
        return [{'key': k, 'field': f'{k}_score', 'label': getattr(self, f'{k}_label'),
                 'max': getattr(self, f'{k}_max')}
                for k in self.COLUMNS if getattr(self, f'{k}_max')]

    @property
    def ca_columns(self):
        return [c for c in self.columns if c['key'] in ('ca1', 'ca2', 'ca3')]

    @property
    def ca_max(self):
        return sum(c['max'] for c in self.ca_columns)

    @property
    def total(self):
        return sum(getattr(self, f'{k}_max') for k in self.COLUMNS)

    @property
    def summary(self):
        return ' + '.join(f"{c['label']} {c['max']}" for c in self.columns)


# ---------------------------------------------------------------------------
# Score entry deadline
# ---------------------------------------------------------------------------

class ScoreLock(TenantModelMixin, models.Model):
    """After `locks_on`, scores for the term can't be changed — unless an administrator reopens entry."""
    term = models.ForeignKey(Term, on_delete=models.CASCADE, related_name='score_locks')
    locks_on = models.DateField(help_text='Entry closes at the end of this day.')
    reopened_until = models.DateTimeField(null=True, blank=True)
    reopen_reason = models.CharField(max_length=255, blank=True)
    reopened_by = models.ForeignKey('users.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantManager()

    class Meta:
        unique_together = ('tenant', 'term')

    @property
    def is_locked(self):
        from django.utils import timezone
        now = timezone.now()
        if self.reopened_until and self.reopened_until > now:
            return False
        return timezone.localdate() > self.locks_on


# ---------------------------------------------------------------------------
# Approval before publishing
# ---------------------------------------------------------------------------

class SubjectSheet(TenantModelMixin, models.Model):
    """One subject's scores for a class and term, moving from entry to review."""
    ENTERED, SUBMITTED, APPROVED, RETURNED = 'ENTERED', 'SUBMITTED', 'APPROVED', 'RETURNED'
    STATUSES = [(ENTERED, 'Being entered'), (SUBMITTED, 'Waiting for review'), (APPROVED, 'Approved'),
                (RETURNED, 'Sent back')]
    school_class = models.ForeignKey(Class, on_delete=models.CASCADE, related_name='+')
    subject = models.ForeignKey(Subject, on_delete=models.CASCADE, related_name='+')
    term = models.ForeignKey(Term, on_delete=models.CASCADE, related_name='+')
    status = models.CharField(max_length=9, choices=STATUSES, default=ENTERED)
    submitted_by = models.ForeignKey('users.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    submitted_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey('users.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    reviewed_at = models.DateTimeField(null=True, blank=True)
    comment = models.CharField(max_length=255, blank=True)
    objects = TenantManager()

    class Meta:
        unique_together = ('tenant', 'school_class', 'subject', 'term')


class ClassApproval(TenantModelMixin, models.Model):
    """The principal's sign-off that a class's results for a term may be published."""
    school_class = models.ForeignKey(Class, on_delete=models.CASCADE, related_name='+')
    term = models.ForeignKey(Term, on_delete=models.CASCADE, related_name='+')
    approved_by = models.ForeignKey('users.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    approved_at = models.DateTimeField(auto_now_add=True)
    comment = models.CharField(max_length=255, blank=True)
    objects = TenantManager()

    class Meta:
        unique_together = ('tenant', 'school_class', 'term')


# ---------------------------------------------------------------------------
# Result PINs (scratch cards)
# ---------------------------------------------------------------------------

class PinBatch(TenantModelMixin, models.Model):
    name = models.CharField(max_length=80)
    price = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    created_by = models.ForeignKey('users.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    objects = TenantManager()

    class Meta:
        ordering = ['-created_at']


class ResultPin(TenantModelMixin, models.Model):
    """A scratch-card PIN. The first use ties it to one student and term."""
    batch = models.ForeignKey(PinBatch, on_delete=models.CASCADE, related_name='pins')
    serial = models.CharField(max_length=20)
    pin = models.CharField(max_length=16)
    student = models.ForeignKey(Student, on_delete=models.SET_NULL, null=True, blank=True, related_name='result_pins')
    term = models.ForeignKey(Term, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    uses = models.PositiveSmallIntegerField(default=0)
    max_uses = models.PositiveSmallIntegerField(default=5)
    first_used_at = models.DateTimeField(null=True, blank=True)
    disabled = models.BooleanField(default=False)
    objects = TenantManager()

    class Meta:
        unique_together = (('tenant', 'pin'), ('tenant', 'serial'))
        ordering = ['serial']

    @property
    def remaining(self):
        return max(0, self.max_uses - self.uses)


def _private_storage():
    from users.models import PrivateStorage
    return PrivateStorage()


def _bundle_path(instance, filename):
    return f'results/{instance.tenant_id}/bundles/{instance.pk or "new"}-{filename}'


# ---------------------------------------------------------------------------
# Whole-class report card downloads
# ---------------------------------------------------------------------------

class ReportBundle(TenantModelMixin, models.Model):
    """A ZIP of a class's report cards, built in the background."""
    PENDING, RUNNING, DONE, FAILED = 'PENDING', 'RUNNING', 'DONE', 'FAILED'
    school_class = models.ForeignKey(Class, on_delete=models.CASCADE, related_name='+')
    term = models.ForeignKey(Term, on_delete=models.CASCADE, related_name='+')
    status = models.CharField(max_length=8, default=PENDING)
    published_only = models.BooleanField(default=False)
    file = models.FileField(upload_to=_bundle_path, storage=_private_storage, blank=True, null=True)
    count = models.PositiveSmallIntegerField(default=0)
    error = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey('users.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    objects = TenantManager()

    class Meta:
        ordering = ['-created_at']


# ---------------------------------------------------------------------------
# Families asking for a result to be checked
# ---------------------------------------------------------------------------

class ResultQuery(TenantModelMixin, models.Model):
    OPEN, ANSWERED, CORRECTED = 'OPEN', 'ANSWERED', 'CORRECTED'
    STATUSES = [(OPEN, 'Waiting for the teacher'), (ANSWERED, 'Answered — no change'),
                (CORRECTED, 'Corrected')]
    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name='result_queries')
    term = models.ForeignKey(Term, on_delete=models.CASCADE, related_name='+')
    subject = models.ForeignKey(Subject, on_delete=models.CASCADE, related_name='+')
    raised_by = models.ForeignKey('users.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    message = models.TextField()
    score_when_raised = models.FloatField(null=True, blank=True)
    status = models.CharField(max_length=9, choices=STATUSES, default=OPEN)
    response = models.TextField(blank=True)
    score_after = models.FloatField(null=True, blank=True)
    responded_by = models.ForeignKey('users.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    responded_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    objects = TenantManager()

    class Meta:
        ordering = ['-created_at']
