import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("tenants", "0006_alter_tenant_owner"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="KPITarget",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("metric", models.CharField(choices=[
                    ("attendance", "Attendance, last 30 days (%)"), ("fees", "Fees collected this term (%)"),
                    ("pass_rate", "Students averaging 50% or more, latest term (%)"),
                    ("average", "Average score, latest term (%)"), ("enrolment", "Students on roll")], max_length=12)),
                ("target", models.DecimalField(decimal_places=1, max_digits=10)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("tenant", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE,
                                             to="tenants.tenant")),
            ],
            options={"unique_together": {("tenant", "metric")}},
        ),
        migrations.CreateModel(
            name="ReportSubscription",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("email", models.EmailField(max_length=254)),
                ("frequency", models.CharField(choices=[("WEEKLY", "Every Monday"), ("MONTHLY", "First of the month")],
                                               default="WEEKLY", max_length=7)),
                ("active", models.BooleanField(default=True)),
                ("last_sent_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("created_by", models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL,
                                                 related_name="+", to=settings.AUTH_USER_MODEL)),
                ("tenant", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE,
                                             to="tenants.tenant")),
            ],
            options={"ordering": ["email"], "unique_together": {("tenant", "email")}},
        ),
    ]
