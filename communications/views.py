"""communications/views.py — Communication Centre"""

from users.models import ENROLLED_STATUSES
from django.contrib import messages

from lms_project.tasks import run_in_background
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q, Sum
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from users.models import Student

from communications.models import (
    Campaign, MessageLog, MessageTemplate,
    OptOut, QuickMessage,
)
from communications.services import (
    get_campaign_recipients, send_campaign,
    send_fee_reminders, send_hostel_reminders,
    send_quick_message, seed_default_templates,
    send_login_details, send_bulk_login_details,
    test_email_connection,
)


def _is_comm_staff(user):
    if user.is_superuser: return True
    if getattr(user, 'is_staff_user', False): return True
    try: return user.staff.role in ('ADMIN', 'ACCOUNT')
    except Exception: return False


def _require_comm_staff(fn):
    def wrap(req, *a, **kw):
        if not req.user.is_authenticated or not _is_comm_staff(req.user):
            return HttpResponse("Communications staff access only.", status=403)
        return fn(req, *a, **kw)
    wrap.__name__ = fn.__name__
    return wrap


# ─────────────────────────────────────────────────────────────────────────────
# DASHBOARD
# ─────────────────────────────────────────────────────────────────────────────

@login_required
@_require_comm_staff
def dashboard(request):
    total_campaigns = Campaign.objects.count()
    sent_campaigns  = Campaign.objects.filter(status='SENT').count()
    total_emails    = MessageLog.objects.filter(channel='EMAIL', status='SENT').count()
    total_sms       = MessageLog.objects.filter(channel='SMS',   status='SENT').count()
    failed_msgs     = MessageLog.objects.filter(status='FAILED').count()
    recent_campaigns = Campaign.objects.select_related('created_by').all()[:10]
    recent_logs      = MessageLog.objects.select_related(
        'campaign', 'student'
    ).order_by('-created_at')[:20]
    quick_msgs       = QuickMessage.objects.select_related(
        'student', 'sent_by'
    ).order_by('-sent_at')[:10]

    # Stats by channel
    from django.db.models import Sum
    email_agg = Campaign.objects.filter(status='SENT').aggregate(
        sent=Sum('emails_sent'), failed=Sum('emails_failed')
    )
    sms_agg = Campaign.objects.filter(status='SENT').aggregate(
        sent=Sum('sms_sent'), failed=Sum('sms_failed')
    )

    # Students with missing contact info
    no_email = Student.objects.filter(
        status__in=ENROLLED_STATUSES
    ).filter(Q(parent_email='') | Q(parent_email__isnull=True)).count()
    no_phone = Student.objects.filter(
        status__in=ENROLLED_STATUSES
    ).filter(Q(parent_phone='') | Q(parent_phone__isnull=True)).count()

    from users.models import Class
    classes = Class.objects.all().order_by('name')

    return render(request, 'communications/dashboard.html', {
        'total_campaigns':  total_campaigns,
        'sent_campaigns':   sent_campaigns,
        'total_emails':     total_emails,
        'total_sms':        total_sms,
        'failed_msgs':      failed_msgs,
        'recent_campaigns': recent_campaigns,
        'recent_logs':      recent_logs,
        'quick_msgs':       quick_msgs,
        'email_sent':       email_agg['sent'] or 0,
        'email_fail':       email_agg['failed'] or 0,
        'sms_sent':         sms_agg['sent']  or 0,
        'sms_fail':         sms_agg['failed'] or 0,
        'no_email_count':   no_email,
        'no_phone_count':   no_phone,
        'classes':          classes,
    })


# ─────────────────────────────────────────────────────────────────────────────
# CAMPAIGN  Create / List / Detail / Send
# ─────────────────────────────────────────────────────────────────────────────

@login_required
@_require_comm_staff
def campaign_list(request):
    campaigns = Campaign.objects.select_related('created_by', 'school_class').all()
    return render(request, 'communications/campaign_list.html', {
        'campaigns': campaigns,
    })


@login_required
@_require_comm_staff
def campaign_create(request):
    if request.method == 'POST':
        # Load from template if selected
        tpl_id = request.POST.get('template_id') or None
        tpl    = MessageTemplate.objects.filter(id=tpl_id).first() if tpl_id else None

        channel    = request.POST.get('channel', 'EMAIL')
        subject    = request.POST.get('subject', '').strip() or (tpl.subject   if tpl else '')
        body_email = request.POST.get('body_email', '').strip() or (tpl.body_email if tpl else '')
        body_sms   = request.POST.get('body_sms', '').strip()   or (tpl.body_sms   if tpl else '')
        audience   = request.POST.get('audience', 'ALL')
        class_id   = request.POST.get('school_class') or None
        title      = request.POST.get('title', '').strip()
        scheduled  = request.POST.get('scheduled_at', '').strip() or None

        from datetime import datetime
        scheduled_dt = None
        if scheduled:
            try:
                scheduled_dt = datetime.fromisoformat(scheduled)
                from django.utils.timezone import make_aware
                if scheduled_dt.tzinfo is None:
                    scheduled_dt = make_aware(scheduled_dt)
            except ValueError:
                scheduled_dt = None

        campaign = Campaign.objects.create(
            title=title, channel=channel, audience=audience,
            topic=request.POST.get('topic') if request.POST.get('topic') in dict(Campaign.TOPIC_CHOICES) else 'GENERAL',
            school_class_id=class_id,
            template=tpl,
            subject=subject, body_email=body_email, body_sms=body_sms,
            status='SCHEDULED' if scheduled_dt else 'DRAFT',
            scheduled_at=scheduled_dt,
            created_by=request.user,
        )
        from communications.engage import submit
        if submit(campaign, request.user, request.tenant):
            messages.success(request, f'Campaign "{title}" created and sent to an administrator for approval. '
                                      'You will get an email when it is approved.')
            return redirect('communications:campaign_detail', campaign_id=campaign.id)
        messages.success(request, f'Campaign "{title}" created.')

        if request.POST.get('send_now') == 'yes':
            # campaign_send is POST-only; redirect to detail with a flag to auto-send
            messages.info(request,
                f'Campaign created. Click "Send Now" below to dispatch it to recipients.')
        return redirect('communications:campaign_detail', campaign_id=campaign.id)

    templates = MessageTemplate.objects.filter(is_active=True).order_by('category', 'name')
    from users.models import Class
    classes = Class.objects.all().order_by('name')
    return render(request, 'communications/campaign_form.html', {
        'templates': templates, 'classes': classes,
    })


@login_required
@_require_comm_staff
def campaign_detail(request, campaign_id):
    campaign    = get_object_or_404(Campaign, id=campaign_id)
    logs        = campaign.logs.select_related('student').order_by('-created_at')
    email_logs  = logs.filter(channel='EMAIL')
    sms_logs    = logs.filter(channel='SMS')
    recipients  = get_campaign_recipients(campaign)

    return render(request, 'communications/campaign_detail.html', {
        'campaign':    campaign,
        'logs':        logs,
        'email_logs':  email_logs,
        'sms_logs':    sms_logs,
        'recipients':  recipients,
    })


@login_required
@_require_comm_staff
@require_POST
def campaign_send(request, campaign_id):
    """Actually send the campaign. Runs synchronously (use Celery in production)."""
    campaign = get_object_or_404(Campaign, id=campaign_id)
    from communications.engage import may_send
    if not may_send(campaign, request.user, request.tenant):
        messages.error(request, 'An administrator must approve this message before it is sent.')
        return redirect('communications:campaign_detail', campaign_id=campaign.id)
    if campaign.status not in ('DRAFT', 'SCHEDULED'):
        messages.error(request, f"Campaign cannot be sent — status is {campaign.status}.")
        return redirect('communications:campaign_detail', campaign_id=campaign.id)

    recipients = len(get_campaign_recipients(campaign))
    run_in_background(send_campaign, campaign.id, user=request.user, tenant=request.tenant)
    messages.success(
        request,
        f"Sending “{campaign.title}” to {recipients} recipient{'' if recipients == 1 else 's'} "
        f"in the background. Delivery results appear in Message Logs."
    )
    return redirect('communications:campaign_detail', campaign_id=campaign.id)


# ─────────────────────────────────────────────────────────────────────────────
# QUICK MESSAGE  (direct to single parent)
# ─────────────────────────────────────────────────────────────────────────────

@login_required
@_require_comm_staff
def quick_message(request):
    if request.method == 'POST':
        student_id = request.POST.get('student_id')
        channel    = request.POST.get('channel', 'EMAIL')
        subject    = request.POST.get('subject', '').strip()
        body       = request.POST.get('body', '').strip()
        student    = get_object_or_404(Student, id=student_id)

        result = send_quick_message(
            student=student, channel=channel,
            subject=subject, body=body,
            sent_by=request.user,
        )
        if result['success']:
            messages.success(request,
                f"Message sent to {student.parent_name or student.full_name}'s parent.")
        else:
            messages.error(request,
                f"Some messages failed: {'; '.join(result['errors'])}")
        return redirect('communications:quick_message')

    students  = Student.objects.filter(status__in=ENROLLED_STATUSES).select_related('class_assigned').order_by('full_name')
    templates = MessageTemplate.objects.filter(is_active=True).order_by('name')
    recent    = QuickMessage.objects.select_related('student', 'sent_by').order_by('-sent_at')[:20]
    return render(request, 'communications/quick_message.html', {
        'students': students, 'templates': templates, 'recent': recent,
    })


# ─────────────────────────────────────────────────────────────────────────────
# FEE REMINDERS  (bulk auto-send to debtors)
# ─────────────────────────────────────────────────────────────────────────────

@login_required
@_require_comm_staff
def fee_reminders(request):
    if request.method == 'POST':
        channel = request.POST.get('channel', 'BOTH')
        custom  = request.POST.get('custom_message', '').strip()
        run_in_background(
            send_fee_reminders, channel=channel, custom_message=custom or '',
            user=request.user, tenant=request.tenant,
        )
        messages.success(
            request,
            "Fee reminders are being sent in the background. "
            "Delivery results appear in Message Logs."
        )
        return redirect('communications:dashboard')

    # Get preview of who will receive
    from django.db.models import Q
    from finance.models import FeeStructure, FeePayment
    debtors = []
    for student in Student.objects.filter(status__in=ENROLLED_STATUSES).select_related('class_assigned'):
        paid_ids = FeePayment.objects.filter(
            student=student, status__in=['PAID', 'WAIVED']
        ).values_list('fee_structure_id', flat=True)
        unpaid = FeeStructure.objects.filter(is_active=True).filter(
            Q(school_class=student.class_assigned) | Q(school_class__isnull=True)
        ).exclude(id__in=paid_ids)
        if unpaid.exists():
            from decimal import Decimal
            total = sum(f.amount for f in unpaid)
            debtors.append({'student': student, 'balance': total})
    debtors.sort(key=lambda x: x['balance'], reverse=True)

    return render(request, 'communications/fee_reminders.html', {
        'debtors': debtors,
        'total_debtors': len(debtors),
    })


# ─────────────────────────────────────────────────────────────────────────────
# MESSAGE TEMPLATES  CRUD
# ─────────────────────────────────────────────────────────────────────────────

@login_required
@_require_comm_staff
def template_list(request):
    templates = MessageTemplate.objects.all().order_by('category', 'name')
    return render(request, 'communications/template_list.html', {
        'templates': templates,
    })


@login_required
@_require_comm_staff
def template_create_edit(request, tpl_id=None):
    tpl = get_object_or_404(MessageTemplate, id=tpl_id) if tpl_id else None

    if request.method == 'POST':
        name       = request.POST.get('name', '').strip()
        category   = request.POST.get('category', 'GENERAL')
        subject    = request.POST.get('subject', '').strip()
        body_email = request.POST.get('body_email', '').strip()
        body_sms   = request.POST.get('body_sms', '').strip()

        dup = MessageTemplate.objects.filter(name__iexact=name)
        if tpl:
            dup = dup.exclude(pk=tpl.pk)
        if dup.exists():
            messages.error(request, f'A template named "{name}" already exists.')
            return redirect(request.path)

        if tpl:
            tpl.name = name; tpl.category = category; tpl.subject = subject
            tpl.body_email = body_email; tpl.body_sms = body_sms; tpl.save()
            messages.success(request, f'Template "{name}" updated.')
        else:
            tpl = MessageTemplate.objects.create(
                name=name, category=category, subject=subject,
                body_email=body_email, body_sms=body_sms,
                created_by=request.user,
            )
            messages.success(request, f'Template "{name}" created.')
        return redirect('communications:template_list')

    return render(request, 'communications/template_form.html', {
        'tpl': tpl,
        'categories': MessageTemplate.CATEGORY_CHOICES,
    })


@login_required
@_require_comm_staff
@require_POST
def template_delete(request, tpl_id):
    MessageTemplate.objects.filter(id=tpl_id).update(is_active=False)
    messages.success(request, 'Template deactivated.')
    return redirect('communications:template_list')


# ─────────────────────────────────────────────────────────────────────────────
# CONTACT AUDIT  (who is missing email / phone)
# ─────────────────────────────────────────────────────────────────────────────

@login_required
@_require_comm_staff
def contact_audit(request):
    # Get ALL students (Active + all other statuses for a full audit)
    students = Student.objects.all().select_related('class_assigned').order_by('full_name')
    active_students = students.filter(status__in=ENROLLED_STATUSES)

    # No email: empty string, null, OR whitespace-only
    def _no_email_filter(qs):
        return qs.filter(Q(parent_email='') | Q(parent_email__isnull=True))

    def _no_phone_filter(qs):
        return qs.filter(Q(parent_phone='') | Q(parent_phone__isnull=True))

    no_email     = _no_email_filter(active_students)
    no_phone     = _no_phone_filter(active_students)
    both_missing = _no_email_filter(_no_phone_filter(active_students))
    complete     = active_students.exclude(
        Q(parent_email='') | Q(parent_email__isnull=True)
    ).exclude(
        Q(parent_phone='') | Q(parent_phone__isnull=True)
    )

    # Gather post-filter lists (evaluate once)
    no_email_list     = list(no_email)
    no_phone_list     = list(no_phone)
    both_missing_list = list(both_missing)
    complete_list     = list(complete)

    # Class breakdown — for filtered view
    class_id = request.GET.get('class_id') or None
    if class_id:
        students = students.filter(class_assigned_id=class_id)
        no_email_list     = list(_no_email_filter(students.filter(status__in=ENROLLED_STATUSES)))
        no_phone_list     = list(_no_phone_filter(students.filter(status__in=ENROLLED_STATUSES)))

    from users.models import Class
    classes = Class.objects.all().order_by('name')

    return render(request, 'communications/contact_audit.html', {
        'no_email':        no_email_list,
        'no_phone':        no_phone_list,
        'both_missing':    both_missing_list,
        'complete':        complete_list,
        'total':           active_students.count(),
        'classes':         classes,
        'sel_class':       class_id,
        'no_email_count':  len(no_email_list),
        'no_phone_count':  len(no_phone_list),
    })


# ─────────────────────────────────────────────────────────────────────────────
# MESSAGE LOGS
# ─────────────────────────────────────────────────────────────────────────────

@login_required
@_require_comm_staff
def message_logs(request):
    logs = MessageLog.objects.select_related(
        'campaign', 'student'
    ).order_by('-created_at')
    channel_f = request.GET.get('channel', '')
    status_f  = request.GET.get('status', '')
    if channel_f: logs = logs.filter(channel=channel_f)
    if status_f:  logs = logs.filter(status=status_f)
    return render(request, 'communications/message_logs.html', {
        'logs': logs[:200], 'channel_f': channel_f, 'status_f': status_f,
    })


# ─────────────────────────────────────────────────────────────────────────────
# API — Get template content (AJAX)
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def get_template_content(request, tpl_id):
    if not (request.user.is_superuser or getattr(request.user, 'staff', None) is not None):
        return JsonResponse({'error': 'unauthorized'}, status=403)
    tpl = get_object_or_404(MessageTemplate, id=tpl_id)
    return JsonResponse({
        'subject':    tpl.subject,
        'body_email': tpl.body_email,
        'body_sms':   tpl.body_sms,
    })


# ─────────────────────────────────────────────────────────────────────────────
# OPT-OUT MANAGEMENT
# ─────────────────────────────────────────────────────────────────────────────

@login_required
@_require_comm_staff
def opt_outs(request):
    if request.method == 'POST':
        student_id = request.POST.get('student_id')
        channel    = request.POST.get('channel', 'ALL')
        action     = request.POST.get('action', 'add')
        student    = get_object_or_404(Student, id=student_id)
        if action == 'add':
            OptOut.objects.get_or_create(student=student, channel=channel)
            messages.success(request, f'{student.full_name} opted out of {channel}.')
        else:
            OptOut.objects.filter(student=student, channel=channel).delete()
            messages.success(request, f'{student.full_name} opt-out removed.')
        return redirect('communications:opt_outs')

    all_opt_outs = OptOut.objects.select_related('student').order_by('student__full_name')
    students     = Student.objects.filter(status__in=ENROLLED_STATUSES).order_by('full_name')
    return render(request, 'communications/opt_outs.html', {
        'opt_outs': all_opt_outs, 'students': students,
    })


# ─────────────────────────────────────────────────────────────────────────────
# HOSTEL FEE REMINDERS
# ─────────────────────────────────────────────────────────────────────────────

@login_required
@_require_comm_staff
def hostel_reminders(request):
    if request.method == 'POST':
        channel = request.POST.get('channel', 'BOTH')
        run_in_background(send_hostel_reminders, channel=channel,
                          user=request.user, tenant=request.tenant)
        messages.success(
            request,
            "Hostel fee reminders are being sent in the background. "
            "Delivery results appear in Message Logs."
        )
        return redirect('communications:dashboard')

    # Preview
    try:
        from hostel.models import HostelTermBilling
        from academics.utils import get_active_session, get_active_term
        active_session = get_active_session()
        active_term    = get_active_term()
        pending = HostelTermBilling.objects.filter(
            status__in=['UNPAID', 'PARTIAL'],
            session=active_session, term=active_term,
        ).select_related(
            'boarder__student__class_assigned',
            'boarder__bed__room__hostel'
        ) if active_session and active_term else []
    except Exception:
        pending = []

    return render(request, 'communications/hostel_reminders.html', {
        'pending': pending,
        'total':   len(list(pending)),
    })


# ─────────────────────────────────────────────────────────────────────────────
# RESULT NOTIFICATIONS  (manual trigger)
# ─────────────────────────────────────────────────────────────────────────────

@login_required
@_require_comm_staff
def result_notifications(request):
    if request.method == 'POST':
        from communications.services import send_result_notification
        from academics.utils import get_active_session, get_active_term

        channel  = request.POST.get('channel', 'BOTH')
        class_id = request.POST.get('class_id') or None
        session  = get_active_session()
        term     = get_active_term()

        qs = Student.objects.filter(status__in=ENROLLED_STATUSES).select_related('class_assigned')
        if class_id:
            qs = qs.filter(class_assigned_id=class_id)

        def notify_all(student_ids):
            for student in Student.objects.filter(id__in=student_ids):
                send_result_notification(student, term, session, channel)
            return f'{len(student_ids)} result notifications processed'

        student_ids = list(qs.values_list('id', flat=True))
        run_in_background(notify_all, student_ids, tenant=request.tenant)
        messages.success(request,
            f"Result notifications are being sent to {len(student_ids)} "
            f"parent{'' if len(student_ids) == 1 else 's'} in the background. "
            f"Delivery results appear in Message Logs.")
        return redirect('communications:dashboard')

    from users.models import Class
    classes  = Class.objects.all().order_by('name')
    return render(request, 'communications/result_notifications.html', {
        'classes': classes,
    })


# ─────────────────────────────────────────────────────────────────────────────
# SEED DEFAULT TEMPLATES
# ─────────────────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────────────────────
# EMAIL CONNECTION TEST
# ─────────────────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────────────────────
# LOGIN DETAILS NOTIFICATION
# ─────────────────────────────────────────────────────────────────────────────

@login_required
@_require_comm_staff
def login_notifications(request):
    """
    Send portal login details to parents.
    For individual students: sends username and (if a new password is being set)
    the plaintext password.
    For bulk: sends username + "contact school to reset" for existing accounts.
    """
    if request.method == 'POST':
        mode    = request.POST.get('mode', 'bulk')
        channel = request.POST.get('channel', 'EMAIL')

        if mode == 'single':
            student_id = request.POST.get('student_id')
            student = get_object_or_404(Student, id=student_id)
            result = send_login_details(
                student=student,
                student_password=None,
                parent_password=None,
                channel=channel,
            )
            if result['success']:
                messages.success(request,
                    f"Login details sent to {student.parent_name or student.full_name}'s parent.")
            else:
                messages.error(request,
                    f"Failed: {'; '.join(result['errors'])}")

        elif mode == 'bulk':
            class_id = request.POST.get('class_id') or None
            qs = Student.objects.filter(status__in=ENROLLED_STATUSES).select_related('class_assigned')
            if class_id:
                qs = qs.filter(class_assigned_id=class_id)
            total = qs.count()
            student_ids = list(qs.values_list('id', flat=True))
            run_in_background(
                lambda ids: send_bulk_login_details(
                    Student.objects.filter(id__in=ids), channel=channel),
                student_ids, tenant=request.tenant,
            )
            messages.success(request,
                f"Login details are being sent for {total} "
                f"student{'' if total == 1 else 's'} in the background. "
                f"Delivery results appear in Message Logs.")

        return redirect('communications:login_notifications')

    from users.models import Class
    classes  = Class.objects.all().order_by('name')
    students = Student.objects.filter(status__in=ENROLLED_STATUSES).select_related('class_assigned').order_by('full_name')
    recent   = QuickMessage.objects.select_related('student', 'sent_by').filter(
        subject__icontains='Login'
    ).order_by('-sent_at')[:20]
    return render(request, 'communications/login_notifications.html', {
        'classes':  classes,
        'students': students,
        'recent':   recent,
    })



# ─────────────────────────────────────────────────────────────────────────────
# DELETE MESSAGE LOG
# ─────────────────────────────────────────────────────────────────────────────

@login_required
@_require_comm_staff
@require_POST
def delete_message_log(request, log_id):
    """Delete a single message log entry."""
    from communications.models import MessageLog
    log = get_object_or_404(MessageLog, id=log_id)
    log.delete()
    messages.success(request, "Log entry deleted.")
    return redirect('communications:message_logs')


@login_required
@_require_comm_staff
@require_POST
def delete_logs_bulk(request):
    """Delete all failed logs or all logs (bulk clear)."""
    from communications.models import MessageLog
    mode = request.POST.get('mode', 'failed')
    if mode == 'failed':
        count = MessageLog.objects.filter(status='FAILED').count()
        MessageLog.objects.filter(status='FAILED').delete()
        messages.success(request, f"{count} failed log entries deleted.")
    elif mode == 'all':
        count = MessageLog.objects.count()
        MessageLog.objects.all().delete()
        messages.success(request, f"All {count} log entries cleared.")
    return redirect('communications:message_logs')


@login_required
@_require_comm_staff
def test_email(request):
    """Test SMTP connection and optionally send a test email."""
    result = None
    if request.method == 'POST':
        to_email = request.POST.get('to_email', '').strip()
        if to_email:
            from django.utils.html import escape
            from communications.services import send_email
            ok, ref = send_email(
                to_email,
                f"{request.tenant.name} — Test Email",
                f"<html><body><p>This is a test email from <strong>{escape(request.tenant.name)}</strong>.</p>"
                "<p>If you received this, your email configuration is working correctly.</p></body></html>",
            )
            result = {'sent': ok, 'to': to_email, 'ref': ref}
            if ok:
                messages.success(request, f"Test email sent to {to_email}.")
            else:
                messages.error(request, f"Test email failed: {ref}")
        else:
            # Just test connection
            conn_result = test_email_connection()
            result = {'connection': conn_result}
            if conn_result['ok']:
                messages.success(request, "SMTP connection successful!")
            else:
                messages.error(request, f"SMTP connection failed: {conn_result['detail']}")

    conn_result = test_email_connection()
    from django.conf import settings as dj_settings
    return render(request, 'communications/test_email.html', {
        'result':        result,
        'conn_status':   conn_result,
        'email_host':    getattr(dj_settings, 'EMAIL_HOST', '—'),
        'email_port':    getattr(dj_settings, 'EMAIL_PORT', '—'),
        'email_user':    getattr(dj_settings, 'EMAIL_HOST_USER', '—'),
        'from_email':    getattr(dj_settings, 'DEFAULT_FROM_EMAIL', '—'),
        'backend':       getattr(dj_settings, 'EMAIL_BACKEND', '—'),
        'termii_key':    'SET' if getattr(dj_settings, 'TERMII_API_KEY', '') else 'NOT SET',
    })


@login_required
@_require_comm_staff
@require_POST
def seed_templates(request):
    result = seed_default_templates()
    messages.success(
        request,
        f"Templates seeded: {result['created']} new templates created "
        f"({result['total']} total default templates)."
    )
    return redirect('communications:template_list')

