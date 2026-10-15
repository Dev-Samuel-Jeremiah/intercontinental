import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("tenants", "0006_alter_tenant_owner"),
        ("users", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="IDCard",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("card_type", models.CharField(choices=[("STUDENT", "Student"), ("STAFF", "Staff")], max_length=10)),
                ("card_uid", models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ("card_number", models.CharField(blank=True, help_text="Human-readable card number printed on the card", max_length=30)),
                ("is_active", models.BooleanField(default=True)),
                ("issued_at", models.DateTimeField(auto_now_add=True)),
                ("reissued_at", models.DateTimeField(blank=True, null=True)),
                ("tenant", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, to="tenants.tenant")),
                ("staff", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="id_card", to="users.staff")),
                ("student", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="id_card", to="users.student")),
            ],
            options={
                "ordering": ["-issued_at"],
            },
        ),
        migrations.CreateModel(
            name="ReceptionStation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(default="Main Reception", max_length=100)),
                ("station_uid", models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("tenant", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, to="tenants.tenant")),
            ],
        ),
        migrations.CreateModel(
            name="StaffClockLog",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("direction", models.CharField(choices=[("IN", "Arrived"), ("OUT", "Departed")], max_length=3)),
                ("scanned_at", models.DateTimeField(auto_now_add=True)),
                ("tenant", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, to="tenants.tenant")),
                ("staff", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="clock_logs", to="users.staff")),
                ("station", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="clock_logs", to="idcards.receptionstation")),
            ],
            options={
                "ordering": ["-scanned_at"],
            },
        ),
        migrations.CreateModel(
            name="GateScanLog",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("direction", models.CharField(choices=[("IN", "Arrived"), ("OUT", "Departed")], max_length=3)),
                ("scanned_at", models.DateTimeField(auto_now_add=True)),
                ("parent_notified", models.BooleanField(default=False)),
                ("notify_error", models.CharField(blank=True, max_length=255)),
                ("tenant", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, to="tenants.tenant")),
                ("card", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="gate_scans", to="idcards.idcard")),
                ("student", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="gate_scans", to="users.student")),
                ("scanned_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="gate_scans_operated", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "ordering": ["-scanned_at"],
            },
        ),
    ]
