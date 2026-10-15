"""
facilities/models.py
────────────────────
Procurement and facilities, built beside the existing inventory app (whose
Asset is an item type with a stock ledger):

  • buying — requisitions, vendors and contracts, purchase orders, goods
    received notes;
  • assets — individually tagged items (with QR codes, depreciation, ICT
    specs and a history), disposals and stock counts;
  • facilities — spaces, maintenance requests, preventive maintenance,
    generators and diesel, utility bills, cleaning, security patrols;
  • stores — laboratory chemicals and kitchen stock;
  • hall hire for outside groups, with invoices.
"""
import uuid
from decimal import Decimal

from django.conf import settings
from django.db import models

from tenants.managers import TenantManager, TenantModelMixin

USER = settings.AUTH_USER_MODEL
ZERO = Decimal('0')


def _private():
    from users.models import PrivateStorage
    return PrivateStorage()


def _upload(instance, filename):
    import os
    ext = os.path.splitext(filename)[1].lower()[:8]
    return f"facilities/{instance.tenant_id}/{instance.__class__.__name__.lower()}/{uuid.uuid4().hex}{ext}"


def _money(**kw):
    return models.DecimalField(max_digits=14, decimal_places=2, default=ZERO, **kw)


SPACE_KINDS = [('CLASS', 'Classroom'), ('LAB', 'Laboratory'), ('ICT', 'Computer room'), ('HALL', 'Hall'),
               ('OFFICE', 'Office'), ('STAFF', 'Staff room'), ('LIBRARY', 'Library'), ('SPORTS', 'Sports field / court'),
               ('HOSTEL', 'Hostel'), ('KITCHEN', 'Kitchen'), ('STORE', 'Store'), ('TOILET', 'Toilets'),
               ('OUTDOOR', 'Outdoor area'), ('OTHER', 'Other')]
JOB_CATEGORIES = [('ELECTRICAL', 'Electrical'), ('PLUMBING', 'Plumbing'), ('CARPENTRY', 'Furniture / carpentry'),
                  ('AC', 'Air conditioning / fans'), ('ICT', 'Computers / network'), ('BUILDING', 'Building / roof'),
                  ('GENERATOR', 'Generator / power'), ('CLEANING', 'Cleaning'), ('SECURITY', 'Security'),
                  ('OTHER', 'Other')]
PRIORITIES = [('LOW', 'Low'), ('NORMAL', 'Normal'), ('HIGH', 'High'), ('URGENT', 'Urgent — safety risk')]
HAZARDS = [('FLAMMABLE', 'Flammable'), ('CORROSIVE', 'Corrosive'), ('TOXIC', 'Toxic'), ('OXIDISER', 'Oxidiser'),
           ('IRRITANT', 'Irritant / harmful'), ('ENVIRONMENT', 'Hazardous to the environment'),
           ('EXPLOSIVE', 'Explosive'), ('GAS', 'Gas under pressure'), ('HEALTH', 'Serious health hazard')]
UTILITIES = [('ELECTRICITY', 'Electricity (DisCo / prepaid)'), ('WATER', 'Water'), ('INTERNET', 'Internet'),
             ('WASTE', 'Waste collection'), ('GAS', 'Cooking gas'), ('PHONE', 'Phone lines'), ('OTHER', 'Other')]
MEALS = [('BREAKFAST', 'Breakfast'), ('LUNCH', 'Lunch'), ('SUPPER', 'Supper'), ('SNACK', 'Snack'), ('OTHER', 'Other')]


class FacilitySettings(TenantModelMixin, models.Model):
    store_keepers = models.ManyToManyField('users.Staff', blank=True, related_name='+',
                                           help_text='Approve small requisitions, receive goods, get stock alerts')
    maintenance_team = models.ManyToManyField('users.Staff', blank=True, related_name='+')
    alert_phones = models.CharField(max_length=255, blank=True,
                                    help_text='Phone numbers for low-stock alerts (WhatsApp first, then SMS), '
                                              'comma separated')
    approval_limit = _money(help_text='Requisitions up to this total can be approved by a store keeper; '
                                      'above it needs an administrator')
    po_prefix = models.CharField(max_length=10, default='PO')
    invoice_prefix = models.CharField(max_length=10, default='HIRE')
    bank_details = models.TextField(blank=True, help_text='Printed on hall-hire invoices')

    objects = TenantManager()


# ─────────────────────────────────────────────────────────────────────────────
# Spaces (F416)
# ─────────────────────────────────────────────────────────────────────────────

class Space(TenantModelMixin, models.Model):
    name = models.CharField(max_length=80)
    building = models.CharField(max_length=80, blank=True)
    kind = models.CharField(max_length=8, choices=SPACE_KINDS, default='CLASS')
    capacity = models.PositiveSmallIntegerField(null=True, blank=True)
    home_class = models.ForeignKey('users.Class', on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='+', help_text='The class based here')
    teaching_room = models.OneToOneField('timetable.TeachingRoom', on_delete=models.SET_NULL, null=True,
                                         blank=True, related_name='space')
    features = models.CharField(max_length=255, blank=True, help_text='Projector, AC, whiteboard…')
    hireable = models.BooleanField(default=False, help_text='Can be hired by outside groups')
    hire_rate_hour = _money()
    hire_rate_day = _money()
    active = models.BooleanField(default=True)

    objects = TenantManager()

    class Meta:
        ordering = ['building', 'name']

    def __str__(self):
        return f'{self.name}' + (f' ({self.building})' if self.building else '')


# ─────────────────────────────────────────────────────────────────────────────
# Buying (F401–F403, F414)
# ─────────────────────────────────────────────────────────────────────────────

class Vendor(TenantModelMixin, models.Model):
    # Own reverse name: another app has a model with the same name, and both
    # can't be Tenant.vendor_set.
    tenant = models.ForeignKey('tenants.Tenant', on_delete=models.CASCADE, db_index=True,
                               null=True, blank=True, related_name='facility_vendors')
    name = models.CharField(max_length=160)
    contact_person = models.CharField(max_length=120, blank=True)
    phone = models.CharField(max_length=20, blank=True)
    email = models.EmailField(blank=True)
    address = models.CharField(max_length=255, blank=True)
    supplies = models.CharField(max_length=160, blank=True, help_text='e.g. Stationery, diesel, lab chemicals')
    tax_id = models.CharField(max_length=40, blank=True, help_text='TIN')
    bank_name = models.CharField(max_length=80, blank=True)
    account_name = models.CharField(max_length=120, blank=True)
    account_number = models.CharField(max_length=20, blank=True)
    active = models.BooleanField(default=True)
    notes = models.TextField(blank=True)

    objects = TenantManager()

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class VendorContract(TenantModelMixin, models.Model):
    vendor = models.ForeignKey(Vendor, on_delete=models.CASCADE, related_name='contracts')
    title = models.CharField(max_length=160)
    starts = models.DateField()
    ends = models.DateField(null=True, blank=True)
    value = _money()
    file = models.FileField(upload_to=_upload, storage=_private, blank=True)
    reminded_at = models.DateTimeField(null=True, blank=True)

    objects = TenantManager()

    class Meta:
        ordering = ['ends']


class VendorRating(TenantModelMixin, models.Model):
    vendor = models.ForeignKey(Vendor, on_delete=models.CASCADE, related_name='ratings')
    order = models.ForeignKey('PurchaseOrder', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    quality = models.PositiveSmallIntegerField()
    delivery = models.PositiveSmallIntegerField()
    price = models.PositiveSmallIntegerField()
    comment = models.CharField(max_length=255, blank=True)
    by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    @property
    def score(self):
        return round((self.quality + self.delivery + self.price) / 3, 1)


class Requisition(TenantModelMixin, models.Model):
    STATUSES = [('SUBMITTED', 'Waiting for approval'), ('APPROVED', 'Approved'), ('DECLINED', 'Declined'),
                ('ORDERED', 'Ordered'), ('DONE', 'Received')]
    title = models.CharField(max_length=160)
    department = models.CharField(max_length=80, blank=True)
    reason = models.TextField()
    needed_by = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=9, choices=STATUSES, default='SUBMITTED')
    requested_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, related_name='+')
    decided_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        ordering = ['-created_at']

    @property
    def total(self):
        return sum((line.amount for line in self.lines.all()), ZERO)


class RequisitionLine(TenantModelMixin, models.Model):
    requisition = models.ForeignKey(Requisition, on_delete=models.CASCADE, related_name='lines')
    description = models.CharField(max_length=200)
    item = models.ForeignKey('inventory.Asset', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    quantity = models.DecimalField(max_digits=10, decimal_places=2)
    unit_cost = _money(help_text='Estimate')

    objects = TenantManager()

    @property
    def amount(self):
        return (self.quantity or ZERO) * (self.unit_cost or ZERO)


class PurchaseOrder(TenantModelMixin, models.Model):
    STATUSES = [('DRAFT', 'Draft'), ('SENT', 'Sent to vendor'), ('PART', 'Partly received'), ('RECEIVED', 'Received'),
                ('CANCELLED', 'Cancelled')]
    number = models.CharField(max_length=20)
    vendor = models.ForeignKey(Vendor, on_delete=models.PROTECT, related_name='orders')
    requisition = models.ForeignKey(Requisition, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name='orders')
    order_date = models.DateField()
    expected_by = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=9, choices=STATUSES, default='DRAFT')
    deliver_to = models.CharField(max_length=160, blank=True)
    notes = models.TextField(blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        ordering = ['-order_date', '-id']
        unique_together = ('tenant', 'number')

    def __str__(self):
        return self.number

    @property
    def total(self):
        return sum((line.amount for line in self.lines.all()), ZERO)


class POLine(TenantModelMixin, models.Model):
    order = models.ForeignKey(PurchaseOrder, on_delete=models.CASCADE, related_name='lines')
    description = models.CharField(max_length=200)
    item = models.ForeignKey('inventory.Asset', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    quantity = models.DecimalField(max_digits=10, decimal_places=2)
    unit_price = _money()
    received = models.DecimalField(max_digits=10, decimal_places=2, default=ZERO)

    objects = TenantManager()

    @property
    def amount(self):
        return self.quantity * self.unit_price

    @property
    def outstanding(self):
        return max(self.quantity - self.received, ZERO)


class GoodsReceived(TenantModelMixin, models.Model):
    order = models.ForeignKey(PurchaseOrder, on_delete=models.CASCADE, related_name='receipts')
    number = models.CharField(max_length=24)
    delivery_note = models.CharField(max_length=60, blank=True, help_text="Supplier's delivery note / waybill")
    received_at = models.DateTimeField(auto_now_add=True)
    received_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, related_name='+')
    note = models.CharField(max_length=255, blank=True)

    objects = TenantManager()

    class Meta:
        ordering = ['-received_at']


class GRNLine(TenantModelMixin, models.Model):
    receipt = models.ForeignKey(GoodsReceived, on_delete=models.CASCADE, related_name='lines')
    line = models.ForeignKey(POLine, on_delete=models.CASCADE, related_name='receipts')
    quantity = models.DecimalField(max_digits=10, decimal_places=2)
    rejected = models.DecimalField(max_digits=10, decimal_places=2, default=ZERO)
    note = models.CharField(max_length=160, blank=True)

    objects = TenantManager()


# ─────────────────────────────────────────────────────────────────────────────
# Tagged assets (F404, F405, F412, F415)
# ─────────────────────────────────────────────────────────────────────────────

class AssetTag(TenantModelMixin, models.Model):
    METHODS = [('STRAIGHT', 'Straight line'), ('REDUCING', 'Reducing balance')]
    STATUSES = [('USE', 'In use'), ('STORE', 'In store'), ('REPAIR', 'Under repair'), ('DISPOSED', 'Disposed')]
    CONDITIONS = [('NEW', 'New'), ('GOOD', 'Good'), ('FAIR', 'Fair'), ('POOR', 'Poor'), ('BROKEN', 'Broken')]
    code = models.CharField(max_length=30, help_text='Printed on the tag, e.g. TAG-00012')
    uid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    name = models.CharField(max_length=160)
    item = models.ForeignKey('inventory.Asset', on_delete=models.SET_NULL, null=True, blank=True, related_name='tags')
    category = models.CharField(max_length=60, blank=True, help_text='Furniture, ICT, vehicle, lab equipment…')
    serial_number = models.CharField(max_length=80, blank=True)
    space = models.ForeignKey(Space, on_delete=models.SET_NULL, null=True, blank=True, related_name='assets')
    custodian = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    status = models.CharField(max_length=8, choices=STATUSES, default='USE')
    condition = models.CharField(max_length=6, choices=CONDITIONS, default='GOOD')
    vendor = models.ForeignKey(Vendor, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    purchase_date = models.DateField(null=True, blank=True)
    cost = _money()
    salvage_value = _money()
    life_years = models.PositiveSmallIntegerField(default=5)
    method = models.CharField(max_length=8, choices=METHODS, default='STRAIGHT')
    reducing_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('25'),
                                        help_text='% a year, for reducing balance')
    warranty_until = models.DateField(null=True, blank=True)
    is_ict = models.BooleanField(default=False)
    cpu = models.CharField(max_length=80, blank=True)
    ram = models.CharField(max_length=30, blank=True)
    storage = models.CharField(max_length=30, blank=True)
    os = models.CharField(max_length=60, blank=True)
    mac_address = models.CharField(max_length=17, blank=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        ordering = ['code']
        unique_together = ('tenant', 'code')

    def __str__(self):
        return f'{self.code} · {self.name}'


class AssetEvent(TenantModelMixin, models.Model):
    KINDS = [('ADDED', 'Added'), ('MOVED', 'Moved'), ('CUSTODY', 'New custodian'), ('REPAIR', 'Repair'),
             ('CONDITION', 'Condition'), ('CHECKED', 'Seen at stock count'), ('NOTE', 'Note'), ('DISPOSED', 'Disposed')]
    tag = models.ForeignKey(AssetTag, on_delete=models.CASCADE, related_name='events')
    kind = models.CharField(max_length=9, choices=KINDS)
    text = models.CharField(max_length=255)
    cost = _money()
    by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, related_name='+')
    at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        ordering = ['-at', '-id']


class Disposal(TenantModelMixin, models.Model):
    METHODS = [('SOLD', 'Sold'), ('SCRAP', 'Scrapped'), ('DONATED', 'Donated'), ('STOLEN', 'Stolen'),
               ('LOST', 'Lost'), ('WRITEOFF', 'Written off (beyond repair)')]
    STATUSES = [('PENDING', 'Waiting for approval'), ('APPROVED', 'Approved'), ('DECLINED', 'Declined')]
    tag = models.ForeignKey(AssetTag, on_delete=models.CASCADE, related_name='disposals')
    method = models.CharField(max_length=8, choices=METHODS)
    reason = models.CharField(max_length=255)
    proceeds = _money()
    book_value = _money()
    status = models.CharField(max_length=8, choices=STATUSES, default='PENDING')
    requested_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, related_name='+')
    decided_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    decided_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        ordering = ['-created_at']

    @property
    def gain(self):
        return self.proceeds - self.book_value


# ─────────────────────────────────────────────────────────────────────────────
# Stock count (F413) and alerts (F410)
# ─────────────────────────────────────────────────────────────────────────────

class StockCount(TenantModelMixin, models.Model):
    title = models.CharField(max_length=120)
    started_at = models.DateTimeField(auto_now_add=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    adjusted = models.BooleanField(default=False)
    started_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, related_name='+')

    objects = TenantManager()

    class Meta:
        ordering = ['-started_at']


class StockCountLine(TenantModelMixin, models.Model):
    count = models.ForeignKey(StockCount, on_delete=models.CASCADE, related_name='lines')
    item = models.ForeignKey('inventory.Asset', on_delete=models.CASCADE, related_name='+')
    expected = models.IntegerField()
    counted = models.IntegerField(null=True, blank=True)
    note = models.CharField(max_length=160, blank=True)

    objects = TenantManager()

    class Meta:
        unique_together = ('count', 'item')

    @property
    def variance(self):
        return None if self.counted is None else self.counted - self.expected


class StockAlert(TenantModelMixin, models.Model):
    item = models.ForeignKey('inventory.Asset', on_delete=models.CASCADE, related_name='+', null=True, blank=True)
    chemical = models.ForeignKey('Chemical', on_delete=models.CASCADE, related_name='+', null=True, blank=True)
    kitchen_item = models.ForeignKey('KitchenItem', on_delete=models.CASCADE, related_name='+', null=True, blank=True)
    level = models.DecimalField(max_digits=12, decimal_places=2)
    sent_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        ordering = ['-sent_at']


# ─────────────────────────────────────────────────────────────────────────────
# Maintenance (F406, F407), generators (F408), utilities (F409)
# ─────────────────────────────────────────────────────────────────────────────

class MaintenanceRequest(TenantModelMixin, models.Model):
    STATUSES = [('OPEN', 'Reported'), ('ASSIGNED', 'Assigned'), ('PROGRESS', 'Being fixed'), ('DONE', 'Fixed'),
                ('CLOSED', 'Closed')]
    title = models.CharField(max_length=160)
    description = models.TextField(blank=True)
    category = models.CharField(max_length=10, choices=JOB_CATEGORIES, default='OTHER')
    priority = models.CharField(max_length=6, choices=PRIORITIES, default='NORMAL')
    space = models.ForeignKey(Space, on_delete=models.SET_NULL, null=True, blank=True, related_name='jobs')
    where = models.CharField(max_length=120, blank=True)
    tag = models.ForeignKey(AssetTag, on_delete=models.SET_NULL, null=True, blank=True, related_name='jobs')
    photo = models.FileField(upload_to=_upload, storage=_private, blank=True)
    status = models.CharField(max_length=8, choices=STATUSES, default='OPEN')
    assigned_to = models.ForeignKey('users.Staff', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    contractor = models.CharField(max_length=120, blank=True)
    cost = _money()
    resolution = models.TextField(blank=True)
    reported_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    done_at = models.DateTimeField(null=True, blank=True)
    from_task = models.ForeignKey('PreventiveTask', on_delete=models.SET_NULL, null=True, blank=True,
                                  related_name='jobs')

    objects = TenantManager()

    class Meta:
        ordering = ['-created_at']


class PreventiveTask(TenantModelMixin, models.Model):
    title = models.CharField(max_length=160, help_text='e.g. Service the 60 kVA generator')
    category = models.CharField(max_length=10, choices=JOB_CATEGORIES, default='OTHER')
    space = models.ForeignKey(Space, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    tag = models.ForeignKey(AssetTag, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    every_days = models.PositiveSmallIntegerField(help_text='Repeat every … days')
    next_due = models.DateField()
    last_done = models.DateField(null=True, blank=True)
    checklist = models.TextField(blank=True, help_text='One step per line')
    assigned_to = models.ForeignKey('users.Staff', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    active = models.BooleanField(default=True)
    reminded_for = models.DateField(null=True, blank=True)

    objects = TenantManager()

    class Meta:
        ordering = ['next_due']


class Generator(TenantModelMixin, models.Model):
    name = models.CharField(max_length=80)
    kva = models.PositiveSmallIntegerField(null=True, blank=True)
    tag = models.ForeignKey(AssetTag, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    service_every_hours = models.PositiveSmallIntegerField(default=250)
    last_service_hours = models.DecimalField(max_digits=9, decimal_places=1, default=ZERO)
    active = models.BooleanField(default=True)

    objects = TenantManager()

    def __str__(self):
        return self.name + (f' ({self.kva} kVA)' if self.kva else '')


class GeneratorLog(TenantModelMixin, models.Model):
    generator = models.ForeignKey(Generator, on_delete=models.CASCADE, related_name='logs')
    date = models.DateField()
    hour_meter = models.DecimalField(max_digits=9, decimal_places=1, null=True, blank=True,
                                     help_text='Hour-meter reading at the end of the day')
    run_hours = models.DecimalField(max_digits=5, decimal_places=1, default=ZERO)
    litres_added = models.DecimalField(max_digits=8, decimal_places=1, default=ZERO)
    price_per_litre = _money()
    note = models.CharField(max_length=160, blank=True)
    by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, related_name='+')

    objects = TenantManager()

    class Meta:
        ordering = ['-date', '-id']

    @property
    def cost(self):
        return self.litres_added * self.price_per_litre


class UtilityBill(TenantModelMixin, models.Model):
    kind = models.CharField(max_length=11, choices=UTILITIES)
    provider = models.CharField(max_length=120)
    account_number = models.CharField(max_length=60, blank=True, help_text='Meter / account number')
    period_start = models.DateField()
    period_end = models.DateField()
    units = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, help_text='kWh, m³, GB…')
    amount = _money()
    due_date = models.DateField(null=True, blank=True)
    paid_on = models.DateField(null=True, blank=True)
    reference = models.CharField(max_length=80, blank=True)
    file = models.FileField(upload_to=_upload, storage=_private, blank=True)
    reminded_at = models.DateTimeField(null=True, blank=True)

    objects = TenantManager()

    class Meta:
        ordering = ['-period_end', 'kind']


# ─────────────────────────────────────────────────────────────────────────────
# Cleaning (F417) and security patrols (F418)
# ─────────────────────────────────────────────────────────────────────────────

class CleaningTask(TenantModelMixin, models.Model):
    FREQ = [('DAILY', 'Every school day'), ('WEEKLY', 'Weekly'), ('MONTHLY', 'Monthly')]
    space = models.ForeignKey(Space, on_delete=models.CASCADE, null=True, blank=True, related_name='cleaning')
    area = models.CharField(max_length=120, blank=True, help_text='If not a listed space, e.g. Main gate')
    frequency = models.CharField(max_length=7, choices=FREQ, default='DAILY')
    checklist = models.TextField(help_text='One job per line, e.g. Sweep floor / Empty bins / Wipe desks')
    cleaner = models.CharField(max_length=120, blank=True)
    active = models.BooleanField(default=True)

    objects = TenantManager()

    @property
    def items(self):
        return [x.strip() for x in self.checklist.splitlines() if x.strip()]

    @property
    def label(self):
        return str(self.space) if self.space_id else self.area


class CleaningCheck(TenantModelMixin, models.Model):
    task = models.ForeignKey(CleaningTask, on_delete=models.CASCADE, related_name='checks')
    date = models.DateField()
    done = models.JSONField(default=list)
    complete = models.BooleanField(default=False)
    note = models.CharField(max_length=255, blank=True)
    by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, related_name='+')
    at = models.DateTimeField(auto_now=True)

    objects = TenantManager()

    class Meta:
        unique_together = ('task', 'date')
        ordering = ['-date']


class PatrolPoint(TenantModelMixin, models.Model):
    name = models.CharField(max_length=80, help_text='e.g. Back fence, Hostel block B')
    uid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    every_hours = models.PositiveSmallIntegerField(default=2, help_text='Check at least every … hours at night')
    active = models.BooleanField(default=True)

    objects = TenantManager()

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class PatrolLog(TenantModelMixin, models.Model):
    point = models.ForeignKey(PatrolPoint, on_delete=models.CASCADE, related_name='logs')
    at = models.DateTimeField(auto_now_add=True)
    guard = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, related_name='+')
    guard_name = models.CharField(max_length=80, blank=True)
    all_clear = models.BooleanField(default=True)
    note = models.CharField(max_length=255, blank=True)

    objects = TenantManager()

    class Meta:
        ordering = ['-at']


# ─────────────────────────────────────────────────────────────────────────────
# Laboratory chemicals (F411) and kitchen stock (F419)
# ─────────────────────────────────────────────────────────────────────────────

class Chemical(TenantModelMixin, models.Model):
    name = models.CharField(max_length=120)
    formula = models.CharField(max_length=60, blank=True)
    cas_number = models.CharField(max_length=20, blank=True)
    hazards = models.JSONField(default=list, blank=True)
    quantity = models.DecimalField(max_digits=10, decimal_places=2, default=ZERO)
    unit = models.CharField(max_length=10, default='g')
    reorder_level = models.DecimalField(max_digits=10, decimal_places=2, default=ZERO)
    space = models.ForeignKey(Space, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    storage = models.CharField(max_length=160, blank=True, help_text='e.g. Acid cabinet, shelf 2')
    expires_on = models.DateField(null=True, blank=True)
    supplier = models.ForeignKey(Vendor, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    sds = models.FileField(upload_to=_upload, storage=_private, blank=True, help_text='Safety data sheet')
    first_aid = models.CharField(max_length=255, blank=True)
    active = models.BooleanField(default=True)

    objects = TenantManager()

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class ChemicalUse(TenantModelMixin, models.Model):
    chemical = models.ForeignKey(Chemical, on_delete=models.CASCADE, related_name='uses')
    quantity = models.DecimalField(max_digits=10, decimal_places=2)
    kind = models.CharField(max_length=4, choices=[('USE', 'Used'), ('IN', 'Received'), ('DISP', 'Disposed')],
                            default='USE')
    purpose = models.CharField(max_length=160, blank=True, help_text='Practical / class')
    date = models.DateField()
    by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, related_name='+')

    objects = TenantManager()

    class Meta:
        ordering = ['-date', '-id']


class KitchenItem(TenantModelMixin, models.Model):
    name = models.CharField(max_length=80)
    unit = models.CharField(max_length=12, default='kg')
    quantity = models.DecimalField(max_digits=10, decimal_places=2, default=ZERO)
    reorder_level = models.DecimalField(max_digits=10, decimal_places=2, default=ZERO)
    unit_cost = _money()
    active = models.BooleanField(default=True)

    objects = TenantManager()

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class KitchenMovement(TenantModelMixin, models.Model):
    KINDS = [('IN', 'Received'), ('USE', 'Used for a meal'), ('WASTE', 'Spoilt / wasted')]
    item = models.ForeignKey(KitchenItem, on_delete=models.CASCADE, related_name='movements')
    kind = models.CharField(max_length=5, choices=KINDS)
    quantity = models.DecimalField(max_digits=10, decimal_places=2)
    unit_cost = _money()
    meal = models.CharField(max_length=9, choices=MEALS, blank=True)
    headcount = models.PositiveIntegerField(null=True, blank=True)
    date = models.DateField()
    note = models.CharField(max_length=160, blank=True)
    by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, related_name='+')

    objects = TenantManager()

    class Meta:
        ordering = ['-date', '-id']


# ─────────────────────────────────────────────────────────────────────────────
# Hall hire (F420)
# ─────────────────────────────────────────────────────────────────────────────

class HireBooking(TenantModelMixin, models.Model):
    STATUSES = [('ENQUIRY', 'Enquiry'), ('CONFIRMED', 'Confirmed'), ('DONE', 'Held'), ('CANCELLED', 'Cancelled')]
    space = models.ForeignKey(Space, on_delete=models.PROTECT, related_name='hires')
    client = models.CharField(max_length=160)
    phone = models.CharField(max_length=20, blank=True)
    email = models.EmailField(blank=True)
    event = models.CharField(max_length=160)
    starts = models.DateTimeField()
    ends = models.DateTimeField()
    guests = models.PositiveIntegerField(null=True, blank=True)
    amount = _money()
    extras = _money(help_text='Chairs, generator, cleaning…')
    extras_note = models.CharField(max_length=160, blank=True)
    caution_fee = _money(help_text='Refundable deposit')
    paid = _money()
    status = models.CharField(max_length=9, choices=STATUSES, default='ENQUIRY')
    invoice_number = models.CharField(max_length=24, blank=True)
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        ordering = ['-starts']

    @property
    def total(self):
        return self.amount + self.extras + self.caution_fee

    @property
    def balance(self):
        return self.total - self.paid


class HirePayment(TenantModelMixin, models.Model):
    booking = models.ForeignKey(HireBooking, on_delete=models.CASCADE, related_name='payments')
    amount = _money()
    method = models.CharField(max_length=8, choices=[('TRANSFER', 'Bank transfer'), ('CASH', 'Cash'), ('POS', 'POS'),
                                                     ('REFUND', 'Caution refund')], default='TRANSFER')
    reference = models.CharField(max_length=80, blank=True)
    date = models.DateField()
    by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, related_name='+')

    objects = TenantManager()

    class Meta:
        ordering = ['-date', '-id']
