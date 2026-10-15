"""
welfare/models.py
─────────────────
Health, welfare and discipline: medical profiles and school health plans,
immunisations, medication, health screening, accidents, discipline
incidents, merit/demerit thresholds, detentions, suspension cases,
counselling, safeguarding, support plans, restorative meetings, sports teams
and fitness tests, and the wellbeing resource hub.

The sick bay itself lives in hostel.SickBayVisit (it already covers day
pupils as well as boarders) and behaviour points in teaching.BehaviourEntry;
this app builds on both.
"""
from django.conf import settings
from django.db import models

from security.crypto import EncryptedTextField

from tenants.managers import TenantManager, TenantModelMixin

USER = settings.AUTH_USER_MODEL


def _private():
    from users.models import PrivateStorage
    return PrivateStorage()


def _upload(instance, filename):
    import os
    import uuid
    ext = os.path.splitext(filename)[1].lower()[:8]
    return f"welfare/{instance.tenant_id}/{instance.__class__.__name__.lower()}/{uuid.uuid4().hex}{ext}"


class WelfareSettings(TenantModelMixin, models.Model):
    nurses = models.ManyToManyField('users.Staff', blank=True, related_name='+')
    counsellors = models.ManyToManyField('users.Staff', blank=True, related_name='+')
    safeguarding_leads = models.ManyToManyField('users.Staff', blank=True, related_name='+')
    discipline_team = models.ManyToManyField('users.Staff', blank=True, related_name='+')
    tell_parents_incidents = models.BooleanField(default=True, help_text='Email families about discipline incidents')
    immunisation_reminder_days = models.PositiveSmallIntegerField(default=14)

    objects = TenantManager()


# ─────────────────────────────────────────────────────────────────────────────
# Health
# ─────────────────────────────────────────────────────────────────────────────

class HealthPlan(TenantModelMixin, models.Model):
    name = models.CharField(max_length=120, help_text='e.g. School health plan 2026/27')
    provider = models.CharField(max_length=120, help_text='HMO or insurer')
    phone = models.CharField(max_length=30, blank=True)
    hospital = models.CharField(max_length=200, blank=True, help_text='Main hospital or clinic on the plan')
    covers = models.TextField(blank=True)
    starts = models.DateField(null=True, blank=True)
    ends = models.DateField(null=True, blank=True)
    active = models.BooleanField(default=True)

    objects = TenantManager()

    class Meta:
        ordering = ['-active', 'name']

    def __str__(self):
        return f'{self.name} ({self.provider})'


class MedicalProfile(TenantModelMixin, models.Model):
    BLOOD = [('', '—'), ('A+', 'A+'), ('A-', 'A−'), ('B+', 'B+'), ('B-', 'B−'), ('AB+', 'AB+'), ('AB-', 'AB−'),
             ('O+', 'O+'), ('O-', 'O−')]
    GENOTYPE = [('', '—'), ('AA', 'AA'), ('AS', 'AS'), ('AC', 'AC'), ('SS', 'SS'), ('SC', 'SC')]
    student = models.OneToOneField('users.Student', on_delete=models.CASCADE, related_name='medical')
    blood_group = models.CharField(max_length=3, choices=BLOOD, blank=True)
    genotype = models.CharField(max_length=2, choices=GENOTYPE, blank=True)
    allergies = models.CharField(max_length=255, blank=True, help_text='Food, drugs, insects…')
    conditions = EncryptedTextField(blank=True, help_text='Asthma, epilepsy, diabetes, sickle cell…')
    emergency_medication = models.CharField(max_length=255, blank=True,
                                            help_text='e.g. Inhaler in school bag; EpiPen at the sick bay')
    diet = models.CharField(max_length=160, blank=True)
    doctor_name = models.CharField(max_length=160, blank=True)
    doctor_phone = models.CharField(max_length=20, blank=True)
    hospital = models.CharField(max_length=200, blank=True)
    consent_first_aid = models.BooleanField(default=True, help_text='Parent agrees to first aid and paracetamol')
    consent_hospital = models.BooleanField(default=True, help_text='Parent agrees to emergency hospital treatment')
    plan = models.ForeignKey(HealthPlan, on_delete=models.SET_NULL, null=True, blank=True, related_name='members')
    member_number = models.CharField(max_length=60, blank=True)
    cover_expires = models.DateField(null=True, blank=True)
    other_insurance = models.CharField(max_length=160, blank=True, help_text="Family's own HMO / policy")
    notes = EncryptedTextField(blank=True, help_text='Nurses only')
    updated_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantManager()

    @property
    def alerts(self):
        out = []
        if self.allergies:
            out.append(f'Allergies: {self.allergies}')
        if self.emergency_medication:
            out.append(f'Medication: {self.emergency_medication}')
        if self.genotype in ('SS', 'SC'):
            out.append(f'Genotype {self.genotype}')
        return out


class Immunisation(TenantModelMixin, models.Model):
    VACCINES = [('BCG', 'BCG'), ('OPV', 'Polio (OPV/IPV)'), ('PENTA', 'Pentavalent (DPT-HepB-Hib)'),
                ('PCV', 'Pneumococcal (PCV)'), ('ROTA', 'Rotavirus'), ('MEASLES', 'Measles / MR'),
                ('YELLOW', 'Yellow fever'), ('MENA', 'Meningitis A'), ('HEPB', 'Hepatitis B'), ('HPV', 'HPV'),
                ('TD', 'Tetanus-diphtheria (Td)'), ('TYPHOID', 'Typhoid'), ('COVID', 'COVID-19'),
                ('OTHER', 'Other')]
    student = models.ForeignKey('users.Student', on_delete=models.CASCADE, related_name='immunisations')
    vaccine = models.CharField(max_length=8, choices=VACCINES)
    other_name = models.CharField(max_length=80, blank=True)
    dose = models.CharField(max_length=30, blank=True, help_text='e.g. Dose 2, booster')
    given_on = models.DateField(null=True, blank=True)
    next_due = models.DateField(null=True, blank=True)
    proof = models.FileField(upload_to=_upload, storage=_private, blank=True)
    verified = models.BooleanField(default=False)
    reminded_at = models.DateTimeField(null=True, blank=True)
    recorded_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        ordering = ['student', '-given_on']

    @property
    def name(self):
        return self.other_name if self.vaccine == 'OTHER' and self.other_name else self.get_vaccine_display()


class MedicationPlan(TenantModelMixin, models.Model):
    student = models.ForeignKey('users.Student', on_delete=models.CASCADE, related_name='medication_plans')
    medicine = models.CharField(max_length=120)
    dose = models.CharField(max_length=60)
    times = models.CharField(max_length=60, help_text='e.g. 12:00, or "when needed"')
    starts = models.DateField()
    ends = models.DateField(null=True, blank=True)
    reason = models.CharField(max_length=160, blank=True)
    parent_consent = models.BooleanField(default=False)
    active = models.BooleanField(default=True)
    added_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, related_name='+')

    objects = TenantManager()

    class Meta:
        ordering = ['student__full_name', 'medicine']


class MedicationDose(TenantModelMixin, models.Model):
    student = models.ForeignKey('users.Student', on_delete=models.CASCADE, related_name='medication_doses')
    plan = models.ForeignKey(MedicationPlan, on_delete=models.SET_NULL, null=True, blank=True, related_name='doses')
    medicine = models.CharField(max_length=120)
    dose = models.CharField(max_length=60)
    given_at = models.DateTimeField()
    reason = models.CharField(max_length=160, blank=True)
    refused = models.BooleanField(default=False)
    note = models.CharField(max_length=255, blank=True)
    given_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, related_name='+')

    objects = TenantManager()

    class Meta:
        ordering = ['-given_at']


class Screening(TenantModelMixin, models.Model):
    student = models.ForeignKey('users.Student', on_delete=models.CASCADE, related_name='screenings')
    date = models.DateField()
    term = models.ForeignKey('academics.Term', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    height_cm = models.DecimalField(max_digits=5, decimal_places=1, null=True, blank=True)
    weight_kg = models.DecimalField(max_digits=5, decimal_places=1, null=True, blank=True)
    vision_left = models.CharField(max_length=10, blank=True, help_text='e.g. 6/6')
    vision_right = models.CharField(max_length=10, blank=True)
    hearing_ok = models.BooleanField(null=True, blank=True)
    dental_ok = models.BooleanField(null=True, blank=True)
    notes = models.CharField(max_length=255, blank=True)
    refer = models.BooleanField(default=False, help_text='Family advised to see a doctor')
    by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, related_name='+')

    objects = TenantManager()

    class Meta:
        ordering = ['-date']

    @property
    def bmi(self):
        if self.height_cm and self.weight_kg and self.height_cm > 0:
            m = float(self.height_cm) / 100
            return round(float(self.weight_kg) / (m * m), 1)
        return None


class Accident(TenantModelMixin, models.Model):
    PLACES = [('PLAYGROUND', 'Playground'), ('SPORTS', 'Sports / PE'), ('CLASS', 'Classroom'), ('LAB', 'Laboratory'),
              ('STAIRS', 'Stairs / corridor'), ('HOSTEL', 'Hostel'), ('TRIP', 'School trip'), ('BUS', 'School bus'),
              ('OTHER', 'Other')]
    SEVERITY = [('MINOR', 'Minor — first aid only'), ('MODERATE', 'Needs a doctor'),
                ('SERIOUS', 'Serious — hospital')]
    student = models.ForeignKey('users.Student', on_delete=models.CASCADE, null=True, blank=True,
                                related_name='accidents')
    other_person = models.CharField(max_length=160, blank=True, help_text='Staff member or visitor hurt')
    happened_at = models.DateTimeField()
    place = models.CharField(max_length=10, choices=PLACES)
    activity = models.CharField(max_length=160, blank=True)
    injury = models.CharField(max_length=255)
    body_part = models.CharField(max_length=80, blank=True)
    first_aid = models.TextField(blank=True)
    severity = models.CharField(max_length=8, choices=SEVERITY, default='MINOR')
    hospital = models.BooleanField(default=False)
    witnesses = models.CharField(max_length=255, blank=True)
    parent_told = models.BooleanField(default=False)
    follow_up = models.TextField(blank=True, help_text='Checks, what changed to stop it happening again')
    closed = models.BooleanField(default=False)
    reported_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        ordering = ['-happened_at']

    @property
    def who(self):
        return self.student.full_name if self.student_id else self.other_person


# ─────────────────────────────────────────────────────────────────────────────
# Discipline
# ─────────────────────────────────────────────────────────────────────────────

class Incident(TenantModelMixin, models.Model):
    # Own reverse name: another app has a model with the same name, and both
    # can't be Tenant.incident_set.
    tenant = models.ForeignKey('tenants.Tenant', on_delete=models.CASCADE, db_index=True,
                               null=True, blank=True, related_name='welfare_incidents')
    CATEGORIES = [('FIGHT', 'Fighting'), ('BULLY', 'Bullying'), ('TRUANCY', 'Truancy / leaving school'),
                  ('CHEAT', 'Exam malpractice / cheating'), ('VANDAL', 'Damage to property'), ('THEFT', 'Theft'),
                  ('DISRESPECT', 'Disrespect to staff'), ('PHONE', 'Phone / device'), ('SUBSTANCE', 'Drugs, alcohol, smoking'),
                  ('DRESS', 'Uniform / appearance'), ('LATE', 'Persistent lateness'), ('CULT', 'Cultism / gang activity'),
                  ('OTHER', 'Other')]
    SEVERITY = [(1, 'Minor'), (2, 'Moderate'), (3, 'Serious'), (4, 'Very serious')]
    ACTIONS = [('WARNING', 'Warning'), ('DETENTION', 'Detention'), ('LINES', 'Extra work / manual labour'),
               ('PARENT', 'Parent meeting'), ('RESTORATIVE', 'Restorative conversation'),
               ('SUSPENSION', 'Suspension'), ('REFER', 'Referred to counsellor'), ('NONE', 'No further action')]
    student = models.ForeignKey('users.Student', on_delete=models.CASCADE, related_name='incidents')
    group = models.CharField(max_length=12, blank=True, help_text='Shared by everyone in the same incident')
    date = models.DateField()
    place = models.CharField(max_length=120, blank=True)
    category = models.CharField(max_length=10, choices=CATEGORIES)
    severity = models.PositiveSmallIntegerField(choices=SEVERITY, default=1)
    description = models.TextField()
    action = models.CharField(max_length=11, choices=ACTIONS, blank=True)
    action_note = models.CharField(max_length=255, blank=True)
    demerits = models.PositiveSmallIntegerField(default=0)
    parent_visible = models.BooleanField(default=True)
    parents_told_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=6, choices=[('OPEN', 'Open'), ('CLOSED', 'Closed')], default='OPEN')
    reported_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        ordering = ['-date', '-id']


class Threshold(TenantModelMixin, models.Model):
    KINDS = [('MERIT', 'Merit points reach'), ('DEMERIT', 'Demerit points reach')]
    name = models.CharField(max_length=80, help_text="e.g. Head's commendation, Report to the vice principal")
    kind = models.CharField(max_length=7, choices=KINDS)
    points = models.PositiveSmallIntegerField()
    action = models.CharField(max_length=255, help_text='What happens next')
    tell_parents = models.BooleanField(default=True)

    objects = TenantManager()

    class Meta:
        ordering = ['kind', 'points']


class ThresholdHit(TenantModelMixin, models.Model):
    threshold = models.ForeignKey(Threshold, on_delete=models.CASCADE, related_name='hits')
    student = models.ForeignKey('users.Student', on_delete=models.CASCADE, related_name='threshold_hits')
    term = models.ForeignKey('academics.Term', on_delete=models.CASCADE, null=True, blank=True, related_name='+')
    total = models.IntegerField()
    reached_at = models.DateTimeField(auto_now_add=True)
    done = models.BooleanField(default=False)

    objects = TenantManager()

    class Meta:
        ordering = ['-reached_at']
        unique_together = ('threshold', 'student', 'term')


class DetentionSession(TenantModelMixin, models.Model):
    date = models.DateField()
    starts = models.TimeField()
    ends = models.TimeField()
    room = models.CharField(max_length=60)
    supervisor = models.ForeignKey('users.Staff', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    capacity = models.PositiveSmallIntegerField(default=30)

    objects = TenantManager()

    class Meta:
        ordering = ['-date', 'starts']

    def __str__(self):
        return f'{self.date:%a %d %b} {self.starts:%H:%M} · {self.room}'


class Detention(TenantModelMixin, models.Model):
    STATUSES = [('SET', 'Set'), ('ATTENDED', 'Attended'), ('MISSED', 'Missed'), ('EXCUSED', 'Excused')]
    session = models.ForeignKey(DetentionSession, on_delete=models.CASCADE, related_name='detentions')
    student = models.ForeignKey('users.Student', on_delete=models.CASCADE, related_name='detentions')
    incident = models.ForeignKey(Incident, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    reason = models.CharField(max_length=255)
    status = models.CharField(max_length=8, choices=STATUSES, default='SET')
    set_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, related_name='+')

    objects = TenantManager()

    class Meta:
        unique_together = ('session', 'student')


class SuspensionCase(TenantModelMixin, models.Model):
    STATUSES = [('PROPOSED', 'Waiting for approval'), ('APPROVED', 'Approved — student away'),
                ('DECLINED', 'Declined'), ('RETURNED', 'Returned after meeting')]
    student = models.ForeignKey('users.Student', on_delete=models.CASCADE, related_name='suspension_cases')
    incident = models.ForeignKey(Incident, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    reason = models.TextField()
    starts = models.DateField()
    ends = models.DateField()
    status = models.CharField(max_length=8, choices=STATUSES, default='PROPOSED')
    proposed_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, related_name='+')
    decided_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    decision_note = models.CharField(max_length=255, blank=True)
    meeting_at = models.DateTimeField(null=True, blank=True, help_text='Return meeting with the family')
    meeting_notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        ordering = ['-created_at']

    @property
    def days(self):
        return (self.ends - self.starts).days + 1


class RestorativeMeeting(TenantModelMixin, models.Model):
    OUTCOMES = [('RESOLVED', 'Resolved'), ('PARTLY', 'Partly resolved'), ('NOT', 'Not resolved')]
    incident = models.ForeignKey(Incident, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    date = models.DateField()
    students = models.ManyToManyField('users.Student', related_name='restorative_meetings')
    others = models.CharField(max_length=255, blank=True, help_text='Staff or parents present')
    what_happened = models.TextField()
    who_was_affected = models.TextField(blank=True)
    agreement = models.TextField(help_text='What everyone agreed to do')
    outcome = models.CharField(max_length=8, choices=OUTCOMES, default='RESOLVED')
    follow_up_on = models.DateField(null=True, blank=True)
    followed_up = models.BooleanField(default=False)
    follow_up_note = models.CharField(max_length=255, blank=True)
    facilitator = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, related_name='+')

    objects = TenantManager()

    class Meta:
        ordering = ['-date', '-id']


# ─────────────────────────────────────────────────────────────────────────────
# Counselling and safeguarding (restricted)
# ─────────────────────────────────────────────────────────────────────────────

class CounsellingCase(TenantModelMixin, models.Model):
    REASONS = [('ACADEMIC', 'Academic worries'), ('FAMILY', 'Family'), ('FRIENDS', 'Friendships / bullying'),
               ('MOOD', 'Mood / anxiety'), ('GRIEF', 'Bereavement'), ('BEHAVIOUR', 'Behaviour'),
               ('CAREER', 'Career guidance'), ('OTHER', 'Other')]
    STATUSES = [('REQUESTED', 'Asked for help'), ('OPEN', 'Open'), ('CLOSED', 'Closed')]
    student = models.ForeignKey('users.Student', on_delete=models.CASCADE, related_name='counselling_cases')
    reason = models.CharField(max_length=9, choices=REASONS, default='OTHER')
    summary = models.CharField(max_length=255, blank=True, help_text='Only counsellors see this')
    status = models.CharField(max_length=9, choices=STATUSES, default='OPEN')
    risk = models.CharField(max_length=6, choices=[('LOW', 'Low'), ('MEDIUM', 'Medium'), ('HIGH', 'High')],
                            default='LOW')
    counsellor = models.ForeignKey('users.Staff', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    self_referred = models.BooleanField(default=False)
    opened_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, related_name='+')
    opened_at = models.DateTimeField(auto_now_add=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    objects = TenantManager()

    class Meta:
        ordering = ['-opened_at']


class CounsellingNote(TenantModelMixin, models.Model):
    case = models.ForeignKey(CounsellingCase, on_delete=models.CASCADE, related_name='notes')
    date = models.DateField()
    text = EncryptedTextField()
    next_session = models.DateField(null=True, blank=True)
    author = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        ordering = ['-date', '-id']


class Concern(TenantModelMixin, models.Model):
    CATEGORIES = [('PHYSICAL', 'Physical harm'), ('EMOTIONAL', 'Emotional harm'), ('SEXUAL', 'Sexual harm'),
                  ('NEGLECT', 'Neglect'), ('SELFHARM', 'Self-harm or suicide risk'), ('ONLINE', 'Online safety'),
                  ('EXPLOIT', 'Exploitation / child labour'), ('BULLYING', 'Serious bullying'),
                  ('DISCLOSURE', 'Something the child told me'), ('OTHER', 'Other')]
    STATUSES = [('NEW', 'New'), ('REVIEW', 'Being looked at'), ('REFERRED', 'Referred outside school'),
                ('MONITOR', 'Monitoring'), ('CLOSED', 'Closed')]
    student = models.ForeignKey('users.Student', on_delete=models.CASCADE, related_name='concerns')
    category = models.CharField(max_length=10, choices=CATEGORIES)
    description = EncryptedTextField(help_text="What you saw or heard — the child's own words where you can")
    immediate_danger = models.BooleanField(default=False)
    status = models.CharField(max_length=8, choices=STATUSES, default='NEW')
    lead = models.ForeignKey('users.Staff', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    referred_to = models.CharField(max_length=160, blank=True,
                                   help_text='e.g. Ministry of Women Affairs, police, hospital')
    raised_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, related_name='+')
    raised_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        ordering = ['-raised_at']


class ConcernAction(TenantModelMixin, models.Model):
    concern = models.ForeignKey(Concern, on_delete=models.CASCADE, related_name='actions')
    text = EncryptedTextField()
    status = models.CharField(max_length=8, blank=True)
    by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, related_name='+')
    at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        ordering = ['at']


# ─────────────────────────────────────────────────────────────────────────────
# Support plans, sports, resources
# ─────────────────────────────────────────────────────────────────────────────

class SupportPlan(TenantModelMixin, models.Model):
    student = models.ForeignKey('users.Student', on_delete=models.CASCADE, related_name='support_plans')
    title = models.CharField(max_length=160, default='Individual support plan')
    needs = models.TextField(help_text='What the student finds hard')
    strengths = models.TextField(blank=True)
    goals = models.TextField(help_text='One per line — what we want to see by the review')
    strategies = models.TextField(help_text='What teachers and parents will do')
    owner = models.ForeignKey('users.Staff', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    starts = models.DateField()
    review_on = models.DateField()
    parent_agreed = models.BooleanField(default=False)
    status = models.CharField(max_length=6, choices=[('ACTIVE', 'Active'), ('CLOSED', 'Closed')], default='ACTIVE')
    reminded_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        ordering = ['review_on']

    @property
    def goal_list(self):
        return [g.strip() for g in self.goals.splitlines() if g.strip()]


class PlanReview(TenantModelMixin, models.Model):
    plan = models.ForeignKey(SupportPlan, on_delete=models.CASCADE, related_name='reviews')
    date = models.DateField()
    progress = models.TextField()
    rating = models.CharField(max_length=8, choices=[('BEHIND', 'Not yet'), ('SOME', 'Some progress'),
                                                     ('MET', 'Goals met')], default='SOME')
    next_steps = models.TextField(blank=True)
    by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, related_name='+')

    objects = TenantManager()

    class Meta:
        ordering = ['-date']


class Team(TenantModelMixin, models.Model):
    name = models.CharField(max_length=80, help_text='e.g. U-15 Boys Football')
    sport = models.CharField(max_length=60)
    coach = models.ForeignKey('users.Staff', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    season = models.CharField(max_length=40, blank=True)
    active = models.BooleanField(default=True)

    objects = TenantManager()

    class Meta:
        ordering = ['sport', 'name']

    def __str__(self):
        return self.name


class TeamMember(TenantModelMixin, models.Model):
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name='members')
    student = models.ForeignKey('users.Student', on_delete=models.CASCADE, related_name='teams')
    position = models.CharField(max_length=40, blank=True)
    captain = models.BooleanField(default=False)
    since = models.DateField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        unique_together = ('team', 'student')
        ordering = ['-captain', 'student__full_name']


class FitnessTest(TenantModelMixin, models.Model):
    TESTS = [('SPRINT', '100 m sprint (seconds)', 's', True), ('BEEP', 'Bleep test (level)', 'level', False),
             ('RUN', '1.5 km run (minutes)', 'min', True), ('PUSHUP', 'Push-ups in 1 min', 'reps', False),
             ('SITUP', 'Sit-ups in 1 min', 'reps', False), ('JUMP', 'Standing long jump (cm)', 'cm', False),
             ('REACH', 'Sit and reach (cm)', 'cm', False)]
    student = models.ForeignKey('users.Student', on_delete=models.CASCADE, related_name='fitness_tests')
    test = models.CharField(max_length=6, choices=[(t[0], t[1]) for t in TESTS])
    value = models.DecimalField(max_digits=7, decimal_places=2)
    date = models.DateField()
    by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, related_name='+')

    objects = TenantManager()

    class Meta:
        ordering = ['-date']

    @classmethod
    def lower_is_better(cls, test):
        return next((t[3] for t in cls.TESTS if t[0] == test), False)


class Resource(TenantModelMixin, models.Model):
    # Own reverse name: another app has a model with the same name, and both
    # can't be Tenant.resource_set.
    tenant = models.ForeignKey('tenants.Tenant', on_delete=models.CASCADE, db_index=True,
                               null=True, blank=True, related_name='welfare_resources')
    AUDIENCES = [('BOTH', 'Students and parents'), ('STUDENTS', 'Students'), ('PARENTS', 'Parents')]
    TOPICS = [('STRESS', 'Exam and school stress'), ('MOOD', 'Feeling low or anxious'), ('FRIENDS', 'Friends and bullying'),
              ('ONLINE', 'Staying safe online'), ('SLEEP', 'Sleep, food and exercise'), ('GRIEF', 'Loss and grief'),
              ('PARENTING', 'For parents'), ('HELP', 'Getting help')]
    title = models.CharField(max_length=160)
    topic = models.CharField(max_length=9, choices=TOPICS)
    audience = models.CharField(max_length=8, choices=AUDIENCES, default='BOTH')
    body = models.TextField()
    link = models.URLField(blank=True)
    contact = models.CharField(max_length=160, blank=True, help_text='Phone line or person to speak to')
    published = models.BooleanField(default=True)
    order = models.PositiveSmallIntegerField(default=0)

    objects = TenantManager()

    class Meta:
        ordering = ['topic', 'order', 'title']
