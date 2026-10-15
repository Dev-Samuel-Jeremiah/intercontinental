from decimal import Decimal

import django.db.models.deletion
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


class Migration(migrations.Migration):

    dependencies = [
        ("communications", "0003_unique_per_tenant"),
        ("curriculum", "0004_event_rsvp_and_reminders"),
        ("tenants", "0006_alter_tenant_owner"),
        ("users", "0011_alter_attendancesettings_school_starts_at"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AlterField(
            model_name="campaign",
            name="status",
            field=models.CharField(choices=[
                ("DRAFT", "Draft"), ("PENDING", "Waiting for approval"), ("REJECTED", "Not approved"),
                ("SCHEDULED", "Scheduled"), ("SENDING", "Sending"), ("SENT", "Sent"), ("FAILED", "Failed"),
                ("CANCELLED", "Cancelled")], default="DRAFT", max_length=10),
        ),
        migrations.AddField(model_name="campaign", name="approved_by",
                            field=models.ForeignKey(blank=True, null=True, on_delete=SET_NULL, related_name="+",
                                                    to=settings.AUTH_USER_MODEL)),
        migrations.AddField(model_name="campaign", name="approved_at",
                            field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name="campaign", name="review_note",
                            field=models.CharField(blank=True, max_length=255)),
        migrations.CreateModel(
            name="CommSettings",
            fields=[
                _id(),
                ("require_approval", models.BooleanField(
                    default=True, help_text="Campaigns from staff who are not administrators wait for approval.")),
                ("birthday_students", models.BooleanField(default=False, help_text="Greet students on their birthday.")),
                ("birthday_staff", models.BooleanField(default=False, help_text="Greet staff on their birthday.")),
                ("birthday_student_sms", models.CharField(
                    default="Happy birthday, {{student_name}}! Wishing you a wonderful year ahead. - {{school_name}}",
                    max_length=320)),
                ("birthday_staff_sms", models.CharField(
                    default="Happy birthday, {{name}}! Thank you for all you do. - {{school_name}}", max_length=320)),
                ("signage_token", models.CharField(blank=True, max_length=40)),
                ("signage_seconds", models.PositiveSmallIntegerField(default=12,
                                                                     help_text="Seconds each slide stays up")),
                _tenant(),
            ],
        ),
        migrations.CreateModel(
            name="Survey",
            fields=[
                _id(),
                ("title", models.CharField(max_length=160)),
                ("description", models.TextField(blank=True)),
                ("audience", models.CharField(choices=[("PARENTS", "Parents"), ("STAFF", "Staff"),
                                                       ("BOTH", "Parents and staff")], default="PARENTS",
                                              max_length=7)),
                ("anonymous", models.BooleanField(default=True, help_text="Names are not shown with the answers")),
                ("questions", models.JSONField(default=list,
                                               help_text='[{"text": "...", "kind": "CHOICE", "options": [...]}]')),
                ("closes", models.DateField()),
                ("published", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                _user("created_by"),
                _tenant(),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="SurveyResponse",
            fields=[
                _id(),
                ("answers", models.JSONField(default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("survey", models.ForeignKey(on_delete=CASCADE, related_name="responses",
                                             to="communications.survey")),
                _user("user"),
                _tenant(),
            ],
            options={"unique_together": {("survey", "user")}},
        ),
        migrations.CreateModel(
            name="ConsentForm",
            fields=[
                _id(),
                ("title", models.CharField(max_length=160)),
                ("details", models.TextField(
                    help_text="What parents are agreeing to: dates, place, transport, cost, what to bring")),
                ("cost", models.DecimalField(decimal_places=2, default=Decimal("0"), max_digits=12)),
                ("deadline", models.DateField()),
                ("closed", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("classes", models.ManyToManyField(blank=True, help_text="Leave empty for the whole school",
                                                   related_name="+", to="users.class")),
                ("event", models.ForeignKey(blank=True, null=True, on_delete=SET_NULL, related_name="+",
                                            to="curriculum.calendarevent")),
                _user("created_by"),
                _tenant(),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="ConsentResponse",
            fields=[
                _id(),
                ("answer", models.CharField(choices=[("YES", "I give consent"), ("NO", "I do not give consent")],
                                            max_length=3)),
                ("signed_name", models.CharField(help_text="Typed full name, as a signature", max_length=160)),
                ("ip_address", models.CharField(blank=True, max_length=45)),
                ("note", models.CharField(blank=True, help_text="Medical or other information for the trip",
                                          max_length=255)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("form", models.ForeignKey(on_delete=CASCADE, related_name="responses",
                                           to="communications.consentform")),
                ("student", models.ForeignKey(on_delete=CASCADE, related_name="consents", to="users.student")),
                _user("signed_by"),
                _tenant(),
            ],
            options={"unique_together": {("form", "student")}},
        ),
        migrations.CreateModel(
            name="BirthdayGreeting",
            fields=[
                _id(),
                ("person", models.CharField(help_text="student:<id> or staff:<id>", max_length=20)),
                ("year", models.PositiveSmallIntegerField()),
                ("sent_at", models.DateTimeField(auto_now_add=True)),
                _tenant(),
            ],
            options={"unique_together": {("tenant", "person", "year")}},
        ),
        migrations.CreateModel(
            name="EmergencyBroadcast",
            fields=[
                _id(),
                ("subject", models.CharField(max_length=160)),
                ("message", models.TextField()),
                ("to_parents", models.BooleanField(default=True)),
                ("to_staff", models.BooleanField(default=True)),
                ("sms_sent", models.PositiveIntegerField(default=0)),
                ("sms_failed", models.PositiveIntegerField(default=0)),
                ("emails_sent", models.PositiveIntegerField(default=0)),
                ("emails_failed", models.PositiveIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                _user("sent_by"),
                _tenant(),
            ],
            options={"ordering": ["-created_at"]},
        ),
    ]
