"""payroll/models.py — Staff payroll tracking"""
from decimal import Decimal
from django.conf import settings
from django.db import models
from tenants.managers import TenantModelMixin, TenantManager

from django.utils import timezone
from users.models import Staff


class SalaryGrade(TenantModelMixin, models.Model):
    """Grade level with base salary — e.g. Grade 8 = ₦80,000/month"""
    name         = models.CharField(max_length=50)
    base_salary  = models.DecimalField(max_digits=14, decimal_places=2)
    description  = models.TextField(blank=True)

    class Meta:
        unique_together = ('tenant', 'name')

    def __str__(self):
        return f"{self.name}  ₦{self.base_salary:,.2f}/mo"


class StaffPayroll(TenantModelMixin, models.Model):
    """Links a Staff member to their salary grade and bank details."""
    staff        = models.OneToOneField(Staff, on_delete=models.CASCADE,
                                        related_name='payroll')
    grade        = models.ForeignKey(SalaryGrade, on_delete=models.CASCADE,
                                     null=True, blank=True)
    basic_salary = models.DecimalField(max_digits=14, decimal_places=2,
                                       default=Decimal('0.00'))
    bank_name    = models.CharField(max_length=100, blank=True)
    account_no   = models.CharField(max_length=20, blank=True)
    account_name = models.CharField(max_length=200, blank=True)
    is_active    = models.BooleanField(default=True)
    created_at   = models.DateTimeField(auto_now_add=True)
    # Monthly allowances (pension is worked out on basic + housing + transport)
    housing      = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    transport    = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    other_allowances = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    # Statutory details
    tax_id       = models.CharField('Tax ID (TIN)', max_length=20, blank=True)
    pays_tax     = models.BooleanField(default=True)
    pension_pin  = models.CharField('Pension PIN (RSA)', max_length=20, blank=True)
    pfa_name     = models.CharField('Pension fund administrator', max_length=100, blank=True)
    pays_pension = models.BooleanField(default=True)
    nhf_number   = models.CharField('NHF number', max_length=20, blank=True)
    pays_nhf     = models.BooleanField(default=False, help_text='National Housing Fund — only with the staff member\'s consent')
    annual_rent  = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'),
                                       help_text='Rent paid a year, with receipts — gives rent relief')
    other_reliefs = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'),
                                        help_text='Yearly health/life insurance, mortgage interest, etc. with evidence')
    union_dues   = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'),
                                       help_text='Fixed amount a month')

    def __str__(self):
        return f"Payroll — {self.staff.full_name}  ₦{self.basic_salary:,.2f}"

    @property
    def gross_monthly(self):
        return self.basic_salary + self.housing + self.transport + self.other_allowances


class SalaryPayment(TenantModelMixin, models.Model):
    """A monthly salary disbursement record."""
    STATUS_CHOICES = [
        ('PENDING',  'Pending'),
        ('PAID',     'Paid'),
        ('REVERSED', 'Reversed'),
    ]
    METHOD_CHOICES = [
        ('BANK_TRANSFER', 'Bank Transfer'),
        ('CASH',          'Cash'),
        ('CHEQUE',        'Cheque'),
    ]

    staff_payroll  = models.ForeignKey(StaffPayroll, on_delete=models.CASCADE,
                                       related_name='payments')
    month          = models.PositiveIntegerField()  # 1–12
    year           = models.PositiveIntegerField()
    gross_salary   = models.DecimalField(max_digits=14, decimal_places=2)
    deductions     = models.DecimalField(max_digits=14, decimal_places=2,
                                         default=Decimal('0.00'))
    net_salary     = models.DecimalField(max_digits=14, decimal_places=2)
    method         = models.CharField(max_length=20, choices=METHOD_CHOICES,
                                      default='BANK_TRANSFER')
    reference      = models.CharField(max_length=80, blank=True)
    status         = models.CharField(max_length=10, choices=STATUS_CHOICES,
                                      default='PENDING')
    note           = models.TextField(blank=True)
    paid_by        = models.ForeignKey(settings.AUTH_USER_MODEL,
                                       on_delete=models.SET_NULL,
                                       null=True, blank=True)
    paid_at        = models.DateTimeField(null=True, blank=True)
    created_at     = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-year', '-month']
        unique_together = ('staff_payroll', 'month', 'year')

    @property
    def month_name(self):
        import calendar
        return calendar.month_name[self.month]

    def __str__(self):
        return (f"{self.staff_payroll.staff.full_name}  "
                f"{self.month_name} {self.year}  ₦{self.net_salary:,.2f}")

# ─────────────────────────────────────────────────────────────────────────────
# Payroll runs (monthly and off-cycle), payslips, loans and settings
# ─────────────────────────────────────────────────────────────────────────────

class PayrollSettings(TenantModelMixin, models.Model):
    pension_employee_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('8.00'))
    pension_employer_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('10.00'))
    nhf_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('2.50'))
    minimum_wage = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('70000.00'),
                                       help_text='Monthly pay at or below this is free of income tax')
    deduct_unpaid_leave = models.BooleanField(default=True)
    separate_approver = models.BooleanField(default=True,
                                            help_text='The person who approves a run must not be the one who prepared it')
    bank_narration = models.CharField(max_length=60, default='Salary')
    cover_rate = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'),
                                     help_text='Paid for each lesson covered for an absent colleague (0 = unpaid)')

    objects = TenantManager()

    class Meta:
        constraints = [models.UniqueConstraint(fields=['tenant'], name='one_payroll_settings_per_school')]


class PayrollRun(TenantModelMixin, models.Model):
    MONTHLY, BONUS, THIRTEENTH = 'MONTHLY', 'BONUS', '13TH'
    KINDS = [(MONTHLY, 'Monthly salary'), (BONUS, 'Bonus'), (THIRTEENTH, '13th month')]
    DRAFT, SUBMITTED, APPROVED, PAID, CANCELLED = 'DRAFT', 'SUBMITTED', 'APPROVED', 'PAID', 'CANCELLED'
    STATUSES = [(DRAFT, 'Being prepared'), (SUBMITTED, 'Waiting for approval'), (APPROVED, 'Approved — waiting to pay'),
                (PAID, 'Paid'), (CANCELLED, 'Cancelled')]

    kind = models.CharField(max_length=7, choices=KINDS, default=MONTHLY)
    month = models.PositiveSmallIntegerField()
    year = models.PositiveSmallIntegerField()
    title = models.CharField(max_length=120, blank=True)
    bonus_percent = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal('0.00'),
                                        help_text='Bonus runs: percent of monthly basic (13th month = 100)')
    status = models.CharField(max_length=9, choices=STATUSES, default=DRAFT)
    prepared_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='+')
    submitted_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name='+')
    approved_at = models.DateTimeField(null=True, blank=True)
    paid_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                related_name='+')
    paid_at = models.DateTimeField(null=True, blank=True)
    note = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        ordering = ['-year', '-month', '-id']

    @property
    def month_name(self):
        import calendar
        return calendar.month_name[self.month]

    def __str__(self):
        return self.title or f'{self.get_kind_display()} — {self.month_name} {self.year}'


class Payslip(TenantModelMixin, models.Model):
    run = models.ForeignKey(PayrollRun, on_delete=models.CASCADE, related_name='payslips')
    staff_payroll = models.ForeignKey(StaffPayroll, on_delete=models.CASCADE, related_name='payslips')
    basic = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    housing = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    transport = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    other_allowances = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    bonus = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    extra_pay = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'),
                                    help_text='Overtime or one-off additions entered by the bursar')
    unpaid_leave = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    extra_lessons = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'),
                                        help_text='After-school groups the teacher ran this month')
    cover_pay = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'),
                                    help_text='Lessons covered for absent colleagues this month')
    gross = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    pension_employee = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    pension_employer = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    nhf = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    paye = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    union_dues = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    loan_repayment = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    other_deductions = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    net = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    workings = models.JSONField(default=dict, help_text='How the tax was worked out')
    note = models.CharField(max_length=160, blank=True)
    emailed_at = models.DateTimeField(null=True, blank=True)

    objects = TenantManager()

    class Meta:
        unique_together = ('run', 'staff_payroll')
        ordering = ['staff_payroll__staff__full_name']

    @property
    def other_total(self):
        return self.union_dues + self.other_deductions

    @property
    def total_deductions(self):
        return (self.pension_employee + self.nhf + self.paye + self.union_dues + self.loan_repayment
                + self.other_deductions)


class StaffLoan(TenantModelMixin, models.Model):
    LOAN, ADVANCE = 'LOAN', 'ADVANCE'
    staff_payroll = models.ForeignKey(StaffPayroll, on_delete=models.CASCADE, related_name='loans')
    kind = models.CharField(max_length=7, choices=[(LOAN, 'Loan'), (ADVANCE, 'Salary advance')], default=LOAN)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    monthly_repayment = models.DecimalField(max_digits=14, decimal_places=2)
    balance = models.DecimalField(max_digits=14, decimal_places=2)
    start_month = models.PositiveSmallIntegerField()
    start_year = models.PositiveSmallIntegerField()
    note = models.CharField(max_length=200, blank=True)
    active = models.BooleanField(default=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        ordering = ['-created_at']


class LoanRepayment(TenantModelMixin, models.Model):
    loan = models.ForeignKey(StaffLoan, on_delete=models.CASCADE, related_name='repayments')
    payslip = models.ForeignKey(Payslip, on_delete=models.CASCADE, related_name='loan_repayments')
    amount = models.DecimalField(max_digits=14, decimal_places=2)

    objects = TenantManager()


class PayDetailsRequest(TenantModelMixin, models.Model):
    """A staff member asking to change their bank or tax details; the bursary checks it before it takes effect."""
    PENDING, APPROVED, REJECTED = 'PENDING', 'APPROVED', 'REJECTED'
    staff_payroll = models.ForeignKey(StaffPayroll, on_delete=models.CASCADE, related_name='change_requests')
    changes = models.JSONField(default=dict)
    status = models.CharField(max_length=8, choices=[(PENDING, 'Waiting'), (APPROVED, 'Approved'),
                                                     (REJECTED, 'Declined')], default=PENDING)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name='+')
    reviewed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        ordering = ['-created_at']
