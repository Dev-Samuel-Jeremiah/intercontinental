from django.db import models
from django.utils import timezone
from users.models import Student, Class, Staff, Subject
from academics.models import AcademicSession, Term
from django.core.validators import FileExtensionValidator
from tenants.managers import TenantModelMixin, TenantManager
# ----------------------------
# Main Exam
# ----------------------------
class Exam(TenantModelMixin, models.Model):
    EXAM_TYPES = (
        ('CA1', 'First Continuous Assessment'),
        ('CA2', 'Second Continuous Assessment'),
        ('EXAM', 'Exam'),
    )

    title = models.CharField(max_length=200)
    subject = models.ForeignKey(Subject, on_delete=models.CASCADE)
    session = models.ForeignKey(AcademicSession, on_delete=models.CASCADE, null=True, blank=True)
    term = models.ForeignKey(Term, on_delete=models.CASCADE, null=True, blank=True)
    classes = models.ManyToManyField(Class, related_name='exams')

    exam_type = models.CharField(max_length=4, choices=EXAM_TYPES, default='EXAM')
    start_time = models.DateTimeField()
    end_time = models.DateTimeField()

    created_by = models.ForeignKey(Staff, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    published = models.BooleanField(default=False)
    duration_minutes = models.PositiveIntegerField(default=0)
    shuffle_questions = models.BooleanField(default=False)
    shuffle_options = models.BooleanField(default=False, help_text='Each student sees the options in their own order.')
    allow_retake = models.BooleanField(default=False)
    REVIEW_CHOICES = (('NEVER', 'Never'), ('AFTER_CLOSE', 'After the exam closes'),
                      ('AFTER_SUBMIT', 'As soon as they submit'))
    answer_review = models.CharField(max_length=12, choices=REVIEW_CHOICES, default='NEVER',
                                     help_text='When students may see correct answers and explanations.')
    lockdown = models.BooleanField(default=True, help_text='Log tab switching, copying and pasting, and warn the student.')

    objects = TenantManager()

    def __str__(self):
        return f"{self.title} ({self.exam_type})"

    @property
    def is_active(self):
        now = timezone.now()
        return self.published and self.start_time <= now <= self.end_time

    @property
    def is_past(self):
        return timezone.now() > self.end_time

    @property
    def is_upcoming(self):
        return timezone.now() < self.start_time

    @property
    def total_marks(self):
        return sum(part.total_marks for part in self.parts.all())


# ----------------------------
# Exam Parts (CBT or Essay)
# ----------------------------
class ExamPart(models.Model):
    PART_TYPES = (
        ('CBT', 'Computer Based Test'),
        ('ESSAY', 'Essay / Written'),
    )

    exam = models.ForeignKey(
        Exam,
        on_delete=models.CASCADE,
        related_name='parts'
    )
    part_type = models.CharField(max_length=10, choices=PART_TYPES)
    total_marks = models.PositiveIntegerField(default=0)
    duration_minutes = models.PositiveIntegerField(default=0)
    pool_size = models.PositiveIntegerField(default=0, help_text='Give each candidate this many questions drawn at '
                                                                 'random from the section (0 = all of them).')

    def __str__(self):
        return f"{self.exam.title} - {self.part_type}"

    def section_for(self, question_type):
        """Where a question of this type belongs. A short-answer (typed) question needs a teacher
        to mark it, so on an objective (CBT) section it goes to the exam's Essay section instead —
        objective sections only mark chosen options, so a typed answer there would always score 0."""
        if self.part_type == 'CBT' and question_type == 'SA':
            essay, _ = ExamPart.objects.get_or_create(
                exam=self.exam, part_type='ESSAY', defaults={'duration_minutes': self.duration_minutes})
            return essay
        return self


# ----------------------------
# CBT Questions
# ----------------------------
class Question(models.Model):
    QUESTION_TYPES = (
        ('MCQ', 'Multiple Choice'),
        ('TF', 'True / False'),
        ('SA', 'Short Answer'),
    )

    exam_part = models.ForeignKey(
        ExamPart,
        on_delete=models.CASCADE,
        related_name='questions'
    )

    subject = models.ForeignKey(
        Subject,
        on_delete=models.CASCADE,
        editable=False,
        null=True,
    )
        # NEW FIELDS - Add these
    passage = models.TextField(
        blank=True, 
        null=True,
        help_text="Reading passage or comprehension text for this question"
    )
    
    passage_title = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text="Title of the passage (optional)"
    )
    
    image = models.ImageField(
        upload_to='question_images/%Y/%m/%d/',
        blank=True,
        null=True,
        validators=[FileExtensionValidator(allowed_extensions=['jpg', 'jpeg', 'png', 'gif'])],
        help_text="Upload an image for this question"
    )
    
    image_caption = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text="Caption for the image (optional)"
    )
    
    # For grouping questions under same passage
    passage_group = models.CharField(
        max_length=50,
        blank=True,
        null=True,
        help_text="Group ID for questions sharing the same passage (e.g., 'passage_1')"
    )
    
    created_at = models.DateTimeField(auto_now_add=True,blank=True, null=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['exam_part', 'id']
    
    def __str__(self):
        return f"{self.exam_part.exam.title} - Q{self.id}"

    question_text = models.TextField()
    question_type = models.CharField(max_length=5, choices=QUESTION_TYPES, null=True, blank=True)
    marks = models.FloatField(default=1.0)
    explanation = models.TextField(blank=True, default='', help_text='Shown to students when answers are reviewed.')
    bank_question = models.ForeignKey('BankQuestion', on_delete=models.SET_NULL, null=True, blank=True,
                                      related_name='uses', help_text='The bank question this was copied from.')

    def save(self, *args, **kwargs):
        if self.exam_part and self.exam_part.exam:
            self.subject = self.exam_part.exam.subject
        if self.exam_part.part_type == 'ESSAY':
            self.question_type = None
        super().save(*args, **kwargs)

    def __str__(self):
        return self.question_text[:60]


# ----------------------------
# CBT Options
# ----------------------------
class Option(models.Model):
    question = models.ForeignKey(
        Question,
        on_delete=models.CASCADE,
        related_name='options'
    )
    text = models.CharField(max_length=255)
    is_correct = models.BooleanField(default=False)

    def __str__(self):
        return self.text


# ----------------------------
# Exam Attempt
# ----------------------------
class ExamAttempt(models.Model):
    student = models.ForeignKey(Student, on_delete=models.CASCADE)
    exam_part = models.ForeignKey(ExamPart, on_delete=models.CASCADE)
 
    score = models.FloatField(default=0)
    completed = models.BooleanField(default=False)
 
    retake_number = models.PositiveIntegerField(default=1)
    started_at = models.DateTimeField(default=timezone.now)
    submitted_at = models.DateTimeField(null=True, blank=True)
 
    # ── Resume state ──────────────────────────────────────────────────
    # Tracks the last question the student was on so we can resume after
    # a power cut, browser crash, or network failure.
    last_question_index = models.PositiveIntegerField(default=0)
 
    # Tracks elapsed seconds at the moment of last auto-save.
    # On resume, remaining = total_duration_seconds - elapsed_seconds_at_last_save
    # This prevents students getting extra time from reconnecting.
    elapsed_seconds = models.PositiveIntegerField(default=0)
 
    # ISO timestamp of last auto-save (for display / debugging)
    last_autosave_at = models.DateTimeField(null=True, blank=True)

    # Extra seconds granted by an invigilator to this one candidate.
    extra_seconds = models.PositiveIntegerField(default=0)
    # This candidate's own question order and option orders, fixed when the
    # attempt starts so a reload or resume shows the same paper.
    question_order = models.JSONField(default=list, blank=True)
    option_order = models.JSONField(default=dict, blank=True)
 
    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['student', 'exam_part'],
                name='unique_student_exam_part_attempt'
            )
        ]
        indexes = [
            models.Index(fields=['student', 'exam_part']),
        ]
 
    def __str__(self):
        return f"{self.student} - {self.exam_part}"
 
    @property
    def time_taken(self):
        if self.submitted_at:
            return (self.submitted_at - self.started_at).total_seconds() / 60
        return None
 


# ----------------------------
# Student Answers (CBT)
# ----------------------------
class StudentAnswer(models.Model):
    attempt = models.ForeignKey(
        ExamAttempt,
        on_delete=models.CASCADE,
        related_name='answers'
    )
    question = models.ForeignKey(Question, on_delete=models.CASCADE)
    selected_option = models.ForeignKey(
        Option,
        on_delete=models.CASCADE,
        null=True,
        blank=True
    )
    text_answer = models.TextField(null=True, blank=True)

    class Meta:
        unique_together = ('attempt', 'question')

    def __str__(self):
        return f"{self.attempt.student} - {self.question}"


# ----------------------------
# Essay Scores
# ----------------------------
class EssayScore(models.Model):
    student = models.ForeignKey(Student, on_delete=models.CASCADE)
    exam_part = models.ForeignKey(
        ExamPart,
        on_delete=models.CASCADE,
        related_name='essay_scores'
    )
    score = models.FloatField(default=0)
    text_answer = models.TextField(null=True, blank=True)  # store student's essay

    class Meta:
        unique_together = ('student', 'exam_part')

    def __str__(self):
        return f"{self.student} - {self.exam_part}"

class ExamStudentRestriction(models.Model):
    """
    When this record exists for an exam, ONLY the listed students
    can see/take that exam (even if the exam is published to their class).
    If NO records exist for an exam, ALL students in the assigned classes can see it.
    """
    exam     = models.ForeignKey(Exam, on_delete=models.CASCADE, related_name='student_restrictions')
    student  = models.ForeignKey('users.Student', on_delete=models.CASCADE, related_name='exam_restrictions')
    added_at = models.DateTimeField(auto_now_add=True)
    added_by = models.ForeignKey(
        'users.Staff', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='exam_restrictions_added'
    )

    class Meta:
        unique_together = ('exam', 'student')
        ordering = ['student__full_name']

    def __str__(self):
        return f"{self.exam.title} → {self.student.full_name}"

class IntegrityEvent(TenantModelMixin, models.Model):
    """Something during an exam that an invigilator may want to know about."""
    KINDS = (('HIDDEN', 'Left the exam tab'), ('BLUR', 'Switched to another window'), ('COPY', 'Copied text'),
             ('PASTE', 'Pasted text'), ('FULLSCREEN', 'Left full screen'), ('CONTEXT', 'Right-clicked'))
    attempt = models.ForeignKey(ExamAttempt, on_delete=models.CASCADE, related_name='integrity_events')
    kind = models.CharField(max_length=10, choices=KINDS)
    at = models.DateTimeField(auto_now_add=True)
    seconds_away = models.PositiveIntegerField(default=0)
    objects = TenantManager()

    class Meta:
        ordering = ['-at']


# ----------------------------
# Question bank
# ----------------------------
class BankQuestion(TenantModelMixin, models.Model):
    """A reusable question, tagged so teachers can find it and results can be read by topic."""
    DIFFICULTY = (('EASY', 'Easy'), ('MEDIUM', 'Medium'), ('HARD', 'Hard'))
    TYPES = (('MCQ', 'Multiple choice'), ('TF', 'True / False'))
    subject = models.ForeignKey(Subject, on_delete=models.CASCADE, related_name='bank_questions')
    level = models.CharField(max_length=40, blank=True, help_text='Class level, e.g. "JSS 2" or "SS 1".')
    topic = models.CharField(max_length=120, blank=True)
    outcome = models.CharField(max_length=200, blank=True, help_text='The learning outcome it checks.')
    difficulty = models.CharField(max_length=6, choices=DIFFICULTY, default='MEDIUM')
    question_type = models.CharField(max_length=3, choices=TYPES, default='MCQ')
    text = models.TextField()
    passage = models.TextField(blank=True)
    image = models.ImageField(upload_to='question_bank/%Y/%m/', blank=True, null=True,
                              validators=[FileExtensionValidator(allowed_extensions=['jpg', 'jpeg', 'png', 'gif'])])
    explanation = models.TextField(blank=True)
    marks = models.FloatField(default=1.0)
    for_practice = models.BooleanField(default=True, help_text='Students may meet it in practice tests.')
    created_by = models.ForeignKey(Staff, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    objects = TenantManager()

    class Meta:
        ordering = ['subject__name', 'level', 'topic', 'id']

    def __str__(self):
        return self.text[:60]


class BankOption(models.Model):
    question = models.ForeignKey(BankQuestion, on_delete=models.CASCADE, related_name='options')
    text = models.CharField(max_length=255)
    is_correct = models.BooleanField(default=False)

    class Meta:
        ordering = ['id']


# ----------------------------
# Practice (JAMB-style and topic drills)
# ----------------------------
class PracticeSession(TenantModelMixin, models.Model):
    MODES = (('QUICK', 'Quick quiz'), ('DAILY', 'Daily challenge'), ('MISTAKES', 'Fix my mistakes'),
             ('JAMB', 'UTME-style mock'), ('TOPIC', 'Topic drill'))
    FEEDBACK = (('END', 'At the end'), ('INSTANT', 'After each question'))
    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name='practice_sessions')
    mode = models.CharField(max_length=8, choices=MODES, default='JAMB')
    difficulty = models.CharField(max_length=6, blank=True, help_text='EASY / MEDIUM / HARD, or blank for mixed.')
    feedback = models.CharField(max_length=7, choices=FEEDBACK, default='END')
    timed = models.BooleanField(default=True)
    points = models.PositiveIntegerField(default=0)
    challenge_date = models.DateField(null=True, blank=True, help_text='The day of a daily challenge.')
    subjects = models.ManyToManyField(Subject, related_name='+')
    topic = models.CharField(max_length=120, blank=True)
    # {subject_id: [bank question ids]} and {question_id: [option ids]} — fixed at the start.
    paper = models.JSONField(default=dict)
    option_order = models.JSONField(default=dict)
    started_at = models.DateTimeField(default=timezone.now)
    duration_seconds = models.PositiveIntegerField(default=0)
    submitted_at = models.DateTimeField(null=True, blank=True)
    score = models.PositiveIntegerField(default=0)
    total = models.PositiveIntegerField(default=0)
    objects = TenantManager()

    class Meta:
        ordering = ['-started_at']

    @property
    def finished(self):
        return self.submitted_at is not None

    @property
    def percent(self):
        return round(100 * self.score / self.total) if self.total else 0


class PracticeAnswer(models.Model):
    session = models.ForeignKey(PracticeSession, on_delete=models.CASCADE, related_name='answers')
    question = models.ForeignKey(BankQuestion, on_delete=models.CASCADE, related_name='practice_answers')
    option = models.ForeignKey(BankOption, on_delete=models.SET_NULL, null=True, blank=True)
    correct = models.BooleanField(default=False)
    answered_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('session', 'question')


# ----------------------------
# Exam halls and seating
# ----------------------------
class ExamHall(TenantModelMixin, models.Model):
    name = models.CharField(max_length=80)
    rows = models.PositiveSmallIntegerField(default=6)
    columns = models.PositiveSmallIntegerField(default=6)
    notes = models.CharField(max_length=160, blank=True, help_text='e.g. "Computer lab — 30 working PCs".')
    active = models.BooleanField(default=True)
    objects = TenantManager()

    class Meta:
        ordering = ['name']
        unique_together = ('tenant', 'name')

    def __str__(self):
        return self.name

    @property
    def capacity(self):
        return self.rows * self.columns


class Seat(TenantModelMixin, models.Model):
    """One candidate's seat for one exam."""
    exam = models.ForeignKey(Exam, on_delete=models.CASCADE, related_name='seats')
    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name='exam_seats')
    hall = models.ForeignKey(ExamHall, on_delete=models.CASCADE, related_name='seats')
    row = models.PositiveSmallIntegerField()
    column = models.PositiveSmallIntegerField()
    objects = TenantManager()

    class Meta:
        unique_together = (('exam', 'student'), ('exam', 'hall', 'row', 'column'))
        ordering = ['hall__name', 'row', 'column']

    @property
    def label(self):
        return f'{chr(64 + self.row) if self.row <= 26 else self.row}{self.column}'


# ----------------------------
# Exam timetable
# ----------------------------
class ExamTimetable(TenantModelMixin, models.Model):
    name = models.CharField(max_length=120)
    term = models.ForeignKey(Term, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    start_date = models.DateField()
    end_date = models.DateField()
    morning_start = models.TimeField(default='09:00')
    afternoon_start = models.TimeField(default='12:00')
    afternoon = models.BooleanField(default=True, help_text='Two papers a day (morning and afternoon).')
    paper_minutes = models.PositiveSmallIntegerField(default=90)
    classes = models.ManyToManyField(Class, related_name='+')
    published = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    objects = TenantManager()

    class Meta:
        ordering = ['-start_date']

    def __str__(self):
        return self.name


class ExamTimetableEntry(TenantModelMixin, models.Model):
    MORNING, AFTERNOON = 'AM', 'PM'
    timetable = models.ForeignKey(ExamTimetable, on_delete=models.CASCADE, related_name='entries')
    subject = models.ForeignKey(Subject, on_delete=models.CASCADE, related_name='+')
    classes = models.ManyToManyField(Class, related_name='+')
    date = models.DateField()
    sitting = models.CharField(max_length=2, choices=((MORNING, 'Morning'), (AFTERNOON, 'Afternoon')))
    start_time = models.TimeField()
    minutes = models.PositiveSmallIntegerField(default=90)
    exam = models.ForeignKey(Exam, on_delete=models.SET_NULL, null=True, blank=True, related_name='timetable_entries')
    objects = TenantManager()

    class Meta:
        ordering = ['date', 'sitting', 'subject__name']
