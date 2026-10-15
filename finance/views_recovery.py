"""
finance/views_recovery.py
─────────────────────────
Two pages for the bursar: penalties for families that pay late, and refunds
for money the school has to give back.
"""
from datetime import date
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from academics.utils import get_active_session, get_active_term
from users.models import Student

from .models import ChildBilling, LateFeeCharge, LateFeeRule, Payment, Refund
from .services import late_fees, refunds as refund_service
from .views import _require_finance_staff


def _amount(raw):
    try:
        value = Decimal(str(raw or '0'))
    except (InvalidOperation, ValueError):
        return None
    return value if value > 0 else None


def _a_date(raw, fallback=None):
    try:
        return date.fromisoformat(raw) if raw else fallback
    except ValueError:
        return fallback


# ─────────────────────────────────────────────────────────────────────────────
# Late payment penalties
# ─────────────────────────────────────────────────────────────────────────────

@_require_finance_staff
def late_fee_rules(request):
    session, term = get_active_session(), get_active_term()

    if request.method == 'POST':
        action = request.POST.get('action')

        if action == 'add':
            due = _a_date(request.POST.get('due_date'))
            value = _amount(request.POST.get('value'))
            if not session or not term:
                messages.error(request, 'Set the current session and term first.')
            elif not due:
                messages.error(request, 'Choose the date fees become late.')
            elif value is None:
                messages.error(request, 'Enter what to charge, above zero.')
            else:
                name = (request.POST.get('name') or '').strip() or 'Late payment charge'
                if LateFeeRule.objects.filter(session=session, term=term,
                                              name__iexact=name).exists():
                    messages.error(request, f'There is already a rule called "{name}" '
                                            f'for this term.')
                else:
                    LateFeeRule.objects.create(
                        tenant=request.tenant, name=name, session=session, term=term,
                        due_date=due,
                        grace_days=int(request.POST.get('grace_days') or 0),
                        charge_type=request.POST.get('charge_type') or LateFeeRule.AMOUNT,
                        value=value,
                        repeat=request.POST.get('repeat') or LateFeeRule.ONCE,
                        max_total=_amount(request.POST.get('max_total')),
                        minimum_balance=_amount(
                            request.POST.get('minimum_balance')) or Decimal('0.00'),
                        note=(request.POST.get('note') or '').strip())
                    messages.success(request, f'"{name}" saved. Nothing is charged until '
                                              f'you run it.')
            return redirect('finance:late_fees')

        if action == 'toggle':
            rule = get_object_or_404(LateFeeRule, pk=request.POST.get('rule_id'))
            rule.is_active = not rule.is_active
            rule.save(update_fields=['is_active'])
            messages.success(request,
                             f'"{rule.name}" {"switched on" if rule.is_active else "paused"}.')
            return redirect('finance:late_fees')

        if action == 'run':
            rule = get_object_or_404(LateFeeRule, pk=request.POST.get('rule_id'))
            raised = late_fees.apply_rule(rule)
            if raised:
                total = sum(c.amount for c in raised)
                messages.success(request, f'{len(raised)} charge(s) raised, '
                                          f'NGN {total:,.2f} in all.')
            else:
                messages.info(request, 'Nothing to charge — no family is late under '
                                       'this rule yet.')
            return redirect('finance:late_fees')

        if action == 'waive':
            charge = get_object_or_404(LateFeeCharge, pk=request.POST.get('charge_id'))
            late_fees.waive(charge, request.user,
                            (request.POST.get('reason') or '').strip())
            messages.success(request, f'Penalty waived for '
                                      f'{charge.child_billing.student.full_name}.')
            return redirect('finance:late_fees')

        if action == 'restore':
            charge = get_object_or_404(LateFeeCharge, pk=request.POST.get('charge_id'))
            late_fees.restore(charge)
            messages.success(request, 'Penalty restored.')
            return redirect('finance:late_fees')

        if action == 'delete':
            rule = get_object_or_404(LateFeeRule, pk=request.POST.get('rule_id'))
            billings = list(ChildBilling.objects.filter(
                late_fees__rule=rule).distinct())
            rule.delete()
            for billing in billings:
                late_fees.refresh_late_fee(billing)
            messages.success(request, 'Rule removed, along with the charges it raised.')
            return redirect('finance:late_fees')

    rules = LateFeeRule.objects.select_related('session', 'term')
    preview_rule = None
    preview = []
    rule_id = request.GET.get('preview')
    if rule_id:
        preview_rule = LateFeeRule.objects.filter(pk=rule_id).first()
        if preview_rule:
            preview = late_fees.preview_rule(preview_rule)

    charges = (LateFeeCharge.objects.select_related(
        'rule', 'child_billing__student', 'waived_by').order_by('-charged_on', '-id')[:100])

    standing = sum((c.amount for c in charges if not c.waived), Decimal('0.00'))
    waived = sum((c.amount for c in charges if c.waived), Decimal('0.00'))

    return render(request, 'finance/late_fees.html', {
        'rules': rules, 'charges': charges, 'session': session, 'term': term,
        'preview': preview, 'preview_rule': preview_rule,
        'standing': standing, 'waived': waived,
        'charge_types': LateFeeRule.CHARGE_CHOICES,
        'repeats': LateFeeRule.REPEAT_CHOICES,
        'today': timezone.now().date(),
    })


# ─────────────────────────────────────────────────────────────────────────────
# Refunds
# ─────────────────────────────────────────────────────────────────────────────

@_require_finance_staff
def refunds(request):
    session, term = get_active_session(), get_active_term()

    if request.method == 'POST':
        action = request.POST.get('action')

        if action == 'add':
            amount = _amount(request.POST.get('amount'))
            student = Student.objects.filter(pk=request.POST.get('student_id')).first()
            if not student:
                messages.error(request, 'Choose the student the money goes back for.')
            elif amount is None:
                messages.error(request, 'Enter how much to refund, above zero.')
            else:
                payment = Payment.objects.filter(pk=request.POST.get('payment_id')).first()
                refund_service.request_refund(
                    request.tenant, student, amount,
                    reason=request.POST.get('reason') or Refund.OVERPAYMENT,
                    payment=payment,
                    detail=(request.POST.get('detail') or '').strip(),
                    method=request.POST.get('method') or Refund.TRANSFER,
                    user=request.user, session=session, term=term,
                    bank_name=(request.POST.get('bank_name') or '').strip(),
                    account_number=(request.POST.get('account_number') or '').strip(),
                    account_name=(request.POST.get('account_name') or '').strip())
                messages.success(request, f'Refund of NGN {amount:,.2f} for '
                                          f'{student.full_name} is awaiting approval.')
            return redirect('finance:refunds')

        refund = get_object_or_404(Refund, pk=request.POST.get('refund_id'))
        try:
            if action == 'approve':
                refund_service.approve(refund, request.user)
                messages.success(request, 'Refund approved. Mark it paid once the money '
                                          'has gone back.')
            elif action == 'reject':
                refund_service.reject(refund, request.user,
                                      (request.POST.get('detail') or '').strip())
                messages.success(request, 'Refund rejected.')
            elif action == 'mark_paid':
                refund_service.mark_paid(
                    refund, request.user,
                    reference=(request.POST.get('reference') or '').strip())
                messages.success(request, f'Refund of NGN {refund.amount:,.2f} recorded '
                                          f'as paid.')
        except ValueError as problem:
            messages.error(request, str(problem))
        return redirect('finance:refunds')

    status = request.GET.get('status') or ''
    query = (request.GET.get('q') or '').strip()
    rows = Refund.objects.select_related('student', 'requested_by', 'approved_by')
    if status:
        rows = rows.filter(status=status)
    if query:
        rows = rows.filter(Q(student__full_name__icontains=query) |
                           Q(reference__icontains=query) |
                           Q(detail__icontains=query))

    return render(request, 'finance/refunds.html', {
        'refunds': rows[:200], 'status': status, 'q': query,
        'session': session, 'term': term,
        'paid_total': refund_service.refunds_total(request.tenant),
        'awaiting': refund_service.awaiting_total(request.tenant),
        'overpaid': refund_service.overpaid_students(request.tenant, session, term),
        'reasons': Refund.REASON_CHOICES, 'methods': Refund.METHOD_CHOICES,
        'statuses': Refund.STATUS_CHOICES,
        'students': Student.objects.all().only('id', 'full_name')[:2000],
    })
