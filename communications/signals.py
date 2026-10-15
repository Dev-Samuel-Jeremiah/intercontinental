"""
communications/signals.py
==========================
Django signals that auto-trigger email/SMS when key events occur:
  • New Announcement posted  → email/SMS all relevant parents
  • Fee payment approved     → email parent receipt confirmation
  • Hostel bill paid         → email confirmation
  • Result published         → notify parents to check portal

All sends happen synchronously (add Celery for production async).
Each signal is guarded so it never crashes the triggering action.
"""

from users.models import ENROLLED_STATUSES
import logging

from django.db.models.signals import post_save
from django.dispatch import receiver

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# HELPER — safe send that never raises
# ─────────────────────────────────────────────────────────────────────────────

def _safe_email(to, subject, html, student=None):
    try:
        from communications.services import send_email
        ok, ref = send_email(to, subject, html, student)
        if not ok:
            logger.warning(f"[SIGNAL-EMAIL] Failed to {to}: {ref}")
        return ok
    except Exception as e:
        logger.error(f"[SIGNAL-EMAIL] Exception: {e}")
        return False


def _safe_sms(to, body):
    try:
        from communications.services import send_sms
        ok, ref = send_sms(to, body)
        if not ok:
            logger.warning(f"[SIGNAL-SMS] Failed to {to}: {ref}")
        return ok
    except Exception as e:
        logger.error(f"[SIGNAL-SMS] Exception: {e}")
        return False


def _school_name(student=None):
    """The school this message is about — never another school's name."""
    tenant = getattr(student, 'tenant', None)
    if tenant is None:
        from tenants.middleware import get_current_tenant
        tenant = get_current_tenant()
    return getattr(tenant, 'name', '') or 'Your school'


def _html_wrap(body_html: str, student=None) -> str:
    """Wrap content in the school's branded email shell."""
    school = _school_name(student)
    return f"""
<html><body style="font-family:Arial,Helvetica,sans-serif;background:#f8fafc;padding:20px;">
<div style="max-width:600px;margin:0 auto;">
  <div style="background:#064e3b;padding:18px 24px;border-radius:10px 10px 0 0;text-align:center;">
    <h2 style="color:#fff;margin:0;font-size:1.1rem;">{school}</h2>
  </div>
  <div style="background:#fff;padding:24px;border:1px solid #e2e8f0;border-radius:0 0 10px 10px;">
    {body_html}
    <hr style="border:none;border-top:1px solid #e2e8f0;margin:20px 0;">
    <p style="color:#94a3b8;font-size:.8rem;margin:0;">
      {school}<br>
      This is an automated notification. Do not reply to this email.
    </p>
  </div>
</div>
</body></html>
"""


# ─────────────────────────────────────────────────────────────────────────────
# SIGNAL 1 — New Announcement → email parents
# ─────────────────────────────────────────────────────────────────────────────

@receiver(post_save, sender='announcement.Announcement')
def announcement_notify_parents(sender, instance, created, **kwargs):
    """
    When a new Announcement is saved with notify_parents=True,
    email/SMS all relevant parents.
    """
    if not created:
        return
    # Only if the model has a notify_parents field (we add it via migration)
    if not getattr(instance, 'notify_parents', False):
        return

    try:
        from django.db.models import Q
        from users.models import Student

        qs = Student.objects.filter(status__in=ENROLLED_STATUSES).select_related('class_assigned')
        if instance.school_class:
            qs = qs.filter(class_assigned=instance.school_class)
        # audience filter
        if instance.audience == 'students':
            return  # no parent notification for student-only announcements

        channel = getattr(instance, 'notify_channel', 'EMAIL')

        for student in qs:
            parent_name = student.parent_name or 'Parent/Guardian'
            class_name  = str(student.class_assigned or '')

            if channel in ('EMAIL', 'BOTH') and student.parent_email:
                html = _html_wrap(student=student, body_html=f"""
                    <p>Dear {parent_name},</p>
                    <p>A new announcement has been posted for <strong>{student.full_name}</strong>
                    ({class_name}).</p>
                    <div style="background:#f0fdf4;border-left:4px solid #16a34a;
                                padding:14px;border-radius:0 8px 8px 0;margin:16px 0;">
                        <h3 style="margin:0 0 8px;color:#065f46;">{instance.title}</h3>
                        <p style="margin:0;color:#374151;">{instance.message[:500]}
                        {'...' if len(instance.message) > 500 else ''}</p>
                    </div>
                    <p>Please log in to the parent portal for full details.</p>
                    <p>Regards,<br><strong>{_school_name(student)}</strong></p>
                """)
                _safe_email(student.parent_email,
                            f"{_school_name(student)} announcement: {instance.title}", html, student)

            if channel in ('SMS', 'BOTH') and student.parent_phone:
                sms = (f"{_school_name(student)} notice for {student.full_name}: "
                       f"{instance.title}. "
                       f"{instance.message[:100]}{'...' if len(instance.message) > 100 else ''}")
                _safe_sms(student.parent_phone, sms[:800])

    except Exception as e:
        logger.error(f"[SIGNAL] announcement_notify_parents failed: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# SIGNAL 2 — Finance Payment Approved → email receipt
# ─────────────────────────────────────────────────────────────────────────────

@receiver(post_save, sender='finance.Payment')
def payment_approved_notify(sender, instance, **kwargs):
    """Send receipt email/SMS when a payment is approved."""
    if instance.status != 'APPROVED':
        return
    # Only fire once (when approved_at first set)
    if not instance.approved_at:
        return

    try:
        student = instance.wallet.student
        parent_email = student.parent_email
        parent_phone = student.parent_phone
        parent_name  = student.parent_name or 'Parent/Guardian'

        if parent_email:
            html = _html_wrap(student=student, body_html=f"""
                <p>Dear {parent_name},</p>
                <p>We confirm receipt of payment for <strong>{student.full_name}</strong>.</p>
                <div style="background:#f0fdf4;border-left:4px solid #16a34a;
                            padding:14px;border-radius:0 8px 8px 0;margin:16px 0;">
                    <table style="width:100%;font-size:.9rem;border-collapse:collapse;">
                        <tr><td style="color:#64748b;padding:4px 0;">Amount Paid</td>
                            <td style="font-weight:700;color:#16a34a;">₦{instance.amount:,.2f}</td></tr>
                        <tr><td style="color:#64748b;padding:4px 0;">Reference</td>
                            <td style="font-family:monospace;">{instance.reference}</td></tr>
                        <tr><td style="color:#64748b;padding:4px 0;">Method</td>
                            <td>{instance.get_method_display()}</td></tr>
                        <tr><td style="color:#64748b;padding:4px 0;">Date</td>
                            <td>{instance.approved_at.strftime('%d %B %Y %H:%M') if instance.approved_at else '—'}</td></tr>
                    </table>
                </div>
                <p>Thank you for your payment. You can view your receipt on the parent portal.</p>
                <p>Regards,<br><strong>{_school_name(student)} Bursary Office</strong></p>
            """)
            _safe_email(parent_email,
                        f"{_school_name(student)}: payment confirmed — ₦{instance.amount:,.2f}", html, student)

        if parent_phone:
            sms = (f"{_school_name(student)}: Payment of NGN{instance.amount:,.2f} confirmed for "
                   f"{student.full_name}. Ref: {instance.reference}. Thank you.")
            _safe_sms(parent_phone, sms)

    except Exception as e:
        logger.error(f"[SIGNAL] payment_approved_notify failed: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# SIGNAL 3 — Hostel Check-In → notify parent
# ─────────────────────────────────────────────────────────────────────────────

@receiver(post_save, sender='hostel.CheckInOut')
def hostel_movement_notify(sender, instance, created, **kwargs):
    """Notify parent when student checks in, gets exeat, or is overdue."""
    if not created:
        return

    try:
        student      = instance.boarder.student
        parent_email = student.parent_email
        parent_phone = student.parent_phone
        parent_name  = student.parent_name or 'Parent/Guardian'
        mv_type      = instance.movement_type

        if mv_type not in ('CHECK_IN', 'EXEAT', 'CHECK_OUT'):
            return

        subject_map = {
            'CHECK_IN':  f"{_school_name(student)}: {student.full_name} has checked into the hostel",
            'EXEAT':     f"{_school_name(student)}: Exeat granted for {student.full_name}",
            'CHECK_OUT': f"{_school_name(student)}: {student.full_name} has checked out of the hostel",
        }
        body_map = {
            'CHECK_IN': f"""
                <p>Dear {parent_name},</p>
                <p>This is to inform you that <strong>{student.full_name}</strong> 
                has been checked into the hostel.</p>
                <p><strong>Date/Time:</strong> {instance.datetime.strftime('%d %B %Y at %H:%M')}</p>
                <p>If you have any questions, please contact the school office.</p>
            """,
            'EXEAT': f"""
                <p>Dear {parent_name},</p>
                <p><strong>{student.full_name}</strong> has been granted an exeat/leave.</p>
                <p><strong>Date/Time:</strong> {instance.datetime.strftime('%d %B %Y at %H:%M')}</p>
                {'<p><strong>Expected Return:</strong> ' + instance.expected_return.strftime('%d %B %Y at %H:%M') + '</p>' if instance.expected_return else ''}
                <p><strong>Reason:</strong> {instance.reason or 'Not specified'}</p>
                <p>Please ensure your child returns to school on time.</p>
            """,
            'CHECK_OUT': f"""
                <p>Dear {parent_name},</p>
                <p><strong>{student.full_name}</strong> has checked out of the hostel.</p>
                <p><strong>Date/Time:</strong> {instance.datetime.strftime('%d %B %Y at %H:%M')}</p>
            """,
        }
        sms_map = {
            'CHECK_IN':  f"{_school_name(student)}: {student.full_name} has checked into the hostel on {instance.datetime.strftime('%d/%m/%Y %H:%M')}.",
            'EXEAT':     f"{_school_name(student)}: Exeat granted for {student.full_name}. {'Expected return: ' + instance.expected_return.strftime('%d/%m/%Y') if instance.expected_return else ''} Reason: {(instance.reason or 'N/A')[:50]}",
            'CHECK_OUT': f"{_school_name(student)}: {student.full_name} has checked out of the hostel on {instance.datetime.strftime('%d/%m/%Y %H:%M')}.",
        }

        if parent_email and mv_type in subject_map:
            html = _html_wrap(body_map[mv_type], student=student)
            _safe_email(parent_email, subject_map[mv_type], html, student)

        if parent_phone and mv_type in sms_map:
            _safe_sms(parent_phone, sms_map[mv_type][:800])

    except Exception as e:
        logger.error(f"[SIGNAL] hostel_movement_notify failed: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# SIGNAL 4 — TopUp Approved → notify parent
# ─────────────────────────────────────────────────────────────────────────────

@receiver(post_save, sender='finance.TopUpRequest')
def topup_approved_notify(sender, instance, **kwargs):
    """Notify parent when their wallet top-up is approved."""
    if instance.status != 'APPROVED':
        return

    try:
        student      = instance.wallet.student
        parent_email = student.parent_email
        parent_phone = student.parent_phone
        parent_name  = student.parent_name or 'Parent/Guardian'

        if parent_email:
            html = _html_wrap(student=student, body_html=f"""
                <p>Dear {parent_name},</p>
                <p>Your wallet top-up request for <strong>{student.full_name}</strong> 
                has been approved and credited to the wallet.</p>
                <div style="background:#f0fdf4;border-left:4px solid #16a34a;
                            padding:14px;border-radius:0 8px 8px 0;margin:16px 0;">
                    <strong style="color:#16a34a;font-size:1.2rem;">
                        ₦{instance.amount:,.2f} Credited
                    </strong>
                </div>
                <p>Method: {instance.get_method_display()}</p>
                <p>You can now use these funds to pay school fees through the parent portal.</p>
                <p>Regards,<br><strong>{_school_name(student)} Finance Office</strong></p>
            """)
            _safe_email(parent_email,
                        f"{_school_name(student)}: Wallet Top-Up Approved — ₦{instance.amount:,.2f}", html, student)

        if parent_phone:
            sms = (f"{_school_name(student)}: Your wallet top-up of NGN{instance.amount:,.2f} for "
                   f"{student.full_name} has been approved. Check your portal.")
            _safe_sms(parent_phone, sms)

    except Exception as e:
        logger.error(f"[SIGNAL] topup_approved_notify failed: {e}")
