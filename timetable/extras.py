"""
timetable/extras.py
───────────────────
After-school lessons and coaching classes (F156): a weekly time and room, a
teacher, who can join, and a fee for the term that is billed on its own —
paid from the family wallet, paid in cash at the bursary, or waived — so it
never mixes with school fees.
"""
from datetime import datetime
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone

from .lessons import published_timetable
from .models import DAY_CHOICES, ExtraLessonEnrolment, ExtraLessonGroup, TimetableSlot

DAY_NAMES = dict(DAY_CHOICES)


class ExtraError(ValueError):
    pass


def money(raw):
    try:
        value = Decimal(str(raw or '0').replace(',', '').strip()).quantize(Decimal('0.01'))
    except (InvalidOperation, ValueError):
        raise ExtraError('Enter the fee as a number, e.g. 15000.')
    if value < 0:
        raise ExtraError('The fee cannot be negative.')
    return value


def _overlap(a1, a2, b1, b2):
    return a1 < b2 and b1 < a2


def clashes(group):
    """Plain sentences for anything the group's teacher or room is already doing at that time."""
    out = []
    others = ExtraLessonGroup.objects.filter(tenant_id=group.tenant_id, active=True, day=group.day) \
        .exclude(pk=group.pk).select_related('teacher', 'room')
    for g in others:
        if not _overlap(group.start_time, group.end_time, g.start_time, g.end_time):
            continue
        if group.teacher_id and g.teacher_id == group.teacher_id:
            out.append(f'{g.teacher.full_name} also runs {g.name} {g.start_time:%-I:%M %p}–{g.end_time:%-I:%M %p}.')
        if group.room_id and g.room_id == group.room_id:
            out.append(f'{g.room.name} is used by {g.name} {g.start_time:%-I:%M %p}–{g.end_time:%-I:%M %p}.')
    tt = published_timetable(group.tenant)
    if tt and group.teacher_id:
        for e in TimetableSlot.objects.filter(timetable=tt, teacher_id=group.teacher_id, day=group.day) \
                .select_related('time_slot', 'school_class', 'subject'):
            if _overlap(group.start_time, group.end_time, e.time_slot.start_time, e.time_slot.end_time):
                out.append(f'{group.teacher.full_name} teaches {e.school_class.name} {e.subject.name} '
                           f'{e.time_slot.start_time:%-I:%M %p}–{e.time_slot.end_time:%-I:%M %p}.')
    if tt and group.room_id:
        for e in TimetableSlot.objects.filter(timetable=tt, room_id=group.room_id, day=group.day) \
                .select_related('time_slot', 'school_class'):
            if _overlap(group.start_time, group.end_time, e.time_slot.start_time, e.time_slot.end_time):
                out.append(f'{group.room.name} has {e.school_class.name} {e.time_slot.start_time:%-I:%M %p}–'
                           f'{e.time_slot.end_time:%-I:%M %p}.')
    return out


def save_group(group, data):
    """Fill a group from a form (dict-like) and save it. Clashes refuse the save."""
    name = (data.get('name') or '').strip()[:120]
    if not name:
        raise ExtraError('Give the group a name.')
    if data.get('day') not in DAY_NAMES:
        raise ExtraError('Choose a day.')
    try:
        start = datetime.strptime(data.get('start') or '', '%H:%M').time()
        end = datetime.strptime(data.get('end') or '', '%H:%M').time()
    except ValueError:
        raise ExtraError('Give a start and an end time.')
    if start >= end:
        raise ExtraError('The end time must be after the start time.')
    group.name, group.day, group.start_time, group.end_time = name, data.get('day'), start, end
    group.fee = money(data.get('fee'))
    group.teacher_pay = money(data.get('teacher_pay'))
    cap = str(data.get('capacity') or '')
    group.capacity = int(cap) if cap.isdigit() and int(cap) > 0 else None
    group.open_to_parents = bool(data.get('open_to_parents'))
    problems = clashes(group)
    if problems:
        raise ExtraError(' '.join(problems))
    group.save()
    return group


def eligible(group, student):
    if not group.active:
        return False
    ids = set(group.classes.values_list('pk', flat=True))
    return not ids or student.class_assigned_id in ids


def places_left(group):
    if group.capacity is None:
        return None
    return max(0, group.capacity - group.enrolments.count())


@transaction.atomic
def enrol(group, student, user=None):
    if not eligible(group, student):
        raise ExtraError(f'{student.full_name} cannot join {group.name}.')
    if ExtraLessonEnrolment.objects.filter(group=group, student=student).exists():
        raise ExtraError(f'{student.full_name} is already in {group.name}.')
    left = places_left(ExtraLessonGroup.objects.select_for_update().get(pk=group.pk))
    if left == 0:
        raise ExtraError(f'{group.name} is full.')
    return ExtraLessonEnrolment.objects.create(
        tenant_id=group.tenant_id, group=group, student=student, fee=group.fee, enrolled_by=user,
        status=ExtraLessonEnrolment.WAIVED if group.fee == 0 else ExtraLessonEnrolment.DUE)


def family_wallet(student):
    from finance.views import _get_family_wallet_for_student
    return _get_family_wallet_for_student(student)


@transaction.atomic
def pay_from_wallet(enrolment, user=None):
    if enrolment.status != ExtraLessonEnrolment.DUE:
        raise ExtraError('Nothing to pay for this one.')
    wallet = family_wallet(enrolment.student)
    try:
        txn = wallet.debit(enrolment.fee, f'Extra lessons: {enrolment.group.name} ({enrolment.student.full_name})',
                           performed_by=user, category='OTHER')
    except ValueError as problem:
        raise ExtraError(str(problem))
    enrolment.status, enrolment.paid_at, enrolment.transaction = ExtraLessonEnrolment.PAID, timezone.now(), txn
    enrolment.payment_note = 'Paid from the family wallet'
    enrolment.save(update_fields=['status', 'paid_at', 'transaction', 'payment_note'])
    return enrolment


def mark_paid(enrolment, note=''):
    if enrolment.status != ExtraLessonEnrolment.DUE:
        raise ExtraError('This one is not waiting for payment.')
    enrolment.status, enrolment.paid_at = ExtraLessonEnrolment.PAID, timezone.now()
    enrolment.payment_note = (note or 'Paid at the bursary').strip()[:160]
    enrolment.save(update_fields=['status', 'paid_at', 'payment_note'])


def waive(enrolment, note=''):
    if enrolment.status == ExtraLessonEnrolment.PAID:
        raise ExtraError('Already paid — withdraw with a refund instead.')
    enrolment.status = ExtraLessonEnrolment.WAIVED
    enrolment.payment_note = (note or 'Waived').strip()[:160]
    enrolment.save(update_fields=['status', 'payment_note'])


@transaction.atomic
def withdraw(enrolment, user=None, refund=True):
    """Take a child out. A wallet payment goes back to the wallet; a cash payment is left for the bursar."""
    refunded = None
    if refund and enrolment.status == ExtraLessonEnrolment.PAID and enrolment.transaction_id:
        refunded = family_wallet(enrolment.student).credit(
            enrolment.fee, f'Refund: extra lessons {enrolment.group.name} ({enrolment.student.full_name})',
            performed_by=user, category='REFUND')
    enrolment.delete()
    return refunded


def totals(group):
    rows = list(group.enrolments.all())
    out = {'count': len(rows), 'expected': Decimal('0'), 'paid': Decimal('0'), 'due': Decimal('0')}
    for e in rows:
        if e.status == ExtraLessonEnrolment.WAIVED:
            continue
        out['expected'] += e.fee
        out['paid' if e.status == ExtraLessonEnrolment.PAID else 'due'] += e.fee
    return out
