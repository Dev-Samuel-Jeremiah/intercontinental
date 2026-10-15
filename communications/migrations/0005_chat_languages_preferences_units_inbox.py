import datetime
from decimal import Decimal

import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models

CASCADE, SET_NULL = django.db.models.deletion.CASCADE, django.db.models.deletion.SET_NULL
LANGUAGES = [("en", "English"), ("ha", "Hausa"), ("yo", "Yoruba"), ("ig", "Igbo"), ("pcm", "Nigerian Pidgin"),
             ("fr", "French")]


def _id():
    return ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID"))


def _tenant():
    return ("tenant", models.ForeignKey(blank=True, null=True, on_delete=CASCADE, to="tenants.tenant"))


def _user(name, blank=False):
    return (name, models.ForeignKey(blank=blank, null=True, on_delete=SET_NULL, related_name="+",
                                    to=settings.AUTH_USER_MODEL))


class Migration(migrations.Migration):

    dependencies = [
        ("communications", "0004_approval_surveys_consent_birthdays"),
        ("announcement", "0002_initial"),
        ("tenants", "0006_alter_tenant_owner"),
        ("users", "0011_alter_attendancesettings_school_starts_at"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="campaign", name="topic",
            field=models.CharField(choices=[("GENERAL", "School news and events"), ("FEES", "Fees and payments"),
                                            ("ACADEMIC", "Results and academics"),
                                            ("URGENT", "Safety and urgent notices")], default="GENERAL",
                                   help_text="Parents can turn off school news; other topics always reach them.",
                                   max_length=8)),
        migrations.AddField(model_name="commsettings", name="chat_enabled", field=models.BooleanField(default=True)),
        migrations.AddField(model_name="commsettings", name="office_days",
                            field=models.CharField(default="MON,TUE,WED,THU,FRI", max_length=40)),
        migrations.AddField(model_name="commsettings", name="office_start",
                            field=models.TimeField(default=datetime.time(8, 0))),
        migrations.AddField(model_name="commsettings", name="office_end",
                            field=models.TimeField(default=datetime.time(16, 0))),
        migrations.AddField(model_name="commsettings", name="whatsapp_enabled",
                            field=models.BooleanField(default=False)),
        migrations.AddField(model_name="commsettings", name="whatsapp_template",
                            field=models.CharField(blank=True, help_text="Approved WhatsApp template with one body "
                                                                         "variable", max_length=80)),
        migrations.AddField(model_name="commsettings", name="whatsapp_language",
                            field=models.CharField(default="en", max_length=10)),
        migrations.CreateModel(
            name="MessageTemplateTranslation",
            fields=[
                _id(),
                ("language", models.CharField(choices=LANGUAGES, max_length=4)),
                ("subject", models.CharField(blank=True, max_length=300)),
                ("body_email", models.TextField(blank=True)),
                ("body_sms", models.CharField(blank=True, max_length=800)),
                ("ai_drafted", models.BooleanField(default=False)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("template", models.ForeignKey(on_delete=CASCADE, related_name="translations",
                                               to="communications.messagetemplate")),
                _user("updated_by"),
                _tenant(),
            ],
            options={"unique_together": {("template", "language")}},
        ),
        migrations.CreateModel(
            name="FamilyPreference",
            fields=[
                _id(),
                ("key", models.CharField(max_length=160)),
                ("language", models.CharField(choices=LANGUAGES, default="en", max_length=4)),
                ("topics_off", models.JSONField(default=list)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                _user("updated_by"),
                _tenant(),
            ],
            options={"unique_together": {("tenant", "key")}},
        ),
        migrations.CreateModel(
            name="Conversation",
            fields=[
                _id(),
                ("subject", models.CharField(max_length=160)),
                ("closed", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("last_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("parent", models.ForeignKey(on_delete=CASCADE, related_name="+", to=settings.AUTH_USER_MODEL)),
                ("student", models.ForeignKey(on_delete=CASCADE, related_name="conversations", to="users.student")),
                ("teacher", models.ForeignKey(on_delete=CASCADE, related_name="conversations", to="users.staff")),
                _tenant(),
            ],
            options={"ordering": ["-last_at"]},
        ),
        migrations.CreateModel(
            name="ChatMessage",
            fields=[
                _id(),
                ("body", models.TextField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("read_at", models.DateTimeField(blank=True, null=True)),
                ("hidden", models.BooleanField(default=False, help_text="Hidden by an administrator")),
                ("flagged", models.BooleanField(default=False)),
                ("flag_reason", models.CharField(blank=True, max_length=255)),
                ("conversation", models.ForeignKey(on_delete=CASCADE, related_name="messages",
                                                   to="communications.conversation")),
                _user("hidden_by", blank=True),
                _user("sender"),
                _tenant(),
            ],
            options={"ordering": ["created_at"]},
        ),
        migrations.CreateModel(
            name="AnnouncementReminder",
            fields=[
                _id(),
                ("families", models.PositiveIntegerField(default=0)),
                ("sent_at", models.DateTimeField(auto_now_add=True)),
                ("announcement", models.ForeignKey(on_delete=CASCADE, related_name="reminders",
                                                   to="announcement.announcement")),
                _user("sent_by"),
                _tenant(),
            ],
            options={"ordering": ["-sent_at"]},
        ),
        migrations.CreateModel(
            name="SmsWallet",
            fields=[
                _id(),
                ("balance", models.IntegerField(default=0)),
                ("used_total", models.PositiveIntegerField(default=0)),
                ("low_alert_sent_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                _tenant(),
            ],
            options={"constraints": [models.UniqueConstraint(fields=("tenant",), name="one_sms_wallet_per_school")]},
        ),
        migrations.CreateModel(
            name="SmsUsageDay",
            fields=[
                _id(),
                ("date", models.DateField()),
                ("units", models.PositiveIntegerField(default=0)),
                ("messages", models.PositiveIntegerField(default=0)),
                _tenant(),
            ],
            options={"ordering": ["-date"], "unique_together": {("tenant", "date")}},
        ),
        migrations.CreateModel(
            name="SmsTopUp",
            fields=[
                _id(),
                ("units", models.PositiveIntegerField()),
                ("amount", models.DecimalField(decimal_places=2, default=Decimal("0"), max_digits=12)),
                ("reference", models.CharField(max_length=80, unique=True)),
                ("kind", models.CharField(choices=[("PURCHASE", "Bought"), ("GRANT", "Free units")],
                                          default="PURCHASE", max_length=8)),
                ("status", models.CharField(choices=[("PENDING", "Waiting for payment"), ("PAID", "Paid"),
                                                     ("FAILED", "Failed")], default="PENDING", max_length=7)),
                ("paid_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                _user("created_by"),
                _tenant(),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="InboundMessage",
            fields=[
                _id(),
                ("channel", models.CharField(choices=[("SMS", "SMS"), ("WA", "WhatsApp")], max_length=3)),
                ("from_number", models.CharField(max_length=30)),
                ("body", models.TextField()),
                ("provider_id", models.CharField(blank=True, max_length=120)),
                ("received_at", models.DateTimeField(auto_now_add=True)),
                ("handled", models.BooleanField(default=False)),
                ("handled_at", models.DateTimeField(blank=True, null=True)),
                ("student", models.ForeignKey(blank=True, null=True, on_delete=SET_NULL, related_name="+",
                                              to="users.student")),
                _user("handled_by", blank=True),
                _tenant(),
            ],
            options={"ordering": ["-received_at"]},
        ),
        migrations.CreateModel(
            name="InboxReply",
            fields=[
                _id(),
                ("body", models.TextField()),
                ("ok", models.BooleanField(default=False)),
                ("error", models.CharField(blank=True, max_length=255)),
                ("sent_at", models.DateTimeField(auto_now_add=True)),
                ("message", models.ForeignKey(on_delete=CASCADE, related_name="replies",
                                              to="communications.inboundmessage")),
                _user("sent_by"),
                _tenant(),
            ],
            options={"ordering": ["sent_at"]},
        ),
    ]
