"""
Timetable Models
================
Supports automatic generation of school timetables using a constraint-based
scheduling algorithm. Handles teacher availability, subject frequency, and
period slots per day.
"""

from django.db import models
from tenants.managers import TenantModelMixin, TenantManager
from tenants.middleware import get_current_tenant

from django.core.exceptions import ValidationError
from users.models import Staff, Subject, Class


# ---------------------------------------------------------------------------
# Configuration Models
# ---------------------------------------------------------------------------

class TimeSlot(TenantModelMixin, models.Model):
    """A single period slot (e.g. "Period 1: 08:00–08:40")."""
    name = models.CharField(max_length=50, help_text='e.g. "Period 1"')
    start_time = models.TimeField()
    end_time = models.TimeField()
    order = models.PositiveSmallIntegerField(default=0, help_text="Sort order within a day")
    is_break = models.BooleanField(default=False, help_text="Mark as break/lunch – skipped in generation")

    class Meta:
        ordering = ['order', 'start_time']

    def __str__(self):
        return f"{self.name} ({self.start_time.strftime('%H:%M')}–{self.end_time.strftime('%H:%M')})"


class TimetableConfiguration(TenantModelMixin, models.Model):
    """Top-level timetable configuration per academic term."""
    DAYS = [
        ('MON', 'Monday'),
        ('TUE', 'Tuesday'),
        ('WED', 'Wednesday'),
        ('THU', 'Thursday'),
        ('FRI', 'Friday'),
        ('SAT', 'Saturday'),
    ]

    name = models.CharField(max_length=100, help_text='e.g. "2024/2025 First Term"')
    active_days = models.JSONField(
        default=list,
        help_text='List of day codes, e.g. ["MON","TUE","WED","THU","FRI"]'
    )
    time_slots = models.ManyToManyField(TimeSlot, blank=True)
    is_active = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        unique_together = ('tenant', 'name')

    def clean(self):
        valid_codes = {d[0] for d in self.DAYS}
        for day in (self.active_days or []):
            if day not in valid_codes:
                raise ValidationError(f"Invalid day code: {day}")

    def save(self, *args, **kwargs):
        # Assign the tenant before full_clean() so the per-school unique
        # check on name actually runs.
        if not self.tenant_id:
            self.tenant = get_current_tenant()
        self.full_clean()
        if self.is_active:
            # Only deactivate this school's other timetables.
            TimetableConfiguration.objects.filter(tenant_id=self.tenant_id) \
                .exclude(pk=self.pk).update(is_active=False)
        super().save(*args, **kwargs)

    def get_active_slots(self):
        return self.time_slots.filter(is_break=False).order_by('order', 'start_time')

    def __str__(self):
        return self.name


class TeacherSubjectClass(TenantModelMixin, models.Model):
    """
    Links a teacher to a subject + class and specifies how many times
    per week that subject should appear in the timetable for that class.
    """
    config = models.ForeignKey(
        TimetableConfiguration, on_delete=models.CASCADE,
        related_name='teacher_subject_classes'
    )
    staff = models.ForeignKey(Staff, on_delete=models.CASCADE, related_name='timetable_assignments')
    subject = models.ForeignKey(Subject, on_delete=models.CASCADE)
    school_class = models.ForeignKey(Class, on_delete=models.CASCADE)
    periods_per_week = models.PositiveSmallIntegerField(
        default=1,
        help_text="How many times this subject appears per week for this class"
    )
    double_periods = models.PositiveSmallIntegerField(
        default=0, help_text="How many of those periods come as back-to-back doubles (each double uses two)")
    room_kind = models.CharField(max_length=10, blank=True, default='',
                                 help_text='Needs a room of this kind, e.g. a lab (blank = the class\'s own room)')

    class Meta:
        unique_together = ('config', 'staff', 'subject', 'school_class')
        ordering = ['school_class__name', 'subject__name']

    def clean(self):
        if self.periods_per_week < 1:
            raise ValidationError("Periods per week must be at least 1.")
        if self.double_periods * 2 > self.periods_per_week:
            raise ValidationError("Each double uses two periods — there aren't enough periods for that many doubles.")
        if self.periods_per_week > 30:
            raise ValidationError("Periods per week cannot exceed 30.")

    def __str__(self):
        return (
            f"{self.staff.full_name} → {self.subject.name} "
            f"({self.school_class.name}) × {self.periods_per_week}/wk"
        )


# ---------------------------------------------------------------------------
# Generated Timetable
# ---------------------------------------------------------------------------

DAY_CHOICES = [
    ('MON', 'Monday'),
    ('TUE', 'Tuesday'),
    ('WED', 'Wednesday'),
    ('THU', 'Thursday'),
    ('FRI', 'Friday'),
    ('SAT', 'Saturday'),
]


class GeneratedTimetable(TenantModelMixin, models.Model):
    """Header record for a successfully generated timetable."""
    config = models.ForeignKey(
        TimetableConfiguration, on_delete=models.CASCADE,
        related_name='generated_timetables'
    )
    generated_at = models.DateTimeField(auto_now_add=True)
    is_published = models.BooleanField(default=False)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ['-generated_at']

    def publish(self):
        GeneratedTimetable.objects.filter(config=self.config).exclude(pk=self.pk).update(is_published=False)
        self.is_published = True
        self.save()

    def __str__(self):
        status = "Published" if self.is_published else "Draft"
        return f"Timetable [{self.config}] – {self.generated_at.strftime('%Y-%m-%d %H:%M')} ({status})"


class TimetableSlot(TenantModelMixin, models.Model):
    """One cell in the generated timetable grid."""
    timetable = models.ForeignKey(
        GeneratedTimetable, on_delete=models.CASCADE, related_name='slots'
    )
    school_class = models.ForeignKey(Class, on_delete=models.CASCADE)
    day = models.CharField(max_length=3, choices=DAY_CHOICES)
    time_slot = models.ForeignKey(TimeSlot, on_delete=models.CASCADE)
    subject = models.ForeignKey(Subject, on_delete=models.CASCADE)
    teacher = models.ForeignKey(Staff, on_delete=models.CASCADE)
    room = models.ForeignKey('TeachingRoom', on_delete=models.SET_NULL, null=True, blank=True, related_name='lessons')

    class Meta:
        unique_together = (
            ('timetable', 'school_class', 'day', 'time_slot'),  # one subject per class per slot
            ('timetable', 'teacher', 'day', 'time_slot'),       # teacher can't be in two places
        )
        ordering = ['school_class__name', 'day', 'time_slot__order']

    def __str__(self):
        return (
            f"{self.school_class} | {self.day} | {self.time_slot.name} | "
            f"{self.subject.name} ({self.teacher.full_name})"
        )


# ---------------------------------------------------------------------------
# Day Override / Special Schedule (e.g. Friday Computer Club)
# ---------------------------------------------------------------------------

class DayOverride(TenantModelMixin, models.Model):
    """
    Defines a custom day schedule for a specific day of the week.
    Overrides the standard timetable with special time blocks (e.g. early
    close + club time on Fridays).
    """
    OVERRIDE_DAY_CHOICES = DAY_CHOICES

    config = models.ForeignKey(
        TimetableConfiguration, on_delete=models.CASCADE,
        related_name='day_overrides'
    )
    day = models.CharField(max_length=3, choices=OVERRIDE_DAY_CHOICES)
    label = models.CharField(
        max_length=100,
        help_text='Short description, e.g. "Friday – Computer Club Day"'
    )
    description = models.TextField(
        blank=True,
        help_text='Optional longer description shown to students/teachers'
    )
    lessons_end_time = models.TimeField(
        help_text='Time when regular lessons finish (e.g. 12:20 for Friday)'
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('config', 'day')
        ordering = ['day']

    def __str__(self):
        return f"{self.get_day_display()} Override: {self.label}"

    def get_special_slots(self):
        return self.special_slots.all().order_by('start_time')


class SpecialSlot(TenantModelMixin, models.Model):
    """
    A custom time block appended to (or replacing part of) a day override.
    Example: Computer Club 12:20–14:10 on Fridays.
    """
    SLOT_TYPE_CHOICES = [
        ('CLUB', 'Club / Extra-curricular'),
        ('ASSEMBLY', 'Assembly'),
        ('SPORT', 'Sport'),
        ('EXAM', 'Exam'),
        ('OTHER', 'Other'),
    ]

    override = models.ForeignKey(
        DayOverride, on_delete=models.CASCADE, related_name='special_slots'
    )
    name = models.CharField(max_length=100, help_text='e.g. "Computer Club"')
    slot_type = models.CharField(max_length=20, choices=SLOT_TYPE_CHOICES, default='CLUB')
    start_time = models.TimeField()
    end_time = models.TimeField()
    venue = models.CharField(max_length=100, blank=True, help_text='e.g. "ICT Lab"')
    supervisor = models.ForeignKey(
        Staff, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='supervised_special_slots'
    )
    notes = models.TextField(blank=True)
    applies_to_all_classes = models.BooleanField(
        default=True,
        help_text='If unchecked, only applies to the selected classes below'
    )
    classes = models.ManyToManyField(
        Class, blank=True,
        help_text='Leave empty if applies to all classes'
    )

    class Meta:
        ordering = ['start_time']

    def clean(self):
        if self.start_time and self.end_time and self.start_time >= self.end_time:
            raise ValidationError("End time must be after start time.")

    def __str__(self):
        return (
            f"{self.name} ({self.start_time.strftime('%H:%M')}–"
            f"{self.end_time.strftime('%H:%M')})"
        )

# ---------------------------------------------------------------------------
# Lesson registers — attendance taken lesson by lesson from the timetable
# ---------------------------------------------------------------------------

class LessonRegister(TenantModelMixin, models.Model):
    """The register for one timetabled lesson on one date."""
    slot = models.ForeignKey(TimetableSlot, on_delete=models.CASCADE, related_name='registers')
    date = models.DateField()
    taken_by = models.ForeignKey(Staff, on_delete=models.SET_NULL, null=True, blank=True,
                                 related_name='lesson_registers')
    taken_at = models.DateTimeField(null=True, blank=True)
    note = models.CharField(max_length=255, blank=True)

    objects = TenantManager()

    class Meta:
        unique_together = ('tenant', 'slot', 'date')
        ordering = ['-date', 'slot__time_slot__order']

    def __str__(self):
        return f"{self.slot} on {self.date}"


class LessonMark(TenantModelMixin, models.Model):
    PRESENT, LATE, ABSENT = 'P', 'L', 'A'
    STATUSES = [(PRESENT, 'Present'), (LATE, 'Late'), (ABSENT, 'Absent')]

    register = models.ForeignKey(LessonRegister, on_delete=models.CASCADE, related_name='marks')
    student = models.ForeignKey('users.Student', on_delete=models.CASCADE, related_name='lesson_marks')
    status = models.CharField(max_length=1, choices=STATUSES, default=PRESENT)
    minutes_late = models.PositiveSmallIntegerField(default=0)

    objects = TenantManager()

    class Meta:
        unique_together = ('tenant', 'register', 'student')


# ---------------------------------------------------------------------------
# Rooms, availability and changes
# ---------------------------------------------------------------------------

class TeachingRoom(TenantModelMixin, models.Model):
    KINDS = [('LAB', 'Science lab'), ('ICT', 'Computer room'), ('HALL', 'Hall'), ('STUDIO', 'Art / music room'),
             ('FIELD', 'Sports field'), ('OTHER', 'Other special room')]
    name = models.CharField(max_length=80)
    kind = models.CharField(max_length=10, choices=KINDS, default='LAB')
    capacity = models.PositiveSmallIntegerField(null=True, blank=True)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ['kind', 'name']
        unique_together = ('tenant', 'name')

    def __str__(self):
        return self.name


class TeacherUnavailability(TenantModelMixin, models.Model):
    """A period a teacher can't teach (part-time staff, other duties)."""
    config = models.ForeignKey(TimetableConfiguration, on_delete=models.CASCADE, related_name='unavailability')
    staff = models.ForeignKey(Staff, on_delete=models.CASCADE, related_name='unavailable_periods')
    day = models.CharField(max_length=3, choices=DAY_CHOICES)
    time_slot = models.ForeignKey(TimeSlot, on_delete=models.CASCADE)

    class Meta:
        unique_together = ('config', 'staff', 'day', 'time_slot')


class TimetableChange(TenantModelMixin, models.Model):
    """A lesson moved by hand after generation, kept so teachers can be told."""
    timetable = models.ForeignKey(GeneratedTimetable, on_delete=models.CASCADE, related_name='changes')
    school_class = models.ForeignKey(Class, on_delete=models.CASCADE, related_name='+')
    subject = models.ForeignKey(Subject, on_delete=models.CASCADE, related_name='+')
    teacher = models.ForeignKey(Staff, on_delete=models.CASCADE, related_name='+')
    from_day = models.CharField(max_length=3)
    from_slot = models.ForeignKey(TimeSlot, on_delete=models.CASCADE, related_name='+')
    to_day = models.CharField(max_length=3)
    to_slot = models.ForeignKey(TimeSlot, on_delete=models.CASCADE, related_name='+')
    changed_by = models.ForeignKey('users.User', on_delete=models.SET_NULL, null=True, related_name='+')
    changed_at = models.DateTimeField(auto_now_add=True)
    notified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-changed_at']


# ---------------------------------------------------------------------------
# Room booking, parent–teacher meetings, version history and calendar feeds
# ---------------------------------------------------------------------------

class RoomBooking(TenantModelMixin, models.Model):
    """A one-off booking of a hall, lab or ICT room (outside its timetabled lessons)."""
    room = models.ForeignKey(TeachingRoom, on_delete=models.CASCADE, related_name='bookings')
    date = models.DateField()
    start_time = models.TimeField()
    end_time = models.TimeField()
    purpose = models.CharField(max_length=160)
    booked_by = models.ForeignKey('users.User', on_delete=models.SET_NULL, null=True, related_name='+')
    cancelled = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        ordering = ['date', 'start_time']

    def __str__(self):
        return f"{self.room} {self.date} {self.start_time:%H:%M}–{self.end_time:%H:%M}: {self.purpose}"


class MeetingEvening(TenantModelMixin, models.Model):
    """A parent–teacher meeting session: parents book short slots with each teacher."""
    title = models.CharField(max_length=120, help_text='e.g. "First term PTA consultation"')
    date = models.DateField()
    start_time = models.TimeField()
    end_time = models.TimeField()
    slot_minutes = models.PositiveSmallIntegerField(default=10)
    location = models.CharField(max_length=120, blank=True)
    notes = models.CharField(max_length=255, blank=True)
    classes = models.ManyToManyField(Class, blank=True, related_name='+',
                                     help_text='Leave empty for every class')
    teachers = models.ManyToManyField(Staff, blank=True, related_name='meeting_evenings',
                                      help_text='Leave empty for every teacher who teaches the class')
    is_open = models.BooleanField(default=True, help_text='Parents can book and cancel')
    created_by = models.ForeignKey('users.User', on_delete=models.SET_NULL, null=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        ordering = ['-date', 'start_time']

    def __str__(self):
        return f"{self.title} ({self.date})"


class MeetingBooking(TenantModelMixin, models.Model):
    evening = models.ForeignKey(MeetingEvening, on_delete=models.CASCADE, related_name='bookings')
    teacher = models.ForeignKey(Staff, on_delete=models.CASCADE, related_name='meeting_bookings')
    student = models.ForeignKey('users.Student', on_delete=models.CASCADE, related_name='meeting_bookings')
    start_time = models.TimeField()
    booked_by = models.ForeignKey('users.User', on_delete=models.SET_NULL, null=True, related_name='+')
    note = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        ordering = ['start_time']
        unique_together = (('evening', 'teacher', 'start_time'), ('evening', 'teacher', 'student'))

    def __str__(self):
        return f"{self.teacher.full_name} – {self.student.full_name} at {self.start_time:%H:%M}"


class TimetableVersion(TenantModelMixin, models.Model):
    """A frozen copy of a timetable as it was published, so it can be compared or restored."""
    config = models.ForeignKey(TimetableConfiguration, on_delete=models.CASCADE, related_name='versions')
    number = models.PositiveIntegerField()
    label = models.CharField(max_length=120, blank=True)
    source = models.ForeignKey(GeneratedTimetable, on_delete=models.SET_NULL, null=True, blank=True,
                               related_name='versions')
    entries = models.JSONField(default=list)
    entry_count = models.PositiveIntegerField(default=0)
    saved_by = models.ForeignKey('users.User', on_delete=models.SET_NULL, null=True, related_name='+')
    saved_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        ordering = ['-number']
        unique_together = ('config', 'number')

    def __str__(self):
        return f"{self.config} v{self.number}"


class CalendarFeed(TenantModelMixin, models.Model):
    """A private, revocable link a person adds to Google/Apple/Outlook calendar."""
    user = models.ForeignKey('users.User', on_delete=models.CASCADE, related_name='calendar_feeds')
    token = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    objects = TenantManager()

    class Meta:
        unique_together = ('tenant', 'user')


# ---------------------------------------------------------------------------
# After-school lessons and coaching classes, billed separately from school fees
# ---------------------------------------------------------------------------

class ExtraLessonGroup(TenantModelMixin, models.Model):
    name = models.CharField(max_length=120, help_text='e.g. "SS3 WAEC Maths coaching"')
    subject = models.ForeignKey(Subject, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    teacher = models.ForeignKey(Staff, on_delete=models.SET_NULL, null=True, blank=True,
                                related_name='extra_lesson_groups')
    day = models.CharField(max_length=3, choices=DAY_CHOICES)
    start_time = models.TimeField()
    end_time = models.TimeField()
    room = models.ForeignKey(TeachingRoom, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    fee = models.DecimalField(max_digits=12, decimal_places=2, default=0, help_text='Per child, for the term')
    teacher_pay = models.DecimalField(max_digits=12, decimal_places=2, default=0,
                                      help_text="Paid to the teacher for each session (added to their salary)")
    term = models.ForeignKey('academics.Term', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    capacity = models.PositiveSmallIntegerField(null=True, blank=True)
    classes = models.ManyToManyField(Class, blank=True, related_name='+', help_text='Who can join (empty = anyone)')
    open_to_parents = models.BooleanField(default=True, help_text='Parents can sign their child up')
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        ordering = ['day', 'start_time', 'name']

    def __str__(self):
        return self.name


class ExtraLessonEnrolment(TenantModelMixin, models.Model):
    DUE, PAID, WAIVED = 'DUE', 'PAID', 'WAIVED'
    STATUSES = [(DUE, 'To pay'), (PAID, 'Paid'), (WAIVED, 'Free / waived')]
    group = models.ForeignKey(ExtraLessonGroup, on_delete=models.CASCADE, related_name='enrolments')
    student = models.ForeignKey('users.Student', on_delete=models.CASCADE, related_name='extra_lessons')
    fee = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    status = models.CharField(max_length=6, choices=STATUSES, default=DUE)
    paid_at = models.DateTimeField(null=True, blank=True)
    payment_note = models.CharField(max_length=160, blank=True)
    transaction = models.ForeignKey('finance.Transaction', on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name='+')
    enrolled_by = models.ForeignKey('users.User', on_delete=models.SET_NULL, null=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        unique_together = ('group', 'student')
        ordering = ['student__full_name']
