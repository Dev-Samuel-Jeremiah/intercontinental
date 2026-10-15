from django.contrib import admin
from .models import SalaryGrade, StaffPayroll, SalaryPayment

@admin.register(SalaryGrade)
class SalaryGradeAdmin(admin.ModelAdmin):
    list_display = ['name', 'base_salary']

@admin.register(StaffPayroll)
class StaffPayrollAdmin(admin.ModelAdmin):
    list_display = ['staff', 'grade', 'basic_salary', 'is_active']
    list_filter  = ['is_active']

@admin.register(SalaryPayment)
class SalaryPaymentAdmin(admin.ModelAdmin):
    list_display = ['staff_payroll', 'month', 'year', 'net_salary', 'status', 'paid_at']
    list_filter  = ['status', 'year', 'month']
    search_fields = ['staff_payroll__staff__full_name']

from payroll.models import (LoanRepayment, PayDetailsRequest, PayrollRun, PayrollSettings, Payslip,  # noqa: E402
                            StaffLoan)

for _m in (PayrollSettings, PayrollRun, Payslip, StaffLoan, LoanRepayment, PayDetailsRequest):
    admin.site.register(_m)
