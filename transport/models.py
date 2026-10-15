"""
transport/models.py
───────────────────
School buses: vehicles and their documents, drivers and attendants, routes
and stops, riders, trips with boarding scans and live position, fees,
maintenance and fuel, field trips, pickup changes and incidents.
"""
from decimal import Decimal

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
    return f"transport/{instance.tenant_id}/{instance.__class__.__name__.lower()}/{uuid.uuid4().hex}{ext}"


class TransportSettings(TenantModelMixin, models.Model):
    notify_scans = models.BooleanField(default=True, help_text='Tell parents when their child gets on or off the bus')
    scan_channel = models.CharField(max_length=5, choices=[('SMS', 'SMS'), ('EMAIL', 'Email'), ('BOTH', 'Both')],
                                    default='SMS')
    reminder_days = models.PositiveSmallIntegerField(default=30, help_text='Warn this many days before documents expire')
    school_lat = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    school_lng = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    managers = models.ManyToManyField('users.Staff', blank=True, related_name='+')

    objects = TenantManager()


class Vehicle(TenantModelMixin, models.Model):
    name = models.CharField(max_length=60, help_text='e.g. Bus 1 (blue)')
    plate = models.CharField(max_length=20)
    make_model = models.CharField(max_length=80, blank=True)
    capacity = models.PositiveSmallIntegerField(default=30, help_text='Seats for students')
    active = models.BooleanField(default=True)
    notes = models.CharField(max_length=255, blank=True)

    objects = TenantManager()

    class Meta:
        ordering = ['name']

    def __str__(self):
        return f'{self.name} ({self.plate})'


class VehicleDocument(TenantModelMixin, models.Model):
    KINDS = [('INSURANCE', 'Insurance'), ('ROADWORTHY', 'Roadworthiness'), ('LICENCE', 'Vehicle licence'),
             ('HACKNEY', 'Hackney permit'), ('PROOF', 'Proof of ownership'), ('OTHER', 'Other')]
    vehicle = models.ForeignKey(Vehicle, on_delete=models.CASCADE, related_name='documents')
    kind = models.CharField(max_length=10, choices=KINDS)
    number = models.CharField(max_length=60, blank=True)
    expires_on = models.DateField(null=True, blank=True)
    file = models.FileField(upload_to=_upload, storage=_private, blank=True, null=True)
    reminded_at = models.DateTimeField(null=True, blank=True)

    objects = TenantManager()

    class Meta:
        ordering = ['expires_on']


class Driver(TenantModelMixin, models.Model):
    ROLES = [('DRIVER', 'Driver'), ('ATTENDANT', 'Bus attendant')]
    name = models.CharField(max_length=120)
    role = models.CharField(max_length=9, choices=ROLES, default='DRIVER')
    phone = models.CharField(max_length=20)
    staff = models.ForeignKey('users.Staff', on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
                              help_text='Their portal account, if they have one (needed to scan students)')
    licence_number = models.CharField(max_length=40, blank=True)
    licence_expires = models.DateField(null=True, blank=True)
    licence_reminded_at = models.DateTimeField(null=True, blank=True)
    address = models.CharField(max_length=255, blank=True)
    next_of_kin = models.CharField(max_length=160, blank=True)
    active = models.BooleanField(default=True)

    objects = TenantManager()

    class Meta:
        ordering = ['role', 'name']

    def __str__(self):
        return self.name


class Route(TenantModelMixin, models.Model):
    name = models.CharField(max_length=80)
    vehicle = models.ForeignKey(Vehicle, on_delete=models.SET_NULL, null=True, blank=True, related_name='routes')
    driver = models.ForeignKey(Driver, on_delete=models.SET_NULL, null=True, blank=True, related_name='driving')
    attendant = models.ForeignKey(Driver, on_delete=models.SET_NULL, null=True, blank=True, related_name='attending')
    morning_departs = models.TimeField(null=True, blank=True)
    afternoon_departs = models.TimeField(null=True, blank=True)
    fee_term = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    fee_month = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    active = models.BooleanField(default=True)

    objects = TenantManager()

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class Stop(TenantModelMixin, models.Model):
    route = models.ForeignKey(Route, on_delete=models.CASCADE, related_name='stops')
    name = models.CharField(max_length=120)
    landmark = models.CharField(max_length=160, blank=True)
    order = models.PositiveSmallIntegerField(default=0)
    morning_time = models.TimeField(null=True, blank=True)
    afternoon_time = models.TimeField(null=True, blank=True)
    lat = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    lng = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)

    objects = TenantManager()

    class Meta:
        ordering = ['route', 'order', 'id']

    def __str__(self):
        return self.name


class Rider(TenantModelMixin, models.Model):
    DIRECTIONS = [('BOTH', 'Morning and afternoon'), ('AM', 'Morning only'), ('PM', 'Afternoon only')]
    PLANS = [('TERM', 'Per term'), ('MONTH', 'Per month')]
    student = models.OneToOneField('users.Student', on_delete=models.CASCADE, related_name='bus')
    route = models.ForeignKey(Route, on_delete=models.CASCADE, related_name='riders')
    stop = models.ForeignKey(Stop, on_delete=models.SET_NULL, null=True, blank=True, related_name='riders')
    direction = models.CharField(max_length=4, choices=DIRECTIONS, default='BOTH')
    plan = models.CharField(max_length=5, choices=PLANS, default='TERM')
    notes = models.CharField(max_length=255, blank=True, help_text='e.g. collected by grandmother at the stop')
    active = models.BooleanField(default=True)
    since = models.DateField(auto_now_add=True)

    objects = TenantManager()


class Trip(TenantModelMixin, models.Model):
    LEGS = [('AM', 'Morning'), ('PM', 'Afternoon'), ('FIELD', 'Field trip')]
    route = models.ForeignKey(Route, on_delete=models.CASCADE, related_name='trips', null=True, blank=True)
    field_trip = models.ForeignKey('FieldTrip', on_delete=models.CASCADE, related_name='trips', null=True, blank=True)
    date = models.DateField()
    leg = models.CharField(max_length=5, choices=LEGS)
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    delay_minutes = models.PositiveSmallIntegerField(default=0)
    delay_note = models.CharField(max_length=160, blank=True)
    last_lat = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    last_lng = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    last_seen = models.DateTimeField(null=True, blank=True)
    run_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, related_name='+')

    objects = TenantManager()

    class Meta:
        ordering = ['-date', 'leg']


class TripScan(TenantModelMixin, models.Model):
    trip = models.ForeignKey(Trip, on_delete=models.CASCADE, related_name='scans')
    student = models.ForeignKey('users.Student', on_delete=models.CASCADE, related_name='bus_scans')
    kind = models.CharField(max_length=3, choices=[('ON', 'Got on'), ('OFF', 'Got off')])
    at = models.DateTimeField(auto_now_add=True)
    by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, related_name='+')

    objects = TenantManager()

    class Meta:
        ordering = ['at']
        unique_together = ('trip', 'student', 'kind')


class TransportBill(TenantModelMixin, models.Model):
    student = models.ForeignKey('users.Student', on_delete=models.CASCADE, related_name='transport_bills')
    route = models.ForeignKey(Route, on_delete=models.SET_NULL, null=True, related_name='+')
    period = models.CharField(max_length=40, help_text='e.g. 1st Term 2026/2027 or October 2026')
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    paid = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    status = models.CharField(max_length=7, choices=[('UNPAID', 'Unpaid'), ('PAID', 'Paid'), ('WAIVED', 'Waived')],
                              default='UNPAID')
    transaction = models.ForeignKey('finance.Transaction', on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name='+')
    note = models.CharField(max_length=160, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        unique_together = ('student', 'period')
        ordering = ['-created_at']


class MaintenanceLog(TenantModelMixin, models.Model):
    # Own reverse name: another app has a model with the same name, and both
    # can't be Tenant.maintenancelog_set.
    tenant = models.ForeignKey('tenants.Tenant', on_delete=models.CASCADE, db_index=True,
                               null=True, blank=True, related_name='vehicle_maintenance_logs')
    KINDS = [('SERVICE', 'Service'), ('REPAIR', 'Repair'), ('TYRES', 'Tyres'), ('INSPECTION', 'Inspection'),
             ('OTHER', 'Other')]
    vehicle = models.ForeignKey(Vehicle, on_delete=models.CASCADE, related_name='maintenance')
    date = models.DateField()
    kind = models.CharField(max_length=10, choices=KINDS, default='SERVICE')
    description = models.CharField(max_length=255)
    cost = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    odometer = models.PositiveIntegerField(null=True, blank=True)
    garage = models.CharField(max_length=120, blank=True)
    next_due_date = models.DateField(null=True, blank=True)
    next_due_km = models.PositiveIntegerField(null=True, blank=True)
    recorded_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, related_name='+')

    objects = TenantManager()

    class Meta:
        ordering = ['-date', '-id']


class FuelLog(TenantModelMixin, models.Model):
    vehicle = models.ForeignKey(Vehicle, on_delete=models.CASCADE, related_name='fuel')
    date = models.DateField()
    litres = models.DecimalField(max_digits=7, decimal_places=2)
    cost = models.DecimalField(max_digits=12, decimal_places=2)
    odometer = models.PositiveIntegerField(null=True, blank=True)
    station = models.CharField(max_length=120, blank=True)
    recorded_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, related_name='+')

    objects = TenantManager()

    class Meta:
        ordering = ['-date', '-id']


class FieldTrip(TenantModelMixin, models.Model):
    title = models.CharField(max_length=160)
    destination = models.CharField(max_length=200)
    date = models.DateField()
    depart = models.TimeField()
    back = models.TimeField()
    vehicle = models.ForeignKey(Vehicle, on_delete=models.SET_NULL, null=True, related_name='field_trips')
    driver = models.ForeignKey(Driver, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    seats_needed = models.PositiveSmallIntegerField(default=0)
    consent_form = models.ForeignKey('communications.ConsentForm', on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name='+')
    notes = models.TextField(blank=True)
    booked_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        ordering = ['date', 'depart']


class PickupChange(TenantModelMixin, models.Model):
    student = models.ForeignKey('users.Student', on_delete=models.CASCADE, related_name='pickup_changes')
    requested_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, related_name='+')
    new_stop = models.ForeignKey(Stop, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    new_address = models.CharField(max_length=255, blank=True, help_text='If no existing stop suits')
    from_date = models.DateField()
    reason = models.CharField(max_length=255)
    status = models.CharField(max_length=8, choices=[('PENDING', 'Waiting'), ('APPROVED', 'Approved'),
                                                     ('DECLINED', 'Declined')], default='PENDING')
    decided_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    note = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        ordering = ['-created_at']


class Incident(TenantModelMixin, models.Model):
    # Own reverse name: another app has a model with the same name, and both
    # can't be Tenant.incident_set.
    tenant = models.ForeignKey('tenants.Tenant', on_delete=models.CASCADE, db_index=True,
                               null=True, blank=True, related_name='transport_incidents')
    KINDS = [('ACCIDENT', 'Accident'), ('BREAKDOWN', 'Breakdown'), ('BEHAVIOUR', 'Behaviour on the bus'),
             ('INJURY', 'Injury'), ('LATE', 'Serious delay'), ('OTHER', 'Other')]
    route = models.ForeignKey(Route, on_delete=models.SET_NULL, null=True, blank=True, related_name='incidents')
    vehicle = models.ForeignKey(Vehicle, on_delete=models.SET_NULL, null=True, blank=True, related_name='incidents')
    date = models.DateField()
    kind = models.CharField(max_length=9, choices=KINDS)
    description = models.TextField()
    students = models.ManyToManyField('users.Student', blank=True, related_name='+')
    action = models.TextField(blank=True)
    parents_told = models.BooleanField(default=False)
    reported_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, related_name='+')
    closed = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        ordering = ['-date', '-id']
