from decimal import Decimal

import django.db.models.deletion
import transport.models
from django.conf import settings
from django.db import migrations, models

CASCADE, SET_NULL = django.db.models.deletion.CASCADE, django.db.models.deletion.SET_NULL
ZERO = Decimal("0")


def _id():
    return ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID"))


def _tenant():
    return ("tenant", models.ForeignKey(blank=True, null=True, on_delete=CASCADE, to="tenants.tenant"))


def _user(name, blank=False):
    return (name, models.ForeignKey(blank=blank, null=True, on_delete=SET_NULL, related_name="+",
                                    to=settings.AUTH_USER_MODEL))


def _coord(name):
    return (name, models.DecimalField(blank=True, decimal_places=6, max_digits=9, null=True))


def _money(name, default=True):
    if default:
        return (name, models.DecimalField(decimal_places=2, default=ZERO, max_digits=12))
    return (name, models.DecimalField(decimal_places=2, max_digits=12))


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("communications", "0006_survey_students"),
        ("finance", "0011_alter_feestructure_category_and_more"),
        ("tenants", "0006_alter_tenant_owner"),
        ("users", "0011_alter_attendancesettings_school_starts_at"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="TransportSettings",
            fields=[
                _id(),
                ("notify_scans", models.BooleanField(default=True,
                                                     help_text="Tell parents when their child gets on or off the bus")),
                ("scan_channel", models.CharField(choices=[("SMS", "SMS"), ("EMAIL", "Email"), ("BOTH", "Both")],
                                                  default="SMS", max_length=5)),
                ("reminder_days", models.PositiveSmallIntegerField(
                    default=30, help_text="Warn this many days before documents expire")),
                _coord("school_lat"), _coord("school_lng"),
                ("managers", models.ManyToManyField(blank=True, related_name="+", to="users.staff")),
                _tenant(),
            ],
        ),
        migrations.CreateModel(
            name="Vehicle",
            fields=[
                _id(),
                ("name", models.CharField(help_text="e.g. Bus 1 (blue)", max_length=60)),
                ("plate", models.CharField(max_length=20)),
                ("make_model", models.CharField(blank=True, max_length=80)),
                ("capacity", models.PositiveSmallIntegerField(default=30, help_text="Seats for students")),
                ("active", models.BooleanField(default=True)),
                ("notes", models.CharField(blank=True, max_length=255)),
                _tenant(),
            ],
            options={"ordering": ["name"]},
        ),
        migrations.CreateModel(
            name="VehicleDocument",
            fields=[
                _id(),
                ("kind", models.CharField(choices=[("INSURANCE", "Insurance"), ("ROADWORTHY", "Roadworthiness"),
                                                   ("LICENCE", "Vehicle licence"), ("HACKNEY", "Hackney permit"),
                                                   ("PROOF", "Proof of ownership"), ("OTHER", "Other")],
                                          max_length=10)),
                ("number", models.CharField(blank=True, max_length=60)),
                ("expires_on", models.DateField(blank=True, null=True)),
                ("file", models.FileField(blank=True, null=True, storage=transport.models._private,
                                          upload_to=transport.models._upload)),
                ("reminded_at", models.DateTimeField(blank=True, null=True)),
                ("vehicle", models.ForeignKey(on_delete=CASCADE, related_name="documents", to="transport.vehicle")),
                _tenant(),
            ],
            options={"ordering": ["expires_on"]},
        ),
        migrations.CreateModel(
            name="Driver",
            fields=[
                _id(),
                ("name", models.CharField(max_length=120)),
                ("role", models.CharField(choices=[("DRIVER", "Driver"), ("ATTENDANT", "Bus attendant")],
                                          default="DRIVER", max_length=9)),
                ("phone", models.CharField(max_length=20)),
                ("licence_number", models.CharField(blank=True, max_length=40)),
                ("licence_expires", models.DateField(blank=True, null=True)),
                ("licence_reminded_at", models.DateTimeField(blank=True, null=True)),
                ("address", models.CharField(blank=True, max_length=255)),
                ("next_of_kin", models.CharField(blank=True, max_length=160)),
                ("active", models.BooleanField(default=True)),
                ("staff", models.ForeignKey(blank=True, help_text="Their portal account, if they have one (needed to "
                                                                  "scan students)", null=True, on_delete=SET_NULL,
                                            related_name="+", to="users.staff")),
                _tenant(),
            ],
            options={"ordering": ["role", "name"]},
        ),
        migrations.CreateModel(
            name="Route",
            fields=[
                _id(),
                ("name", models.CharField(max_length=80)),
                ("morning_departs", models.TimeField(blank=True, null=True)),
                ("afternoon_departs", models.TimeField(blank=True, null=True)),
                _money("fee_term"), _money("fee_month"),
                ("active", models.BooleanField(default=True)),
                ("attendant", models.ForeignKey(blank=True, null=True, on_delete=SET_NULL, related_name="attending",
                                                to="transport.driver")),
                ("driver", models.ForeignKey(blank=True, null=True, on_delete=SET_NULL, related_name="driving",
                                             to="transport.driver")),
                ("vehicle", models.ForeignKey(blank=True, null=True, on_delete=SET_NULL, related_name="routes",
                                              to="transport.vehicle")),
                _tenant(),
            ],
            options={"ordering": ["name"]},
        ),
        migrations.CreateModel(
            name="Stop",
            fields=[
                _id(),
                ("name", models.CharField(max_length=120)),
                ("landmark", models.CharField(blank=True, max_length=160)),
                ("order", models.PositiveSmallIntegerField(default=0)),
                ("morning_time", models.TimeField(blank=True, null=True)),
                ("afternoon_time", models.TimeField(blank=True, null=True)),
                _coord("lat"), _coord("lng"),
                ("route", models.ForeignKey(on_delete=CASCADE, related_name="stops", to="transport.route")),
                _tenant(),
            ],
            options={"ordering": ["route", "order", "id"]},
        ),
        migrations.CreateModel(
            name="Rider",
            fields=[
                _id(),
                ("direction", models.CharField(choices=[("BOTH", "Morning and afternoon"), ("AM", "Morning only"),
                                                        ("PM", "Afternoon only")], default="BOTH", max_length=4)),
                ("plan", models.CharField(choices=[("TERM", "Per term"), ("MONTH", "Per month")], default="TERM",
                                          max_length=5)),
                ("notes", models.CharField(blank=True, help_text="e.g. collected by grandmother at the stop",
                                           max_length=255)),
                ("active", models.BooleanField(default=True)),
                ("since", models.DateField(auto_now_add=True)),
                ("route", models.ForeignKey(on_delete=CASCADE, related_name="riders", to="transport.route")),
                ("stop", models.ForeignKey(blank=True, null=True, on_delete=SET_NULL, related_name="riders",
                                           to="transport.stop")),
                ("student", models.OneToOneField(on_delete=CASCADE, related_name="bus", to="users.student")),
                _tenant(),
            ],
        ),
        migrations.CreateModel(
            name="FieldTrip",
            fields=[
                _id(),
                ("title", models.CharField(max_length=160)),
                ("destination", models.CharField(max_length=200)),
                ("date", models.DateField()),
                ("depart", models.TimeField()),
                ("back", models.TimeField()),
                ("seats_needed", models.PositiveSmallIntegerField(default=0)),
                ("notes", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                _user("booked_by"),
                ("consent_form", models.ForeignKey(blank=True, null=True, on_delete=SET_NULL, related_name="+",
                                                   to="communications.consentform")),
                ("driver", models.ForeignKey(blank=True, null=True, on_delete=SET_NULL, related_name="+",
                                             to="transport.driver")),
                ("vehicle", models.ForeignKey(null=True, on_delete=SET_NULL, related_name="field_trips",
                                              to="transport.vehicle")),
                _tenant(),
            ],
            options={"ordering": ["date", "depart"]},
        ),
        migrations.CreateModel(
            name="Trip",
            fields=[
                _id(),
                ("date", models.DateField()),
                ("leg", models.CharField(choices=[("AM", "Morning"), ("PM", "Afternoon"), ("FIELD", "Field trip")],
                                         max_length=5)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("ended_at", models.DateTimeField(blank=True, null=True)),
                ("delay_minutes", models.PositiveSmallIntegerField(default=0)),
                ("delay_note", models.CharField(blank=True, max_length=160)),
                _coord("last_lat"), _coord("last_lng"),
                ("last_seen", models.DateTimeField(blank=True, null=True)),
                ("field_trip", models.ForeignKey(blank=True, null=True, on_delete=CASCADE, related_name="trips",
                                                 to="transport.fieldtrip")),
                ("route", models.ForeignKey(blank=True, null=True, on_delete=CASCADE, related_name="trips",
                                            to="transport.route")),
                _user("run_by"),
                _tenant(),
            ],
            options={"ordering": ["-date", "leg"]},
        ),
        migrations.CreateModel(
            name="TripScan",
            fields=[
                _id(),
                ("kind", models.CharField(choices=[("ON", "Got on"), ("OFF", "Got off")], max_length=3)),
                ("at", models.DateTimeField(auto_now_add=True)),
                _user("by"),
                ("student", models.ForeignKey(on_delete=CASCADE, related_name="bus_scans", to="users.student")),
                ("trip", models.ForeignKey(on_delete=CASCADE, related_name="scans", to="transport.trip")),
                _tenant(),
            ],
            options={"ordering": ["at"], "unique_together": {("trip", "student", "kind")}},
        ),
        migrations.CreateModel(
            name="TransportBill",
            fields=[
                _id(),
                ("period", models.CharField(help_text="e.g. 1st Term 2026/2027 or October 2026", max_length=40)),
                _money("amount", default=False), _money("paid"),
                ("status", models.CharField(choices=[("UNPAID", "Unpaid"), ("PAID", "Paid"), ("WAIVED", "Waived")],
                                            default="UNPAID", max_length=7)),
                ("note", models.CharField(blank=True, max_length=160)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("route", models.ForeignKey(null=True, on_delete=SET_NULL, related_name="+", to="transport.route")),
                ("student", models.ForeignKey(on_delete=CASCADE, related_name="transport_bills", to="users.student")),
                ("transaction", models.ForeignKey(blank=True, null=True, on_delete=SET_NULL, related_name="+",
                                                  to="finance.transaction")),
                _tenant(),
            ],
            options={"ordering": ["-created_at"], "unique_together": {("student", "period")}},
        ),
        migrations.CreateModel(
            name="MaintenanceLog",
            fields=[
                _id(),
                ("date", models.DateField()),
                ("kind", models.CharField(choices=[("SERVICE", "Service"), ("REPAIR", "Repair"), ("TYRES", "Tyres"),
                                                   ("INSPECTION", "Inspection"), ("OTHER", "Other")],
                                          default="SERVICE", max_length=10)),
                ("description", models.CharField(max_length=255)),
                _money("cost"),
                ("odometer", models.PositiveIntegerField(blank=True, null=True)),
                ("garage", models.CharField(blank=True, max_length=120)),
                ("next_due_date", models.DateField(blank=True, null=True)),
                ("next_due_km", models.PositiveIntegerField(blank=True, null=True)),
                _user("recorded_by"),
                ("vehicle", models.ForeignKey(on_delete=CASCADE, related_name="maintenance", to="transport.vehicle")),
                _tenant(),
            ],
            options={"ordering": ["-date", "-id"]},
        ),
        migrations.CreateModel(
            name="FuelLog",
            fields=[
                _id(),
                ("date", models.DateField()),
                ("litres", models.DecimalField(decimal_places=2, max_digits=7)),
                _money("cost", default=False),
                ("odometer", models.PositiveIntegerField(blank=True, null=True)),
                ("station", models.CharField(blank=True, max_length=120)),
                _user("recorded_by"),
                ("vehicle", models.ForeignKey(on_delete=CASCADE, related_name="fuel", to="transport.vehicle")),
                _tenant(),
            ],
            options={"ordering": ["-date", "-id"]},
        ),
        migrations.CreateModel(
            name="PickupChange",
            fields=[
                _id(),
                ("new_address", models.CharField(blank=True, help_text="If no existing stop suits", max_length=255)),
                ("from_date", models.DateField()),
                ("reason", models.CharField(max_length=255)),
                ("status", models.CharField(choices=[("PENDING", "Waiting"), ("APPROVED", "Approved"),
                                                     ("DECLINED", "Declined")], default="PENDING", max_length=8)),
                ("note", models.CharField(blank=True, max_length=255)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                _user("decided_by", blank=True),
                ("new_stop", models.ForeignKey(blank=True, null=True, on_delete=SET_NULL, related_name="+",
                                               to="transport.stop")),
                _user("requested_by"),
                ("student", models.ForeignKey(on_delete=CASCADE, related_name="pickup_changes", to="users.student")),
                _tenant(),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="Incident",
            fields=[
                _id(),
                ("date", models.DateField()),
                ("kind", models.CharField(choices=[("ACCIDENT", "Accident"), ("BREAKDOWN", "Breakdown"),
                                                   ("BEHAVIOUR", "Behaviour on the bus"), ("INJURY", "Injury"),
                                                   ("LATE", "Serious delay"), ("OTHER", "Other")], max_length=9)),
                ("description", models.TextField()),
                ("action", models.TextField(blank=True)),
                ("parents_told", models.BooleanField(default=False)),
                ("closed", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                _user("reported_by"),
                ("route", models.ForeignKey(blank=True, null=True, on_delete=SET_NULL, related_name="incidents",
                                            to="transport.route")),
                ("students", models.ManyToManyField(blank=True, related_name="+", to="users.student")),
                ("vehicle", models.ForeignKey(blank=True, null=True, on_delete=SET_NULL, related_name="incidents",
                                              to="transport.vehicle")),
                _tenant(),
            ],
            options={"ordering": ["-date", "-id"]},
        ),
    ]
