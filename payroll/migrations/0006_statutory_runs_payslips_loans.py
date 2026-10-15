# PAYE, pension and NHF details, payroll runs with approvals, payslips and staff loans.

from decimal import Decimal

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def money(**kw):
    return models.DecimalField(decimal_places=2, default=Decimal("0.00"), max_digits=14, **kw)


def _id():
    return ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID"))


def _tenant():
    return ("tenant", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE,
                                        to="tenants.tenant"))


def _user(name, **kw):
    return (name, models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+",
                                    to=settings.AUTH_USER_MODEL, **kw))


class Migration(migrations.Migration):

    dependencies = [
        ("payroll", "0005_unique_per_tenant"),
        ("tenants", "0006_alter_tenant_owner"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField("staffpayroll", "housing", money()),
        migrations.AddField("staffpayroll", "transport", money()),
        migrations.AddField("staffpayroll", "other_allowances", money()),
        migrations.AddField("staffpayroll", "tax_id", models.CharField(blank=True, max_length=20, verbose_name="Tax ID (TIN)")),
        migrations.AddField("staffpayroll", "pays_tax", models.BooleanField(default=True)),
        migrations.AddField("staffpayroll", "pension_pin",
                            models.CharField(blank=True, max_length=20, verbose_name="Pension PIN (RSA)")),
        migrations.AddField("staffpayroll", "pfa_name",
                            models.CharField(blank=True, max_length=100, verbose_name="Pension fund administrator")),
        migrations.AddField("staffpayroll", "pays_pension", models.BooleanField(default=True)),
        migrations.AddField("staffpayroll", "nhf_number", models.CharField(blank=True, max_length=20, verbose_name="NHF number")),
        migrations.AddField("staffpayroll", "pays_nhf", models.BooleanField(
            default=False, help_text="National Housing Fund — only with the staff member's consent")),
        migrations.AddField("staffpayroll", "annual_rent", money(help_text="Rent paid a year, with receipts — gives rent relief")),
        migrations.AddField("staffpayroll", "other_reliefs", money(
            help_text="Yearly health/life insurance, mortgage interest, etc. with evidence")),
        migrations.AddField("staffpayroll", "union_dues", money(help_text="Fixed amount a month")),
        migrations.CreateModel(
            name="PayrollSettings",
            fields=[
                _id(),
                ("pension_employee_rate", models.DecimalField(decimal_places=2, default=Decimal("8.00"), max_digits=5)),
                ("pension_employer_rate", models.DecimalField(decimal_places=2, default=Decimal("10.00"), max_digits=5)),
                ("nhf_rate", models.DecimalField(decimal_places=2, default=Decimal("2.50"), max_digits=5)),
                ("minimum_wage", models.DecimalField(decimal_places=2, default=Decimal("70000.00"), max_digits=14,
                                                     help_text="Monthly pay at or below this is free of income tax")),
                ("deduct_unpaid_leave", models.BooleanField(default=True)),
                ("separate_approver", models.BooleanField(
                    default=True, help_text="The person who approves a run must not be the one who prepared it")),
                ("bank_narration", models.CharField(default="Salary", max_length=60)),
                _tenant(),
            ],
        ),
        migrations.AddConstraint(
            model_name="payrollsettings",
            constraint=models.UniqueConstraint(fields=("tenant",), name="one_payroll_settings_per_school"),
        ),
        migrations.CreateModel(
            name="PayrollRun",
            fields=[
                _id(),
                ("kind", models.CharField(choices=[("MONTHLY", "Monthly salary"), ("BONUS", "Bonus"),
                                                   ("13TH", "13th month")], default="MONTHLY", max_length=7)),
                ("month", models.PositiveSmallIntegerField()),
                ("year", models.PositiveSmallIntegerField()),
                ("title", models.CharField(blank=True, max_length=120)),
                ("bonus_percent", models.DecimalField(decimal_places=2, default=Decimal("0.00"), max_digits=6,
                                                      help_text="Bonus runs: percent of monthly basic (13th month = 100)")),
                ("status", models.CharField(choices=[("DRAFT", "Being prepared"), ("SUBMITTED", "Waiting for approval"),
                                                     ("APPROVED", "Approved — waiting to pay"), ("PAID", "Paid"),
                                                     ("CANCELLED", "Cancelled")], default="DRAFT", max_length=9)),
                ("submitted_at", models.DateTimeField(blank=True, null=True)),
                ("approved_at", models.DateTimeField(blank=True, null=True)),
                ("paid_at", models.DateTimeField(blank=True, null=True)),
                ("note", models.CharField(blank=True, max_length=255)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                _user("prepared_by"),
                _user("approved_by", blank=True),
                _user("paid_by", blank=True),
                _tenant(),
            ],
            options={"ordering": ["-year", "-month", "-id"]},
        ),
        migrations.CreateModel(
            name="Payslip",
            fields=[
                _id(),
                ("basic", money()), ("housing", money()), ("transport", money()), ("other_allowances", money()),
                ("bonus", money()),
                ("extra_pay", money(help_text="Overtime or one-off additions entered by the bursar")),
                ("unpaid_leave", money()), ("gross", money()), ("pension_employee", money()),
                ("pension_employer", money()), ("nhf", money()), ("paye", money()), ("union_dues", money()),
                ("loan_repayment", money()), ("other_deductions", money()), ("net", money()),
                ("workings", models.JSONField(default=dict, help_text="How the tax was worked out")),
                ("note", models.CharField(blank=True, max_length=160)),
                ("emailed_at", models.DateTimeField(blank=True, null=True)),
                ("run", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="payslips",
                                          to="payroll.payrollrun")),
                ("staff_payroll", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                                    related_name="payslips", to="payroll.staffpayroll")),
                _tenant(),
            ],
            options={"ordering": ["staff_payroll__staff__full_name"], "unique_together": {("run", "staff_payroll")}},
        ),
        migrations.CreateModel(
            name="StaffLoan",
            fields=[
                _id(),
                ("kind", models.CharField(choices=[("LOAN", "Loan"), ("ADVANCE", "Salary advance")], default="LOAN",
                                          max_length=7)),
                ("amount", models.DecimalField(decimal_places=2, max_digits=14)),
                ("monthly_repayment", models.DecimalField(decimal_places=2, max_digits=14)),
                ("balance", models.DecimalField(decimal_places=2, max_digits=14)),
                ("start_month", models.PositiveSmallIntegerField()),
                ("start_year", models.PositiveSmallIntegerField()),
                ("note", models.CharField(blank=True, max_length=200)),
                ("active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                _user("created_by"),
                ("staff_payroll", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="loans",
                                                    to="payroll.staffpayroll")),
                _tenant(),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="LoanRepayment",
            fields=[
                _id(),
                ("amount", models.DecimalField(decimal_places=2, max_digits=14)),
                ("loan", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="repayments",
                                           to="payroll.staffloan")),
                ("payslip", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                              related_name="loan_repayments", to="payroll.payslip")),
                _tenant(),
            ],
        ),
        migrations.CreateModel(
            name="PayDetailsRequest",
            fields=[
                _id(),
                ("changes", models.JSONField(default=dict)),
                ("status", models.CharField(choices=[("PENDING", "Waiting"), ("APPROVED", "Approved"),
                                                     ("REJECTED", "Declined")], default="PENDING", max_length=8)),
                ("reviewed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                _user("reviewed_by", blank=True),
                ("staff_payroll", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                                    related_name="change_requests", to="payroll.staffpayroll")),
                _tenant(),
            ],
            options={"ordering": ["-created_at"]},
        ),
    ]
