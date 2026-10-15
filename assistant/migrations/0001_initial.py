# AI settings, usage log, review queue and announcement translations.

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

PROVIDERS = [("openai", "OpenAI"), ("gemini", "Google Gemini"), ("claude", "Anthropic Claude")]


def _id():
    return ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID"))


def _tenant():
    return ("tenant", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE,
                                        to="tenants.tenant"))


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("announcement", "0002_initial"),
        ("tenants", "0006_alter_tenant_owner"),
        ("users", "0011_alter_attendancesettings_school_starts_at"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="AISettings",
            fields=[
                _id(),
                ("enabled", models.BooleanField(default=True)),
                ("default_provider", models.CharField(blank=True, choices=PROVIDERS,
                                                      help_text="Blank = the platform default", max_length=10)),
                ("feature_providers", models.JSONField(blank=True, default=dict, help_text="{feature: provider}")),
                ("monthly_tokens", models.PositiveIntegerField(blank=True, help_text="Blank = the platform allowance",
                                                               null=True)),
                ("second_reviewer", models.BooleanField(
                    default=False,
                    help_text="AI messages to parents must be approved by someone other than the writer")),
                ("updated_at", models.DateTimeField(auto_now=True)),
                _tenant(),
            ],
            options={"verbose_name_plural": "AI settings"},
        ),
        migrations.AddConstraint(
            model_name="aisettings",
            constraint=models.UniqueConstraint(fields=("tenant",), name="one_ai_settings_per_school"),
        ),
        migrations.CreateModel(
            name="AIUsage",
            fields=[
                _id(),
                ("feature", models.CharField(max_length=20)),
                ("provider", models.CharField(max_length=10)),
                ("model", models.CharField(blank=True, max_length=60)),
                ("input_tokens", models.PositiveIntegerField(default=0)),
                ("output_tokens", models.PositiveIntegerField(default=0)),
                ("ok", models.BooleanField(default=True)),
                ("error", models.CharField(blank=True, max_length=255)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                _tenant(),
                ("user", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                           related_name="+", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="AIDraft",
            fields=[
                _id(),
                ("purpose", models.CharField(choices=[("FEES", "Fees"), ("DISCIPLINE", "Behaviour"),
                                                      ("ATTENDANCE", "Attendance"), ("ACADEMIC", "Schoolwork"),
                                                      ("HEALTH", "Wellbeing"), ("PRAISE", "Praise"),
                                                      ("OTHER", "Other")], default="OTHER", max_length=10)),
                ("language", models.CharField(default="en", max_length=10)),
                ("channel", models.CharField(choices=[("EMAIL", "Email"), ("SMS", "SMS"), ("BOTH", "Email and SMS")],
                                             default="EMAIL", max_length=5)),
                ("brief", models.TextField(help_text="What the writer asked for")),
                ("subject", models.CharField(max_length=200)),
                ("body", models.TextField()),
                ("status", models.CharField(choices=[("PENDING", "Waiting for review"), ("SENT", "Approved and sent"),
                                                     ("REJECTED", "Rejected")], default="PENDING", max_length=8)),
                ("reviewed_at", models.DateTimeField(blank=True, null=True)),
                ("review_note", models.CharField(blank=True, max_length=255)),
                ("send_error", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("created_by", models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL,
                                                 related_name="+", to=settings.AUTH_USER_MODEL)),
                ("reviewed_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                                  related_name="+", to=settings.AUTH_USER_MODEL)),
                ("student", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="ai_drafts",
                                              to="users.student")),
                _tenant(),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="AnnouncementTranslation",
            fields=[
                _id(),
                ("language", models.CharField(choices=[("ha", "Hausa"), ("yo", "Yoruba"), ("ig", "Igbo"),
                                                       ("pcm", "Nigerian Pidgin"), ("fr", "French")], max_length=4)),
                ("title", models.CharField(max_length=255)),
                ("message", models.TextField()),
                ("approved", models.BooleanField(default=False)),
                ("approved_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("announcement", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                                   related_name="translations", to="announcement.announcement")),
                ("approved_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                                  related_name="+", to=settings.AUTH_USER_MODEL)),
                _tenant(),
            ],
            options={"ordering": ["language"], "unique_together": {("announcement", "language")}},
        ),
    ]
