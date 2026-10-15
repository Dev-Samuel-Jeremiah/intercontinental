# classroom/models.py
from django.db import models
from tenants.managers import TenantModelMixin, TenantManager

from django.conf import settings
from users.models import Class  # Your existing Class model
from security.media import signed_storage

User = settings.AUTH_USER_MODEL


def _private_storage():
    from users.models import PrivateStorage
    return PrivateStorage()


def _private_upload(instance, filename):
    import os
    import uuid
    ext = os.path.splitext(filename)[1].lower()[:8]
    return f"classroom/{instance.tenant_id}/{instance.__class__.__name__.lower()}/{uuid.uuid4().hex}{ext}"

# ------------------ COURSE MODEL ------------------
class Course(TenantModelMixin, models.Model):
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    teacher = models.ForeignKey(User, on_delete=models.CASCADE, related_name='courses')
    class_assigned = models.ForeignKey(Class, on_delete=models.SET_NULL, null=True, blank=True, related_name='courses')
    students = models.ManyToManyField(User, related_name='enrolled_courses', blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


# ------------------ SECTION MODEL ------------------
class Section(TenantModelMixin, models.Model):
    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name='sections')
    title = models.CharField(max_length=255)
    order = models.PositiveIntegerField(help_text="Order of the section in the course")

    class Meta:
        ordering = ['order']
        unique_together = ('course', 'order')

    def __str__(self):
        return f"{self.course.name} - {self.title}"


# ------------------ LESSON MODEL ------------------
class Lesson(TenantModelMixin, models.Model):
    section = models.ForeignKey(Section, on_delete=models.CASCADE, related_name='lessons')
    title = models.CharField(max_length=255)
    video = models.FileField(upload_to="course_videos/", storage=signed_storage)
    duration_minutes = models.PositiveIntegerField(default=0)
    order = models.PositiveIntegerField(help_text="Order of lesson in the section")

    class Meta:
        ordering = ['order']
        unique_together = ('section', 'order')

    def __str__(self):
        return self.title


# ------------------ STUDENT SECTION PROGRESS ------------------
class StudentSectionProgress(TenantModelMixin, models.Model):
    student = models.ForeignKey(User, on_delete=models.CASCADE)
    section = models.ForeignKey(Section, on_delete=models.CASCADE)
    completed = models.BooleanField(default=False)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ('student', 'section')

    def __str__(self):
        status = "Completed" if self.completed else "In Progress"
        return f"{self.student} - {self.section} ({status})"


# ------------------ LESSON PROGRESS ------------------
class LessonProgress(TenantModelMixin, models.Model):
    student = models.ForeignKey(User, on_delete=models.CASCADE)
    lesson = models.ForeignKey(Lesson, on_delete=models.CASCADE)
    watched = models.BooleanField(default=False)

    class Meta:
        unique_together = ('student', 'lesson')

    def __str__(self):
        status = "Watched" if self.watched else "Not Watched"
        return f"{self.student} - {self.lesson} ({status})"


# ------------------ MATERIAL MODEL ------------------
class Material(TenantModelMixin, models.Model):
    section = models.ForeignKey(
        Section, 
        on_delete=models.CASCADE, 
        related_name='materials'
    )
    title = models.CharField(max_length=255)
    file = models.FileField(upload_to='section_materials/', storage=signed_storage)
    description = models.TextField(blank=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['uploaded_at']

    def __str__(self):
        section_title = self.section.title if self.section else "No Section"
        return f"{section_title} - {self.title}"


class Assignment(TenantModelMixin, models.Model):
    section = models.ForeignKey(
        Section, 
        on_delete=models.CASCADE, 
        related_name='assignments'
    )
    title = models.CharField(max_length=255)
    description = models.TextField()
    due_date = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    # Late submissions
    allow_late = models.BooleanField(default=True)
    grace_minutes = models.PositiveIntegerField(default=0, help_text='Minutes after the deadline still counted on time.')
    late_penalty_per_day = models.PositiveSmallIntegerField(default=10, help_text='Percent off the mark per late day.')
    max_late_days = models.PositiveSmallIntegerField(default=3, help_text='After this many days, submissions close.')

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        section_title = self.section.title if self.section else "No Section"
        return f"{section_title} - {self.title}"



# ------------------ QUIZ MODEL ------------------

class Quiz(TenantModelMixin, models.Model):
    section = models.ForeignKey(
        Section, 
        on_delete=models.CASCADE, 
        related_name='quizzes'
    )
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    duration_minutes = models.PositiveIntegerField(default=0)
    pass_mark = models.PositiveIntegerField(default=50)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        section_title = self.section.title if self.section else "No Section"
        return f"{section_title} - {self.title}"


# ------------------ ASSIGNMENT QUESTION MODEL ------------------
class AssignmentQuestion(TenantModelMixin, models.Model):
    assignment = models.ForeignKey(Assignment, on_delete=models.CASCADE, related_name='questions')
    question_text = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.assignment.title} - {self.question_text[:50]}"



# ------------------ QUESTION MODEL ------------------
class Question(TenantModelMixin, models.Model):
    quiz = models.ForeignKey(Quiz, on_delete=models.CASCADE, related_name='questions')
    question_text = models.TextField()
    option_a = models.CharField(max_length=255)
    option_b = models.CharField(max_length=255)
    option_c = models.CharField(max_length=255, blank=True, null=True)
    option_d = models.CharField(max_length=255, blank=True, null=True)
    correct_option = models.CharField(max_length=1, choices=[('A','A'),('B','B'),('C','C'),('D','D')])

    def __str__(self):
        return f"{self.quiz.title} - {self.question_text[:50]}"


# ------------------ SUBMISSION MODEL ------------------
class Submission(TenantModelMixin, models.Model):
    assignment = models.ForeignKey(
        Assignment,
        on_delete=models.CASCADE,
        related_name='submissions'
    )
    student = models.ForeignKey(User, on_delete=models.CASCADE)

    file = models.FileField(
        upload_to='submissions/',
        storage=signed_storage,
        blank=True,
        null=True
    )

    # NULL means NOT YET SUBMITTED
    submitted_at = models.DateTimeField(
        null=True,
        blank=True
    )

    grade = models.FloatField(null=True, blank=True)
    feedback = models.TextField(blank=True)
    late_days = models.PositiveSmallIntegerField(default=0)
    penalty_percent = models.PositiveSmallIntegerField(default=0)
    feedback_audio = models.FileField(upload_to=_private_upload, storage=_private_storage, blank=True, null=True)

    @property
    def final_grade(self):
        """The teacher's mark less any late penalty."""
        if self.grade is None:
            return None
        return round(self.grade * (100 - self.penalty_percent) / 100, 2)

    class Meta:
        unique_together = ('assignment', 'student')
        ordering = ['-submitted_at']

    def __str__(self):
        return f"{self.student} - {self.assignment.title}"


class SubmissionAnswer(TenantModelMixin, models.Model):
    submission = models.ForeignKey(
        Submission,
        on_delete=models.CASCADE,
        related_name='answers'
    )
    student = models.ForeignKey(User, on_delete=models.CASCADE)
    question = models.ForeignKey(AssignmentQuestion, on_delete=models.CASCADE)
    answer_text = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('submission', 'question')

    def __str__(self):
        return f"{self.submission.assignment.title} - {self.question.question_text[:50]}"



class QuizSubmission(TenantModelMixin, models.Model):
    quiz = models.ForeignKey(Quiz, on_delete=models.CASCADE, related_name="submissions")
    student = models.ForeignKey(User, on_delete=models.CASCADE)
    score = models.FloatField(null=True, blank=True)
    total_questions = models.PositiveIntegerField()
    submitted_at = models.DateTimeField(null=True, blank=True)
    

    class Meta:
        unique_together = ("quiz", "student")

    def __str__(self):
        return f"{self.student} - {self.quiz.title}"


class QuizAnswer(TenantModelMixin, models.Model):
    submission = models.ForeignKey(QuizSubmission, on_delete=models.CASCADE, related_name="answers")
    question = models.ForeignKey(Question, on_delete=models.CASCADE)
    selected_option = models.CharField(max_length=1, choices=[('A','A'),('B','B'),('C','C'),('D','D')])

    class Meta:
        unique_together = ("submission", "question")

    def __str__(self):
        return f"{self.submission.student} - {self.question.question_text[:50]}"

# ------------------ COURSE ANNOUNCEMENTS ------------------
class CourseAnnouncement(TenantModelMixin, models.Model):
    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name='announcements')
    author = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='+')
    title = models.CharField(max_length=160)
    body = models.TextField()
    pinned = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-pinned', '-created_at']


# ------------------ LIVE CLASSES ------------------
class LiveClass(TenantModelMixin, models.Model):
    JITSI, LINK = 'JITSI', 'LINK'
    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name='live_classes')
    title = models.CharField(max_length=160)
    starts_at = models.DateTimeField()
    minutes = models.PositiveSmallIntegerField(default=40)
    platform = models.CharField(max_length=5, choices=((JITSI, 'Jitsi (free, no account)'), (LINK, 'Zoom / Google Meet link')),
                                default=JITSI)
    url = models.URLField(max_length=500, blank=True)
    room = models.CharField(max_length=80, blank=True)
    recording_url = models.URLField(max_length=500, blank=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-starts_at']

    @property
    def ends_at(self):
        from datetime import timedelta
        return self.starts_at + timedelta(minutes=self.minutes)

    @property
    def join_url(self):
        return self.url if self.platform == self.LINK else f'https://meet.jit.si/{self.room}'


class LiveAttendance(TenantModelMixin, models.Model):
    live_class = models.ForeignKey(LiveClass, on_delete=models.CASCADE, related_name='attendance')
    student = models.ForeignKey(User, on_delete=models.CASCADE, related_name='+')
    first_joined = models.DateTimeField(auto_now_add=True)
    last_joined = models.DateTimeField(auto_now=True)
    joins = models.PositiveSmallIntegerField(default=1)

    class Meta:
        unique_together = ('live_class', 'student')


# ------------------ DISCUSSION ------------------
class Thread(TenantModelMixin, models.Model):
    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name='threads')
    author = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='+')
    title = models.CharField(max_length=160)
    body = models.TextField()
    pinned = models.BooleanField(default=False)
    locked = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    last_activity = models.DateTimeField(auto_now_add=True)
    held = models.BooleanField(default=False, help_text='Held by the safety check until a teacher looks at it')
    held_reason = models.CharField(max_length=160, blank=True)

    class Meta:
        ordering = ['-pinned', '-last_activity']


class Post(TenantModelMixin, models.Model):
    thread = models.ForeignKey(Thread, on_delete=models.CASCADE, related_name='posts')
    author = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='+')
    body = models.TextField()
    is_answer = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    held = models.BooleanField(default=False, help_text='Held by the safety check until a teacher looks at it')
    held_reason = models.CharField(max_length=160, blank=True)

    class Meta:
        ordering = ['created_at']


# ------------------ RUBRICS ------------------
class RubricCriterion(TenantModelMixin, models.Model):
    assignment = models.ForeignKey(Assignment, on_delete=models.CASCADE, related_name='criteria')
    name = models.CharField(max_length=120)
    description = models.CharField(max_length=255, blank=True)
    order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ['order', 'id']

    @property
    def max_points(self):
        return max((l.points for l in self.levels.all()), default=0)


class RubricLevel(TenantModelMixin, models.Model):
    criterion = models.ForeignKey(RubricCriterion, on_delete=models.CASCADE, related_name='levels')
    label = models.CharField(max_length=60)
    points = models.PositiveSmallIntegerField()
    description = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ['-points']


class RubricScore(TenantModelMixin, models.Model):
    submission = models.ForeignKey(Submission, on_delete=models.CASCADE, related_name='rubric_scores')
    criterion = models.ForeignKey(RubricCriterion, on_delete=models.CASCADE, related_name='+')
    level = models.ForeignKey(RubricLevel, on_delete=models.CASCADE, related_name='+')

    class Meta:
        unique_together = ('submission', 'criterion')


# ------------------ CERTIFICATES ------------------
class CourseCertificate(TenantModelMixin, models.Model):
    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name='certificates')
    student = models.ForeignKey(User, on_delete=models.CASCADE, related_name='+')
    issued_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('course', 'student')
