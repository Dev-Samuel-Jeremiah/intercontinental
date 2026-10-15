"""
finance/services/refunds.py
───────────────────────────
Money going back to a family: an overpayment, a withdrawal, a cancelled trip.

A refund is requested, approved, then paid, so the person asking for it is
never the only person who signs it off. Only a refund that has actually been
paid comes off the school's income — a request sitting in the queue does not
quietly change the books.

A refund paid as "credited to the wallet" is not cash leaving the school: it
tops the family's wallet up instead, and is still recorded as income given
back so the statement stays honest.
"""
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from finance.models import Refund, Wallet


def request_refund(tenant, student, amount, reason=Refund.OVERPAYMENT, *,
                   payment=None, detail='', method=Refund.TRANSFER, user=None,
                   session=None, term=None, bank_name='', account_number='',
                   account_name=''):
    """Open a refund request. Nothing moves until it is approved and paid."""
    amount = Decimal(str(amount))
    if amount <= Decimal('0.00'):
        raise ValueError('A refund must be more than zero.')
    return Refund.objects.create(
        tenant=tenant, student=student, amount=amount, reason=reason, payment=payment,
        detail=detail, method=method, requested_by=user, session=session, term=term,
        bank_name=bank_name, account_number=account_number, account_name=account_name)


@transaction.atomic
def approve(refund, user=None):
    if refund.status not in (Refund.REQUESTED,):
        raise ValueError('Only a refund still awaiting approval can be approved.')
    refund.status = Refund.APPROVED
    refund.approved_by = user
    refund.approved_at = timezone.now()
    refund.save(update_fields=['status', 'approved_by', 'approved_at'])
    return refund


@transaction.atomic
def reject(refund, user=None, detail=''):
    if refund.status == Refund.PAID:
        raise ValueError('A refund that has been paid cannot be rejected.')
    refund.status = Refund.REJECTED
    refund.approved_by = user
    refund.approved_at = timezone.now()
    if detail:
        refund.detail = detail
    refund.save(update_fields=['status', 'approved_by', 'approved_at', 'detail'])
    return refund


@transaction.atomic
def mark_paid(refund, user=None, reference='', paid_on=None):
    """Record that the money went back. A wallet refund tops the wallet up."""
    if refund.status != Refund.APPROVED:
        raise ValueError('A refund must be approved before it is paid.')
    refund.status = Refund.PAID
    refund.paid_on = paid_on or timezone.now().date()
    if reference:
        refund.reference = reference
    refund.save(update_fields=['status', 'paid_on', 'reference'])

    if refund.method == Refund.WALLET:
        wallet = Wallet.get_or_create_for_student(refund.student)
        wallet.credit(refund.amount,
                      f'Refund — {refund.get_reason_display().lower()}',
                      performed_by=user, category='REFUND')
    return refund


def refunds_total(tenant, start=None, end=None):
    """Refunds actually paid in the period."""
    paid = Refund.objects.filter(tenant=tenant, status=Refund.PAID)
    if start:
        paid = paid.filter(paid_on__gte=start)
    if end:
        paid = paid.filter(paid_on__lte=end)
    return (paid.aggregate(total=Sum('amount'))['total']
            or Decimal('0.00')).quantize(Decimal('0.01'))


def awaiting_total(tenant):
    """Refunds asked for but not yet paid — what the school may still owe out."""
    pending = Refund.objects.filter(tenant=tenant,
                                    status__in=[Refund.REQUESTED, Refund.APPROVED])
    return (pending.aggregate(total=Sum('amount'))['total']
            or Decimal('0.00')).quantize(Decimal('0.01'))


def overpaid_students(tenant, session=None, term=None):
    """Families that have paid more than their bill — refunds waiting to happen."""
    from finance.models import ChildBilling
    billings = ChildBilling.objects.filter(tenant=tenant).select_related(
        'student', 'parent_billing')
    if session:
        billings = billings.filter(parent_billing__session=session)
    if term:
        billings = billings.filter(parent_billing__term=term)
    rows = []
    for billing in billings:
        over = billing.paid - billing.amount_due
        if over > Decimal('0.00'):
            rows.append({'student': billing.student, 'child_billing': billing,
                         'over': over.quantize(Decimal('0.01'))})
    return sorted(rows, key=lambda r: r['over'], reverse=True)
