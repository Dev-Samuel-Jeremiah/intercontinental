"""
finance/views_billing.py
────────────────────────
Fee discounts and scholarships, payment plans, chasing debtors, and the public
receipt-verification page.
"""
from users.models import ENROLLED_STATUSES
from datetime import date, timedelta
from decimal import Decimal

from django.contrib import messages
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from academics.utils import get_active_session, get_active_term
from users.models import Class, Student

from .models import (ChildBilling, DebtorContact, FeeDiscount, Payment, PaymentPlan,
                     PaymentPlanInstallment)
from .services.discounts import apply_discounts, discounts_for, sibling_position
from .services.plans import create_plan, due_soon, record_instalment_payment
from .views import _require_finance_staff


# ─────────────────────────────────────────────────────────────────────────────
# Discounts, rebates & scholarships
# ─────────────────────────────────────────────────────────────────────────────

@_require_finance_staff
def discounts(request):
    if request.method == 'POST':
        action = request.POST.get('action')

        if action == 'add':
            try:
                value = Decimal(request.POST.get('value') or '0')
            except Exception:
                value = Decimal('0')
            applies_to = request.POST.get('applies_to') or FeeDiscount.STUDENT
            student_id = request.POST.get('student_id') or None
            class_id = request.POST.get('class_id') or None
            name = (request.POST.get('name') or '').strip()

            if value <= 0 or not name:
                messages.error(request, 'Give the discount a name and an amount above zero.')
            elif applies_to == FeeDiscount.STUDENT and not student_id:
                messages.error(request, 'Choose the student this award is for.')
            elif applies_to == FeeDiscount.CLASS and not class_id:
                messages.error(request, 'Choose the class this applies to.')
            else:
                FeeDiscount.objects.create(
                    tenant=request.tenant, name=name, applies_to=applies_to,
                    value_type=request.POST.get('value_type') or FeeDiscount.PERCENT,
                    value=value,
                    student_id=student_id if applies_to == FeeDiscount.STUDENT else None,
                    school_class_id=class_id if applies_to == FeeDiscount.CLASS else None,
                    sibling_from=int(request.POST.get('sibling_from') or 3),
                    category=request.POST.get('category') or '',
                    reason=(request.POST.get('reason') or '').strip(),
                    approved_by=request.user,
                )
                messages.success(request, f'“{name}” will now be applied automatically.')
            return redirect('finance:discounts')

        if action == 'toggle':
            discount = get_object_or_404(FeeDiscount, pk=request.POST.get('discount_id'))
            discount.is_active = not discount.is_active
            discount.save(update_fields=['is_active'])
            messages.success(request, f'“{discount.name}” is now '
                                      f'{"active" if discount.is_active else "paused"}.')
            return redirect('finance:discounts')

        if action == 'delete':
            discount = get_object_or_404(FeeDiscount, pk=request.POST.get('discount_id'))
            name = discount.name
            discount.delete()
            messages.success(request, f'“{name}” removed.')
            return redirect('finance:discounts')

    rules = FeeDiscount.objects.exclude(applies_to=FeeDiscount.STUDENT)
    awards = FeeDiscount.objects.filter(applies_to=FeeDiscount.STUDENT).select_related('student')
    return render(request, 'finance/discounts.html', {
        'rules': rules, 'awards': awards,
        'classes': Class.objects.all().order_by('name'),
        'students': Student.objects.filter(status__in=ENROLLED_STATUSES).order_by('full_name'),
        'applies_choices': FeeDiscount.APPLIES_CHOICES,
        'value_choices': FeeDiscount.VALUE_CHOICES,
        'categories': [c for c in getattr(Payment, 'CATEGORY_CHOICES', [])] or [],
        'active_count': FeeDiscount.objects.filter(is_active=True).count(),
    })


@_require_finance_staff
def discount_preview(request):
    """Show what a chosen student would pay once discounts are applied."""
    student = None
    result = None
    student_id = request.GET.get('student_id')
    if student_id:
        student = Student.objects.filter(pk=student_id).first()
    if student:
        from .models import FeeStructure
        session, term = get_active_session(), get_active_term()
        fees = FeeStructure.objects.filter(is_active=True).filter(
            Q(school_class=student.class_assigned) | Q(school_class__isnull=True)).filter(
            Q(term=term) | Q(term__isnull=True)).filter(
            Q(session=session) | Q(session__isnull=True))
        gross, off, net, lines = apply_discounts(student, list(fees), term=term, session=session)
        result = {'gross': gross, 'off': off, 'net': net, 'lines': lines,
                  'fees': fees, 'position': sibling_position(student)}
    return render(request, 'finance/discount_preview.html', {
        'students': Student.objects.filter(status__in=ENROLLED_STATUSES).order_by('full_name'),
        'student': student, 'result': result,
    })


# ─────────────────────────────────────────────────────────────────────────────
# Payment plans
# ─────────────────────────────────────────────────────────────────────────────

@_require_finance_staff
def payment_plans(request):
    if request.method == 'POST':
        action = request.POST.get('action')

        if action == 'create':
            student = get_object_or_404(Student, pk=request.POST.get('student_id'))
            try:
                total = Decimal(request.POST.get('total_amount') or '0')
                count = int(request.POST.get('instalments') or 3)
                every = int(request.POST.get('every_days') or 30)
            except Exception:
                messages.error(request, 'Check the amount and number of instalments.')
                return redirect('finance:payment_plans')
            first_due = request.POST.get('first_due') or ''
            try:
                first_due = date.fromisoformat(first_due)
            except ValueError:
                first_due = timezone.now().date() + timedelta(days=7)
            if total <= 0:
                messages.error(request, 'The amount must be more than zero.')
            else:
                create_plan(student, total, count, first_due, every_days=every,
                            session=get_active_session(), term=get_active_term(),
                            created_by=request.user,
                            note=(request.POST.get('note') or '').strip())
                messages.success(request, f'{count} instalments set up for {student.full_name}.')
            return redirect('finance:payment_plans')

        if action == 'record':
            instalment = get_object_or_404(PaymentPlanInstallment,
                                           pk=request.POST.get('instalment_id'))
            try:
                amount = Decimal(request.POST.get('amount') or '0')
            except Exception:
                amount = Decimal('0')
            if amount <= 0:
                messages.error(request, 'Enter the amount received.')
            else:
                record_instalment_payment(instalment.plan, amount)
                messages.success(request, f'₦{amount:,.0f} recorded against the plan.')
            return redirect('finance:payment_plans')

        if action == 'cancel':
            plan = get_object_or_404(PaymentPlan, pk=request.POST.get('plan_id'))
            plan.status = PaymentPlan.CANCELLED
            plan.save(update_fields=['status'])
            messages.success(request, 'Plan cancelled.')
            return redirect('finance:payment_plans')

    plans = (PaymentPlan.objects.select_related('student')
             .prefetch_related('installments').order_by('-created_at'))
    upcoming = due_soon(request.tenant, within_days=7)
    return render(request, 'finance/payment_plans.html', {
        'plans': plans,
        'upcoming': upcoming,
        'overdue': [i for i in upcoming if i.is_overdue],
        'students': Student.objects.filter(status__in=ENROLLED_STATUSES).order_by('full_name'),
        'default_due': (timezone.now().date() + timedelta(days=7)).isoformat(),
    })


@_require_finance_staff
def send_plan_reminders(request):
    from lms_project.tasks import run_in_background

    from .services.plans import send_instalment_reminders
    count = len(due_soon(request.tenant, within_days=7))
    run_in_background(send_instalment_reminders, request.tenant, 7, tenant=request.tenant)
    messages.success(request, f'Reminders are being sent for {count} instalment'
                              f'{"" if count == 1 else "s"}. Results appear in Message Logs.')
    return redirect('finance:payment_plans')


# ─────────────────────────────────────────────────────────────────────────────
# Debtors & follow-up
# ─────────────────────────────────────────────────────────────────────────────

@_require_finance_staff
def debtors(request):
    """Who owes money, and what happened the last time we called."""
    if request.method == 'POST':
        student = get_object_or_404(Student, pk=request.POST.get('student_id'))
        promise_date = request.POST.get('promise_date') or None
        try:
            promise_date = date.fromisoformat(promise_date) if promise_date else None
        except ValueError:
            promise_date = None
        amount = request.POST.get('amount_promised') or None
        DebtorContact.objects.create(
            tenant=request.tenant, student=student,
            method=request.POST.get('method') or DebtorContact.CALL,
            outcome=request.POST.get('outcome') or DebtorContact.PROMISED,
            promise_date=promise_date,
            amount_promised=Decimal(amount) if amount else None,
            note=(request.POST.get('note') or '').strip(),
            contacted_by=request.user,
        )
        messages.success(request, f'Follow-up saved for {student.full_name}.')
        return redirect('finance:debtors')

    session, term = get_active_session(), get_active_term()
    billings = (ChildBilling.objects
                .select_related('student', 'student__class_assigned', 'parent_billing')
                .filter(parent_billing__session=session, parent_billing__term=term))

    class_id = request.GET.get('class_id') or ''
    search = (request.GET.get('q') or '').strip()
    if class_id:
        billings = billings.filter(student__class_assigned_id=class_id)
    if search:
        billings = billings.filter(Q(student__full_name__icontains=search)
                                   | Q(student__admission_number__icontains=search)
                                   | Q(student__parent_phone__icontains=search))

    last_contacts = {}
    for contact in DebtorContact.objects.select_related('student', 'contacted_by'):
        last_contacts.setdefault(contact.student_id, contact)

    rows = []
    for billing in billings:
        if billing.balance <= 0:
            continue
        rows.append({'billing': billing, 'student': billing.student,
                     'last': last_contacts.get(billing.student_id)})
    rows.sort(key=lambda r: r['billing'].balance, reverse=True)

    promised_today = [r for r in rows
                      if r['last'] and r['last'].promise_date
                      and r['last'].promise_date <= timezone.now().date()
                      and r['last'].outcome == DebtorContact.PROMISED]

    return render(request, 'finance/debtors.html', {
        'rows': rows,
        'total_owed': sum((r['billing'].balance for r in rows), Decimal('0.00')),
        'promised_today': promised_today,
        'never_contacted': [r for r in rows if not r['last']],
        'classes': Class.objects.all().order_by('name'),
        'class_id': class_id, 'search': search,
        'methods': DebtorContact.METHOD_CHOICES,
        'outcomes': DebtorContact.OUTCOME_CHOICES,
        'term': term, 'session': session,
    })


@_require_finance_staff
def debtor_history(request, student_id):
    student = get_object_or_404(Student, pk=student_id)
    return render(request, 'finance/debtor_history.html', {
        'student': student,
        'contacts': DebtorContact.objects.filter(student=student).select_related('contacted_by'),
    })


# ─────────────────────────────────────────────────────────────────────────────
# Public receipt verification
# ─────────────────────────────────────────────────────────────────────────────

def verify_receipt(request, reference):
    """Anyone with the receipt (or its QR) can check that it is genuine."""
    payment = (Payment.objects.select_related('wallet__student')
               .filter(reference=reference, status=Payment.APPROVED).first())
    return render(request, 'finance/verify_receipt.html', {
        'payment': payment, 'reference': reference,
    }, status=200 if payment else 404)
