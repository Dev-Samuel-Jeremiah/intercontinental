"""
hostel/services.py — Hostel business logic layer
All mutating operations (assign bed, check in/out, bill, pay) live here.
Views stay thin.
"""

from decimal import Decimal
from django.db import transaction as db_transaction
from django.utils import timezone

from hostel.models import (
    Bed, BoarderProfile, CheckInOut,
    HostelFeeStructure, HostelTermBilling, IncidentReport, VisitorLog,
)
from finance.models import Wallet, _gen_ref


# ─────────────────────────────────────────────────────────────────────────────
# BED ASSIGNMENT
# ─────────────────────────────────────────────────────────────────────────────

def assign_bed(boarder: BoarderProfile, bed: Bed, performed_by=None) -> None:
    """
    Assign a bed to a boarder.
    - Validates bed is AVAILABLE.
    - Frees previous bed if any.
    - Updates bed status to OCCUPIED.
    - Records a CHECK_IN movement.
    """
    if bed.status != 'AVAILABLE':
        raise ValueError(f"Bed {bed.bed_number} is not available "
                         f"(currently: {bed.get_status_display()}).")

    with db_transaction.atomic():
        # Free old bed
        if boarder.bed and boarder.bed != bed:
            old_bed = boarder.bed
            old_bed.status = 'AVAILABLE'
            old_bed.save(update_fields=['status'])

        # Assign new bed
        bed.status = 'OCCUPIED'
        bed.save(update_fields=['status'])

        boarder.bed    = bed
        boarder.status = 'ACTIVE'
        boarder.save(update_fields=['bed', 'status'])

        # Log check-in
        CheckInOut.objects.create(
            boarder=boarder,
            movement_type='CHECK_IN',
            authorized_by=performed_by,
            reason='Bed assignment',
        )

        # Update room status
        _refresh_room_status(bed.room)


def unassign_bed(boarder: BoarderProfile, reason: str = '', performed_by=None) -> None:
    """Remove a boarder from their bed and mark them as checked out."""
    with db_transaction.atomic():
        if boarder.bed:
            bed = boarder.bed
            bed.status = 'AVAILABLE'
            bed.save(update_fields=['status'])
            boarder.bed = None
            _refresh_room_status(bed.room)

        boarder.status = 'CHECKED_OUT'
        boarder.save(update_fields=['bed', 'status'])

        CheckInOut.objects.create(
            boarder=boarder,
            movement_type='CHECK_OUT',
            authorized_by=performed_by,
            reason=reason or 'Manual check-out',
        )


def _refresh_room_status(room) -> None:
    """Update room status based on actual bed occupancy."""
    if room.occupied_count >= room.capacity:
        room.status = 'FULL'
    else:
        room.status = 'AVAILABLE'
    room.save(update_fields=['status'])


# ─────────────────────────────────────────────────────────────────────────────
# EXEAT / LEAVE
# ─────────────────────────────────────────────────────────────────────────────

def grant_exeat(boarder: BoarderProfile, expected_return, reason: str,
                authorized_by=None, parent_consent: bool = False) -> CheckInOut:
    """Record an exeat (boarding leave) for a student."""
    movement = CheckInOut.objects.create(
        boarder=boarder,
        movement_type='EXEAT',
        expected_return=expected_return,
        reason=reason,
        authorized_by=authorized_by,
        parent_consent=parent_consent,
    )
    boarder.status = 'EXEAT'
    boarder.save(update_fields=['status'])
    return movement


def record_return(boarder: BoarderProfile, movement: CheckInOut,
                  performed_by=None) -> None:
    """Record a boarder returning from exeat."""
    movement.actual_return = timezone.now()
    movement.save(update_fields=['actual_return'])
    boarder.status = 'ACTIVE'
    boarder.save(update_fields=['status'])
    CheckInOut.objects.create(
        boarder=boarder,
        movement_type='RETURN',
        authorized_by=performed_by,
        reason=f'Returned from exeat (ref: {movement.id})',
    )


# ─────────────────────────────────────────────────────────────────────────────
# HOSTEL BILLING
# ─────────────────────────────────────────────────────────────────────────────

def generate_hostel_bills(term, session, performed_by=None) -> dict:
    """
    Create HostelTermBilling records for ALL active boarders for the given term.
    Looks up HostelFeeStructure for their hostel (or the default structure).
    Returns a summary dict.
    """
    boarders  = BoarderProfile.objects.filter(
        status__in=['ACTIVE', 'EXEAT'],
        student_type__in=['BOARDER', 'WEEKLY'],
    ).select_related('student', 'bed__room__hostel')

    created = skipped = 0
    for boarder in boarders:
        # Skip if bill already exists
        if HostelTermBilling.objects.filter(
            boarder=boarder, term=term, session=session
        ).exists():
            skipped += 1
            continue

        # Find applicable fee structure
        hostel = boarder.hostel
        fee_qs = HostelFeeStructure.objects.filter(is_active=True)
        if hostel:
            fee = (fee_qs.filter(hostel=hostel, term=term, session=session).first()
                   or fee_qs.filter(hostel=hostel, term=term).first()
                   or fee_qs.filter(hostel=hostel).first()
                   or fee_qs.filter(hostel__isnull=True, term=term, session=session).first()
                   or fee_qs.filter(hostel__isnull=True).first())
        else:
            fee = (fee_qs.filter(hostel__isnull=True, term=term, session=session).first()
                   or fee_qs.filter(hostel__isnull=True).first())

        boarding = fee.boarding_fee if fee else Decimal('0')
        meal     = fee.meal_fee     if fee else Decimal('0')
        laundry  = fee.laundry_fee  if fee else Decimal('0')
        other    = fee.other_fee    if fee else Decimal('0')
        total    = boarding + meal + laundry + other

        bill = HostelTermBilling(
            boarder=boarder, term=term, session=session,
            boarding_fee=boarding, meal_fee=meal,
            laundry_fee=laundry,   other_fee=other,
            total_fee=total,
        )
        bill.save()
        created += 1

    return {'created': created, 'skipped': skipped,
            'total_boarders': boarders.count()}


def pay_hostel_bill(billing: HostelTermBilling, amount: Decimal,
                    performed_by=None) -> None:
    """
    Debit the family wallet for hostel fee and update billing record.
    Integrates with the existing finance wallet system.
    """
    if amount <= 0:
        raise ValueError("Payment amount must be positive.")
    if amount > billing.balance_due:
        raise ValueError(
            f"Payment ₦{amount:,.2f} exceeds balance due ₦{billing.balance_due:,.2f}."
        )

    with db_transaction.atomic():
        student = billing.boarder.student

        # Use family wallet
        if student.parent_email:
            from users.models import Student as St
            oldest = St.objects.filter(
                parent_email=student.parent_email
            ).order_by('id').first() or student
        else:
            oldest = student

        wallet = Wallet.get_or_create_for_student(oldest)

        if wallet.balance < amount:
            raise ValueError(
                f"Insufficient wallet balance. "
                f"Available: ₦{wallet.balance:,.2f}, Required: ₦{amount:,.2f}"
            )

        txn = wallet.debit(
            amount=amount,
            description=(f"Hostel fee: {billing.term} / {billing.session} "
                         f"— {student.full_name}"),
            ref=_gen_ref("HST"),
            performed_by=performed_by,
            category='FEE',
        )

        billing.amount_paid         += amount
        billing.finance_transaction  = txn
        billing.status = (
            'PAID'    if billing.amount_paid >= billing.total_fee else
            'PARTIAL' if billing.amount_paid > 0                  else
            'UNPAID'
        )
        billing.save(update_fields=[
            'amount_paid', 'finance_transaction', 'status', 'updated_at'
        ])


# ─────────────────────────────────────────────────────────────────────────────
# ANALYTICS
# ─────────────────────────────────────────────────────────────────────────────

def get_hostel_summary():
    """Dashboard summary statistics."""
    from django.db.models import Sum, Count, Q
    from hostel.models import Hostel, Bed, BoarderProfile

    total_beds      = Bed.objects.count()
    occupied_beds   = Bed.objects.filter(status='OCCUPIED').count()
    available_beds  = Bed.objects.filter(status='AVAILABLE').count()
    total_boarders  = BoarderProfile.objects.filter(
        student_type__in=['BOARDER', 'WEEKLY'], status='ACTIVE'
    ).count()
    on_exeat        = BoarderProfile.objects.filter(status='EXEAT').count()

    # Overdue exeats
    from hostel.models import CheckInOut
    overdue = CheckInOut.objects.filter(
        movement_type='EXEAT',
        actual_return__isnull=True,
        expected_return__lt=timezone.now(),
    ).count()

    # Billing summary
    from django.db.models import Sum as S
    billing_agg = HostelTermBilling.objects.aggregate(
        total_expected=S('total_fee'),
        total_paid=S('amount_paid'),
    )
    total_expected = billing_agg['total_expected'] or Decimal('0')
    total_paid     = billing_agg['total_paid']     or Decimal('0')

    # Open incidents
    open_incidents = IncidentReport.objects.filter(
        status__in=['OPEN', 'UNDER_REVIEW']
    ).count()

    # Pending visitors
    pending_visitors = VisitorLog.objects.filter(status='PENDING').count()

    return {
        'total_beds':       total_beds,
        'occupied_beds':    occupied_beds,
        'available_beds':   available_beds,
        'occupancy_rate':   round((occupied_beds / total_beds) * 100, 1) if total_beds else 0,
        'total_boarders':   total_boarders,
        'on_exeat':         on_exeat,
        'overdue_exeats':   overdue,
        'total_expected':   total_expected,
        'total_paid':       total_paid,
        'outstanding':      max(Decimal('0'), total_expected - total_paid),
        'open_incidents':   open_incidents,
        'pending_visitors': pending_visitors,
    }
