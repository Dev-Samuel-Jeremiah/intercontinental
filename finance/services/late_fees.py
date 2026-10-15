"""
finance/services/late_fees.py
─────────────────────────────
Penalties for paying school fees late.

A rule names the due date for a term and what to charge afterwards. Running
`apply_rule` walks the families that still owe money and raises the charges
they have earned — one for a rule that charges once, one per week or per
month for a rule that keeps charging. Running it twice on the same day
changes nothing, so it is safe on a schedule or on a button.

Nothing is charged automatically behind the school's back: a rule only bites
when the school creates it and runs it.
"""
from datetime import date
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from finance.models import ChildBilling, LateFeeCharge, LateFeeRule


def _today():
    return timezone.now().date()


def periods_elapsed(rule, as_of=None):
    """How many charges this rule has earned by `as_of` — 0 while still in time."""
    as_of = as_of or _today()
    start = rule.starts_on
    if as_of < start:
        return 0
    if rule.repeat == LateFeeRule.ONCE:
        return 1
    days_late = (as_of - start).days
    step = 7 if rule.repeat == LateFeeRule.WEEKLY else 30
    return 1 + days_late // step


def charges_for(child_billing, rule=None):
    """The penalties standing against a bill, ignoring waived ones."""
    charges = LateFeeCharge.objects.filter(child_billing=child_billing, waived=False)
    if rule:
        charges = charges.filter(rule=rule)
    return charges


def total_charged(child_billing, rule=None):
    return charges_for(child_billing, rule).aggregate(
        total=Sum('amount'))['total'] or Decimal('0.00')


def refresh_late_fee(child_billing):
    """
    Re-add the standing penalties onto the bill after anything changes.

    The total is always written, never compared against the copy in hand —
    that copy may have been loaded before another charge was raised, and
    skipping the write would leave the bill showing a penalty the family no
    longer owes.
    """
    total = total_charged(child_billing)
    child_billing.late_fee = total
    child_billing.save(update_fields=['late_fee'])
    child_billing.parent_billing.refresh_from_children()
    return total


def preview_rule(rule, as_of=None):
    """Who would be charged and how much, without writing anything."""
    as_of = as_of or _today()
    rows = []
    for billing, due, amount, period in _candidates(rule, as_of):
        rows.append({'child_billing': billing, 'student': billing.student,
                     'balance': due, 'amount': amount, 'period': period})
    return rows


def _candidates(rule, as_of):
    """Yield (billing, outstanding, amount to charge now, period number)."""
    periods = periods_elapsed(rule, as_of)
    if periods <= 0 or not rule.is_active:
        return

    billings = (ChildBilling.objects
                .filter(parent_billing__session=rule.session,
                        parent_billing__term=rule.term)
                .select_related('student', 'parent_billing'))

    for billing in billings:
        outstanding = billing.total_fee - billing.paid
        if outstanding <= Decimal('0.00') or outstanding < rule.minimum_balance:
            continue

        already = LateFeeCharge.objects.filter(rule=rule, child_billing=billing)
        charged_periods = set(already.values_list('period', flat=True))
        standing = already.filter(waived=False).aggregate(
            total=Sum('amount'))['total'] or Decimal('0.00')

        for period in range(1, periods + 1):
            if period in charged_periods:
                continue
            amount = rule.charge_for(outstanding)
            if rule.max_total is not None:
                room = rule.max_total - standing
                if room <= Decimal('0.00'):
                    break
                amount = min(amount, room)
            if amount <= Decimal('0.00'):
                break
            standing += amount
            yield billing, outstanding, amount, period


@transaction.atomic
def apply_rule(rule, as_of=None):
    """Raise every charge this rule has earned. Returns the new charges."""
    as_of = as_of or _today()
    raised, touched = [], set()
    for billing, outstanding, amount, period in _candidates(rule, as_of):
        charge, created = LateFeeCharge.objects.get_or_create(
            tenant=rule.tenant, rule=rule, child_billing=billing, period=period,
            defaults={'amount': amount, 'balance_then': outstanding, 'charged_on': as_of})
        if created:
            raised.append(charge)
            touched.add(billing.id)

    for billing in ChildBilling.objects.filter(id__in=touched):
        refresh_late_fee(billing)
    return raised


def apply_all(tenant, as_of=None, session=None, term=None):
    """Run every active rule for the school. Returns the charges raised."""
    rules = LateFeeRule.objects.filter(tenant=tenant, is_active=True)
    if session:
        rules = rules.filter(session=session)
    if term:
        rules = rules.filter(term=term)
    raised = []
    for rule in rules:
        raised += apply_rule(rule, as_of)
    return raised


@transaction.atomic
def waive(charge, user=None, reason=''):
    """Forgive a penalty — the family stops owing it, but the record stays."""
    charge.waived = True
    charge.waived_by = user
    charge.waived_reason = reason
    charge.waived_at = timezone.now()
    charge.save(update_fields=['waived', 'waived_by', 'waived_reason', 'waived_at'])
    refresh_late_fee(charge.child_billing)
    return charge


@transaction.atomic
def restore(charge):
    """Undo a waiver — the penalty stands again."""
    charge.waived = False
    charge.waived_by = None
    charge.waived_reason = ''
    charge.waived_at = None
    charge.save(update_fields=['waived', 'waived_by', 'waived_reason', 'waived_at'])
    refresh_late_fee(charge.child_billing)
    return charge
