"""
labs/models.py
──────────────
Labs and practical rooms. Labs are expensive to equip and run, and practicals
count in WAEC and NECO, so this keeps:

  • each lab — who runs it, its subjects, safety information and inspections;
  • bookings — made through the timetable's room bookings, so a lab can't be
    double-booked or booked over a timetabled lesson;
  • equipment — quantities, working/broken, value and service dates;
  • chemicals — the school's chemicals register (facilities) for this lab,
    drawn down when a practical uses them;
  • breakages — what broke, who broke it, and any charge to the family;
  • safety — inspection checklists and incidents (injuries, spills, fires…);
  • practicals — experiments, the sessions classes do them in, attendance and
    marks, which can be sent to the result sheet as a CA score.
"""
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone

from tenants.managers import TenantManager, TenantModelMixin

USER = settings.AUTH_USER_MODEL
ZERO = Decimal('0.00')


class Lab(TenantModelMixin, models.Model):
    KINDS = [('CHEMISTRY', 'Chemistry lab'), ('PHYSICS', 'Physics lab'), ('BIOLOGY', 'Biology lab'),
             ('SCIENCE', 'Integrated science lab'), ('AGRIC', 'Agricultural science lab'),
             ('FOOD', 'Food & nutrition room'), ('TECH', 'Technical / workshop'), ('ICT', 'Computer lab'),
             ('OTHER', 'Other practical room')]
    ICONS = {'CHEMISTRY': 'droplet-half', 'PHYSICS': 'lightning-charge-fill', 'BIOLOGY': 'flower1',
             'SCIENCE': 'radioactive', 'AGRIC': 'tree-fill', 'FOOD': 'egg-fried', 'TECH': 'tools', 'ICT': 'pc-display',
             'OTHER': 'eyedropper'}
    name = models.CharField(max_length=80)
    kind = models.CharField(max_length=10, choices=KINDS, default='CHEMISTRY')
    building = models.CharField(max_length=80, blank=True)
    capacity = models.PositiveSmallIntegerField(default=30, help_text='Students at once')
    workstations = models.PositiveSmallIntegerField(default=0, help_text='Benches / work stations')
    subjects = models.ManyToManyField('users.Subject', blank=True, related_name='+')
    in_charge = models.ForeignKey('users.Staff', on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
                                  help_text='Head of the lab (e.g. the HOD)')
    technicians = models.ManyToManyField('users.Staff', blank=True, related_name='+',
                                         help_text='Lab technicians / attendants')
    # Linked rooms in the timetable (for bookings) and in facilities (for chemicals and maintenance)
    room = models.OneToOneField('timetable.TeachingRoom', on_delete=models.SET_NULL, null=True, blank=True,
                                related_name='lab')
    space = models.OneToOneField('facilities.Space', on_delete=models.SET_NULL, null=True, blank=True, related_name='lab')
    safety_rules = models.TextField(blank=True, help_text='Shown on the lab page and on every practical sheet')
    first_aid_kit = models.CharField(max_length=160, blank=True, help_text='Where it is kept')
    fire_extinguisher = models.CharField(max_length=160, blank=True, help_text='Where, and the type (CO₂, powder…)')
    emergency_shutoff = models.CharField(max_length=160, blank=True, help_text='Gas / power / water shut-off points')
    eyewash = models.CharField(max_length=160, blank=True)
    inspect_every_days = models.PositiveSmallIntegerField(default=30, help_text='How often a safety inspection is due')
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    objects = TenantManager()

    class Meta:
        ordering = ['name']
        unique_together = ('tenant', 'name')

    def __str__(self):
        return self.name

    @property
    def icon(self):
        return self.ICONS.get(self.kind, 'eyedropper')


class LabBooking(TenantModelMixin, models.Model):
    """A lab booked for a class: the timetable booking, plus what it is for and what the technician should set out."""
    lab = models.ForeignKey(Lab, on_delete=models.CASCADE, related_name='bookings')
    booking = models.OneToOneField('timetable.RoomBooking', on_delete=models.CASCADE, related_name='lab_booking')
    school_class = models.ForeignKey('users.Class', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    subject = models.ForeignKey('users.Subject', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    practical = models.ForeignKey('Practical', on_delete=models.SET_NULL, null=True, blank=True, related_name='bookings')
    students = models.PositiveSmallIntegerField(null=True, blank=True)
    prep_notes = models.TextField(blank=True, help_text='For the technician: apparatus to set out, solutions to make up')
    prepared = models.BooleanField(default=False)
    prepared_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    objects = TenantManager()

    class Meta:
        ordering = ['booking__date', 'booking__start_time']


class Equipment(TenantModelMixin, models.Model):
    CATEGORIES = [('GLASSWARE', 'Glassware'), ('APPARATUS', 'Apparatus'), ('INSTRUMENT', 'Instrument / meter'),
                  ('OPTICAL', 'Microscope / optical'), ('ELECTRICAL', 'Electrical'), ('MODEL', 'Model / specimen'),
                  ('SAFETY', 'Safety equipment'), ('TOOL', 'Tool'), ('ICT', 'Computer / ICT'), ('FURNITURE', 'Furniture'),
                  ('OTHER', 'Other')]
    lab = models.ForeignKey(Lab, on_delete=models.CASCADE, related_name='equipment')
    name = models.CharField(max_length=120)
    category = models.CharField(max_length=10, choices=CATEGORIES, default='APPARATUS')
    quantity = models.PositiveIntegerField(default=1, help_text='Working items')
    broken = models.PositiveIntegerField(default=0, help_text='Broken / awaiting repair')
    min_quantity = models.PositiveIntegerField(default=0, help_text='Warn when working items fall below this')
    unit_cost = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO,
                                    help_text='Replacement cost of one — used for breakage charges')
    location = models.CharField(max_length=120, blank=True, help_text='e.g. Cupboard B, shelf 2')
    serial = models.CharField(max_length=80, blank=True, help_text='Serial or asset tag (for single items)')
    service_every_days = models.PositiveSmallIntegerField(default=0, help_text='Calibration/servicing interval (0 = none)')
    last_serviced = models.DateField(null=True, blank=True)
    notes = models.CharField(max_length=255, blank=True)
    active = models.BooleanField(default=True)
    objects = TenantManager()

    class Meta:
        ordering = ['category', 'name']

    def __str__(self):
        return self.name

    @property
    def value(self):
        return (self.unit_cost or ZERO) * self.quantity

    @property
    def low(self):
        return bool(self.min_quantity) and self.quantity < self.min_quantity

    @property
    def service_due(self):
        if not self.service_every_days:
            return None
        if not self.last_serviced:
            return timezone.localdate()
        return self.last_serviced + timedelta(days=self.service_every_days)


class EquipmentMove(TenantModelMixin, models.Model):
    KINDS = [('ADD', 'Added / bought'), ('BREAK', 'Broken'), ('REPAIR', 'Repaired'), ('WRITEOFF', 'Written off'),
             ('SERVICE', 'Serviced / calibrated'), ('COUNT', 'Stock count correction')]
    equipment = models.ForeignKey(Equipment, on_delete=models.CASCADE, related_name='moves')
    kind = models.CharField(max_length=8, choices=KINDS)
    quantity = models.IntegerField(default=0)
    note = models.CharField(max_length=200, blank=True)
    by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, related_name='+')
    at = models.DateTimeField(default=timezone.now)
    objects = TenantManager()

    class Meta:
        ordering = ['-at', '-id']


class Breakage(TenantModelMixin, models.Model):
    CAUSES = [('ACCIDENT', 'Accident'), ('CARELESS', 'Carelessness'), ('MISUSE', 'Misuse / horseplay'),
              ('WEAR', 'Wear and tear'), ('UNKNOWN', 'Not known')]
    STATUSES = [('NONE', 'No charge'), ('UNPAID', 'Charged — unpaid'), ('PAID', 'Paid'), ('WAIVED', 'Waived')]
    lab = models.ForeignKey(Lab, on_delete=models.CASCADE, related_name='breakages')
    equipment = models.ForeignKey(Equipment, on_delete=models.SET_NULL, null=True, blank=True, related_name='breakages')
    item = models.CharField(max_length=120, help_text='What broke (kept even if the equipment record goes)')
    quantity = models.PositiveIntegerField(default=1)
    student = models.ForeignKey('users.Student', on_delete=models.SET_NULL, null=True, blank=True,
                                related_name='lab_breakages')
    session = models.ForeignKey('PracticalSession', on_delete=models.SET_NULL, null=True, blank=True,
                                related_name='breakages')
    cause = models.CharField(max_length=8, choices=CAUSES, default='ACCIDENT')
    description = models.CharField(max_length=300, blank=True)
    date = models.DateField(default=timezone.localdate)
    cost = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO)
    charge = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO, help_text='Amount charged to the family')
    status = models.CharField(max_length=6, choices=STATUSES, default='NONE')
    transaction = models.ForeignKey('finance.Transaction', on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name='+')
    reported_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, related_name='+')
    settled_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    objects = TenantManager()

    class Meta:
        ordering = ['-date', '-id']


class SafetyCheck(TenantModelMixin, models.Model):
    """A safety inspection: each checklist item passed or failed, with notes."""
    lab = models.ForeignKey(Lab, on_delete=models.CASCADE, related_name='checks')
    date = models.DateField(default=timezone.localdate)
    items = models.JSONField(default=list, help_text='[{"item": "...", "ok": true, "note": ""}]')
    passed = models.BooleanField(default=True)
    notes = models.TextField(blank=True)
    by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, related_name='+')
    objects = TenantManager()

    class Meta:
        ordering = ['-date', '-id']

    @property
    def failed_items(self):
        return [i for i in self.items if not i.get('ok')]


class SafetyIncident(TenantModelMixin, models.Model):
    KINDS = [('INJURY', 'Injury (cut, burn…)'), ('CHEMICAL', 'Chemical contact / splash'), ('SPILL', 'Spill'),
             ('FIRE', 'Fire'), ('GAS', 'Gas leak'), ('ELECTRICAL', 'Electric shock / fault'), ('FAINT', 'Fainting / illness'),
             ('NEARMISS', 'Near miss'), ('OTHER', 'Other')]
    SEVERITIES = [('MINOR', 'Minor — first aid only'), ('MODERATE', 'Moderate — sick bay / parent called'),
                  ('SERIOUS', 'Serious — hospital or emergency services')]
    lab = models.ForeignKey(Lab, on_delete=models.CASCADE, related_name='incidents')
    happened_at = models.DateTimeField(default=timezone.now)
    kind = models.CharField(max_length=10, choices=KINDS)
    severity = models.CharField(max_length=8, choices=SEVERITIES, default='MINOR')
    students = models.ManyToManyField('users.Student', blank=True, related_name='lab_incidents')
    session = models.ForeignKey('PracticalSession', on_delete=models.SET_NULL, null=True, blank=True,
                                related_name='incidents')
    description = models.TextField()
    first_aid = models.TextField(blank=True, help_text='First aid given, and by whom')
    action = models.TextField(blank=True, help_text='What has been done so it does not happen again')
    families_told = models.BooleanField(default=False)
    closed = models.BooleanField(default=False)
    reported_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    objects = TenantManager()

    class Meta:
        ordering = ['closed', '-happened_at']


class Practical(TenantModelMixin, models.Model):
    """An experiment or practical exercise, with its apparatus, safety notes and marking scheme."""
    KINDS = [('CLASS', 'Class practical'), ('ASSESSED', 'Assessed practical (CA)'),
             ('MOCK', 'Mock — WAEC/NECO practical'), ('ALT', 'Alternative to practical')]
    title = models.CharField(max_length=160)
    subject = models.ForeignKey('users.Subject', on_delete=models.CASCADE, related_name='+')
    lab = models.ForeignKey(Lab, on_delete=models.SET_NULL, null=True, blank=True, related_name='practicals')
    level = models.CharField(max_length=40, blank=True, help_text='e.g. SS 2')
    kind = models.CharField(max_length=8, choices=KINDS, default='CLASS')
    aim = models.TextField(blank=True)
    apparatus = models.TextField(blank=True, help_text='One item per line')
    procedure = models.TextField(blank=True)
    hazards = models.TextField(blank=True, help_text='Hazards and precautions — read out before starting')
    rubric = models.JSONField(default=list, blank=True,
                              help_text='[{"criterion": "Observations", "max": 5}, …]; blank = one overall mark')
    max_score = models.PositiveSmallIntegerField(default=20)
    duration_minutes = models.PositiveSmallIntegerField(default=80)
    created_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    active = models.BooleanField(default=True)
    objects = TenantManager()

    class Meta:
        ordering = ['subject__name', 'level', 'title']

    def __str__(self):
        return self.title

    @property
    def apparatus_list(self):
        return [line.strip(' -•\t') for line in (self.apparatus or '').splitlines() if line.strip(' -•\t')]


class PracticalChemical(TenantModelMixin, models.Model):
    """A chemical a practical uses, per group of students."""
    practical = models.ForeignKey(Practical, on_delete=models.CASCADE, related_name='chemicals')
    chemical = models.ForeignKey('facilities.Chemical', on_delete=models.CASCADE, related_name='+')
    per_group = models.DecimalField(max_digits=10, decimal_places=2)
    objects = TenantManager()

    class Meta:
        unique_together = ('practical', 'chemical')


class PracticalSession(TenantModelMixin, models.Model):
    """A class doing a practical on a day: the register, the marks and what was used."""
    PLANNED, DONE, CANCELLED = 'PLANNED', 'DONE', 'CANCELLED'
    practical = models.ForeignKey(Practical, on_delete=models.CASCADE, related_name='sessions')
    school_class = models.ForeignKey('users.Class', on_delete=models.CASCADE, related_name='+')
    lab = models.ForeignKey(Lab, on_delete=models.SET_NULL, null=True, blank=True, related_name='sessions')
    booking = models.OneToOneField(LabBooking, on_delete=models.SET_NULL, null=True, blank=True, related_name='session')
    date = models.DateField(default=timezone.localdate)
    teacher = models.ForeignKey('users.Staff', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    session = models.ForeignKey('academics.AcademicSession', on_delete=models.SET_NULL, null=True, blank=True,
                                related_name='+')
    term = models.ForeignKey('academics.Term', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    status = models.CharField(max_length=9, default=PLANNED,
                              choices=[(PLANNED, 'Planned'), (DONE, 'Done'), (CANCELLED, 'Cancelled')])
    groups = models.PositiveSmallIntegerField(default=1, help_text='Working groups (for chemical quantities)')
    safety_briefing = models.BooleanField(default=False, help_text='Hazards and precautions read out before starting')
    chemicals_used = models.BooleanField(default=False, help_text='Chemicals have been deducted from stock')
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    objects = TenantManager()

    class Meta:
        ordering = ['-date', '-id']


class PracticalRecord(TenantModelMixin, models.Model):
    PRESENT, LATE, ABSENT, EXCUSED = 'P', 'L', 'A', 'E'
    STATUSES = [(PRESENT, 'Present'), (LATE, 'Late'), (ABSENT, 'Absent'), (EXCUSED, 'Excused')]
    session = models.ForeignKey(PracticalSession, on_delete=models.CASCADE, related_name='records')
    student = models.ForeignKey('users.Student', on_delete=models.CASCADE, related_name='practical_records')
    attendance = models.CharField(max_length=1, choices=STATUSES, default=PRESENT)
    scores = models.JSONField(default=list, blank=True, help_text='Marks per rubric criterion')
    score = models.FloatField(null=True, blank=True)
    bench = models.CharField(max_length=10, blank=True)
    remark = models.CharField(max_length=160, blank=True)
    objects = TenantManager()

    class Meta:
        unique_together = ('session', 'student')

    @property
    def attended(self):
        return self.attendance in (self.PRESENT, self.LATE)
