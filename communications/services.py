"""
communications/services.py — Email + SMS Service Layer
============================================================
Providers supported:
  Email : Django SMTP (already configured — Gmail App Password)
  SMS   : Termii (primary — Nigerian provider, very popular)
          Africa's Talking (fallback)
          Console backend (development — prints to terminal)

All sends are logged to MessageLog.
"""

from users.models import ENROLLED_STATUSES
from users.records import contacts
import json
import logging
import re
from datetime import datetime
from decimal import Decimal
from typing import Optional

import requests
from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template import Context, Template
from django.utils import timezone
from django.utils.html import strip_tags

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Placeholder rendering
# ─────────────────────────────────────────────────────────────────────────────

def _get_school_name():
    """Get current tenant school name for use in emails/SMS."""
    from tenants.middleware import get_current_tenant
    tenant = get_current_tenant()
    return tenant.name if tenant else 'School Portal'


def _render_body(body: str, context: dict) -> str:
    """Render {{placeholder}} style variables in message body."""
    for key, value in context.items():
        body = body.replace('{{' + key + '}}', str(value or ''))
        body = body.replace('{{ ' + key + ' }}', str(value or ''))
    return body


def _build_context(student) -> dict:
    """Build standard context dict for a student/parent."""
    from finance.models import FeePayment, FeeStructure
    from django.db.models import Sum, Q

    # Outstanding fees
    class_assigned = student.class_assigned
    paid_ids = FeePayment.objects.filter(
        student=student, status__in=['PAID', 'WAIVED']
    ).values_list('fee_structure_id', flat=True)
    unpaid = FeeStructure.objects.filter(is_active=True).filter(
        Q(school_class=class_assigned) | Q(school_class__isnull=True)
    ).exclude(id__in=paid_ids).aggregate(s=Sum('amount'))['s'] or Decimal('0')

    # Wallet balance
    try:
        from finance.models import Wallet
        wallet = Wallet.objects.filter(student=student).first()
        wallet_balance = wallet.balance if wallet else Decimal('0')
    except Exception:
        wallet_balance = Decimal('0')

    return {
        'student_name':  student.full_name,
        'parent_name':   student.parent_name or 'Parent/Guardian',
        'admission_no':  student.admission_number,
        'class_name':    str(student.class_assigned or ''),
        'school_name':   (student.tenant.name
                          if student.tenant_id
                          else '{{school_name}}'),
        'school_phone':  getattr(settings, 'SCHOOL_PHONE', ''),
        'outstanding_fee': f'₦{unpaid:,.2f}',
        'wallet_balance':  f'₦{wallet_balance:,.2f}',
    }


def _normalise_phone(phone: str) -> str:
    """Normalise Nigerian phone number to +234 international format."""
    if not phone:
        return ''
    phone = re.sub(r'\D', '', phone)   # digits only
    if phone.startswith('234'):
        phone = '+' + phone
    elif phone.startswith('0') and len(phone) == 11:
        phone = '+234' + phone[1:]
    elif len(phone) == 10:
        phone = '+234' + phone
    else:
        phone = '+' + phone
    return phone


# ─────────────────────────────────────────────────────────────────────────────
# EMAIL  (Django SMTP — already configured)
# ─────────────────────────────────────────────────────────────────────────────

def send_email(to_email: str, subject: str, html_body: str,
               student=None) -> tuple[bool, str]:
    """
    Send a single HTML email via Django SMTP backend.
    Returns (success: bool, provider_ref_or_error: str).

    NOTE: If you see "Temporary failure in name resolution", it means the server
    cannot reach smtp.gmail.com (no internet access or DNS blocked).
    Solutions:
      1. Check server firewall — port 587 must be open to smtp.gmail.com
      2. Or switch to EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'
         in settings.py to log emails to terminal instead of sending (for development)
    """
    if not to_email or '@' not in to_email:
        return False, f"Invalid email address: {to_email}"

    # The same news on WhatsApp, for families who read WhatsApp rather than email
    from tenants.middleware import get_current_tenant
    from communications import connect
    connect.copy_email_to_whatsapp(get_current_tenant(), to_email, subject, html_body, student)

    try:
        from_email = getattr(settings, 'DEFAULT_FROM_EMAIL',
                             f'{_get_school_name()} <noreply@example.com>')
        text_body  = strip_tags(html_body)

        msg = EmailMultiAlternatives(
            subject=subject,
            body=text_body,
            from_email=from_email,
            to=[to_email],
        )
        msg.attach_alternative(html_body, "text/html")
        msg.send(fail_silently=False)
        ref = f"smtp-{timezone.now().strftime('%Y%m%d%H%M%S')}"
        logger.info(f"[EMAIL OK] → {to_email} | {subject}")
        return True, ref

    except OSError as e:
        # Network/DNS errors — give a clear message
        if "Name or service not known" in str(e) or "Temporary failure" in str(e) or "Network" in str(e):
            msg_err = (
                "EMAIL_NETWORK_ERROR: Server cannot reach smtp.gmail.com. "
                "Check that port 587 is open on this server's firewall. "
                f"Detail: {e}"
            )
        else:
            msg_err = str(e)
        logger.error(f"[EMAIL FAIL] → {to_email}: {msg_err}")
        return False, msg_err

    except Exception as e:
        logger.error(f"[EMAIL FAIL] → {to_email}: {e}")
        return False, str(e)


def test_email_connection() -> dict:
    """
    Test the SMTP connection without sending an email.
    Returns {'ok': bool, 'detail': str}
    Used by the admin email test view.
    """
    try:
        from django.core.mail import get_connection
        conn = get_connection()
        conn.open()
        conn.close()
        return {'ok': True, 'detail': 'SMTP connection successful.'}
    except Exception as e:
        return {'ok': False, 'detail': str(e)}


# ─────────────────────────────────────────────────────────────────────────────
# SMS  (Termii — primary Nigerian SMS provider)
# ─────────────────────────────────────────────────────────────────────────────

def send_sms_termii(to_phone: str, message: str) -> tuple[bool, str]:
    """
    Send SMS via Termii API.
    Set TERMII_API_KEY and TERMII_SENDER_ID in settings.
    Docs: https://developer.termii.com/messaging
    """
    api_key   = getattr(settings, 'TERMII_API_KEY', '')
    sender_id = getattr(settings, 'TERMII_SENDER_ID', 'TIT')

    if not api_key:
        # Development fallback — print to console
        logger.info(f"[SMS-CONSOLE] To: {to_phone} | {message}")
        return True, 'console-dev'

    phone = _normalise_phone(to_phone)
    if not phone:
        return False, f"Invalid phone: {to_phone}"

    try:
        resp = requests.post(
            'https://api.ng.termii.com/api/sms/send',
            json={
                'to':         phone,
                'from':       sender_id,
                'sms':        message,
                'type':       'plain',
                'channel':    'generic',
                'api_key':    api_key,
            },
            timeout=15,
        )
        data = resp.json()
        if resp.status_code == 200 and data.get('message') == 'Successfully Sent':
            ref = data.get('message_id', 'termii-ok')
            logger.info(f"[SMS-TERMII] Sent to {phone}")
            return True, ref
        err = data.get('message', str(data))
        logger.error(f"[SMS-TERMII] Failed to {phone}: {err}")
        return False, err
    except Exception as e:
        logger.error(f"[SMS-TERMII] Exception for {phone}: {e}")
        return False, str(e)


def send_sms_africastalking(to_phone: str, message: str) -> tuple[bool, str]:
    """
    Africa's Talking SMS (fallback).
    Set AT_API_KEY and AT_USERNAME in settings.
    """
    api_key  = getattr(settings, 'AT_API_KEY', '')
    username = getattr(settings, 'AT_USERNAME', 'sandbox')
    sender   = getattr(settings, 'AT_SENDER_ID', None)

    if not api_key or username == 'sandbox':
        logger.info(f"[SMS-AT-CONSOLE] To: {to_phone} | {message}")
        return True, 'at-console-dev'

    phone = _normalise_phone(to_phone)
    try:
        payload = {
            'username': username,
            'to':       phone,
            'message':  message,
        }
        if sender:
            payload['from'] = sender
        resp = requests.post(
            'https://api.africastalking.com/version1/messaging',
            data=payload,
            headers={
                'apiKey':  api_key,
                'Accept':  'application/json',
                'Content-Type': 'application/x-www-form-urlencoded',
            },
            timeout=15,
        )
        data = resp.json()
        entries = data.get('SMSMessageData', {}).get('Recipients', [])
        if entries and entries[0].get('statusCode') == 101:
            return True, entries[0].get('messageId', 'at-ok')
        return False, str(data)
    except Exception as e:
        return False, str(e)


def send_sms(to_phone: str, message: str) -> tuple[bool, str]:
    """
    Main SMS dispatcher — tries Termii first, falls back to Africa's Talking.
    If neither key is configured, logs to console (dev mode).
    """
    from tenants.middleware import get_current_tenant
    from communications import connect
    tenant = get_current_tenant()

    # WhatsApp first, when the school switched it on and the platform has it set up (F241).
    ok, ref = connect.try_whatsapp(tenant, to_phone, message)
    if ok:
        return ok, ref

    if not getattr(settings, 'SMS_ENABLED', True):
        logger.info(f"[SMS-DISABLED] Would send to {to_phone}: {message[:80]}")
        return False, 'sms-disabled'

    # SMS units (F246): refuse when a metered school has run out.
    pages = connect.sms_pages(message)
    if not connect.reserve(tenant, pages):
        return False, 'Not enough SMS units — top up under Communications → SMS units.'

    ok, ref = send_sms_termii(to_phone, message)
    if not ok:
        # Fallback
        logger.warning(f"[SMS] Termii failed, trying Africa's Talking for {to_phone}")
        ok, ref = send_sms_africastalking(to_phone, message)
    connect.settle(tenant, pages, ok)
    return ok, ref


# ─────────────────────────────────────────────────────────────────────────────
# CAMPAIGN SENDING
# ─────────────────────────────────────────────────────────────────────────────

def get_campaign_recipients(campaign):
    """
    Return a QuerySet of Student objects for a campaign's audience.
    """
    from django.db.models import Q
    from users.models import Student

    qs = Student.objects.filter(status__in=ENROLLED_STATUSES).select_related('class_assigned')

    if campaign.audience == 'CLASS' and campaign.school_class:
        qs = qs.filter(class_assigned=campaign.school_class)

    elif campaign.audience == 'BOARDERS':
        try:
            from hostel.models import BoarderProfile
            boarder_ids = BoarderProfile.objects.filter(
                student_type__in=['BOARDER', 'WEEKLY'],
                status='ACTIVE',
            ).values_list('student_id', flat=True)
            qs = qs.filter(id__in=boarder_ids)
        except Exception:
            pass

    elif campaign.audience == 'DEBTORS':
        from finance.models import FeeStructure, FeePayment
        # Find students with any unpaid fee
        paid_student_ids = set()
        all_students = qs.all()
        debtor_ids = []
        for student in all_students:
            paid_ids = FeePayment.objects.filter(
                student=student, status__in=['PAID', 'WAIVED']
            ).values_list('fee_structure_id', flat=True)
            unpaid = FeeStructure.objects.filter(
                is_active=True
            ).filter(
                Q(school_class=student.class_assigned) | Q(school_class__isnull=True)
            ).exclude(id__in=paid_ids)
            if unpaid.exists():
                debtor_ids.append(student.id)
        qs = qs.filter(id__in=debtor_ids)

    return qs


def _is_opted_out(student, channel: str) -> bool:
    from communications.models import OptOut
    return OptOut.objects.filter(
        student=student,
        channel__in=[channel, 'ALL']
    ).exists()


def send_campaign(campaign_id: int, user=None) -> dict:
    """
    Execute a campaign — send email and/or SMS to all recipients.
    Updates campaign stats and creates MessageLog records.
    Returns summary dict.
    """
    from communications.models import Campaign, MessageLog

    campaign = Campaign.objects.get(id=campaign_id)
    if campaign.status not in ('DRAFT', 'SCHEDULED'):
        return {'error': f"Campaign is {campaign.status}, cannot send."}

    campaign.status = 'SENDING'
    campaign.save(update_fields=['status'])

    recipients  = get_campaign_recipients(campaign)
    emails_sent = emails_fail = sms_sent = sms_fail = 0

    from communications.connect import texts_for, wants_topic
    for student in recipients:
        # Families can turn off school news (F255); fees, results and urgent notices always go.
        if campaign.topic == 'GENERAL' and not wants_topic(student, 'GENERAL'):
            continue
        ctx = _build_context(student)
        c_subject, c_email, c_sms = texts_for(campaign, student)   # family's language (F251)

        # ── EMAIL ────────────────────────────────────────────────────────────
        if campaign.channel in ('EMAIL', 'BOTH') and not _is_opted_out(student, 'EMAIL'):
            # The main contact plus any guardian who agreed to email, once each.
            for _name, email in contacts(student, 'EMAIL'):
                subject  = _render_body(c_subject, ctx)
                html     = _render_body(c_email, ctx)
                ok, ref  = send_email(email, subject, html, student)
                MessageLog.objects.create(
                    campaign=campaign, student=student, channel='EMAIL',
                    recipient=email, subject=subject, body=html,
                    status='SENT' if ok else 'FAILED',
                    provider_ref=ref if ok else '',
                    error_message=ref if not ok else '',
                    sent_at=timezone.now() if ok else None,
                )
                if ok: emails_sent += 1
                else:  emails_fail += 1

        # ── SMS ──────────────────────────────────────────────────────────────
        if campaign.channel in ('SMS', 'BOTH') and not _is_opted_out(student, 'SMS'):
            for _name, phone in contacts(student, 'SMS'):
                body     = _render_body(c_sms, ctx)
                ok, ref  = send_sms(phone, body)
                MessageLog.objects.create(
                    campaign=campaign, student=student, channel='SMS',
                    recipient=phone, body=body,
                    status='SENT' if ok else 'FAILED',
                    provider_ref=ref if ok else '',
                    error_message=ref if not ok else '',
                    sent_at=timezone.now() if ok else None,
                )
                if ok: sms_sent += 1
                else:  sms_fail += 1

    # Update campaign
    campaign.status           = 'SENT'
    campaign.sent_at          = timezone.now()
    campaign.total_recipients = recipients.count()
    campaign.emails_sent      = emails_sent
    campaign.emails_failed    = emails_fail
    campaign.sms_sent         = sms_sent
    campaign.sms_failed       = sms_fail
    campaign.save()

    return {
        'total':        recipients.count(),
        'emails_sent':  emails_sent,
        'emails_fail':  emails_fail,
        'sms_sent':     sms_sent,
        'sms_fail':     sms_fail,
    }


# ─────────────────────────────────────────────────────────────────────────────
# QUICK MESSAGE  (single parent direct message)
# ─────────────────────────────────────────────────────────────────────────────

def send_quick_message(student, channel: str, subject: str,
                       body: str, sent_by=None) -> dict:
    """
    Send an immediate message to a single student's parent.
    channel: 'EMAIL', 'SMS', or 'BOTH'
    Returns result dict.
    """
    from communications.models import QuickMessage

    ctx    = _build_context(student)
    body   = _render_body(body, ctx)
    result = {'email': None, 'sms': None}
    errors = []

    if channel in ('EMAIL', 'BOTH'):
        emails = contacts(student, 'EMAIL')
        if emails:
            sub = _render_body(subject, ctx)
            for _name, email in emails:
                ok, ref = send_email(email, sub, body, student)
                result['email'] = 'sent' if ok else f'failed: {ref}'
                if not ok: errors.append(f"Email to {email}: {ref}")
        else:
            errors.append("No parent email on record.")

    if channel in ('SMS', 'BOTH'):
        phones = contacts(student, 'SMS')
        if phones:
            # Use plain text for SMS
            sms_body = strip_tags(body)[:800]
            for _name, phone in phones:
                ok, ref  = send_sms(phone, sms_body)
                result['sms'] = 'sent' if ok else f'failed: {ref}'
                if not ok: errors.append(f"SMS to {phone}: {ref}")
        else:
            errors.append("No parent phone on record.")

    QuickMessage.objects.create(
        student=student, channel=channel,
        subject=subject, body=body,
        status='SENT' if not errors else 'FAILED',
        sent_by=sent_by,
        error='\n'.join(errors),
    )
    return {**result, 'errors': errors, 'success': len(errors) == 0}


# ─────────────────────────────────────────────────────────────────────────────
# FEE REMINDER  (auto-send to all debtors)
# ─────────────────────────────────────────────────────────────────────────────

def send_fee_reminders(channel: str = 'BOTH',
                       custom_message: str = '',
                       user=None) -> dict:
    """
    Auto-send fee reminder messages to all parents with outstanding balances.
    Uses a default template if no custom_message provided.
    """
    from django.db.models import Q
    from users.models import Student
    from finance.models import FeeStructure, FeePayment

    DEFAULT_EMAIL = """
<html><body style="font-family:Arial,sans-serif;color:#1e293b;">
<div style="max-width:600px;margin:0 auto;padding:20px;">
  <div style="background:#064e3b;padding:20px;border-radius:10px 10px 0 0;text-align:center;">
    <h2 style="color:#fff;margin:0;">{{school_name}}</h2>
    <p style="color:rgba(255,255,255,.8);margin:4px 0 0;">School Fee Reminder</p>
  </div>
  <div style="background:#fff;padding:24px;border:1px solid #e2e8f0;border-radius:0 0 10px 10px;">
    <p>Dear {{parent_name}},</p>
    <p>This is a friendly reminder that the school fee for <strong>{{student_name}}</strong>
       ({{class_name}}) has an outstanding balance.</p>
    <div style="background:#fef2f2;border-left:4px solid #dc2626;padding:14px;border-radius:0 8px 8px 0;margin:16px 0;">
      <strong style="color:#dc2626;">Outstanding Balance: {{outstanding_fee}}</strong>
    </div>
    <p>Please make payment at your earliest convenience to avoid disruption to your child's education.</p>
    <p>You can fund your wallet and pay directly through the school portal.</p>
    <br>
    <p>Regards,<br><strong>{{school_name}} Bursary Office</strong></p>
  </div>
</div></body></html>"""

    DEFAULT_SMS = (
        "Dear {{parent_name}}, your child {{student_name}} ({{class_name}}) "
        "has an outstanding school fee of {{outstanding_fee}}. "
        "Please pay promptly. {{school_name}}."
    )

    email_tpl = custom_message if custom_message else DEFAULT_EMAIL
    sms_tpl   = custom_message if custom_message else DEFAULT_SMS

    students = Student.objects.filter(status__in=ENROLLED_STATUSES).select_related('class_assigned')
    sent = failed = skipped = 0

    for student in students:
        ctx = _build_context(student)
        outstanding_str = ctx.get('outstanding_fee', '₦0.00')
        # Skip if no outstanding balance
        if outstanding_str in ('₦0.00', '₦0'):
            skipped += 1
            continue

        errors = []
        if channel in ('EMAIL', 'BOTH') and not _is_opted_out(student, 'EMAIL'):
            for _name, email in contacts(student, 'EMAIL', fees=True):
                body = _render_body(email_tpl, ctx)
                ok, ref = send_email(email, 'School Fee Reminder — Action Required', body, student)
                if ok: sent += 1
                else:  failed += 1; errors.append(ref)

        if channel in ('SMS', 'BOTH') and not _is_opted_out(student, 'SMS'):
            for _name, phone in contacts(student, 'SMS', fees=True):
                body = _render_body(sms_tpl, ctx)[:800]
                ok, ref = send_sms(phone, body)
                if ok: sent += 1
                else:  failed += 1; errors.append(ref)

    return {
        'sent': sent, 'failed': failed,
        'skipped_no_debt': skipped,
    }


# ─────────────────────────────────────────────────────────────────────────────
# SCHEDULED CAMPAIGN RUNNER  (call from management command or cron)
# ─────────────────────────────────────────────────────────────────────────────

def run_scheduled_campaigns() -> dict:
    """
    Process all campaigns with status=SCHEDULED and scheduled_at <= now().
    Call this from a cron job or Django management command every minute.
    Returns summary of campaigns processed.
    """
    from communications.models import Campaign
    from django.utils import timezone as tz

    due = Campaign.objects.filter(
        status='SCHEDULED',
        scheduled_at__lte=tz.now(),
    )
    results = []
    for campaign in due:
        result = send_campaign(campaign.id)
        results.append({'campaign': campaign.title, **result})
    return {'processed': len(results), 'details': results}


# ─────────────────────────────────────────────────────────────────────────────
# HOSTEL PAYMENT REMINDER
# ─────────────────────────────────────────────────────────────────────────────

def send_hostel_reminders(channel: str = 'BOTH', user=None) -> dict:
    """
    Auto-send hostel fee reminders to all boarders with unpaid/partial bills
    for the current active term.
    """
    from academics.utils import get_active_session, get_active_term
    from django.utils.html import strip_tags

    active_session = get_active_session()
    active_term    = get_active_term()

    if not active_session or not active_term:
        return {'error': 'No active session/term found.'}

    try:
        from hostel.models import HostelTermBilling
    except ImportError:
        return {'error': 'Hostel module not available.'}

    unpaid = HostelTermBilling.objects.filter(
        status__in=['UNPAID', 'PARTIAL'],
        session=active_session,
        term=active_term,
    ).select_related('boarder__student')

    sent = failed = 0
    for bill in unpaid:
        student      = bill.boarder.student
        parent_name  = student.parent_name or 'Parent/Guardian'
        parent_email = student.parent_email
        parent_phone = student.parent_phone

        email_body = f"""
<html><body style="font-family:Arial,sans-serif;color:#1e293b;">
<div style="max-width:600px;margin:0 auto;padding:20px;">
  <div style="background:#1e3a5f;padding:20px;border-radius:10px 10px 0 0;text-align:center;">
    <h2 style="color:#fff;margin:0;">{student.tenant.name}</h2>
    <p style="color:rgba(255,255,255,.8);margin:4px 0 0;">Hostel Fee Reminder</p>
  </div>
  <div style="background:#fff;padding:24px;border:1px solid #e2e8f0;border-radius:0 0 10px 10px;">
    <p>Dear {parent_name},</p>
    <p>This is a reminder that the hostel fee for <strong>{student.full_name}</strong>
    ({str(student.class_assigned or '')}) has an outstanding balance for
    <strong>{active_term} / {active_session}</strong>.</p>
    <div style="background:#fef2f2;border-left:4px solid #dc2626;padding:14px;
                border-radius:0 8px 8px 0;margin:16px 0;">
      <strong style="color:#dc2626;">Hostel Fee Balance: ₦{bill.balance_due:,.2f}</strong><br>
      <small style="color:#64748b;">Total: ₦{bill.total_fee:,.2f} | Paid: ₦{bill.amount_paid:,.2f}</small>
    </div>
    <p>Please log in to the parent portal to make payment.</p>
    <p>Regards,<br><strong>{student.tenant.name} Finance Office</strong></p>
  </div>
</div></body></html>"""

        sms_body = (
            f"{student.tenant.name} Hostel Reminder: {student.full_name}'s hostel fee balance is "
            f"NGN{bill.balance_due:,.2f} for {active_term}. Please pay via the parent portal."
        )

        if channel in ('EMAIL', 'BOTH') and parent_email:
            ok, _ = send_email(parent_email,
                               f"{student.tenant.name} Hostel Fee Reminder — ₦{bill.balance_due:,.2f}",
                               email_body, student)
            if ok: sent += 1
            else:  failed += 1

        if channel in ('SMS', 'BOTH') and parent_phone:
            ok, _ = send_sms(parent_phone, sms_body[:800])
            if ok: sent += 1
            else:  failed += 1

    return {
        'sent': sent, 'failed': failed,
        'total_reminders': unpaid.count(),
    }


# ─────────────────────────────────────────────────────────────────────────────
# RESULT NOTIFICATION  (when results are published)
# ─────────────────────────────────────────────────────────────────────────────

def send_result_notification(student, term, session, channel: str = 'BOTH') -> dict:
    """
    Notify a parent that their child's result is available on the portal.
    Called after a result batch is published.
    """
    parent_name  = student.parent_name or 'Parent/Guardian'
    parent_email = student.parent_email
    parent_phone = student.parent_phone
    errors = []

    html = f"""
<html><body style="font-family:Arial,sans-serif;color:#1e293b;">
<div style="max-width:600px;margin:0 auto;padding:20px;">
  <div style="background:#064e3b;padding:20px;border-radius:10px 10px 0 0;text-align:center;">
    <h2 style="color:#fff;margin:0;">{student.tenant.name}</h2>
    <p style="color:rgba(255,255,255,.8);margin:4px 0 0;">Result Notification</p>
  </div>
  <div style="background:#fff;padding:24px;border:1px solid #e2e8f0;border-radius:0 0 10px 10px;">
    <p>Dear {parent_name},</p>
    <p>The examination result for <strong>{student.full_name}</strong>
    ({str(student.class_assigned or '')}) for <strong>{term} / {session}</strong>
    is now available.</p>
    <div style="background:#f0fdf4;border-left:4px solid #16a34a;padding:14px;
                border-radius:0 8px 8px 0;margin:16px 0;">
      <p style="margin:0;color:#065f46;">
        <strong>Log in to the parent portal</strong> to view your child's full result,
        subject scores, position, and teacher remarks.
      </p>
    </div>
    <p>If you have any questions, please contact the school.</p>
    <p>Regards,<br><strong>{student.tenant.name}</strong></p>
  </div>
</div></body></html>"""

    sms = (f"{student.tenant.name}: The {term}/{session} result for {student.full_name} is now available. "
           f"Log in to the parent portal to view it.")

    if channel in ('EMAIL', 'BOTH'):
        for _name, email in contacts(student, 'EMAIL'):
            ok, ref = send_email(email,
                                 f"{student.tenant.name}: {student.full_name}'s Result is Now Available",
                                 html, student)
            if not ok: errors.append(f"Email to {email}: {ref}")

    if channel in ('SMS', 'BOTH'):
        for _name, phone in contacts(student, 'SMS'):
            ok, ref = send_sms(phone, sms[:800])
            if not ok: errors.append(f"SMS to {phone}: {ref}")

    return {'success': len(errors) == 0, 'errors': errors}


# ─────────────────────────────────────────────────────────────────────────────
# SEED DEFAULT TEMPLATES  (run once on first setup)
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_TEMPLATES = [
    {
        'name':     'School Fee Reminder',
        'category': 'FEE',
        'subject':  'Important: Outstanding School Fee — {{student_name}}',
        'body_email': """<html><body style="font-family:Arial,sans-serif;color:#1e293b;">
<div style="max-width:600px;margin:0 auto;padding:20px;">
  <div style="background:#064e3b;padding:20px;border-radius:10px 10px 0 0;text-align:center;">
    <h2 style="color:#fff;margin:0;">{{school_name}}</h2>
    <p style="color:rgba(255,255,255,.8);">School Fee Reminder</p>
  </div>
  <div style="background:#fff;padding:24px;border:1px solid #e2e8f0;border-radius:0 0 10px 10px;">
    <p>Dear {{parent_name}},</p>
    <p>We write to remind you that the school fee account for <strong>{{student_name}}</strong>
    ({{class_name}}) has an outstanding balance.</p>
    <div style="background:#fef2f2;border-left:4px solid #dc2626;padding:14px;border-radius:0 8px 8px 0;margin:16px 0;">
      <strong style="color:#dc2626;font-size:1.1rem;">Outstanding: {{outstanding_fee}}</strong>
    </div>
    <p>Please make payment at your earliest convenience. You can pay via the parent portal
    or visit the school bursary.</p>
    <p>Regards,<br><strong>{{school_name}} Bursary</strong></p>
  </div>
</div></body></html>""",
        'body_sms': "Dear {{parent_name}}, {{student_name}} ({{class_name}}) has an outstanding fee of {{outstanding_fee}}. Please pay promptly. {{school_name}}.",
    },
    {
        'name':     'General Announcement',
        'category': 'GENERAL',
        'subject':  '{{school_name}} Notice: {{title}}',
        'body_email': """<html><body style="font-family:Arial,sans-serif;color:#1e293b;">
<div style="max-width:600px;margin:0 auto;padding:20px;">
  <div style="background:#064e3b;padding:20px;border-radius:10px 10px 0 0;text-align:center;">
    <h2 style="color:#fff;margin:0;">{{school_name}}</h2>
  </div>
  <div style="background:#fff;padding:24px;border:1px solid #e2e8f0;border-radius:0 0 10px 10px;">
    <p>Dear {{parent_name}},</p>
    <p>Please note the following announcement from {{school_name}}:</p>
    <div style="background:#f0fdf4;border-left:4px solid #16a34a;padding:14px;border-radius:0 8px 8px 0;margin:16px 0;">
      {{message}}
    </div>
    <p>Regards,<br><strong>{{school_name}} Administration</strong></p>
  </div>
</div></body></html>""",
        'body_sms': "{{school_name}} notice to {{parent_name}}: {{message}} - {{school_name}}",
    },
    {
        'name':     'End of Term Exam Reminder',
        'category': 'EXAM',
        'subject':  '{{school_name}}: End of Term Examination — {{student_name}}',
        'body_email': """<html><body style="font-family:Arial,sans-serif;color:#1e293b;">
<div style="max-width:600px;margin:0 auto;padding:20px;">
  <div style="background:#1e3a5f;padding:20px;border-radius:10px 10px 0 0;text-align:center;">
    <h2 style="color:#fff;margin:0;">{{school_name}}</h2>
    <p style="color:rgba(255,255,255,.8);">Examination Notice</p>
  </div>
  <div style="background:#fff;padding:24px;border:1px solid #e2e8f0;border-radius:0 0 10px 10px;">
    <p>Dear {{parent_name}},</p>
    <p>This is to inform you that the end-of-term examinations for <strong>{{student_name}}</strong>
    ({{class_name}}) will be commencing soon.</p>
    <p>Please ensure your child:</p>
    <ul>
      <li>Comes to school on time with all required materials</li>
      <li>Has all outstanding fees cleared before the exam period</li>
      <li>Is well-rested and prepared</li>
    </ul>
    <p>Current fee balance: <strong>{{outstanding_fee}}</strong></p>
    <p>Regards,<br><strong>{{school_name}} Academic Office</strong></p>
  </div>
</div></body></html>""",
        'body_sms': "{{school_name}}: Exams starting soon for {{student_name}} ({{class_name}}). Ensure fees ({{outstanding_fee}}) are paid & your child is prepared. {{school_name}}.",
    },
    {
        'name':     'Welcome / New Student',
        'category': 'WELCOME',
        'subject':  'Welcome to {{school_name}} — {{student_name}}',
        'body_email': """<html><body style="font-family:Arial,sans-serif;color:#1e293b;">
<div style="max-width:600px;margin:0 auto;padding:20px;">
  <div style="background:#064e3b;padding:24px;border-radius:10px 10px 0 0;text-align:center;">
    <h2 style="color:#fff;margin:0;">Welcome to</h2>
    <h1 style="color:#a7f3d0;margin:4px 0 0;font-size:1.4rem;">{{school_name}}</h1>
  </div>
  <div style="background:#fff;padding:24px;border:1px solid #e2e8f0;border-radius:0 0 10px 10px;">
    <p>Dear {{parent_name}},</p>
    <p>We warmly welcome <strong>{{student_name}}</strong> to {{school_name}}!</p>
    <div style="background:#f0fdf4;border-left:4px solid #16a34a;padding:14px;border-radius:0 8px 8px 0;margin:16px 0;">
      <strong>Student:</strong> {{student_name}}<br>
      <strong>Admission No.:</strong> {{admission_no}}<br>
      <strong>Class:</strong> {{class_name}}
    </div>
    <p>You can log in to our parent portal using your child's admission number to 
    view results, make payments, and stay updated.</p>
    <p>We look forward to partnering with you in your child's education.</p>
    <p>Warm regards,<br><strong>The Principal<br>{{school_name}}</strong></p>
  </div>
</div></body></html>""",
        'body_sms': "Welcome to {{school_name}}! {{student_name}} ({{class_name}}, Adm: {{admission_no}}) has been enrolled. Log in to the parent portal for details.",
    },
    {
        'name':     'Emergency Alert',
        'category': 'EMERGENCY',
        'subject':  '🚨 URGENT: {{school_name}} Emergency Alert',
        'body_email': """<html><body style="font-family:Arial,sans-serif;color:#1e293b;">
<div style="max-width:600px;margin:0 auto;padding:20px;">
  <div style="background:#dc2626;padding:20px;border-radius:10px 10px 0 0;text-align:center;">
    <h2 style="color:#fff;margin:0;">⚠ URGENT NOTICE</h2>
    <p style="color:rgba(255,255,255,.9);">{{school_name}}</p>
  </div>
  <div style="background:#fff;padding:24px;border:1px solid #e2e8f0;border-radius:0 0 10px 10px;">
    <p>Dear {{parent_name}},</p>
    <div style="background:#fef2f2;border-left:4px solid #dc2626;padding:14px;border-radius:0 8px 8px 0;margin:16px 0;">
      {{message}}
    </div>
    <p>Please contact the school office immediately: {{school_phone}}</p>
    <p><strong>{{school_name}} Management</strong></p>
  </div>
</div></body></html>""",
        'body_sms': "URGENT - {{school_name}}: {{message}} Call: {{school_phone}}",
    },
    {
        'name':     'Hostel Fee Reminder',
        'category': 'HOSTEL',
        'subject':  '{{school_name}}: Hostel Fee Outstanding — {{student_name}}',
        'body_email': """<html><body style="font-family:Arial,sans-serif;color:#1e293b;">
<div style="max-width:600px;margin:0 auto;padding:20px;">
  <div style="background:#1e3a5f;padding:20px;border-radius:10px 10px 0 0;text-align:center;">
    <h2 style="color:#fff;margin:0;">{{school_name}}</h2>
    <p style="color:rgba(255,255,255,.8);">Hostel Fee Reminder</p>
  </div>
  <div style="background:#fff;padding:24px;border:1px solid #e2e8f0;border-radius:0 0 10px 10px;">
    <p>Dear {{parent_name}},</p>
    <p>The hostel fee for <strong>{{student_name}}</strong> ({{class_name}}) is outstanding.</p>
    <p>Please pay via the parent portal or contact the bursary office.</p>
    <p>Outstanding Fee Balance: <strong>{{outstanding_fee}}</strong></p>
    <p>Regards,<br><strong>{{school_name}} Finance Office</strong></p>
  </div>
</div></body></html>""",
        'body_sms': "{{school_name}} Hostel Reminder: {{student_name}}'s hostel fee balance is {{outstanding_fee}}. Please pay promptly via the parent portal.",
    },
]


# ─────────────────────────────────────────────────────────────────────────────
# LOGIN DETAILS NOTIFICATION  (send username + password to parent & student)
# ─────────────────────────────────────────────────────────────────────────────

def send_login_details(student, student_password: str = None,
                       parent_password: str = None,
                       channel: str = 'BOTH') -> dict:
    """
    Send login credentials to a student (via parent email/phone).
    
    student_password: the plaintext password (only available at creation/reset time)
    parent_password:  the parent's plaintext password (only available at creation time)
    channel:          'EMAIL', 'SMS', or 'BOTH'
    
    NOTE: Passwords can only be sent when first created or reset. 
    Once hashed, they cannot be retrieved. If student_password is None,
    the email will say to contact the school for their password.
    """
    from django.conf import settings as dj_settings
    
    school_url  = getattr(dj_settings, 'SCHOOL_PORTAL_URL', 'http://127.0.0.1:8000')
    school_name = '{{school_name}}'
    errors = []

    # ── Student username (= admission number) ──────────────────────────────
    student_username = student.admission_number

    # ── Parent username (= parent_email) ──────────────────────────────────
    parent_username = student.parent_email or '(not set)'

    # ── EMAIL ──────────────────────────────────────────────────────────────
    if channel in ('EMAIL', 'BOTH') and student.parent_email:
        html = f"""
<html><body style="font-family:Arial,Helvetica,sans-serif;background:#f8fafc;padding:20px;">
<div style="max-width:600px;margin:0 auto;">
  <div style="background:#064e3b;padding:20px 26px;border-radius:12px 12px 0 0;text-align:center;">
    <h2 style="color:#fff;margin:0;font-size:1.2rem;">{school_name}</h2>
    <p style="color:rgba(255,255,255,.8);margin:4px 0 0;font-size:.9rem;">Parent/Student Portal — Login Details</p>
  </div>
  <div style="background:#fff;padding:28px;border:1px solid #e2e8f0;border-radius:0 0 12px 12px;">

    <p style="margin-top:0;">Dear {student.parent_name or 'Parent/Guardian'},</p>
    <p>Your child <strong>{student.full_name}</strong> ({student.class_assigned or 'N/A'})
    has been registered on our school portal. Below are the login credentials.</p>

    <!-- Student Login -->
    <div style="background:#f0fdf4;border:1px solid #bbf7d0;border-radius:10px;padding:18px;margin:20px 0;">
      <div style="font-weight:800;color:#065f46;font-size:.95rem;margin-bottom:12px;">
        🎓 Student Login
      </div>
      <table style="width:100%;border-collapse:collapse;font-size:.9rem;">
        <tr>
          <td style="padding:6px 0;color:#64748b;width:120px;">Username</td>
          <td style="padding:6px 0;font-weight:700;font-family:monospace;color:#1e293b;">{student_username}</td>
        </tr>
        <tr>
          <td style="padding:6px 0;color:#64748b;">Password</td>
          <td style="padding:6px 0;font-weight:700;font-family:monospace;color:#16a34a;">
            {student_password if student_password else '(contact school to reset)'}
          </td>
        </tr>
      </table>
    </div>

    <!-- Parent Login -->
    <div style="background:#eff6ff;border:1px solid #bfdbfe;border-radius:10px;padding:18px;margin:20px 0;">
      <div style="font-weight:800;color:#1e40af;font-size:.95rem;margin-bottom:12px;">
        👨‍👩‍👧 Parent Login
      </div>
      <table style="width:100%;border-collapse:collapse;font-size:.9rem;">
        <tr>
          <td style="padding:6px 0;color:#64748b;width:120px;">Username</td>
          <td style="padding:6px 0;font-weight:700;font-family:monospace;color:#1e293b;">{parent_username}</td>
        </tr>
        <tr>
          <td style="padding:6px 0;color:#64748b;">Password</td>
          <td style="padding:6px 0;font-weight:700;font-family:monospace;color:#2563eb;">
            {parent_password if parent_password else '(same as before — unchanged)'}
          </td>
        </tr>
      </table>
    </div>

    <div style="background:#fef9c3;border-left:4px solid #eab308;padding:12px 16px;border-radius:0 8px 8px 0;font-size:.85rem;color:#713f12;margin:16px 0;">
      <strong>⚠ Security Notice:</strong> Please keep these credentials safe and 
      change your password after your first login.
      Do not share this email with anyone outside your family.
    </div>

    <p style="margin-bottom:4px;">
      <strong>Portal URL:</strong>
      <a href="{school_url}" style="color:#2563eb;">{school_url}</a>
    </p>
    <p>If you have any trouble logging in, please contact the school office.</p>

    <p style="margin-top:24px;">Regards,<br>
    <strong>{school_name}</strong></p>
  </div>
</div>
</body></html>"""
        ok, ref = send_email(
            student.parent_email,
            f"{school_name} — Portal Login Details for {student.full_name}",
            html,
            student,
        )
        if not ok:
            errors.append(f"Email: {ref}")

    # ── SMS ────────────────────────────────────────────────────────────────
    if channel in ('SMS', 'BOTH') and student.parent_phone:
        sms_parts = [f"{student.tenant.name} Portal Login for {student.full_name}:"]
        sms_parts.append(f"STUDENT — Username: {student_username}")
        if student_password:
            sms_parts.append(f"Password: {student_password}")
        sms_parts.append(f"PARENT — Username: {parent_username}")
        if parent_password:
            sms_parts.append(f"Password: {parent_password}")
        sms_parts.append(f"URL: {school_url}")
        sms_body = " | ".join(sms_parts)[:800]

        ok, ref = send_sms(student.parent_phone, sms_body)
        if not ok:
            errors.append(f"SMS: {ref}")

    return {
        'success': len(errors) == 0,
        'errors':  errors,
        'student_username': student_username,
        'parent_username':  parent_username,
    }


def send_bulk_login_details(students_qs, channel: str = 'EMAIL') -> dict:
    """
    Send login details to multiple students' parents.
    NOTE: Passwords are NOT retrievable after hashing — this only sends
    the usernames and a "contact school to reset password" message.
    Used for resending login reminders to existing accounts.
    """
    sent = failed = 0
    for student in students_qs:
        result = send_login_details(
            student=student,
            student_password=None,   # cannot retrieve hashed password
            parent_password=None,
            channel=channel,
        )
        if result['success']:
            sent += 1
        else:
            failed += 1
    return {'sent': sent, 'failed': failed, 'total': sent + failed}


def seed_default_templates():
    """
    Create default message templates if they don't exist.
    Safe to call multiple times — only creates missing ones.
    """
    from communications.models import MessageTemplate
    created = 0
    for tpl_data in DEFAULT_TEMPLATES:
        _, was_created = MessageTemplate.objects.get_or_create(
            name=tpl_data['name'],
            defaults={
                'category':   tpl_data['category'],
                'subject':    tpl_data['subject'],
                'body_email': tpl_data['body_email'],
                'body_sms':   tpl_data['body_sms'],
                'is_active':  True,
            }
        )
        if was_created:
            created += 1
    return {'created': created, 'total': len(DEFAULT_TEMPLATES)}