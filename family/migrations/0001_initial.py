import datetime
from decimal import Decimal

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

CASCADE, SET_NULL = django.db.models.deletion.CASCADE, django.db.models.deletion.SET_NULL
DAYS = [("MON", "Monday"), ("TUE", "Tuesday"), ("WED", "Wednesday"), ("THU", "Thursday"), ("FRI", "Friday"),
        ("SAT", "Saturday")]
MODES = [("IN_PERSON", "At school"), ("PHONE", "Phone call"), ("VIDEO", "Video call")]


def _id():
    return ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID"))


def _tenant():
    return ("tenant", models.ForeignKey(blank=True, null=True, on_delete=CASCADE, to="tenants.tenant"))


def _user(name, on_delete=SET_NULL, null=True):
    return (name, models.ForeignKey(null=null, on_delete=on_delete, related_name="+", to=settings.AUTH_USER_MODEL))


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("finance", "0011_alter_feestructure_category_and_more"),
        ("tenants", "0006_alter_tenant_owner"),
        ("users", "0011_alter_attendancesettings_school_starts_at"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="FamilySettings",
            fields=[
                _id(),
                ("digest_enabled", models.BooleanField(default=False,
                                                       help_text="Email families a summary of each school day")),
                ("satisfaction_enabled", models.BooleanField(default=True,
                                                             help_text="Ask parents for a 0–10 score each term")),
                ("complaint_days", models.PositiveSmallIntegerField(default=5,
                                                                    help_text="Target days to resolve a complaint")),
                ("magic_login", models.BooleanField(
                    default=True, help_text="Parents may sign in with an emailed link or SMS code")),
                _tenant(),
            ],
        ),
        migrations.CreateModel(
            name="DigestLog",
            fields=[
                _id(),
                ("family", models.CharField(max_length=160)),
                ("date", models.DateField()),
                ("sent_at", models.DateTimeField(auto_now_add=True)),
                _tenant(),
            ],
            options={"unique_together": {("tenant", "family", "date")}},
        ),
        migrations.CreateModel(
            name="Availability",
            fields=[
                _id(),
                ("day", models.CharField(choices=DAYS, max_length=3)),
                ("start", models.TimeField()),
                ("end", models.TimeField()),
                ("slot_minutes", models.PositiveSmallIntegerField(default=15)),
                ("location", models.CharField(blank=True, max_length=120)),
                ("staff", models.ForeignKey(on_delete=CASCADE, related_name="availability", to="users.staff")),
                _tenant(),
            ],
            options={"ordering": ["staff__full_name", "day", "start"]},
        ),
        migrations.CreateModel(
            name="Appointment",
            fields=[
                _id(),
                ("date", models.DateField()),
                ("start", models.TimeField()),
                ("end", models.TimeField()),
                ("mode", models.CharField(choices=MODES, default="IN_PERSON", max_length=9)),
                ("reason", models.CharField(max_length=255)),
                ("status", models.CharField(choices=[("BOOKED", "Booked"), ("CANCELLED", "Cancelled"),
                                                     ("DONE", "Held")], default="BOOKED", max_length=9)),
                ("note", models.CharField(blank=True, max_length=255)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                _user("parent", on_delete=CASCADE, null=False),
                ("staff", models.ForeignKey(on_delete=CASCADE, related_name="appointments", to="users.staff")),
                ("student", models.ForeignKey(on_delete=CASCADE, related_name="appointments", to="users.student")),
                _tenant(),
            ],
            options={"ordering": ["date", "start"]},
        ),
        migrations.CreateModel(
            name="PtaExecutive",
            fields=[
                _id(),
                ("name", models.CharField(max_length=120)),
                ("role", models.CharField(max_length=80)),
                ("phone", models.CharField(blank=True, max_length=20)),
                ("email", models.EmailField(blank=True, max_length=254)),
                ("order", models.PositiveSmallIntegerField(default=0)),
                _tenant(),
            ],
            options={"ordering": ["order", "id"]},
        ),
        migrations.CreateModel(
            name="PtaMeeting",
            fields=[
                _id(),
                ("title", models.CharField(max_length=160)),
                ("date", models.DateField()),
                ("time", models.TimeField(default=datetime.time(10, 0))),
                ("venue", models.CharField(blank=True, max_length=160)),
                ("agenda", models.TextField(blank=True)),
                ("minutes", models.TextField(blank=True)),
                ("minutes_published", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                _tenant(),
            ],
            options={"ordering": ["-date"]},
        ),
        migrations.CreateModel(
            name="PtaLevy",
            fields=[
                _id(),
                ("title", models.CharField(max_length=160)),
                ("amount", models.DecimalField(decimal_places=2, max_digits=12)),
                ("due_date", models.DateField(blank=True, null=True)),
                ("active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                _tenant(),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="PtaPayment",
            fields=[
                _id(),
                ("family", models.CharField(max_length=160)),
                ("amount", models.DecimalField(decimal_places=2, default=Decimal("0"), max_digits=12)),
                ("method", models.CharField(choices=[("WALLET", "Family wallet"), ("CASH", "Cash"),
                                                     ("TRANSFER", "Bank transfer"), ("WAIVED", "Waived")],
                                            max_length=8)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("levy", models.ForeignKey(on_delete=CASCADE, related_name="payments", to="family.ptalevy")),
                _user("received_by"),
                ("student", models.ForeignKey(null=True, on_delete=SET_NULL, related_name="+", to="users.student")),
                ("transaction", models.ForeignKey(blank=True, null=True, on_delete=SET_NULL, related_name="+",
                                                  to="finance.transaction")),
                _tenant(),
            ],
            options={"unique_together": {("levy", "family")}},
        ),
        migrations.CreateModel(
            name="Opportunity",
            fields=[
                _id(),
                ("title", models.CharField(max_length=160)),
                ("details", models.TextField(blank=True)),
                ("date", models.DateField()),
                ("start_time", models.TimeField(blank=True, null=True)),
                ("end_time", models.TimeField(blank=True, null=True)),
                ("places", models.PositiveSmallIntegerField(blank=True, help_text="Empty = no limit", null=True)),
                ("cancelled", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                _user("created_by"),
                _tenant(),
            ],
            options={"ordering": ["date"]},
        ),
        migrations.CreateModel(
            name="Signup",
            fields=[
                _id(),
                ("name", models.CharField(max_length=120)),
                ("phone", models.CharField(blank=True, max_length=20)),
                ("note", models.CharField(blank=True, max_length=255)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("opportunity", models.ForeignKey(on_delete=CASCADE, related_name="signups",
                                                  to="family.opportunity")),
                _user("user", on_delete=CASCADE, null=False),
                _tenant(),
            ],
            options={"ordering": ["created_at"], "unique_together": {("opportunity", "user")}},
        ),
        migrations.CreateModel(
            name="Ticket",
            fields=[
                _id(),
                ("reference", models.CharField(max_length=20)),
                ("kind", models.CharField(choices=[("COMPLAINT", "Complaint"), ("FEEDBACK", "Feedback"),
                                                   ("SUGGESTION", "Suggestion"), ("COMPLIMENT", "Compliment")],
                                          default="COMPLAINT", max_length=10)),
                ("category", models.CharField(choices=[
                    ("ACADEMIC", "Teaching and learning"), ("FEES", "Fees and payments"),
                    ("WELFARE", "Welfare and safety"), ("TRANSPORT", "Transport"), ("FACILITIES", "Facilities"),
                    ("STAFF", "A member of staff"), ("FOOD", "Food and canteen"), ("OTHER", "Something else")],
                    default="OTHER", max_length=10)),
                ("subject", models.CharField(max_length=160)),
                ("body", models.TextField()),
                ("status", models.CharField(choices=[("OPEN", "New"), ("PROGRESS", "Being looked at"),
                                                     ("RESOLVED", "Resolved"), ("CLOSED", "Closed")],
                                            default="OPEN", max_length=8)),
                ("rating", models.PositiveSmallIntegerField(blank=True, null=True)),
                ("rating_comment", models.CharField(blank=True, max_length=255)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("resolved_at", models.DateTimeField(blank=True, null=True)),
                ("assigned_to", models.ForeignKey(blank=True, null=True, on_delete=SET_NULL, related_name="+",
                                                  to="users.staff")),
                _user("parent"),
                ("student", models.ForeignKey(blank=True, null=True, on_delete=SET_NULL, related_name="+",
                                              to="users.student")),
                _tenant(),
            ],
            options={"ordering": ["-created_at"], "unique_together": {("tenant", "reference")}},
        ),
        migrations.CreateModel(
            name="TicketMessage",
            fields=[
                _id(),
                ("body", models.TextField()),
                ("internal", models.BooleanField(default=False, help_text="Staff note, not shown to the parent")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                _user("author"),
                ("ticket", models.ForeignKey(on_delete=CASCADE, related_name="messages", to="family.ticket")),
                _tenant(),
            ],
            options={"ordering": ["created_at"]},
        ),
        migrations.CreateModel(
            name="LoginToken",
            fields=[
                _id(),
                ("token_hash", models.CharField(db_index=True, max_length=64)),
                ("channel", models.CharField(choices=[("EMAIL", "Email"), ("SMS", "SMS")], max_length=5)),
                ("sent_to", models.CharField(max_length=160)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("expires_at", models.DateTimeField()),
                ("used_at", models.DateTimeField(blank=True, null=True)),
                ("attempts", models.PositiveSmallIntegerField(default=0)),
                ("student", models.ForeignKey(on_delete=CASCADE, related_name="+", to="users.student")),
                _tenant(),
            ],
        ),
        migrations.CreateModel(
            name="SatisfactionResponse",
            fields=[
                _id(),
                ("family", models.CharField(max_length=160)),
                ("period", models.CharField(help_text="e.g. 2026-T1", max_length=10)),
                ("score", models.PositiveSmallIntegerField()),
                ("comment", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                _user("user"),
                _tenant(),
            ],
            options={"ordering": ["-created_at"], "unique_together": {("tenant", "family", "period")}},
        ),
    ]
