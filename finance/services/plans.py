"""
finance/services/plans.py
─────────────────────────
Payment plans: a family agrees to clear a term's fees in instalments, and the
school can remind them before each date.
"""
from datetime import timedelta
from decimal import Decimal, ROUND_DOWN

from django.utils import timezone

from finance.models import PaymentPlan, PaymentPlanInstallment


def create_plan(student, total_amount, instalments, first_due, every_days=30,
                session=None, term=None, created_by=None, note=''):
    """Split an amount into equal instalments (the first one absorbs the rounding)."""
    total_amount = Decimal(str(total_amount))
    instalments = max(1, int(instalments))

    plan = PaymentPlan.objects.create(
        tenant=student.tenant, student=student, session=session, term=term,
        total_amount=total_amount, created_by=created_by, note=note)

    each = (total_amount / instalments).quantize(Decimal('0.01'), rounding=ROUND_DOWN)
    amounts = [each] * instalments
    amounts[0] += total_amount - sum(amounts)   # keep the total exact

    due = first_due
    for amount in amounts:
        PaymentPlanInstallment.objects.create(
            tenant=student.tenant, plan=plan, due_date=due, amount=amount)
        due = due + timedelta(days=every_days)
    return plan


def record_instalment_payment(plan, amount, when=None):
    """Apply money to the earliest unpaid instalments, oldest first."""
    amount = Decimal(str(amount))
    when = when or timezone.now()
    for instalment in plan.installments.order_by('due_date'):
        if amount <= 0:
            break
        owing = instalment.balance
        if owing <= 0:
            continue
        paying = min(owing, amount)
        instalment.amount_paid += paying
        if instalment.is_paid:
            instalment.paid_at = when
        instalment.save(update_fields=['amount_paid', 'paid_at'])
        amount -= paying

    if plan.balance <= 0:
        plan.status = PaymentPlan.COMPLETED
        plan.save(update_fields=['status'])
    return plan


def due_soon(tenant, within_days=3, include_overdue=True):
    """Instalments that need a reminder: due within N days, or already late."""
    today = timezone.now().date()
    instalments = (PaymentPlanInstallment.objects
                   .filter(tenant=tenant, plan__status=PaymentPlan.ACTIVE)
                   .select_related('plan', 'plan__student')
                   .order_by('due_date'))
    horizon = today + timedelta(days=within_days)
    result = []
    for instalment in instalments:
        if instalment.is_paid:
            continue
        if instalment.due_date <= horizon and (include_overdue or instalment.due_date >= today):
            result.append(instalment)
    return result


def send_instalment_reminders(tenant, within_days=3):
    """Remind parents about instalments falling due. Returns how many were sent."""
    from communications.services import send_email, send_sms

    sent = 0
    school = getattr(tenant, 'name', 'Your school')
    for instalment in due_soon(tenant, within_days=within_days):
        student = instalment.plan.student
        when = instalment.due_date.strftime('%d %b %Y')
        late = instalment.is_overdue
        message = (f"{school}: {'overdue' if late else 'upcoming'} instalment of "
                   f"₦{instalment.balance:,.0f} for {student.full_name}, due {when}. "
                   f"Thank you.")
        if student.parent_email:
            send_email(student.parent_email,
                       f"{school}: fee instalment due {when}",
                       f"<p>Dear {student.parent_name or 'Parent/Guardian'},</p>"
                       f"<p>{message}</p><p>Regards,<br><strong>{school}</strong></p>",
                       student)
        if student.parent_phone:
            send_sms(student.parent_phone, message)
        instalment.reminder_sent_at = timezone.now()
        instalment.save(update_fields=['reminder_sent_at'])
        sent += 1
    return sent
