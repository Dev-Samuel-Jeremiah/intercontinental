"""
safety/services.py
──────────────────
The rules at the gate.

A child is released only to someone on their approved list (the main
contact, or a guardian marked "may collect"), to someone holding a valid
one-time pickup code issued by the parent, or — when neither applies — on
an administrator's override with a written reason. Every release is logged
and the family is told. Too many wrong codes for one child lock code
pickups for the day, so a code cannot be guessed at the gate.
"""
import secrets
from datetime import datetime, time, timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from safety.models import (LostCardReport, PickupPass, PickupRelease, RollCall, RollCallEntry,
                           SafetySettings)
from users.models import ENROLLED_STATUSES, Attendance, Guardian, Staff, Student

MAX_WRONG_CODES = 5


class SafetyError(ValueError):
    pass


def settings_for(tenant):
    obj, _ = SafetySettings.objects.get_or_create(tenant=tenant)
    return obj


# ─────────────────────────────────────────────────────────────────────────────
# Who may collect
# ─────────────────────────────────────────────────────────────────────────────

def collectors(student):
    """Everyone connected to the child, approved or not, for the gate screen."""
    out = []
    if student.parent_name or student.parent_phone:
        out.append({'kind': 'main', 'id': 'main', 'name': student.parent_name or 'Main contact',
                    'relationship': 'Main contact', 'phone': student.parent_phone, 'photo': None,
                    'allowed': True, 'note': ''})
    for g in Guardian.objects.filter(tenant_id=student.tenant_id, student=student):
        out.append({'kind': 'guardian', 'id': g.id, 'name': g.full_name,
                    'relationship': g.get_relationship_display(), 'phone': g.phone,
                    'photo': g.photo, 'allowed': g.can_pick_up, 'note': g.note})
    return out


def _today_passes(student):
    today = timezone.localdate()
    return PickupPass.objects.filter(tenant_id=student.tenant_id, student=student, valid_on=today)


def code_pickups_locked(student):
    return sum(_today_passes(student).values_list('failed_attempts', flat=True)) >= MAX_WRONG_CODES


# ─────────────────────────────────────────────────────────────────────────────
# Pickup codes
# ─────────────────────────────────────────────────────────────────────────────

def issue_pass(student, collector_name, *, collector_phone='', relationship='', reason='',
               valid_on=None, user=None):
    name = (collector_name or '').strip()
    if len(name) < 3:
        raise SafetyError("Enter the full name of the person collecting.")
    today = timezone.localdate()
    valid_on = valid_on or today
    if valid_on < today or valid_on > today + timedelta(days=7):
        raise SafetyError('A pickup code can be for today or up to a week ahead.')
    if _today_passes(student).filter(status=PickupPass.ISSUED).count() >= 3 and valid_on == today:
        raise SafetyError('There are already three pickup codes waiting for today.')
    cfg = settings_for(student.tenant)
    end_of_day = timezone.make_aware(datetime.combine(valid_on, time(23, 59)))
    start = max(timezone.now(), timezone.make_aware(datetime.combine(valid_on, time(0, 0))))
    expires = min(end_of_day, start + timedelta(hours=cfg.pass_valid_hours))
    p = PickupPass.objects.create(
        tenant_id=student.tenant_id, student=student, collector_name=name[:120],
        collector_phone=(collector_phone or '').strip()[:20],
        relationship=(relationship or '').strip()[:60], reason=(reason or '').strip()[:200],
        code=f'{secrets.randbelow(1_000_000):06d}', valid_on=valid_on, expires_at=expires,
        issued_by=user)
    if p.collector_phone:
        from lms_project.tasks import run_in_background
        run_in_background(_sms_code, p.pk, tenant=student.tenant)
    return p


def _sms_code(pass_id):
    from communications.services import send_sms
    p = PickupPass.objects.select_related('student', 'student__tenant').get(pk=pass_id)
    send_sms(p.collector_phone,
             f"{p.student.tenant.name}: your code to collect {p.student.full_name} is {p.code}, "
             f"valid {p.valid_on:%d %b} until {timezone.localtime(p.expires_at):%H:%M}. "
             f"Bring ID and show this code at the gate.")


def cancel_pass(p):
    if p.status == PickupPass.ISSUED:
        p.status = PickupPass.CANCELLED
        p.save(update_fields=['status'])
    return p


def check_code(student, code):
    """The pass this code unlocks for this child today, or SafetyError."""
    code = (code or '').strip()
    if code_pickups_locked(student):
        raise SafetyError('Too many wrong codes for this child today. Release only with the '
                          "head's approval.")
    candidates = [p for p in _today_passes(student).filter(status=PickupPass.ISSUED)]
    for p in candidates:
        if secrets.compare_digest(p.code, code):
            if timezone.now() > p.expires_at:
                raise SafetyError('That pickup code has expired.')
            return p
    # Count the miss against today's passes so guessing is limited.
    target = candidates[0] if candidates else _today_passes(student).first()
    if target:
        PickupPass.objects.filter(pk=target.pk).update(failed_attempts=target.failed_attempts + 1)
    raise SafetyError('That code does not match any pickup code for this child today.')


# ─────────────────────────────────────────────────────────────────────────────
# Release
# ─────────────────────────────────────────────────────────────────────────────

def is_early(tenant, moment=None):
    cfg = settings_for(tenant)
    local = timezone.localtime(moment or timezone.now())
    return local.time() < cfg.school_ends_at


@transaction.atomic
def release(student, user, *, collector='', code='', override_reason='', other_name='',
            relationship='', reason='', can_override=False):
    """
    Hand a child over. `collector` is 'main', a guardian id, or 'other'
    (with a code or an override). Returns the PickupRelease.
    """
    if student.status not in ENROLLED_STATUSES:
        raise SafetyError(f'{student.full_name} is not on the roll.')
    guardian, pass_obj, checked = None, None, None
    if collector == 'main':
        if not (student.parent_name or student.parent_phone):
            raise SafetyError('There is no main contact on record.')
        name, rel, checked = student.parent_name or 'Main contact', 'Main contact', PickupRelease.LIST
    elif collector and collector.isdigit():
        guardian = Guardian.objects.filter(tenant_id=student.tenant_id, student=student,
                                           pk=int(collector)).first()
        if not guardian:
            raise SafetyError('That person is not linked to this child.')
        if not guardian.can_pick_up:
            if not (can_override and override_reason.strip()):
                raise SafetyError(f'{guardian.full_name} is NOT allowed to collect '
                                  f'{student.full_name}.' + (f' Note: {guardian.note}' if guardian.note else ''))
            checked = PickupRelease.OVERRIDE
        else:
            checked = PickupRelease.LIST
        name, rel = guardian.full_name, guardian.get_relationship_display()
    else:
        if code:
            pass_obj = check_code(student, code)
            name, rel, checked = pass_obj.collector_name, pass_obj.relationship, PickupRelease.PASS
            reason = reason or pass_obj.reason
        elif can_override and override_reason.strip() and other_name.strip():
            name, rel, checked = other_name.strip()[:120], relationship[:60], PickupRelease.OVERRIDE
        else:
            raise SafetyError('Someone not on the approved list needs a pickup code from the parent, '
                              "or the head's approval with a reason.")
    if checked == PickupRelease.OVERRIDE and not can_override:
        raise SafetyError('Only an administrator can approve this release.')

    now = timezone.now()
    early = is_early(student.tenant, now)
    rel_obj = PickupRelease.objects.create(
        tenant_id=student.tenant_id, student=student, collector_name=name, relationship=rel,
        guardian=guardian, pickup_pass=pass_obj, checked_by=checked,
        override_reason=override_reason.strip()[:255] if checked == PickupRelease.OVERRIDE else '',
        early=early, reason=(reason or '').strip()[:200], released_at=now, released_by=user)
    if pass_obj:
        pass_obj.status = PickupPass.USED
        pass_obj.save(update_fields=['status'])
    if early:
        note = f'Left {timezone.localtime(now):%H:%M} with {name}' + (f' — {reason}' if reason else '')
        mark = Attendance.objects.filter(tenant_id=student.tenant_id, student=student,
                                         date=timezone.localdate(), subject__isnull=True).first()
        if mark:
            mark.remarks = ((mark.remarks or '') + ' ' + note).strip()[:500]
            mark.save(update_fields=['remarks'])
    if settings_for(student.tenant).notify_parent_on_pickup:
        from lms_project.tasks import run_in_background
        run_in_background(_notify_pickup, rel_obj.pk, tenant=student.tenant)
    return rel_obj


def _notify_pickup(release_id):
    from communications.services import send_sms
    from users.records import contacts
    r = PickupRelease.objects.select_related('student', 'student__tenant').get(pk=release_id)
    when = timezone.localtime(r.released_at)
    text = (f"{r.student.tenant.name}: {r.student.full_name} was collected by {r.collector_name}"
            f" at {when:%H:%M}" + (' (early' + (f': {r.reason}' if r.reason else '') + ')' if r.early else '')
            + '. If this is unexpected, call the school now.')
    sent = False
    for _name, phone in contacts(r.student, 'SMS'):
        ok, _ = send_sms(phone, text)
        sent = sent or ok
    if sent:
        PickupRelease.objects.filter(pk=r.pk).update(parent_notified=True)


# ─────────────────────────────────────────────────────────────────────────────
# Emergency roll call
# ─────────────────────────────────────────────────────────────────────────────

def start_roll_call(tenant, title, kind, assembly_point='', user=None):
    if RollCall.objects.filter(tenant=tenant, ended_at__isnull=True).exists():
        raise SafetyError('A roll call is already running. End it before starting another.')
    return RollCall.objects.create(tenant=tenant, title=(title or 'Roll call').strip()[:120],
                                   kind=kind if kind in ('DRILL', 'REAL') else 'DRILL',
                                   assembly_point=(assembly_point or '').strip()[:120], started_by=user)


def expected(roll):
    """Students on the roll who are in school today (not marked absent), and all staff."""
    tenant_id = roll.tenant_id
    day = timezone.localtime(roll.started_at).date()
    absent = set(Attendance.objects.filter(tenant_id=tenant_id, date=day, status='A',
                                           subject__isnull=True).values_list('student_id', flat=True))
    left = set(PickupRelease.objects.filter(tenant_id=tenant_id, released_at__date=day,
                                            released_at__lte=roll.started_at)
               .values_list('student_id', flat=True))
    students = (Student.objects.filter(tenant_id=tenant_id, status__in=ENROLLED_STATUSES)
                .exclude(pk__in=absent | left).select_related('class_assigned').order_by(
                    'class_assigned__name', 'full_name'))
    staff = Staff.objects.filter(tenant_id=tenant_id).order_by('full_name')
    return list(students), list(staff), len(absent), len(left)


def mark(roll, user, *, student=None, staff=None, status=RollCallEntry.SAFE, note=''):
    if not roll.is_open:
        raise SafetyError('This roll call has ended.')
    if status not in dict(RollCallEntry.STATUSES):
        raise SafetyError('Choose safe, hurt or missing.')
    if (student is None) == (staff is None):
        raise SafetyError('Mark one person at a time.')
    key = {'student': student} if student else {'staff': staff}
    entry, _ = RollCallEntry.objects.update_or_create(
        tenant_id=roll.tenant_id, roll_call=roll, **key,
        defaults={'status': status, 'note': (note or '')[:200], 'marked_by': user,
                  'marked_at': timezone.now()})
    return entry


def roll_status(roll):
    students, staff, absent_today, left_early = expected(roll)
    entries = {(e.student_id, e.staff_id): e for e in roll.entries.select_related('marked_by')}
    def state(student=None, member=None):
        e = entries.get((student.id if student else None, member.id if member else None))
        return e.status if e else 'UNACCOUNTED'
    people = ([{'type': 'student', 'id': s.id, 'name': s.full_name,
                'group': getattr(s.class_assigned, 'name', 'No class'), 'status': state(student=s)}
               for s in students] +
              [{'type': 'staff', 'id': m.id, 'name': m.full_name, 'group': 'Staff',
                'status': state(member=m)} for m in staff])
    counts = {'SAFE': 0, 'INJURED': 0, 'MISSING': 0, 'UNACCOUNTED': 0}
    for p in people:
        counts[p['status']] += 1
    return {'people': people, 'counts': counts, 'total': len(people),
            'absent_today': absent_today, 'left_early': left_early,
            'elapsed': int((((roll.ended_at or timezone.now()) - roll.started_at).total_seconds()))}


def end_roll_call(roll, note=''):
    if roll.ended_at:
        return roll
    roll.ended_at = timezone.now()
    roll.note = (note or '').strip()
    roll.save(update_fields=['ended_at', 'note'])
    return roll


# ─────────────────────────────────────────────────────────────────────────────
# Lost ID cards
# ─────────────────────────────────────────────────────────────────────────────

@transaction.atomic
def report_lost(card, user=None, note=''):
    if card.loss_reports.filter(status=LostCardReport.REPORTED).exists():
        raise SafetyError('This card has already been reported lost.')
    card.is_active = False
    card.save(update_fields=['is_active'])
    return LostCardReport.objects.create(tenant_id=card.tenant_id, card=card, reported_by=user,
                                         note=(note or '').strip()[:200],
                                         fee=settings_for(card.tenant).card_replacement_fee)


@transaction.atomic
def reissue(report, user, fee_mode):
    """New QR (the lost card stops working for good), card back on, fee settled or recorded."""
    if report.status == LostCardReport.REISSUED:
        raise SafetyError('A new card was already issued for this report.')
    if fee_mode not in ('WALLET', 'CASH', 'OWED', 'WAIVED'):
        raise SafetyError('Choose how the replacement fee is handled.')
    card = report.card
    if fee_mode == 'WALLET' and report.fee:
        if card.card_type != 'STUDENT' or not card.student:
            raise SafetyError('Only a student card can be charged to a family wallet.')
        from finance.services.shop import family_wallet
        from finance.models import Wallet
        wallet = Wallet.objects.select_for_update().get(pk=family_wallet(card.student).pk)
        if wallet.balance < report.fee:
            raise SafetyError(f'The family wallet has NGN {wallet.balance:,.2f}; the fee is '
                              f'NGN {report.fee:,.2f}. Choose another way to settle it.')
        wallet.debit(report.fee, f'ID card replacement — {card.student.full_name}',
                     performed_by=user, category='OTHER')
    card.reissue()
    card.is_active = True
    card.save(update_fields=['is_active'])
    report.status = LostCardReport.REISSUED
    report.fee_status = 'NONE' if not report.fee else fee_mode
    report.reissued_by = user
    report.reissued_at = timezone.now()
    report.save(update_fields=['status', 'fee_status', 'reissued_by', 'reissued_at'])
    return report
