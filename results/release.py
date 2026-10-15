"""
results/release.py
──────────────────
Everything between "scores entered" and "family reads the report card":
subject-sheet review and class approval, the fees check, result PINs,
telling families, and verifying a printed report card.
"""
import secrets
from decimal import Decimal

from django.db import transaction
from django.db.models import Count
from django.utils import timezone

from users.models import ClassSubject

from .models import ClassApproval, PinBatch, ResultPin, ResultSheetSettings, SubjectSheet, TermResult

VERIFY_SALT = 'report-card'


class ReleaseError(ValueError):
    pass


def settings_for(tenant):
    return ResultSheetSettings.objects.filter(tenant=tenant).first() or ResultSheetSettings(tenant=tenant)


# ─────────────────────────────────────────────────────────────────────────────
# Review and approval
# ─────────────────────────────────────────────────────────────────────────────

def sheet_for(school_class, subject, term):
    sheet, _ = SubjectSheet.objects.get_or_create(tenant_id=school_class.tenant_id, school_class=school_class,
                                                  subject=subject, term=term)
    return sheet


def may_review_subject(user, subject):
    from curriculum.services import may_review
    return may_review(user, subject)


def may_edit_scores(user, school_class, subject, term):
    """(allowed, reason) under the approval workflow."""
    if not settings_for(school_class.tenant).require_approval:
        return True, ''
    sheet = SubjectSheet.objects.filter(tenant_id=school_class.tenant_id, school_class=school_class,
                                        subject=subject, term=term).first()
    if ClassApproval.objects.filter(tenant_id=school_class.tenant_id, school_class=school_class, term=term).exists():
        return False, f'{school_class.name} results for {term.name} are approved. An administrator must withdraw the approval first.'
    if not sheet or sheet.status in (SubjectSheet.ENTERED, SubjectSheet.RETURNED):
        return True, ''
    if sheet.status == SubjectSheet.SUBMITTED and may_review_subject(user, subject):
        return True, ''
    if sheet.status == SubjectSheet.SUBMITTED:
        return False, f'{subject.name} for {school_class.name} is waiting for review. It can be changed if it is sent back.'
    return False, f'{subject.name} for {school_class.name} is approved. Ask the reviewer to send it back to change it.'


def submit_sheet(sheet, user):
    if sheet.status not in (SubjectSheet.ENTERED, SubjectSheet.RETURNED):
        raise ReleaseError('This sheet has already been submitted.')
    rows = TermResult.objects.filter(tenant_id=sheet.tenant_id, class_assigned=sheet.school_class,
                                     subject=sheet.subject, term=sheet.term)
    if not rows.exists():
        raise ReleaseError('Enter the scores before submitting them.')
    empty = rows.filter(is_not_offering=False, total_score=0).count()
    if empty:
        raise ReleaseError(f'{empty} student{"s have" if empty != 1 else " has"} no score. Enter a score or mark them '
                           'as not offering first.')
    sheet.status, sheet.submitted_by, sheet.submitted_at, sheet.comment = SubjectSheet.SUBMITTED, user, timezone.now(), ''
    sheet.save()
    return sheet


def review_sheet(sheet, user, approve, comment=''):
    if not may_review_subject(user, sheet.subject):
        raise ReleaseError("Only the subject's head of department or an administrator can review it.")
    if approve and sheet.status != SubjectSheet.SUBMITTED:
        raise ReleaseError('Only a submitted sheet can be approved.')
    if not approve and sheet.status not in (SubjectSheet.SUBMITTED, SubjectSheet.APPROVED):
        raise ReleaseError('This sheet is not with a reviewer.')
    if not approve and not (comment or '').strip():
        raise ReleaseError('Say what needs fixing when you send a sheet back.')
    if not approve and ClassApproval.objects.filter(tenant_id=sheet.tenant_id, school_class=sheet.school_class,
                                                    term=sheet.term).exists():
        raise ReleaseError('The class is already approved — withdraw the class approval first.')
    sheet.status = SubjectSheet.APPROVED if approve else SubjectSheet.RETURNED
    sheet.reviewed_by, sheet.reviewed_at, sheet.comment = user, timezone.now(), (comment or '').strip()[:255]
    sheet.save()
    return sheet


def class_status(school_class, term):
    """[{subject, sheet, rows}] for every subject the class takes, plus the class approval if any."""
    sheets = {s.subject_id: s for s in SubjectSheet.objects.filter(tenant_id=school_class.tenant_id,
                                                                   school_class=school_class, term=term)}
    counts = dict(TermResult.objects.filter(tenant_id=school_class.tenant_id, class_assigned=school_class, term=term)
                  .values('subject_id').annotate(n=Count('id')).values_list('subject_id', 'n'))
    subjects = [cs.subject for cs in ClassSubject.objects.filter(tenant_id=school_class.tenant_id,
                                                                 school_class=school_class)
                .select_related('subject').order_by('subject__name')]
    rows = [{'subject': s, 'sheet': sheets.get(s.id), 'rows': counts.get(s.id, 0)} for s in subjects]
    approval = ClassApproval.objects.filter(tenant_id=school_class.tenant_id, school_class=school_class,
                                            term=term).select_related('approved_by').first()
    return rows, approval


def approve_class(school_class, term, user, comment=''):
    from users.views_profile import can_manage
    if not can_manage(user):
        raise ReleaseError('Only an administrator (the principal) approves a class for publishing.')
    rows, approval = class_status(school_class, term)
    if approval:
        raise ReleaseError('This class is already approved.')
    waiting = [r['subject'].name for r in rows if r['rows'] and
               (not r['sheet'] or r['sheet'].status != SubjectSheet.APPROVED)]
    if waiting and not (comment or '').strip():
        raise ReleaseError(f"Not reviewed yet: {', '.join(waiting)}. Approve anyway only with a comment explaining why.")
    return ClassApproval.objects.create(tenant_id=school_class.tenant_id, school_class=school_class, term=term,
                                        approved_by=user, comment=(comment or '').strip()[:255])


def withdraw_approval(school_class, term):
    ClassApproval.objects.filter(tenant_id=school_class.tenant_id, school_class=school_class, term=term).delete()


def publishable_class_ids(tenant, term):
    """Classes whose results may be published; None means any (no approval workflow)."""
    if not settings_for(tenant).require_approval:
        return None
    return set(ClassApproval.objects.filter(tenant=tenant, term=term).values_list('school_class_id', flat=True))


# ─────────────────────────────────────────────────────────────────────────────
# Fees
# ─────────────────────────────────────────────────────────────────────────────

def fee_status(student, term):
    """(due, paid, percent paid) for the term, from the family's bills. percent is None with no bill."""
    from finance.models import ChildBilling
    rows = ChildBilling.objects.filter(tenant_id=student.tenant_id, student=student, parent_billing__term=term)
    due = sum((b.amount_due for b in rows), Decimal('0'))
    paid = sum((b.paid for b in rows), Decimal('0'))
    if not due:
        return due, paid, None
    return due, paid, min(100, int(paid * 100 / due))


def fees_cleared(student, term, cfg=None):
    cfg = cfg or settings_for(student.tenant)
    if not cfg.fee_gate:
        return True
    _, _, pct = fee_status(student, term)
    return pct is None or pct >= cfg.fee_gate_percent


# ─────────────────────────────────────────────────────────────────────────────
# PINs
# ─────────────────────────────────────────────────────────────────────────────

def _code(n=12):
    return ''.join(secrets.choice('0123456789') for _ in range(n))


@transaction.atomic
def generate_pins(tenant, name, count, price=0, max_uses=5, user=None):
    if not 1 <= int(count) <= 2000:
        raise ReleaseError('Generate between 1 and 2,000 PINs at a time.')
    if not 1 <= int(max_uses) <= 50:
        raise ReleaseError('A PIN can be used between 1 and 50 times.')
    batch = PinBatch.objects.create(tenant=tenant, name=(name or '').strip()[:80] or 'Result PINs',
                                    price=Decimal(str(price or 0)), created_by=user)
    taken = set(ResultPin.objects.filter(tenant=tenant).values_list('pin', flat=True))
    pins = []
    for i in range(int(count)):
        code = _code()
        while code in taken:
            code = _code()
        taken.add(code)
        pins.append(ResultPin(tenant=tenant, batch=batch, serial=f'{batch.id:04d}-{i + 1:05d}', pin=code,
                              max_uses=int(max_uses)))
    ResultPin.objects.bulk_create(pins)
    return batch


def _session_key(student, term):
    return f'{student.id}:{term.id}'


def pin_ok_in_session(request, student, term):
    return _session_key(student, term) in request.session.get('result_pins', [])


@transaction.atomic
def redeem(request, raw, student, term):
    """Check a PIN and open the report card for this browser session. Each new session uses up one view."""
    code = ''.join(ch for ch in (raw or '') if ch.isdigit())
    pin = ResultPin.objects.select_for_update().filter(tenant_id=student.tenant_id, pin=code).first()
    if not pin or pin.disabled:
        raise ReleaseError("That PIN isn't valid. Check the number on the card.")
    if pin.student_id and (pin.student_id != student.id or pin.term_id != term.id):
        raise ReleaseError('That PIN has already been used for another student or term.')
    if pin.uses >= pin.max_uses:
        raise ReleaseError(f'That PIN has been used {pin.max_uses} times — buy a new one from the school.')
    if not pin.student_id:
        pin.student, pin.term, pin.first_used_at = student, term, timezone.now()
    pin.uses += 1
    pin.save()
    keys = request.session.get('result_pins', [])
    keys.append(_session_key(student, term))
    request.session['result_pins'] = keys[-20:]
    return pin


def access(request, student, term):
    """
    None if this person may see the report card now, otherwise
    {'reason': 'fees'|'pin', ...} explaining what's needed.
    """
    from users.views_profile import can_view
    if can_view(request.user):
        return None
    cfg = settings_for(student.tenant)
    if not fees_cleared(student, term, cfg):
        due, paid, pct = fee_status(student, term)
        return {'reason': 'fees', 'due': due, 'paid': paid, 'percent': pct, 'needed': cfg.fee_gate_percent,
                'outstanding': max(Decimal('0'), due * cfg.fee_gate_percent / 100 - paid)}
    if cfg.require_pin and not pin_ok_in_session(request, student, term):
        pin = ResultPin.objects.filter(tenant_id=student.tenant_id, student=student, term=term).first()
        return {'reason': 'pin', 'pin': pin}
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Telling families
# ─────────────────────────────────────────────────────────────────────────────

def notify_published(tenant, student_ids, term_id, portal_url, actor=None):
    from lms_project.tasks import run_in_background
    from notifications.events import results_published
    results_published(tenant, student_ids, term_id, actor=actor)      # in-app, whatever the text/email setting
    if student_ids and settings_for(tenant).notify_on_publish:
        run_in_background(_send_published, list(student_ids), term_id, portal_url, tenant=tenant)


def _send_published(student_ids, term_id, portal_url):
    from academics.models import Term
    from communications.services import _is_opted_out, send_email, send_sms
    from users.models import Student
    from users.records import contacts
    term = Term.objects.filter(pk=term_id).select_related('session').first()
    for st in Student.objects.filter(pk__in=student_ids).select_related('tenant'):
        text = (f"{st.tenant.name}: {st.full_name}'s {term.name} {term.session.name} result is ready. "
                f"View it in the parent portal: {portal_url}")
        if not _is_opted_out(st, 'SMS'):
            for _name, phone in contacts(st, 'SMS'):
                send_sms(phone, text)
        if not _is_opted_out(st, 'EMAIL'):
            for _name, email in contacts(st, 'EMAIL'):
                send_email(email, f"{st.full_name}'s {term.name} result is ready",
                           f"<p>{st.full_name}'s {term.name} {term.session.name} result from {st.tenant.name} "
                           f'is ready.</p><p><a href="{portal_url}">Open the parent portal</a> to view it.</p>', st)


# ─────────────────────────────────────────────────────────────────────────────
# Verifying a printed report card
# ─────────────────────────────────────────────────────────────────────────────

def reference(student, term):
    from django.core import signing
    return signing.dumps({'s': student.id, 't': str(student.tenant_id), 'r': term.id}, salt=VERIFY_SALT, compress=True)


def verify(ref, tenant):
    """The published scores behind a report card, or None if the reference is forged or from another school."""
    from django.core import signing

    from academics.models import Term
    from users.models import Student
    try:
        data = signing.loads(ref, salt=VERIFY_SALT)
    except signing.BadSignature:
        return None
    if data.get('t') != str(tenant.id):
        return None
    student = Student.objects.filter(tenant=tenant, pk=data.get('s')).select_related('class_assigned').first()
    term = Term.objects.filter(tenant=tenant, pk=data.get('r')).select_related('session').first()
    if not student or not term:
        return None
    rows = list(TermResult.objects.filter(tenant=tenant, student=student, term=term, published=True,
                                          is_not_offering=False).select_related('subject').order_by('subject__name'))
    if not rows:
        return None
    from .analysis import bands, grade_for
    band_list = bands(tenant)
    for r in rows:
        g = grade_for(r.total_score, band_list)
        r.grade = g.grade if g else ''
    avg = round(sum(r.total_score for r in rows) / len(rows), 2)
    return {'student': student, 'term': term, 'rows': rows, 'average': avg}
