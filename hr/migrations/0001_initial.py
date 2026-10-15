from decimal import Decimal

import django.db.models.deletion
import hr.models
from django.conf import settings
from django.db import migrations, models

CASCADE, SET_NULL = django.db.models.deletion.CASCADE, django.db.models.deletion.SET_NULL


def _id():
    return ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID"))


def _tenant():
    return ("tenant", models.ForeignKey(blank=True, null=True, on_delete=CASCADE, to="tenants.tenant"))


def _user(name, blank=False):
    return (name, models.ForeignKey(blank=blank, null=True, on_delete=SET_NULL, related_name="+",
                                    to=settings.AUTH_USER_MODEL))


def _file(name, blank=True):
    kw = {"blank": True, "null": True} if blank else {}
    return (name, models.FileField(storage=hr.models._private, upload_to=hr.models._upload, **kw))


def _staff(related, on_delete=CASCADE, **kw):
    return ("staff", models.ForeignKey(on_delete=on_delete, related_name=related, to="users.staff", **kw))


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("curriculum", "0004_event_rsvp_and_reminders"),
        ("tenants", "0006_alter_tenant_owner"),
        ("users", "0011_alter_attendancesettings_school_starts_at"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="StaffRecord",
            fields=[
                _id(), _file("photo"),
                ("job_title", models.CharField(blank=True, max_length=100)),
                ("phone", models.CharField(blank=True, max_length=20)),
                ("extension", models.CharField(blank=True, max_length=10)),
                ("date_joined", models.DateField(blank=True, null=True)),
                ("emergency_name", models.CharField(blank=True, max_length=120)),
                ("emergency_phone", models.CharField(blank=True, max_length=20)),
                ("address", models.CharField(blank=True, max_length=255)),
                ("left_on", models.DateField(blank=True, null=True)),
                ("department", models.ForeignKey(blank=True, null=True, on_delete=SET_NULL, related_name="+",
                                                 to="curriculum.department")),
                ("staff", models.OneToOneField(on_delete=CASCADE, related_name="record", to="users.staff")),
                _tenant(),
            ],
        ),
        migrations.CreateModel(
            name="OnboardingTask",
            fields=[
                _id(),
                ("title", models.CharField(max_length=120)),
                ("order", models.PositiveSmallIntegerField(default=0)),
                ("needs_file", models.BooleanField(default=False,
                                                   help_text="A document must be uploaded to tick it off")),
                ("active", models.BooleanField(default=True)),
                _tenant(),
            ],
            options={"ordering": ["order", "id"]},
        ),
        migrations.CreateModel(
            name="OnboardingStep",
            fields=[
                _id(),
                ("done", models.BooleanField(default=False)),
                ("done_at", models.DateTimeField(blank=True, null=True)),
                _file("file"),
                ("note", models.CharField(blank=True, max_length=200)),
                _user("done_by", blank=True),
                _staff("onboarding"),
                ("task", models.ForeignKey(on_delete=CASCADE, related_name="steps", to="hr.onboardingtask")),
                _tenant(),
            ],
            options={"unique_together": {("staff", "task")}},
        ),
        migrations.CreateModel(
            name="Contract",
            fields=[
                _id(),
                ("kind", models.CharField(choices=[("PERMANENT", "Permanent"), ("FIXED", "Fixed term"),
                                                   ("PROBATION", "Probation"), ("PART_TIME", "Part-time"),
                                                   ("CONSULTANT", "Consultant / NYSC")], default="PERMANENT",
                                          max_length=10)),
                ("start", models.DateField()),
                ("end", models.DateField(blank=True, help_text="Leave empty for a permanent contract", null=True)),
                _file("file"),
                ("note", models.CharField(blank=True, max_length=200)),
                ("reminded_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                _user("created_by"),
                _staff("contracts"),
                _tenant(),
            ],
            options={"ordering": ["-start"]},
        ),
        migrations.CreateModel(
            name="AppraisalCycle",
            fields=[
                _id(),
                ("title", models.CharField(max_length=120)),
                ("opens", models.DateField()),
                ("closes", models.DateField()),
                ("criteria", models.JSONField(default=list, help_text='[{"name": "...", "weight": 20}]')),
                ("open", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                _user("created_by"),
                _tenant(),
            ],
            options={"ordering": ["-opens"]},
        ),
        migrations.CreateModel(
            name="Appraisal",
            fields=[
                _id(),
                ("self_scores", models.JSONField(default=dict)),
                ("self_comment", models.TextField(blank=True)),
                ("scores", models.JSONField(default=dict)),
                ("reviewer_comment", models.TextField(blank=True)),
                ("overall", models.DecimalField(blank=True, decimal_places=1, max_digits=5, null=True)),
                ("status", models.CharField(choices=[("SELF", "Self-review"), ("REVIEW", "With the reviewer"),
                                                     ("DONE", "Complete")], default="SELF", max_length=6)),
                ("submitted_at", models.DateTimeField(blank=True, null=True)),
                ("reviewed_at", models.DateTimeField(blank=True, null=True)),
                ("cycle", models.ForeignKey(on_delete=CASCADE, related_name="appraisals", to="hr.appraisalcycle")),
                ("reviewer", models.ForeignKey(blank=True, null=True, on_delete=SET_NULL, related_name="+",
                                               to="users.staff")),
                _staff("appraisals"),
                _tenant(),
            ],
            options={"ordering": ["staff__full_name"], "unique_together": {("cycle", "staff")}},
        ),
        migrations.CreateModel(
            name="TrainingRecord",
            fields=[
                _id(),
                ("title", models.CharField(max_length=160)),
                ("provider", models.CharField(blank=True, max_length=120)),
                ("date", models.DateField()),
                ("hours", models.DecimalField(decimal_places=1, default=Decimal("0"), max_digits=6)),
                ("cost", models.DecimalField(decimal_places=2, default=Decimal("0"), max_digits=12)),
                _file("certificate"),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                _user("added_by"),
                _staff("training"),
                _tenant(),
            ],
            options={"ordering": ["-date"]},
        ),
        migrations.CreateModel(
            name="DisciplinaryCase",
            fields=[
                _id(),
                ("kind", models.CharField(choices=[("QUERY", "Query"), ("WARNING", "Warning letter"),
                                                   ("SUSPENSION", "Suspension"), ("OTHER", "Other")],
                                          default="QUERY", max_length=10)),
                ("date", models.DateField()),
                ("allegation", models.TextField()),
                ("respond_by", models.DateField(blank=True, null=True)),
                ("response", models.TextField(blank=True)),
                ("responded_at", models.DateTimeField(blank=True, null=True)),
                ("outcome", models.CharField(blank=True, choices=[
                    ("NO_ACTION", "No further action"), ("WARNING", "Warning"), ("FINAL", "Final warning"),
                    ("SUSPENSION", "Suspension"), ("DISMISSAL", "Dismissal")], max_length=10)),
                ("outcome_note", models.TextField(blank=True)),
                ("closed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                _user("issued_by"),
                _staff("disciplinary"),
                _tenant(),
            ],
            options={"ordering": ["-date", "-id"]},
        ),
        migrations.CreateModel(
            name="ExitCase",
            fields=[
                _id(),
                ("kind", models.CharField(choices=[("RESIGNATION", "Resignation"), ("RETIREMENT", "Retirement"),
                                                   ("END", "End of contract"), ("DISMISSAL", "Dismissal"),
                                                   ("OTHER", "Other")], default="RESIGNATION", max_length=11)),
                ("notice_date", models.DateField()),
                ("last_day", models.DateField()),
                ("reason", models.CharField(blank=True, max_length=255)),
                ("handover", models.JSONField(default=list, help_text='[{"item": "...", "done": false}]')),
                ("other_amounts", models.DecimalField(
                    decimal_places=2, default=Decimal("0"), max_digits=14,
                    help_text="Anything else owed to them (or minus, owed by them)")),
                ("settlement", models.JSONField(default=dict)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                _user("completed_by", blank=True),
                _staff("exits"),
                _tenant(),
            ],
            options={"ordering": ["-last_day"]},
        ),
        migrations.CreateModel(
            name="Vacancy",
            fields=[
                _id(),
                ("title", models.CharField(max_length=160)),
                ("department", models.CharField(blank=True, max_length=80)),
                ("description", models.TextField()),
                ("requirements", models.TextField(blank=True)),
                ("closing_date", models.DateField()),
                ("published", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                _user("created_by"),
                _tenant(),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="JobApplication",
            fields=[
                _id(),
                ("full_name", models.CharField(max_length=160)),
                ("email", models.EmailField(max_length=254)),
                ("phone", models.CharField(max_length=20)),
                _file("cv", blank=False),
                ("cover_letter", models.TextField(blank=True)),
                ("status", models.CharField(choices=[("NEW", "New"), ("SHORTLISTED", "Shortlisted"),
                                                     ("INTERVIEW", "Interview booked"), ("OFFERED", "Offered"),
                                                     ("HIRED", "Hired"), ("REJECTED", "Not successful")],
                                            default="NEW", max_length=11)),
                ("interview_at", models.DateTimeField(blank=True, null=True)),
                ("interview_venue", models.CharField(blank=True, max_length=160)),
                ("notes", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("vacancy", models.ForeignKey(on_delete=CASCADE, related_name="applications", to="hr.vacancy")),
                _tenant(),
            ],
            options={"ordering": ["-created_at"]},
        ),
    ]
