"""payroll/views.py"""
import uuid
from decimal import Decimal, InvalidOperation
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST
from users.models import Staff
from payroll.models import SalaryGrade, StaffPayroll, SalaryPayment
from finance.models import AuditLog


def _is_payroll_staff(user):
    if user.is_superuser: return True
    try: return user.staff.role in ('ADMIN','ACCOUNT')
    except Exception: return False


@login_required
def payroll_dashboard(request):
    if not _is_payroll_staff(request.user):
        return HttpResponse("Access denied.", status=403)

    payrolls = StaffPayroll.objects.filter(is_active=True).select_related('staff','grade')
    grades   = SalaryGrade.objects.all()
    recent   = SalaryPayment.objects.select_related('staff_payroll__staff').all()[:20]

    from django.db.models import Sum
    total_monthly = payrolls.aggregate(s=Sum('basic_salary'))['s'] or Decimal('0')
    paid_this_month = SalaryPayment.objects.filter(
        year=timezone.now().year, month=timezone.now().month, status='PAID'
    ).aggregate(s=Sum('net_salary'))['s'] or Decimal('0')

    return render(request, 'payroll/dashboard.html', dict(
        payrolls=payrolls, grades=grades, recent=recent,
        total_monthly=total_monthly, paid_this_month=paid_this_month,
    ))


@login_required
@require_POST
def add_staff_payroll(request):
    if not _is_payroll_staff(request.user):
        return HttpResponse("Access denied.", status=403)
    staff_id = request.POST.get('staff_id')
    grade_id = request.POST.get('grade_id') or None
    try:
        basic = Decimal(str(request.POST.get('basic_salary','0')))
    except InvalidOperation:
        messages.error(request,"Invalid salary."); return redirect("payroll:dashboard")
    staff = get_object_or_404(Staff, id=staff_id)
    pr, created = StaffPayroll.objects.get_or_create(staff=staff)
    pr.basic_salary = basic
    pr.grade_id     = grade_id
    pr.bank_name    = request.POST.get('bank_name','').strip()
    pr.account_no   = request.POST.get('account_no','').strip()
    pr.account_name = request.POST.get('account_name','').strip()
    pr.is_active    = True
    pr.save()
    messages.success(request, f"Payroll {'created' if created else 'updated'} for {staff.full_name}.")
    return redirect("payroll:dashboard")


@login_required
@require_POST
def process_salary(request, payroll_id):
    if not _is_payroll_staff(request.user):
        return HttpResponse("Access denied.", status=403)
    pr = get_object_or_404(StaffPayroll, id=payroll_id, is_active=True)
    try:
        month      = int(request.POST.get('month', timezone.now().month))
        year       = int(request.POST.get('year',  timezone.now().year))
        deductions = Decimal(str(request.POST.get('deductions','0')))
    except (ValueError, InvalidOperation):
        messages.error(request,"Invalid data."); return redirect("payroll:dashboard")

    if SalaryPayment.objects.filter(staff_payroll=pr, month=month, year=year).exists():
        messages.warning(request, f"Salary for {pr.staff.full_name} ({month}/{year}) already recorded.")
        return redirect("payroll:dashboard")

    gross = pr.basic_salary
    net   = max(Decimal('0'), gross - deductions)
    ref   = "SAL" + uuid.uuid4().hex[:8].upper()
    sp = SalaryPayment.objects.create(
        staff_payroll=pr, month=month, year=year,
        gross_salary=gross, deductions=deductions, net_salary=net,
        method=request.POST.get('method','BANK_TRANSFER'),
        reference=ref, status='PAID',
        note=request.POST.get('note','').strip(),
        paid_by=request.user, paid_at=timezone.now(),
    )
    AuditLog.objects.create(
        performed_by=request.user, action='SALARY_PAY',
        target_model='SalaryPayment', target_id=sp.id,
        description=f"Salary ₦{net:,.2f} paid to {pr.staff.full_name} ({month}/{year})",
    )
    messages.success(request, f"Salary ₦{net:,.2f} recorded for {pr.staff.full_name}.")
    return redirect("payroll:dashboard")


@login_required
def staff_payslip(request, payroll_id, month, year):
    if not _is_payroll_staff(request.user):
        return HttpResponse("Access denied.", status=403)
    sp = get_object_or_404(SalaryPayment, staff_payroll_id=payroll_id, month=month, year=year)
    return render(request, 'payroll/payslip.html', {'sp': sp})
