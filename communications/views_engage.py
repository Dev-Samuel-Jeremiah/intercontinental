"""
communications/views_engage.py
──────────────────────────────
Approvals, settings, surveys, consent forms, emergency broadcast, the
messages calendar, delivery reports, SMS delivery webhooks and the
reception TV notice board.
"""
import json
from datetime import datetime

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from users.models import Class

from . import engage as eg
from .models import Campaign, CommSettings, ConsentForm, EmergencyBroadcast, Survey
from .views import _is_comm_staff


def _deny(request, text='Only administrators can do that.'):
    messages.error(request, text)
    return redirect('dashboard:router')


def _date(raw):
    try:
        return datetime.strptime(raw or '', '%Y-%m-%d').date()
    except ValueError:
        return None


def _ip(request):
    xff = request.META.get('HTTP_X_FORWARDED_FOR', '')
    return xff.split(',')[0].strip() if xff else request.META.get('REMOTE_ADDR', '')


def _parent_children(request):
    from users.views_achievements import _parent_children as kids
    return list(kids(request))


# ─────────────────────────────────────────────────────────────────────────────
# F250 — approvals
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def approvals(request):
    if not eg.is_admin(request.user):
        return _deny(request)
    if request.method == 'POST':
        c = get_object_or_404(Campaign, pk=request.POST.get('id'), tenant=request.tenant)
        try:
            if request.POST.get('action') == 'approve':
                what = eg.approve(c, request.user)
                messages.success(request, f'"{c.title}" approved — ' +
                                 ('it will go out at the scheduled time.' if what == 'scheduled' else 'sending now.'))
            else:
                eg.reject(c, request.user, request.POST.get('note'))
                messages.success(request, f'"{c.title}" sent back to the author.')
        except eg.CommError as problem:
            messages.error(request, str(problem))
        return redirect('communications:approvals')
    pending = Campaign.objects.filter(tenant=request.tenant, status='PENDING').select_related('created_by', 'school_class')
    for c in pending:
        from .services import get_campaign_recipients
        c.reach = get_campaign_recipients(c).count()
    recent = Campaign.objects.filter(tenant=request.tenant, approved_by__isnull=False).select_related(
        'created_by', 'approved_by').order_by('-updated_at')[:15]
    return render(request, 'communications/approvals.html', {'pending': pending, 'recent': recent})


# ─────────────────────────────────────────────────────────────────────────────
# Settings (approval, birthdays, notice board)
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def comm_settings(request):
    if not eg.is_admin(request.user):
        return _deny(request)
    cfg = eg.settings_for(request.tenant)
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'renew':
            eg.signage_token(request.tenant, renew=True)
            messages.success(request, 'New notice board link made — the old one no longer works.')
        elif action == 'birthdays':
            n = eg.send_birthdays(request.tenant)
            messages.success(request, f"Greeted {n['students']} student(s) and {n['staff']} member(s) of staff.")
        else:
            cfg.require_approval = bool(request.POST.get('require_approval'))
            cfg.birthday_students = bool(request.POST.get('birthday_students'))
            cfg.birthday_staff = bool(request.POST.get('birthday_staff'))
            cfg.birthday_student_sms = (request.POST.get('birthday_student_sms') or cfg.birthday_student_sms)[:320]
            cfg.birthday_staff_sms = (request.POST.get('birthday_staff_sms') or cfg.birthday_staff_sms)[:320]
            cfg.whatsapp_enabled = bool(request.POST.get('whatsapp_enabled'))
            cfg.whatsapp_template = (request.POST.get('whatsapp_template') or '').strip()[:80]
            cfg.whatsapp_language = (request.POST.get('whatsapp_language') or 'en').strip()[:10]
            if cfg.whatsapp_enabled and not cfg.whatsapp_template:
                cfg.whatsapp_enabled = False
                messages.error(request, 'Give the name of your approved WhatsApp template to switch WhatsApp on.')
            secs = str(request.POST.get('signage_seconds') or '')
            cfg.signage_seconds = min(120, max(4, int(secs))) if secs.isdigit() else cfg.signage_seconds
            cfg.save()
            messages.success(request, 'Saved.')
        return redirect('communications:settings')
    token = eg.signage_token(request.tenant)
    students, staff = eg.birthdays_today(request.tenant)
    return render(request, 'communications/settings.html', {
        'cfg': cfg, 'students': students, 'staff': staff, 'wa_ready': _wa_ready(),
        'signage_url': request.build_absolute_uri(f'/communications/signage/{token}/')})


def _wa_ready():
    from .connect import wa_configured
    return wa_configured()


# ─────────────────────────────────────────────────────────────────────────────
# F252 — surveys
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def surveys(request):
    admin = eg.is_admin(request.user)
    if request.method == 'POST' and admin:
        try:
            s = eg.create_survey(request.tenant, request.user, request.POST.get('title'),
                                 request.POST.get('description'), request.POST.get('audience'),
                                 request.POST.get('anonymous'), _date(request.POST.get('closes')),
                                 request.POST.get('questions'))
            if request.POST.get('invite'):
                from lms_project.tasks import run_in_background
                run_in_background(eg.invite, s, request.tenant, tenant=request.tenant)
            messages.success(request, f'"{s.title}" is open for answers.')
            return redirect('communications:survey_results', pk=s.pk)
        except eg.CommError as problem:
            messages.error(request, str(problem))
            return render(request, 'communications/surveys.html', _surveys_ctx(request, admin, request.POST))
    if getattr(request.user, 'is_parent', False) and not _is_comm_staff(request.user):
        return render(request, 'communications/surveys_parent.html',
                      {'todo': eg.open_surveys(request.user, request.tenant), 'page_title': 'Surveys'})
    return render(request, 'communications/surveys.html', _surveys_ctx(request, admin))


def _surveys_ctx(request, admin, form=None):
    items = list(Survey.objects.filter(tenant=request.tenant)) if admin else []
    for s in items:
        s.count = s.responses.count()
    return {'items': items, 'admin': admin, 'todo': eg.open_surveys(request.user, request.tenant),
            'audiences': Survey.AUDIENCES, 'form': form or {}, 'today': timezone.localdate(),
            'example': 'How satisfied are you with communication from the school? | Very satisfied | Satisfied | '
                       'Not satisfied\n[multi] Which clubs should we add next term? | Chess | Coding | Drama | Football\n'
                       '[rating] How would you rate the end-of-term concert?\n'
                       '[text] Anything else you would like to tell us?'}


@login_required
def survey_answer(request, pk):
    s = get_object_or_404(Survey, pk=pk, tenant=request.tenant)
    done = s.responses.filter(user=request.user).exists()
    if not eg.may_answer(s, request.user):
        raise Http404
    if request.method == 'POST' and not done:
        try:
            eg.answer(s, request.user, request.POST)
            done = True
            messages.success(request, 'Thank you — your answers have been recorded.')
        except eg.CommError as problem:
            messages.error(request, str(problem))
    rows = [{'i': i, **q} for i, q in enumerate(s.questions)]
    return render(request, 'communications/survey_answer.html', {
        's': s, 'rows': rows, 'done': done, 'open': eg.is_open(s), 'page_title': s.title,
        'parent': getattr(request.user, 'is_parent', False)})


@login_required
def survey_results(request, pk):
    s = get_object_or_404(Survey, pk=pk, tenant=request.tenant)
    if not (eg.is_admin(request.user) or s.created_by_id == request.user.pk):
        return _deny(request)
    if request.method == 'POST':
        if request.POST.get('action') == 'close':
            s.published = not s.published
            s.save(update_fields=['published'])
        elif request.POST.get('action') == 'invite':
            from lms_project.tasks import run_in_background
            run_in_background(eg.invite, s, request.tenant, tenant=request.tenant)
            messages.success(request, 'Invitations are being emailed.')
        return redirect('communications:survey_results', pk=s.pk)
    fmt = request.GET.get('export')
    if fmt in ('csv', 'xlsx'):
        from analytics.export import respond
        header, rows = eg.export_rows(s)
        return respond(fmt, s.title, header, rows, request.tenant.name)
    return render(request, 'communications/survey_results.html', {'s': s, 'r': eg.results(s), 'open': eg.is_open(s)})


# ─────────────────────────────────────────────────────────────────────────────
# F253 — consent forms
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def consent_manage(request):
    if not _is_comm_staff(request.user):
        return _deny(request, 'This page is for staff.')
    from curriculum.models import CalendarEvent
    if request.method == 'POST':
        try:
            event = CalendarEvent.objects.filter(tenant=request.tenant, pk=request.POST.get('event') or 0).first()
            f = eg.create_consent(request.tenant, request.user, request.POST.get('title'), request.POST.get('details'),
                                  _date(request.POST.get('deadline')), request.POST.get('cost'),
                                  request.POST.getlist('classes'), event)
            if request.POST.get('notify'):
                from lms_project.tasks import run_in_background
                run_in_background(eg.notify_consent, f.pk, channel=request.POST.get('channel') or 'BOTH',
                                  tenant=request.tenant)
            messages.success(request, f'"{f.title}" created' + (' and parents are being asked.' if request.POST.get('notify') else '.'))
            return redirect('communications:consent_detail', pk=f.pk)
        except eg.CommError as problem:
            messages.error(request, str(problem))
    items = list(ConsentForm.objects.filter(tenant=request.tenant).prefetch_related('classes'))
    for f in items:
        f.summary = eg.consent_summary(f)
    return render(request, 'communications/consent_manage.html', {
        'items': items, 'classes': Class.objects.filter(tenant=request.tenant).order_by('name'),
        'events': CalendarEvent.objects.filter(tenant=request.tenant, start_date__gte=timezone.localdate())
        .order_by('start_date')[:40], 'today': timezone.localdate()})


@login_required
def consent_detail(request, pk):
    if not _is_comm_staff(request.user):
        return _deny(request, 'This page is for staff.')
    f = get_object_or_404(ConsentForm, pk=pk, tenant=request.tenant)
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'remind':
            from lms_project.tasks import run_in_background
            run_in_background(eg.notify_consent, f.pk, reminder=True, channel=request.POST.get('channel') or 'BOTH',
                              tenant=request.tenant)
            messages.success(request, 'Reminders are going to parents who have not replied.')
        elif action == 'close':
            f.closed = not f.closed
            f.save(update_fields=['closed'])
        return redirect('communications:consent_detail', pk=f.pk)
    summary = eg.consent_summary(f)
    fmt = request.GET.get('export')
    if fmt in ('csv', 'xlsx'):
        from analytics.export import respond
        rows = [[r['student'].full_name, r['student'].admission_number, str(r['student'].class_assigned or ''),
                 r['reply'].get_answer_display() if r['reply'] else 'No reply',
                 r['reply'].signed_name if r['reply'] else '', r['reply'].note if r['reply'] else '',
                 timezone.localtime(r['reply'].created_at).strftime('%Y-%m-%d %H:%M') if r['reply'] else '']
                for r in summary['rows']]
        return respond(fmt, f.title, ['Student', 'Admission no.', 'Class', 'Answer', 'Signed by', 'Note', 'When'],
                       rows, request.tenant.name)
    return render(request, 'communications/consent_detail.html', {'f': f, 'sm': summary, 'open': eg.consent_open(f)})


@login_required
def consent_parent(request):
    children = _parent_children(request)
    if not children:
        return _deny(request, 'This page is for parents.')
    if request.method == 'POST':
        f = get_object_or_404(ConsentForm, pk=request.POST.get('form'), tenant=request.tenant)
        child = next((c for c in children if str(c.pk) == request.POST.get('student')), None)
        try:
            if not child:
                raise eg.CommError('Choose one of your children.')
            eg.give_consent(f, child, request.user, request.POST.get('answer'), request.POST.get('signed_name'),
                            request.POST.get('note'), _ip(request))
            messages.success(request, f'Thank you — your answer for {child.full_name} has been recorded.')
        except eg.CommError as problem:
            messages.error(request, str(problem))
        return redirect('communications:consent_parent')
    cards = []
    for f in ConsentForm.objects.filter(tenant=request.tenant).order_by('deadline'):
        kids = [c for c in children if eg.consent_students(f).filter(pk=c.pk).exists()]
        if not kids:
            continue
        replies = {r.student_id: r for r in f.responses.filter(student__in=kids)}
        if not eg.consent_open(f) and not replies:
            continue
        cards.append({'f': f, 'open': eg.consent_open(f), 'kids': [(c, replies.get(c.pk)) for c in kids]})
    return render(request, 'communications/consent_parent.html', {'cards': cards, 'page_title': 'Consent forms'})


# ─────────────────────────────────────────────────────────────────────────────
# F249 — emergency broadcast
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def emergency(request):
    if not eg.is_admin(request.user):
        return _deny(request)
    if request.method == 'POST':
        if (request.POST.get('confirm') or '').strip().upper() != 'SEND':
            messages.error(request, 'Type SEND in the box to confirm.')
        else:
            try:
                eg.start_broadcast(request.tenant, request.user, request.POST.get('subject'),
                                   request.POST.get('message'), bool(request.POST.get('to_parents')),
                                   bool(request.POST.get('to_staff')))
                messages.success(request, 'Emergency broadcast is going out by SMS and email.')
            except eg.CommError as problem:
                messages.error(request, str(problem))
        return redirect('communications:emergency')
    return render(request, 'communications/emergency.html', {
        'history': EmergencyBroadcast.objects.filter(tenant=request.tenant).select_related('sent_by')[:20],
        'limit': eg.SMS_LIMIT})


# ─────────────────────────────────────────────────────────────────────────────
# F260 — calendar
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def messages_calendar(request):
    if not _is_comm_staff(request.user):
        return _deny(request, 'This page is for staff.')
    today = timezone.localdate()
    try:
        year, month = int(request.GET.get('y', today.year)), int(request.GET.get('m', today.month))
        if not 1 <= month <= 12:
            raise ValueError
    except ValueError:
        year, month = today.year, today.month
    prev = (year - 1, 12) if month == 1 else (year, month - 1)
    nxt = (year + 1, 1) if month == 12 else (year, month + 1)
    weeks = eg.month_grid(year, month, eg.month_items(request.tenant, year, month))
    return render(request, 'communications/calendar.html', {
        'weeks': weeks, 'title': datetime(year, month, 1).strftime('%B %Y'), 'prev': prev, 'next': nxt,
        'today': today, 'days': ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']})


# ─────────────────────────────────────────────────────────────────────────────
# F247 — delivery report and provider webhook
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def campaign_report(request, campaign_id):
    if not _is_comm_staff(request.user):
        return _deny(request, 'This page is for staff.')
    c = get_object_or_404(Campaign, pk=campaign_id, tenant=request.tenant)
    if request.method == 'POST':
        ok, bad = eg.retry_failed(c)
        messages.success(request, f'Re-sent {ok}; {bad} still failing.')
        return redirect('communications:campaign_report', campaign_id=c.pk)
    rep = eg.report(c)
    fmt = request.GET.get('export')
    if fmt in ('csv', 'xlsx'):
        from analytics.export import respond
        rows = [[log.student.full_name, log.channel, log.recipient, log.get_status_display(),
                 timezone.localtime(log.sent_at).strftime('%Y-%m-%d %H:%M') if log.sent_at else '', log.error_message]
                for log in rep['logs']]
        return respond(fmt, f'{c.title} delivery', ['Student', 'Channel', 'To', 'Status', 'Sent', 'Problem'], rows,
                       request.tenant.name)
    return render(request, 'communications/campaign_report.html', {'c': c, 'rep': rep})


@csrf_exempt
def delivery_webhook(request, token):
    import hmac
    expected = getattr(settings, 'SMS_DLR_TOKEN', '')
    if not expected or not hmac.compare_digest(token.encode(), expected.encode()):
        raise Http404
    if request.content_type == 'application/json':
        try:
            payload = json.loads(request.body or b'{}')
        except ValueError:
            payload = {}
    else:
        payload = request.POST.dict() or request.GET.dict()
    items = payload if isinstance(payload, list) else [payload]
    n = sum(eg.apply_dlr(p) for p in items if isinstance(p, dict))
    return JsonResponse({'updated': n})


# ─────────────────────────────────────────────────────────────────────────────
# F256 — reception TV notice board (no sign-in; secret link)
# ─────────────────────────────────────────────────────────────────────────────

def signage(request, token):
    if not getattr(request, 'tenant', None):
        raise Http404
    cfg = CommSettings.objects.filter(tenant=request.tenant).first()
    if not cfg or not cfg.signage_token or token != cfg.signage_token:
        raise Http404
    if request.GET.get('format') == 'json':
        return JsonResponse({'slides': eg.signage_slides(request.tenant)})
    response = render(request, 'communications/signage.html', {
        'school': request.tenant, 'slides': eg.signage_slides(request.tenant), 'seconds': cfg.signage_seconds})
    response['X-Robots-Tag'] = 'noindex'
    return response

