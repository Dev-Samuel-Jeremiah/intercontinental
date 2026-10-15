from decimal import Decimal

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("payroll", "0006_statutory_runs_payslips_loans"),
    ]

    operations = [
        migrations.AddField(
            model_name="payrollsettings", name="cover_rate",
            field=models.DecimalField(decimal_places=2, default=Decimal("0.00"), max_digits=10,
                                      help_text="Paid for each lesson covered for an absent colleague (0 = unpaid)"),
        ),
        migrations.AddField(
            model_name="payslip", name="extra_lessons",
            field=models.DecimalField(decimal_places=2, default=Decimal("0.00"), max_digits=14,
                                      help_text="After-school groups the teacher ran this month"),
        ),
        migrations.AddField(
            model_name="payslip", name="cover_pay",
            field=models.DecimalField(decimal_places=2, default=Decimal("0.00"), max_digits=14,
                                      help_text="Lessons covered for absent colleagues this month"),
        ),
    ]
