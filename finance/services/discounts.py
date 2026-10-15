"""
finance/services/discounts.py
─────────────────────────────
Works out what a student should actually pay after sibling rebates, staff
discounts, class-wide reductions and scholarships.

One place decides this, so the bursar's report, the parent's wallet and the
invoice can never disagree.
"""
from users.models import ENROLLED_STATUSES
from decimal import Decimal

from django.db.models import Q

from finance.models import FeeDiscount


def sibling_position(student):
    """Where this child comes in the family (1 = oldest), by guardian contact.

    Returns 1 when we can't tell, so a sibling rule never fires by accident.
    """
    from users.models import Student

    email = (student.parent_email or '').strip()
    phone = ''.join(ch for ch in (student.parent_phone or '') if ch.isdigit())[-9:]
    if not email and not phone:
        return 1

    criteria = Q()
    if email:
        criteria |= Q(parent_email__iexact=email)
    if phone:
        criteria |= Q(parent_phone__endswith=phone)

    siblings = list(Student.objects.filter(criteria)
                    .filter(tenant=student.tenant, status__in=ENROLLED_STATUSES)
                    .order_by('date_of_birth', 'id')
                    .values_list('id', flat=True))
    if student.id in siblings:
        return siblings.index(student.id) + 1
    return 1


def _is_staff_child(student):
    """A child whose guardian email or phone matches a staff member's."""
    from users.models import Staff

    email = (student.parent_email or '').strip()
    if not email:
        return False
    return Staff.objects.filter(tenant=student.tenant,
                                user__email__iexact=email).exists()


def discounts_for(student, term=None, session=None):
    """Every active discount that applies to this student right now."""
    applicable = []
    candidates = FeeDiscount.objects.filter(tenant=student.tenant, is_active=True)
    position = None

    for discount in candidates:
        if discount.session_id and session and discount.session_id != getattr(session, 'id', None):
            continue
        if discount.term_id and term and discount.term_id != getattr(term, 'id', None):
            continue

        kind = discount.applies_to
        if kind == FeeDiscount.STUDENT:
            if discount.student_id != student.id:
                continue
        elif kind == FeeDiscount.CLASS:
            if discount.school_class_id != student.class_assigned_id:
                continue
        elif kind == FeeDiscount.SIBLING:
            if position is None:
                position = sibling_position(student)
            if position < (discount.sibling_from or 1):
                continue
        elif kind == FeeDiscount.STAFF_CHILD:
            if not _is_staff_child(student):
                continue
        # ALL applies to everybody
        applicable.append(discount)
    return applicable


def apply_discounts(student, fees, term=None, session=None):
    """Return (gross, discount_total, net, lines) for this student's fees.

    `fees` is the list of FeeStructure rows that make up the bill. Percentage
    discounts come first, then fixed amounts, and the bill never goes below zero.
    """
    gross = sum((Decimal(str(f.amount)) for f in fees), Decimal('0.00'))
    if gross <= 0:
        return Decimal('0.00'), Decimal('0.00'), Decimal('0.00'), []

    applicable = discounts_for(student, term=term, session=session)
    lines = []
    total_off = Decimal('0.00')

    for discount in sorted(applicable, key=lambda d: 0 if d.value_type == FeeDiscount.PERCENT else 1):
        # A discount limited to one fee category only applies to those fees.
        if discount.category:
            base = sum((Decimal(str(f.amount)) for f in fees
                        if f.category == discount.category), Decimal('0.00'))
        else:
            base = gross
        remaining = max(Decimal('0.00'), min(base, gross - total_off))
        off = discount.amount_off(remaining)
        if off <= 0:
            continue
        total_off += off
        lines.append({'discount': discount, 'amount': off,
                      'label': f'{discount.name} ({discount.label})'})
        if total_off >= gross:
            break

    total_off = min(total_off, gross)
    return gross, total_off, gross - total_off, lines
