from decimal import Decimal

import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models

CASCADE, SET_NULL = django.db.models.deletion.CASCADE, django.db.models.deletion.SET_NULL
RATINGS = [(5, "Excellent"), (4, "Very good"), (3, "Good"), (2, "Fair"), (1, "Needs improvement")]


def _id():
    return ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID"))


def _tenant():
    return ("tenant", models.ForeignKey(blank=True, null=True, on_delete=CASCADE, to="tenants.tenant"))


def _user(name, blank=False):
    return (name, models.ForeignKey(blank=blank, null=True, on_delete=SET_NULL, related_name="+",
                                    to=settings.AUTH_USER_MODEL))


class Migration(migrations.Migration):

    dependencies = [
        ("hostel", "0005_unique_per_tenant"),
        ("academics", "0002_initial"),
        ("tenants", "0006_alter_tenant_owner"),
        ("users", "0011_alter_attendancesettings_school_starts_at"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="boarderprofile", name="diet",
            field=models.CharField(choices=[("NONE", "No special diet"), ("VEGETARIAN", "Vegetarian"),
                                            ("HALAL", "Halal only"), ("NO_PORK", "No pork"),
                                            ("ALLERGY", "Food allergy"), ("MEDICAL", "Medical diet"),
                                            ("OTHER", "Other")], default="NONE", max_length=10)),
        migrations.AddField(
            model_name="boarderprofile", name="diet_note",
            field=models.CharField(blank=True, help_text="e.g. allergic to groundnuts", max_length=255)),
        migrations.CreateModel(
            name="RollCall",
            fields=[
                _id(),
                ("kind", models.CharField(choices=[("MORNING", "Morning roll call"), ("NIGHT", "Night roll call"),
                                                   ("PREP", "Prep (study)"), ("LIGHTS", "Lights-out check"),
                                                   ("OTHER", "Other")], default="NIGHT", max_length=7)),
                ("date", models.DateField(default=django.utils.timezone.localdate)),
                ("note", models.CharField(blank=True, max_length=255)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("hostel", models.ForeignKey(on_delete=CASCADE, related_name="roll_calls", to="hostel.hostel")),
                _user("taken_by"),
                _tenant(),
            ],
            options={"ordering": ["-date", "kind"], "unique_together": {("hostel", "kind", "date")}},
        ),
        migrations.CreateModel(
            name="RollCallMark",
            fields=[
                _id(),
                ("status", models.CharField(choices=[("PRESENT", "Present"), ("ABSENT", "Absent"), ("LATE", "Late"),
                                                     ("EXEAT", "On exeat"), ("SICK", "In sick bay"),
                                                     ("EXCUSED", "Excused")], default="PRESENT", max_length=7)),
                ("note", models.CharField(blank=True, max_length=160)),
                ("boarder", models.ForeignKey(on_delete=CASCADE, related_name="roll_marks",
                                              to="hostel.boarderprofile")),
                ("roll_call", models.ForeignKey(on_delete=CASCADE, related_name="marks", to="hostel.rollcall")),
                _tenant(),
            ],
            options={"unique_together": {("roll_call", "boarder")}},
        ),
        migrations.CreateModel(
            name="ExeatRequest",
            fields=[
                _id(),
                ("leave_at", models.DateTimeField()),
                ("return_by", models.DateTimeField()),
                ("reason", models.CharField(max_length=255)),
                ("pickup_person", models.CharField(help_text="Who collects the child", max_length=160)),
                ("pickup_phone", models.CharField(blank=True, max_length=20)),
                ("status", models.CharField(choices=[
                    ("PARENT", "Waiting for the parent"), ("SCHOOL", "Waiting for the housemaster"),
                    ("APPROVED", "Approved"), ("DECLINED", "Declined"), ("OUT", "Signed out"),
                    ("RETURNED", "Back in school"), ("CANCELLED", "Cancelled")], default="SCHOOL", max_length=9)),
                ("parent_approved_at", models.DateTimeField(blank=True, null=True)),
                ("school_note", models.CharField(blank=True, max_length=255)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("boarder", models.ForeignKey(on_delete=CASCADE, related_name="exeat_requests",
                                              to="hostel.boarderprofile")),
                ("movement", models.ForeignKey(blank=True, null=True, on_delete=SET_NULL, related_name="+",
                                               to="hostel.checkinout")),
                _user("parent_approved_by", blank=True),
                _user("requested_by"),
                _user("school_decided_by", blank=True),
                _tenant(),
            ],
            options={"ordering": ["-leave_at"]},
        ),
        migrations.CreateModel(
            name="VisitingDay",
            fields=[
                _id(),
                ("date", models.DateField()),
                ("start", models.TimeField()),
                ("end", models.TimeField()),
                ("slot_minutes", models.PositiveSmallIntegerField(default=60)),
                ("families_per_slot", models.PositiveSmallIntegerField(default=20)),
                ("notes", models.TextField(blank=True, help_text="Rules, what to bring, where to park")),
                ("open", models.BooleanField(default=True)),
                _tenant(),
            ],
            options={"ordering": ["date"]},
        ),
        migrations.CreateModel(
            name="VisitBooking",
            fields=[
                _id(),
                ("slot", models.TimeField()),
                ("visitors", models.PositiveSmallIntegerField(default=1)),
                ("names", models.CharField(help_text="Names of the visitors", max_length=255)),
                ("arrived_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("boarder", models.ForeignKey(on_delete=CASCADE, related_name="visit_bookings",
                                              to="hostel.boarderprofile")),
                ("day", models.ForeignKey(on_delete=CASCADE, related_name="bookings", to="hostel.visitingday")),
                _user("parent"),
                _tenant(),
            ],
            options={"ordering": ["slot"], "unique_together": {("day", "boarder")}},
        ),
        migrations.CreateModel(
            name="Inspection",
            fields=[
                _id(),
                ("date", models.DateField(default=django.utils.timezone.localdate)),
                ("scores", models.JSONField(default=dict, help_text='{"Cleanliness": 4, ...} each out of 5')),
                ("total", models.DecimalField(decimal_places=1, default=Decimal("0"), help_text="Percent",
                                              max_digits=5)),
                ("note", models.CharField(blank=True, max_length=255)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                _user("inspector"),
                ("room", models.ForeignKey(on_delete=CASCADE, related_name="inspections", to="hostel.room")),
                _tenant(),
            ],
            options={"ordering": ["-date", "-id"]},
        ),
        migrations.CreateModel(
            name="SickBayVisit",
            fields=[
                _id(),
                ("admitted_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("complaint", models.CharField(max_length=255)),
                ("temperature", models.DecimalField(blank=True, decimal_places=1, max_digits=4, null=True)),
                ("treatment", models.TextField(blank=True)),
                ("discharged_at", models.DateTimeField(blank=True, null=True)),
                ("outcome", models.CharField(blank=True, choices=[("", "Still in sick bay"),
                                                                  ("DORM", "Back to the dormitory/class"),
                                                                  ("HOME", "Sent home"),
                                                                  ("HOSPITAL", "Referred to hospital")],
                                             max_length=8)),
                ("parent_told", models.BooleanField(default=False)),
                _user("nurse"),
                ("student", models.ForeignKey(on_delete=CASCADE, related_name="sick_bay_visits",
                                              to="users.student")),
                _tenant(),
            ],
            options={"ordering": ["-admitted_at"]},
        ),
        migrations.CreateModel(
            name="RoomChangeRequest",
            fields=[
                _id(),
                ("reason", models.TextField()),
                ("status", models.CharField(choices=[("PENDING", "Waiting"), ("APPROVED", "Approved"),
                                                     ("DECLINED", "Declined")], default="PENDING", max_length=8)),
                ("note", models.CharField(blank=True, max_length=255)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("boarder", models.ForeignKey(on_delete=CASCADE, related_name="room_requests",
                                              to="hostel.boarderprofile")),
                _user("decided_by", blank=True),
                ("preferred_room", models.ForeignKey(blank=True, null=True, on_delete=SET_NULL, related_name="+",
                                                     to="hostel.room")),
                _user("requested_by"),
                _tenant(),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="BoardingReport",
            fields=[
                _id(),
                ("conduct", models.PositiveSmallIntegerField(choices=RATINGS, default=3)),
                ("neatness", models.PositiveSmallIntegerField(choices=RATINGS, default=3)),
                ("relationships", models.PositiveSmallIntegerField(choices=RATINGS, default=3)),
                ("comment", models.TextField(blank=True)),
                ("published", models.BooleanField(default=False)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("boarder", models.ForeignKey(on_delete=CASCADE, related_name="reports", to="hostel.boarderprofile")),
                ("term", models.ForeignKey(on_delete=CASCADE, related_name="+", to="academics.term")),
                _user("written_by"),
                _tenant(),
            ],
            options={"unique_together": {("boarder", "term")}},
        ),
    ]
