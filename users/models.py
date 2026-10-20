"""
users/models.py — tenant-scoped
Every model (except User itself) now carries a `tenant` FK via TenantModelMixin.
User has its own `tenant` FK; usernames are unique per school (see User.Meta).
"""

from django.contrib.auth.models import AbstractUser, UserManager
from django.contrib.auth.validators import UnicodeUsernameValidator
from django.db import models
from django.db.models import Q
from django.conf import settings
from django.utils import timezone
import datetime
from datetime import date
import random, string

from tenants.managers import TenantModelMixin, TenantManager
from security.crypto import EncryptedTextField
from security.media import signed_storage


class TenantUserManager(UserManager):

    def get_by_natural_key(self, username):
        """Used by ModelBackend at login. Usernames are only unique per
        school, so look the user up in the current school first; fall back
        to platform-level accounts (no school, e.g. superadmins)."""
        from tenants.middleware import get_current_tenant
        qs = self.filter(**{self.model.USERNAME_FIELD: username})
        tenant = get_current_tenant()
        user = None
        if tenant is not None:
            user = qs.filter(tenant=tenant).first()
        if user is None:
            user = qs.filter(tenant__isnull=True).first()
        if user is None:
            raise self.model.DoesNotExist
        return user


class User(AbstractUser):
    # Same as AbstractUser.username but without unique=True — uniqueness
    # is per school instead (see Meta.constraints).
    username = models.CharField(
        'username',
        max_length=150,
        help_text='Required. 150 characters or fewer. Letters, digits and @/./+/-/_ only.',
        validators=[UnicodeUsernameValidator()],
    )
    is_staff_user    = models.BooleanField(default=False)
    is_student       = models.BooleanField(default=False)
    is_parent        = models.BooleanField(default=False)
    restricted       = models.BooleanField(default=False)
    term_password    = models.CharField(max_length=128, blank=True)
    must_change_password = models.BooleanField(
        default=False,
        help_text="Forces the user to set a new password on next login. "
                  "Set True for auto-generated accounts (e.g. school admin created after payment)."
    )
    can_create_course = models.BooleanField(default=False)
    can_create_exam   = models.BooleanField(default=False)
    # Tenant this user belongs to — NULL only for platform superadmins
    tenant = models.ForeignKey(
        'tenants.Tenant',
        on_delete=models.CASCADE,
        null=True, blank=True,
        related_name='users',
        db_index=True,
    )

    objects = TenantUserManager()

    class Meta(AbstractUser.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=['tenant', 'username'], name='unique_username_per_tenant'),
            # Platform accounts (no school) must still be globally unique
            models.UniqueConstraint(
                fields=['username'], condition=Q(tenant__isnull=True),
                name='unique_username_platform'),
        ]

    def __str__(self):
        return self.username

    @property
    def staff(self):
        """Return this user's Staff profile for the current tenant."""
        from tenants.middleware import get_current_tenant
        tenant = get_current_tenant()
        if tenant:
            return self.staff_profiles.filter(tenant=tenant).first()
        return self.staff_profiles.first()

    @property
    def student(self):
        """Return this user's Student profile for the current tenant."""
        from tenants.middleware import get_current_tenant
        tenant = get_current_tenant()
        if tenant:
            return self.student_profiles.filter(tenant=tenant).first()
        return self.student_profiles.first()


class Staff(TenantModelMixin, models.Model):
    ROLE_CHOICES = [
        ('ADMIN','Administrator'),('PROPRIETOR','Proprietor / Proprietress'),('TEACHER','Teacher'),
        ('ACCOUNT','Account Officer'),('LIB','Librarian'),
    ]
    user      = models.ForeignKey(User, on_delete=models.CASCADE, related_name='staff_profiles')
    full_name = models.CharField(max_length=255)
    role      = models.CharField(max_length=20, choices=ROLE_CHOICES, default='TEACHER')
    can_generate_password = models.BooleanField(default=False)
    can_create_staff      = models.BooleanField(default=False)
    can_manage_students   = models.BooleanField(default=True)
    can_manage_exams      = models.BooleanField(default=False)
    can_view_all_exams    = models.BooleanField(default=False)
    can_enter_scores      = models.BooleanField(default=False)
    can_view_all_results  = models.BooleanField(default=False)
    can_publish_results   = models.BooleanField(default=False)
    can_access_finance    = models.BooleanField(default=False)
    can_approve_payments  = models.BooleanField(default=False)
    can_access_hostel     = models.BooleanField(default=False)
    can_manage_boarders   = models.BooleanField(default=False)
    can_send_messages     = models.BooleanField(default=False)
    can_mark_attendance   = models.BooleanField(default=False)
    can_access_inventory  = models.BooleanField(default=False)
    objects = TenantManager()
    class Meta:
        unique_together = ('tenant', 'user')
    def __str__(self):
        return f"{self.full_name} ({self.role})"


class Class(TenantModelMixin, models.Model):
    name        = models.CharField(max_length=50)
    description = models.TextField(blank=True, null=True)
    level       = models.CharField(max_length=40, blank=True,
                                   help_text='The year group, e.g. "JSS 1". Arms share a level.')
    arm         = models.CharField(max_length=10, blank=True, help_text='e.g. "A", "Gold".')
    form_teacher = models.ForeignKey('Staff', on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name='form_classes')
    objects = TenantManager()
    class Meta:
        unique_together = ('tenant', 'name')
        verbose_name_plural = 'Classes'
    def __str__(self):
        return self.name


STATUS_OPTIONS  = [('Active','Active'),('Suspended','Suspended'),('Withdrawn','Withdrawn'),
                   ('Graduated','Graduated'),('Deceased','Deceased')]
# Still on the roll: counted in classes, registers, results and messages.
# A suspended student is away for a while, not gone.
ENROLLED_STATUSES = ('Active', 'Suspended')
# Gone for good: their own login is switched off.
LEFT_STATUSES = ('Withdrawn', 'Graduated', 'Deceased')
TERM_OPTIONS    = [('1st Term','1st Term'),('2nd Term','2nd Term'),('3rd Term','3rd Term')]
GENDER_OPTIONS  = [('Male','Male'),('Female','Female')]


class Student(TenantModelMixin, models.Model):
    user             = models.ForeignKey(User, on_delete=models.CASCADE, related_name='student_profiles')
    full_name        = models.CharField(max_length=255)
    gender           = models.CharField(max_length=10, choices=GENDER_OPTIONS, blank=True, null=True)
    date_of_birth    = models.DateField(blank=True, null=True)
    admission_number = models.CharField(max_length=30)
    class_assigned   = models.ForeignKey(Class, on_delete=models.SET_NULL, null=True, blank=True)
    status           = models.CharField(max_length=20, choices=STATUS_OPTIONS, default='Active')
    current_term     = models.CharField(max_length=20, choices=TERM_OPTIONS, default='1st Term')
    current_session  = models.ForeignKey('academics.AcademicSession', on_delete=models.SET_NULL,
                        null=True, blank=True, related_name='students')
    passport         = models.ImageField(upload_to='student_passports/', storage=signed_storage, blank=True, null=True)
    parent_name          = models.CharField(max_length=255, blank=True, null=True)
    parent_phone         = models.CharField(max_length=20, blank=True, null=True)
    parent_email         = models.EmailField(blank=True, null=True)
    parent_term_password = models.CharField(max_length=128, blank=True, null=True)
    address          = models.TextField(blank=True, null=True)
    status_reason    = models.CharField(max_length=255, blank=True)
    status_changed_on = models.DateField(blank=True, null=True)
    left_on          = models.DateField(blank=True, null=True,
                                        help_text='The day a student withdrew or graduated.')
    leaving_class    = models.CharField(max_length=50, blank=True,
                                        help_text='The class they were in when they left.')
    destination      = models.CharField(max_length=255, blank=True,
                                        help_text='Next school, university or work, if known.')
    nin              = models.CharField('NIN', max_length=11, blank=True,
                                        help_text='National Identification Number (11 digits).')
    house            = models.ForeignKey('House', on_delete=models.SET_NULL, null=True, blank=True,
                                         related_name='members')
    created_at       = models.DateTimeField(auto_now_add=True)
    objects = TenantManager()
    class Meta:
        unique_together = ('tenant', 'admission_number')
    def __str__(self):
        return f"{self.full_name} ({self.admission_number})"
    def generate_term_password(self):
        from users.passwords import generate_term_password
        sp = generate_term_password()
        pp = generate_term_password()
        self.user.set_password(sp); self.user.term_password = sp; self.user.save()
        self.parent_term_password = pp
        return sp, pp
    @property
    def is_enrolled(self):
        return self.status in ENROLLED_STATUSES

    @property
    def is_alumnus(self):
        return self.status == 'Graduated'

    @property
    def age(self):
        if self.date_of_birth:
            t = date.today()
            return t.year - self.date_of_birth.year - (
                (t.month, t.day) < (self.date_of_birth.month, self.date_of_birth.day))
        return None


class Subject(TenantModelMixin, models.Model):
    name        = models.CharField(max_length=100)
    code        = models.CharField(max_length=20, blank=True, null=True)
    description = models.TextField(blank=True, null=True)
    objects = TenantManager()
    class Meta:
        unique_together = ('tenant', 'name')
    def __str__(self):
        return self.name


class ClassSubject(TenantModelMixin, models.Model):
    school_class = models.ForeignKey(Class, on_delete=models.CASCADE, related_name='class_subjects')
    subject      = models.ForeignKey(Subject, on_delete=models.CASCADE)
    objects = TenantManager()
    class Meta:
        unique_together = ('tenant', 'school_class', 'subject')
    def __str__(self):
        return f"{self.school_class} – {self.subject}"


class StudentSubject(TenantModelMixin, models.Model):
    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name='student_subjects')
    subject = models.ForeignKey(Subject, on_delete=models.CASCADE)
    objects = TenantManager()
    class Meta:
        unique_together = ('tenant', 'student', 'subject')


class StaffSubjectClass(TenantModelMixin, models.Model):
    staff        = models.ForeignKey(Staff, on_delete=models.CASCADE, related_name='subject_classes')
    subject      = models.ForeignKey(Subject, on_delete=models.CASCADE)
    school_class = models.ForeignKey(Class, on_delete=models.CASCADE)
    objects = TenantManager()
    class Meta:
        unique_together = ('tenant', 'staff', 'subject', 'school_class')
    def __str__(self):
        return f"{self.staff.full_name} – {self.subject.name} ({self.school_class.name})"


ATTENDANCE_STATUS = [('P','Present'),('A','Absent')]
ABSENCE_REASONS = [('SICK', 'Illness'), ('MEDICAL', 'Medical appointment'), ('FAMILY', 'Family matter'),
                   ('RELIGIOUS', 'Religious observance'), ('TRAVEL', 'Travel'),
                   ('BEREAVE', 'Bereavement'), ('OTHER', 'Other')]
EXCUSED_CHOICES = [('PENDING', 'Waiting for review'), ('EXCUSED', 'Excused'),
                   ('UNEXCUSED', 'Not excused')]


class Attendance(TenantModelMixin, models.Model):
    student      = models.ForeignKey(Student, on_delete=models.CASCADE, related_name='attendances')
    school_class = models.ForeignKey(Class, on_delete=models.CASCADE, related_name='attendances')
    subject      = models.ForeignKey(Subject, on_delete=models.CASCADE, null=True, blank=True)
    date         = models.DateField(default=timezone.now)
    status       = models.CharField(max_length=1, choices=ATTENDANCE_STATUS, default='P')
    remarks      = models.TextField(blank=True, null=True)
    # Lateness — filled from the gate scanner or by hand. A late student is present.
    minutes_late = models.PositiveSmallIntegerField(default=0)
    arrived_at   = models.TimeField(null=True, blank=True)
    # Why a student was absent, who said so, and whether the school accepts it.
    absence_reason = models.CharField(max_length=10, choices=ABSENCE_REASONS, blank=True)
    reason_note  = models.CharField(max_length=255, blank=True)
    reason_from  = models.CharField(max_length=6, choices=[('PARENT', 'Parent'), ('STAFF', 'Staff')],
                                    blank=True)
    excused      = models.CharField(max_length=9, choices=EXCUSED_CHOICES, blank=True)
    alerted_at   = models.DateTimeField(null=True, blank=True,
                                        help_text='When the parent was told the child was absent.')
    objects = TenantManager()
    class Meta:
        unique_together = ('tenant','student','school_class','subject','date')
        ordering = ['-date']
    def __str__(self):
        return f"{self.student.full_name} – {self.date} – {self.status}"


PROMOTION_ACTION_CHOICES = [
    ('PROMOTED','Promoted'),('RETURNED','Returned / Repeated'),
    ('GRADUATED','Graduated'),('WITHDRAWN','Withdrawn'),
]


class StudentPromotionRecord(TenantModelMixin, models.Model):
    student      = models.ForeignKey(Student, on_delete=models.CASCADE, related_name='promotion_records')
    promoted_by  = models.ForeignKey(Staff, on_delete=models.SET_NULL, null=True)
    from_class   = models.ForeignKey(Class, on_delete=models.SET_NULL, null=True, related_name='promotions_from')
    from_term    = models.CharField(max_length=20)
    from_session = models.ForeignKey('academics.AcademicSession', on_delete=models.SET_NULL, null=True, related_name='promotions_from')
    to_class     = models.ForeignKey(Class, on_delete=models.SET_NULL, null=True, related_name='promotions_to')
    to_term      = models.CharField(max_length=20)
    to_session   = models.ForeignKey('academics.AcademicSession', on_delete=models.SET_NULL, null=True, related_name='promotions_to')
    action       = models.CharField(max_length=20, choices=PROMOTION_ACTION_CHOICES, default='PROMOTED')
    note         = models.TextField(blank=True)
    promoted_at  = models.DateTimeField(auto_now_add=True)
    objects = TenantManager()
    class Meta:
        ordering = ['-promoted_at']
    def __str__(self):
        return f"{self.student.full_name}: {self.from_class} → {self.to_class}"


# ── Student history ───────────────────────────────────────────────────────────
class ClassHistory(TenantModelMixin, models.Model):
    """
    One spell in one class: which class and section, in which session, from
    when to when, and how the student got there. Written automatically
    whenever a student's class changes, so the record shows every class a
    pupil passed through from admission to graduation.
    """
    HOW = [('ADMITTED', 'Joined the school'), ('PROMOTED', 'Promoted'), ('REPEATED', 'Repeated the class'),
           ('MOVED', 'Changed class'), ('EARLIER', 'From earlier records')]
    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name='class_history')
    school_class = models.ForeignKey(Class, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    class_name = models.CharField(max_length=50)
    section = models.ForeignKey('saas_admin.Campus', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    section_name = models.CharField(max_length=120, blank=True)
    session = models.ForeignKey('academics.AcademicSession', on_delete=models.SET_NULL, null=True, blank=True,
                                related_name='+')
    session_name = models.CharField(max_length=40, blank=True)
    started_on = models.DateField()
    ended_on = models.DateField(null=True, blank=True)
    how = models.CharField(max_length=8, choices=HOW, default='MOVED')
    recorded_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    objects = TenantManager()

    class Meta:
        ordering = ['-started_on', '-id']

    def __str__(self):
        return f"{self.student.full_name}: {self.class_name} from {self.started_on}"

    @property
    def current(self):
        return self.ended_on is None


class StudentStatusChange(TenantModelMixin, models.Model):
    """Each move between active, suspended, withdrawn, graduated and deceased."""
    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name='status_changes')
    from_status = models.CharField(max_length=20, choices=STATUS_OPTIONS)
    to_status = models.CharField(max_length=20, choices=STATUS_OPTIONS)
    effective_on = models.DateField(default=timezone.now)
    reason = models.CharField(max_length=255, blank=True)
    until = models.DateField(blank=True, null=True,
                             help_text='For a suspension: the day the student may return.')
    changed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='student_status_changes')
    created_at = models.DateTimeField(auto_now_add=True)
    objects = TenantManager()

    class Meta:
        ordering = ['-effective_on', '-created_at']

    def __str__(self):
        return f"{self.student.full_name}: {self.from_status} → {self.to_status}"


class StudentChange(TenantModelMixin, models.Model):
    """One field on a student's record that changed: who, when, old and new value."""
    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name='changes')
    field = models.CharField(max_length=50)
    label = models.CharField(max_length=80)
    old_value = models.TextField(blank=True)
    new_value = models.TextField(blank=True)
    changed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='student_changes')
    source = models.CharField(max_length=40, blank=True,
                              help_text='Where the change came from — edit form, promotion…')
    changed_at = models.DateTimeField(default=timezone.now)
    objects = TenantManager()

    class Meta:
        ordering = ['-changed_at', '-id']

    def __str__(self):
        return f"{self.student.full_name}: {self.label} {self.old_value!r} → {self.new_value!r}"



# ── Guardians, documents and custom fields ────────────────────────────────────
def mask_nin(nin):
    """Show only the last four digits of a NIN to people who do not manage records."""
    return f"•••••••{nin[-4:]}" if nin and len(nin) >= 4 else ''


class Guardian(TenantModelMixin, models.Model):
    """
    Another adult responsible for a student — mother, father, guardian or
    sponsor — besides the main contact on the student's record.
    """
    RELATIONSHIPS = [('MOTHER', 'Mother'), ('FATHER', 'Father'), ('GUARDIAN', 'Guardian'),
                     ('SPONSOR', 'Sponsor'), ('GRANDPARENT', 'Grandparent'),
                     ('SIBLING', 'Older sibling'), ('OTHER', 'Other')]
    CONTACT_CHOICES = [('BOTH', 'SMS and email'), ('SMS', 'SMS only'), ('EMAIL', 'Email only'),
                       ('NONE', 'Do not message')]

    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name='guardians')
    full_name = models.CharField(max_length=160)
    relationship = models.CharField(max_length=12, choices=RELATIONSHIPS, default='GUARDIAN')
    phone = models.CharField(max_length=20, blank=True)
    email = models.EmailField(blank=True)
    address = models.CharField(max_length=255, blank=True)
    occupation = models.CharField(max_length=120, blank=True)
    nin = models.CharField('NIN', max_length=11, blank=True)
    photo = models.ImageField(upload_to='guardian_photos/', storage=signed_storage, blank=True, null=True,
                              help_text='Shown to gate staff when this person collects the child.')
    contact_by = models.CharField(max_length=5, choices=CONTACT_CHOICES, default='BOTH',
                                  help_text='How the school may message this person.')
    gets_fee_messages = models.BooleanField(default=False,
                                            help_text='Receives fee reminders as well.')
    can_pick_up = models.BooleanField(default=True,
                                      help_text='Allowed to collect the child from school.')
    emergency_contact = models.BooleanField(default=False)
    note = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    objects = TenantManager()

    class Meta:
        ordering = ['-emergency_contact', 'full_name']

    def __str__(self):
        return f"{self.full_name} ({self.get_relationship_display()} of {self.student.full_name})"

    def wants(self, channel):
        return self.contact_by == 'BOTH' or self.contact_by == channel


def _document_path(instance, filename):
    import os
    import uuid
    ext = os.path.splitext(filename)[1].lower()[:8]
    return f"student_documents/{instance.tenant_id}/{instance.student_id}/{uuid.uuid4().hex}{ext}"


def _private_storage():
    return PrivateStorage()


from django.core.files.storage import FileSystemStorage as _FileSystemStorage  # noqa: E402


class PrivateStorage(_FileSystemStorage):
    """
    Files with no public URL. The folder is read from settings each time it
    is needed rather than fixed when the app starts, so a deployment can move
    it and tests can point it at a temporary folder.
    """

    @property
    def base_location(self):
        from django.conf import settings
        return str(settings.PRIVATE_MEDIA_ROOT)

    @property
    def location(self):
        import os
        return os.path.abspath(self.base_location)

    @property
    def base_url(self):
        return None

    def url(self, name):
        raise ValueError('Private files have no URL; serve them through a checked view.')


class StudentDocument(TenantModelMixin, models.Model):
    """
    A certificate, medical form or ID kept for a student. Stored outside the
    public media folder and only served to staff through a checked view.
    """
    TYPES = [('BIRTH', 'Birth certificate'), ('PREV_RESULT', 'Previous school result'),
             ('TRANSFER', 'Transfer letter'), ('MEDICAL', 'Medical report'),
             ('IMMUNISATION', 'Immunisation card'), ('NIN', 'NIN slip'),
             ('PASSPORT', 'International passport'), ('VISA', 'Visa or residence permit'),
             ('CONSENT', 'Signed consent form'), ('OTHER', 'Other')]

    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name='documents')
    doc_type = models.CharField(max_length=14, choices=TYPES, default='OTHER')
    title = models.CharField(max_length=160, blank=True)
    file = models.FileField(upload_to=_document_path, storage=_private_storage)
    original_name = models.CharField(max_length=255, blank=True)
    size = models.PositiveIntegerField(default=0)
    issued_on = models.DateField(null=True, blank=True)
    expires_on = models.DateField(null=True, blank=True)
    note = models.CharField(max_length=255, blank=True)
    uploaded_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name='student_documents')
    created_at = models.DateTimeField(auto_now_add=True)
    objects = TenantManager()

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.display_name} — {self.student.full_name}"

    @property
    def display_name(self):
        return self.title or self.get_doc_type_display()

    @property
    def days_to_expiry(self):
        if not self.expires_on:
            return None
        return (self.expires_on - timezone.localdate()).days

    @property
    def is_expired(self):
        d = self.days_to_expiry
        return d is not None and d < 0

    @property
    def expires_soon(self):
        d = self.days_to_expiry
        return d is not None and 0 <= d <= 30


class StudentField(TenantModelMixin, models.Model):
    """A question a school adds to every student's record — state of origin, blood group…"""
    TEXT, NUMBER, DATE, CHOICE, YES_NO = 'TEXT', 'NUMBER', 'DATE', 'CHOICE', 'YES_NO'
    TYPES = [(TEXT, 'Text'), (NUMBER, 'Number'), (DATE, 'Date'), (CHOICE, 'Pick from a list'),
             (YES_NO, 'Yes / No')]

    label = models.CharField(max_length=80)
    key = models.SlugField(max_length=60)
    field_type = models.CharField(max_length=8, choices=TYPES, default=TEXT)
    choices = models.TextField(blank=True, help_text='One option per line, for a list.')
    help_text = models.CharField(max_length=160, blank=True)
    required = models.BooleanField(default=False)
    sensitive = models.BooleanField(default=False,
                                    help_text='Only administrators can see the answer.')
    order = models.PositiveSmallIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    objects = TenantManager()

    class Meta:
        ordering = ['order', 'label']
        unique_together = ('tenant', 'key')

    def __str__(self):
        return self.label

    @property
    def options(self):
        return [line.strip() for line in self.choices.splitlines() if line.strip()]


class StudentFieldValue(TenantModelMixin, models.Model):
    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name='field_values')
    field = models.ForeignKey(StudentField, on_delete=models.CASCADE, related_name='values')
    value = models.TextField(blank=True)
    objects = TenantManager()

    class Meta:
        unique_together = ('tenant', 'student', 'field')

    def __str__(self):
        return f"{self.field.label}: {self.value}"



# ── Houses ────────────────────────────────────────────────────────────────────
class House(TenantModelMixin, models.Model):
    """A sports house — its colour, its staff lead and captain, and its points."""
    name = models.CharField(max_length=60)
    colour = models.CharField(max_length=7, default='#c9a96e', help_text='Hex colour, e.g. #1d4ed8.')
    motto = models.CharField(max_length=160, blank=True)
    house_master = models.ForeignKey(Staff, on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name='houses_led')
    captain = models.ForeignKey(Student, on_delete=models.SET_NULL, null=True, blank=True,
                                related_name='houses_captained')
    is_active = models.BooleanField(default=True)
    objects = TenantManager()

    class Meta:
        ordering = ['name']
        unique_together = ('tenant', 'name')

    def __str__(self):
        return self.name


class HousePoint(TenantModelMixin, models.Model):
    """Points won or lost by a house, with why — and the student, when one earned them."""
    house = models.ForeignKey(House, on_delete=models.CASCADE, related_name='points')
    points = models.IntegerField()
    reason = models.CharField(max_length=200)
    student = models.ForeignKey(Student, on_delete=models.SET_NULL, null=True, blank=True,
                                related_name='house_points')
    awarded_on = models.DateField(default=timezone.now)
    awarded_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='house_points_awarded')
    created_at = models.DateTimeField(auto_now_add=True)
    objects = TenantManager()

    class Meta:
        ordering = ['-awarded_on', '-created_at']

    def __str__(self):
        return f"{self.house}: {self.points:+d} — {self.reason}"


class DuplicateDismissal(TenantModelMixin, models.Model):
    """Two records an administrator checked and confirmed are different students."""
    first = models.ForeignKey(Student, on_delete=models.CASCADE, related_name='+')
    second = models.ForeignKey(Student, on_delete=models.CASCADE, related_name='+')
    dismissed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    objects = TenantManager()

    class Meta:
        unique_together = ('tenant', 'first', 'second')


# ── Awards, leadership, support needs, change requests, data exports ──────────
class Award(TenantModelMixin, models.Model):
    """A prize, competition placing or certificate a student earned."""
    CATEGORIES = [('ACADEMIC', 'Academic'), ('SPORTS', 'Sports'), ('ARTS', 'Arts & culture'),
                  ('LEADERSHIP', 'Leadership'), ('CONDUCT', 'Conduct'),
                  ('COMPETITION', 'Competition'), ('OTHER', 'Other')]
    LEVELS = [('SCHOOL', 'School'), ('INTER', 'Inter-school'), ('STATE', 'State'),
              ('NATIONAL', 'National'), ('INTERNATIONAL', 'International')]

    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name='awards')
    title = models.CharField(max_length=160)
    category = models.CharField(max_length=12, choices=CATEGORIES, default='ACADEMIC')
    level = models.CharField(max_length=13, choices=LEVELS, default='SCHOOL')
    position = models.CharField(max_length=40, blank=True, help_text='e.g. 1st, Gold, Finalist.')
    awarded_on = models.DateField(default=timezone.now)
    session = models.ForeignKey('academics.AcademicSession', on_delete=models.SET_NULL,
                                null=True, blank=True, related_name='awards')
    term = models.ForeignKey('academics.Term', on_delete=models.SET_NULL, null=True, blank=True,
                             related_name='awards')
    description = models.CharField(max_length=255, blank=True)
    show_on_report = models.BooleanField(default=True,
                                         help_text="Print it on the term's report card.")
    recorded_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name='awards_recorded')
    created_at = models.DateTimeField(auto_now_add=True)
    objects = TenantManager()

    class Meta:
        ordering = ['-awarded_on', '-created_at']

    def __str__(self):
        return f"{self.title} — {self.student.full_name}"


class LeadershipRole(TenantModelMixin, models.Model):
    """Head girl, prefect, class captain, club president — and for how long."""
    KINDS = [('SCHOOL', 'School'), ('CLASS', 'Class'), ('HOUSE', 'House'), ('CLUB', 'Club'),
             ('SPORT', 'Sport')]

    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name='roles')
    title = models.CharField(max_length=100, help_text='e.g. Head Girl, Labour Prefect.')
    kind = models.CharField(max_length=6, choices=KINDS, default='SCHOOL')
    group = models.CharField(max_length=100, blank=True,
                             help_text='Which class, house or club, where it applies.')
    start_date = models.DateField(default=timezone.now)
    end_date = models.DateField(null=True, blank=True)
    session = models.ForeignKey('academics.AcademicSession', on_delete=models.SET_NULL,
                                null=True, blank=True, related_name='leadership_roles')
    note = models.CharField(max_length=255, blank=True)
    appointed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name='roles_appointed')
    created_at = models.DateTimeField(auto_now_add=True)
    objects = TenantManager()

    class Meta:
        ordering = ['-start_date', 'title']

    def __str__(self):
        return f"{self.title} — {self.student.full_name}"

    @property
    def is_current(self):
        return self.end_date is None or self.end_date >= timezone.localdate()


class SupportNeeds(TenantModelMixin, models.Model):
    """
    Adjustments a student needs to learn and be assessed fairly. Teachers see
    the adjustments so they can apply them; only administrators see the
    private notes about why.
    """
    EXTRA_TIME = [(0, 'None'), (10, '10% extra'), (25, '25% extra'), (50, '50% extra'),
                  (100, 'Double time')]

    student = models.OneToOneField(Student, on_delete=models.CASCADE, related_name='support')
    extra_time_percent = models.PositiveSmallIntegerField(choices=EXTRA_TIME, default=0)
    large_print = models.BooleanField(default=False)
    reader = models.BooleanField(default=False, help_text='Needs questions read aloud.')
    scribe = models.BooleanField(default=False, help_text='Needs someone to write answers.')
    rest_breaks = models.BooleanField(default=False)
    seat_near_front = models.BooleanField(default=False)
    separate_room = models.BooleanField(default=False)
    classroom_notes = models.CharField(max_length=255, blank=True,
                                       help_text='What teachers should do — visible to all staff.')
    private_notes = EncryptedTextField(blank=True,
                                     help_text='Diagnosis or background — administrators only.')
    review_on = models.DateField(null=True, blank=True)
    updated_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='+')
    updated_at = models.DateTimeField(auto_now=True)
    objects = TenantManager()

    def __str__(self):
        return f"Support for {self.student.full_name}"

    @property
    def adjustments(self):
        out = []
        if self.extra_time_percent:
            out.append(self.get_extra_time_percent_display())
        for flag, label in (('large_print', 'Large print'), ('reader', 'Reader'),
                            ('scribe', 'Scribe'), ('rest_breaks', 'Rest breaks'),
                            ('seat_near_front', 'Seat near the front'),
                            ('separate_room', 'Separate room')):
            if getattr(self, flag):
                out.append(label)
        return out


class ProfileChangeRequest(TenantModelMixin, models.Model):
    """A parent asking for a detail on their child's record to be corrected."""
    FIELDS = [('parent_name', 'Parent name'), ('parent_phone', 'Parent phone'),
              ('parent_email', 'Parent email'), ('address', 'Home address'),
              ('date_of_birth', 'Date of birth'), ('full_name', "Child's name spelling")]
    PENDING, APPROVED, REJECTED = 'PENDING', 'APPROVED', 'REJECTED'
    STATUSES = [(PENDING, 'Waiting'), (APPROVED, 'Approved'), (REJECTED, 'Declined')]

    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name='change_requests')
    field = models.CharField(max_length=20, choices=FIELDS)
    current_value = models.CharField(max_length=255, blank=True)
    new_value = models.CharField(max_length=255)
    note = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=8, choices=STATUSES, default=PENDING)
    requested_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name='+')
    reviewed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name='+')
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_note = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    objects = TenantManager()

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.get_field_display()} for {self.student.full_name}: {self.new_value}"


class DataExport(TenantModelMixin, models.Model):
    """A record that someone downloaded everything held about a child, and why."""
    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name='data_exports')
    exported_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name='+')
    reason = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)
    objects = TenantManager()

    class Meta:
        ordering = ['-created_at']



class AttendanceSettings(TenantModelMixin, models.Model):
    """How a school runs attendance: start time, lateness, alerts and follow-up thresholds."""
    school_starts_at = models.TimeField(default=datetime.time(8, 0))
    late_grace_minutes = models.PositiveSmallIntegerField(
        default=0, help_text='Arrivals within this many minutes after the start are not late.')
    absence_alerts = models.BooleanField(default=True,
                                         help_text='Message parents when a child is marked absent.')
    alert_channel = models.CharField(max_length=5, default='SMS',
                                     choices=[('SMS', 'SMS'), ('EMAIL', 'Email'), ('BOTH', 'Both')])
    chronic_rate = models.PositiveSmallIntegerField(
        default=90, help_text='Flag students whose attendance this term falls below this percent.')
    chronic_streak = models.PositiveSmallIntegerField(
        default=3, help_text='Flag students absent this many school days in a row.')
    gate_marks_present = models.BooleanField(
        default=True, help_text='A gate arrival scan marks the student present for the day.')
    updated_at = models.DateTimeField(auto_now=True)
    objects = TenantManager()

    class Meta:
        constraints = [models.UniqueConstraint(fields=['tenant'], name='one_attendance_settings_per_school')]

    def __str__(self):
        return f"Attendance settings — {self.tenant}"
