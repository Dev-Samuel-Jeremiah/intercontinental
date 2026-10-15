"""
communications/views_connect.py
───────────────────────────────
Family preferences, template translations, parent–teacher chat and its
moderation, announcement read receipts, SMS units, the reply inbox, and
the SMS / WhatsApp webhooks.
"""
import json
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from users.models import Staff

from . import connect as cn
from .engage import is_admin, settings_for
from .models import (LANGUAGES, ChatMessage, Conversation, FamilyPreference, InboundMessage, MessageTemplate,
                     SmsTopUp, SmsUsageDay)
from .views import _is_comm_staff


def _deny(request, text='Only administrators can do that.'):
    messages.error(request, text)
    return redirect('dashboard:router')


def _children(request):
    from users.views_achievements import _parent_children
    return list(_parent_children(request))


def _me(request):
    return Staff.objects.filter(user=request.user, tenant=request.tenant).first()


# ─────────────────────────────────────────────────────────────────────────────
# F255 — family preferences
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def preferences(request):
    children = _children(request)
    if not children:
        return _deny(request, 'This page is for parents.')
    if request.method == 'POST':
        try:
            cn.save_preferences(children, request.user, request.POST.get('language'), request.POST.get('channel'),
                                request.POST.getlist('topics'))
            messages.success(request, 'Your choices are saved.')
        except cn.ConnectError as problem:
            messages.error(request, str(problem))
        return redirect('communications:preferences')
    pref = cn.preference_for(children[0])
    return render(request, 'communications/preferences.html', {
        'page_title': 'Messages from school', 'children': children, 'languages': LANGUAGES,
        'language': pref.language if pref else 'en', 'channel': cn.channel_for(children[0]),
        'topics': [(k, label, not (pref and k in pref.topics_off)) for k, label in FamilyPreference.OPTIONAL_TOPICS]})


# ─────────────────────────────────────────────────────────────────────────────
# F251 — template translations
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def template_translations(request, tpl_id):
    if not _is_comm_staff(request.user):
        return _deny(request, 'This page is for staff.')
    tpl = get_object_or_404(MessageTemplate, pk=tpl_id, tenant=request.tenant)
    if request.method == 'POST':
        lang = request.POST.get('language')
        try:
            if request.POST.get('action') == 'ai':
                from assistant.providers import AIError
                try:
                    cn.draft_translation(tpl, lang, request.tenant, request.user)
                    messages.success(request, f'Draft {dict(LANGUAGES).get(lang)} translation ready — please check it '
                                              f'with a fluent speaker before using it.')
                except AIError as problem:
                    messages.error(request, str(problem))
            elif request.POST.get('action') == 'delete':
                tpl.translations.filter(language=lang).delete()
                messages.success(request, 'Translation removed.')
            else:
                cn.save_translation(tpl, lang, request.POST, request.user)
                messages.success(request, 'Translation saved.')
        except cn.ConnectError as problem:
            messages.error(request, str(problem))
        return redirect('communications:template_translations', tpl_id=tpl.pk)
    have = {t.language: t for t in tpl.translations.all()}
    rows = [{'code': c, 'name': n, 't': have.get(c)} for c, n in LANGUAGES if c != 'en']
    families = FamilyPreference.objects.filter(tenant=request.tenant).exclude(language='en')
    counts = {}
    for f in families:
        counts[f.language] = counts.get(f.language, 0) + 1
    for r in rows:
        r['families'] = counts.get(r['code'], 0)
    return render(request, 'communications/template_translations.html', {'tpl': tpl, 'rows': rows})


# ─────────────────────────────────────────────────────────────────────────────
# F242 — chat
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def chat(request):
    children = _children(request)
    me = _me(request)
    if children:
        if request.method == 'POST':
            child = next((c for c in children if str(c.pk) == request.POST.get('student')), None)
            teacher = Staff.objects.filter(tenant=request.tenant, pk=request.POST.get('teacher') or 0).first()
            try:
                if not child or not teacher:
                    raise cn.ConnectError('Choose your child and the teacher.')
                conv = cn.start_conversation(request.user, child, teacher, request.POST.get('subject'),
                                             request.POST.get('body'))
                return redirect('communications:chat_thread', pk=conv.pk)
            except cn.ConnectError as problem:
                messages.error(request, str(problem))
            return redirect('communications:chat')
        convs = Conversation.objects.filter(tenant=request.tenant, parent=request.user).select_related(
            'teacher', 'student')
        for c in convs:
            c.unread = c.messages.filter(read_at__isnull=True, hidden=False).exclude(sender=request.user).count()
        return render(request, 'communications/chat_parent.html', {
            'page_title': 'Talk to a teacher', 'convs': convs,
            'choices': [(c, cn.chat_teachers(c)) for c in children], 'hours': cn.office_hours_text(request.tenant),
            'enabled': settings_for(request.tenant).chat_enabled})
    if not me:
        return _deny(request, 'Chat is for parents and teachers.')
    convs = Conversation.objects.filter(tenant=request.tenant, teacher=me).select_related('student', 'parent')
    for c in convs:
        c.unread = c.messages.filter(read_at__isnull=True, hidden=False).exclude(sender=request.user).count()
    return render(request, 'communications/chat_staff.html', {'convs': convs, 'admin': is_admin(request.user),
                                                              'hours': cn.office_hours_text(request.tenant)})


@login_required
def chat_thread(request, pk):
    conv = get_object_or_404(Conversation.objects.select_related('teacher__user', 'student', 'parent'), pk=pk,
                             tenant=request.tenant)
    member = cn.is_participant(conv, request.user)
    admin = is_admin(request.user)
    if not (member or admin):
        raise Http404
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action == 'send' and member:
                cn.post(conv, request.user, request.POST.get('body'))
            elif action == 'flag' and member:
                m = get_object_or_404(ChatMessage, pk=request.POST.get('id'), conversation=conv)
                cn.flag(m, request.user, request.POST.get('reason'))
                messages.success(request, 'Reported to the school administrators.')
            elif action in ('hide', 'unhide') and admin:
                m = get_object_or_404(ChatMessage, pk=request.POST.get('id'), conversation=conv)
                cn.hide(m, request.user, action == 'hide')
            elif action == 'close' and (member or admin):
                conv.closed = not conv.closed
                conv.save(update_fields=['closed'])
        except cn.ConnectError as problem:
            messages.error(request, str(problem))
        return redirect('communications:chat_thread', pk=conv.pk)
    if member:
        cn.mark_read(conv, request.user)
    msgs = list(conv.messages.select_related('sender'))
    ctx = {'conv': conv, 'msgs': msgs, 'member': member, 'admin': admin, 'page_title': conv.subject,
           'hours': cn.office_hours_text(request.tenant), 'open_now': cn.in_office_hours(request.tenant)}
    if request.user.pk == conv.parent_id:
        return render(request, 'communications/chat_thread_parent.html', ctx)
    return render(request, 'communications/chat_thread.html', ctx)


@login_required
def chat_moderate(request):
    if not is_admin(request.user):
        return _deny(request)
    cfg = settings_for(request.tenant)
    if request.method == 'POST':
        from datetime import datetime
        cfg.chat_enabled = bool(request.POST.get('chat_enabled'))
        cfg.office_days = ','.join(d for d in cn.DAY_CODES if d in request.POST.getlist('days')) or cfg.office_days
        for f in ('office_start', 'office_end'):
            try:
                setattr(cfg, f, datetime.strptime(request.POST.get(f) or '', '%H:%M').time())
            except ValueError:
                pass
        if cfg.office_end <= cfg.office_start:
            messages.error(request, 'Office hours must end after they start.')
        else:
            cfg.save()
            messages.success(request, 'Chat settings saved.')
        return redirect('communications:chat_moderate')
    return render(request, 'communications/chat_moderate.html', {
        'cfg': cfg, 'days': cn.DAY_CODES, 'on_days': cfg.office_days.split(','),
        'flagged': ChatMessage.objects.filter(tenant=request.tenant, flagged=True, hidden=False)
        .select_related('conversation__student', 'sender'),
        'convs': Conversation.objects.filter(tenant=request.tenant).select_related('teacher', 'student', 'parent')[:60]})


# ─────────────────────────────────────────────────────────────────────────────
# F259 — announcement read receipts
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def announcement_reads(request, pk):
    if not _is_comm_staff(request.user):
        return _deny(request, 'This page is for staff.')
    from announcement.models import Announcement
    ann = get_object_or_404(Announcement, pk=pk, tenant=request.tenant)
    if request.method == 'POST':
        from lms_project.tasks import run_in_background
        run_in_background(cn.remind_unread, ann.pk, user=request.user, channel=request.POST.get('channel') or 'BOTH',
                          tenant=request.tenant)
        messages.success(request, 'Reminders are going to families who have not read it.')
        return redirect('communications:announcement_reads', pk=ann.pk)
    return render(request, 'communications/announcement_reads.html', {
        'ann': ann, 'rep': cn.read_report(ann), 'reminders': ann.reminders.select_related('sent_by')[:10]})


# ─────────────────────────────────────────────────────────────────────────────
# F246 — SMS units
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def sms_units(request):
    if not is_admin(request.user):
        return _deny(request)
    if request.method == 'POST':
        units = str(request.POST.get('units') or '')
        try:
            url = cn.start_topup(request.tenant, request.user, int(units) if units.isdigit() else 0,
                                 request.build_absolute_uri('/communications/sms-units/callback/'))
            return redirect(url)
        except cn.ConnectError as problem:
            messages.error(request, str(problem))
        return redirect('communications:sms_units')
    w = cn.wallet(request.tenant)
    since = timezone.localdate() - timedelta(days=29)
    days = list(SmsUsageDay.objects.filter(tenant=request.tenant, date__gte=since))
    peak = max([d.units for d in days] + [1])
    price = cn.unit_price()
    return render(request, 'communications/sms_units.html', {
        'w': w, 'days': days, 'peak': peak, 'month_units': sum(d.units for d in days),
        'bundles': [(b, price * b) for b in cn.bundles()], 'price': price, 'metering': cn.metering_on(),
        'history': SmsTopUp.objects.filter(tenant=request.tenant)[:20]})


@login_required
def sms_units_callback(request):
    if not is_admin(request.user):
        return _deny(request)
    ref = request.GET.get('reference') or request.GET.get('trxref') or ''
    top = SmsTopUp.objects.filter(tenant=request.tenant, reference=ref).first()
    if not top:
        messages.error(request, 'That payment was not found.')
        return redirect('communications:sms_units')
    try:
        top = cn.verify_topup(ref)
        if top.status == SmsTopUp.PAID:
            messages.success(request, f'Payment confirmed — {top.units:,} SMS units added.')
        else:
            messages.error(request, 'The payment was not completed. No units were added.')
    except cn.ConnectError as problem:
        messages.error(request, str(problem))
    return redirect('communications:sms_units')


# ─────────────────────────────────────────────────────────────────────────────
# F258 — inbox
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def inbox(request):
    if not _is_comm_staff(request.user):
        return _deny(request, 'This page is for staff.')
    if request.method == 'POST':
        msg = get_object_or_404(InboundMessage, pk=request.POST.get('id'), tenant=request.tenant)
        try:
            if request.POST.get('action') == 'reply':
                r = cn.reply(msg, request.user, request.POST.get('body'))
                if r.ok:
                    messages.success(request, 'Reply sent.')
                else:
                    messages.error(request, f'The reply could not be sent: {r.error}')
            else:
                msg.handled = not msg.handled
                msg.handled_by, msg.handled_at = (request.user, timezone.now()) if msg.handled else (None, None)
                msg.save(update_fields=['handled', 'handled_by', 'handled_at'])
        except cn.ConnectError as problem:
            messages.error(request, str(problem))
        return redirect(request.get_full_path())
    show = request.GET.get('show', 'open')
    items = InboundMessage.objects.filter(tenant=request.tenant).select_related('student', 'handled_by') \
        .prefetch_related('replies')
    if show == 'open':
        items = items.filter(handled=False)
    return render(request, 'communications/inbox.html', {
        'items': items[:100], 'show': show,
        'open_count': InboundMessage.objects.filter(tenant=request.tenant, handled=False).count()})


# ─────────────────────────────────────────────────────────────────────────────
# Webhooks (work on the main domain and on school addresses)
# ─────────────────────────────────────────────────────────────────────────────

@csrf_exempt
def sms_inbound(request, token):
    expected = getattr(settings, 'SMS_INBOUND_TOKEN', '')
    if not expected or token != expected:
        raise Http404
    data = request.POST.dict() or request.GET.dict()
    if not data and request.body:
        try:
            data = json.loads(request.body)
        except ValueError:
            data = {}
    sender, text, ref = cn.parse_sms_inbound(data)
    tenant = getattr(request, 'tenant', None) or cn.route_tenant(sender)
    if tenant:
        from tenants.middleware import set_current_tenant
        set_current_tenant(tenant)
    msg = cn.receive(tenant, 'SMS', sender, text, ref)
    return JsonResponse({'stored': bool(msg)})


@csrf_exempt
def whatsapp_hook(request):
    if request.method == 'GET':
        token = getattr(settings, 'WHATSAPP_VERIFY_TOKEN', '')
        if token and request.GET.get('hub.mode') == 'subscribe' and request.GET.get('hub.verify_token') == token:
            return HttpResponse(request.GET.get('hub.challenge', ''), content_type='text/plain')
        return HttpResponseForbidden('Verification failed')
    if not cn.check_signature(request.body, request.headers.get('X-Hub-Signature-256')):
        return HttpResponseForbidden('Bad signature')
    try:
        payload = json.loads(request.body or b'{}')
    except ValueError:
        return JsonResponse({'stored': 0})
    from tenants.middleware import clear_current_tenant, set_current_tenant

    from .engage import apply_dlr
    msgs, statuses = cn.parse_whatsapp(payload)
    stored = 0
    for sender, text, ref in msgs:
        tenant = getattr(request, 'tenant', None) or cn.route_tenant(sender)
        if not tenant:
            continue
        set_current_tenant(tenant)
        try:
            stored += 1 if cn.receive(tenant, 'WA', sender, text, ref) else 0
        finally:
            if not getattr(request, 'tenant', None):
                clear_current_tenant()
    for ref, status in statuses:
        apply_dlr({'message_id': f'wa:{ref}', 'status': status})
    return JsonResponse({'stored': stored})
