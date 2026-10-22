"""
communications/connect.py
─────────────────────────
Communications, part 2:

    F255  family preferences — language, email/SMS, optional topics
    F251  message templates in several languages (AI can draft them)
    F242  parent–teacher chat with office hours and moderation
    F259  who has read an announcement, and reminders for the rest
    F246  SMS units: metered per school, topped up through Paystack
    F241  WhatsApp Business (Meta Cloud API) first, SMS as fallback
    F258  a shared inbox for SMS and WhatsApp replies
"""
import hashlib
import hmac
import logging
import math
import re
import uuid
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone
from django.utils.html import escape

from users.models import ENROLLED_STATUSES, Guardian, Staff, StaffSubjectClass, Student

from .models import (AnnouncementReminder, ChatMessage, Conversation, FamilyPreference, InboundMessage, InboxReply,
                     MessageTemplateTranslation, OptOut, SmsTopUp, SmsUsageDay, SmsWallet)

logger = logging.getLogger(__name__)


class ConnectError(ValueError):
    pass


def _email(to, subject, html):
    from .services import send_email
    return send_email(to, subject, html)


def _phone_key(p):
    from users.records import _phone_key as key
    return key(p)


# ─────────────────────────────────────────────────────────────────────────────
# F255 — family preferences
# ─────────────────────────────────────────────────────────────────────────────

def family_key(student):
    return (student.parent_email or '').strip().lower() or f'student:{student.pk}'


def preference_for(student):
    return FamilyPreference.objects.filter(tenant_id=student.tenant_id, key=family_key(student)).first()


def wants_topic(student, topic):
    pref = preference_for(student)
    return not (pref and topic in (pref.topics_off or []))


def language_for(student):
    pref = preference_for(student)
    return pref.language if pref else 'en'


def channel_for(student):
    outs = set(OptOut.objects.filter(student=student).values_list('channel', flat=True))
    if 'SMS' in outs and 'EMAIL' not in outs:
        return 'EMAIL'
    if 'EMAIL' in outs and 'SMS' not in outs:
        return 'SMS'
    return 'BOTH'


def save_preferences(children, user, language, channel, topics_on):
    from .models import LANGUAGES
    if language not in dict(LANGUAGES):
        raise ConnectError('Choose a language.')
    if channel not in ('BOTH', 'EMAIL', 'SMS'):
        raise ConnectError('Choose how the school should reach you.')
    optional = [k for k, _ in FamilyPreference.OPTIONAL_TOPICS]
    off = [t for t in optional if t not in set(topics_on or [])]
    for key in {family_key(c) for c in children}:
        tenant_id = children[0].tenant_id
        FamilyPreference.objects.update_or_create(tenant_id=tenant_id, key=key,
                                                  defaults={'language': language, 'topics_off': off,
                                                            'updated_by': user})
    for child in children:
        OptOut.objects.filter(student=child).delete()
        if channel == 'EMAIL':
            OptOut.objects.create(tenant_id=child.tenant_id, student=child, channel='SMS',
                                  reason='Parent chose email only')
        elif channel == 'SMS':
            OptOut.objects.create(tenant_id=child.tenant_id, student=child, channel='EMAIL',
                                  reason='Parent chose SMS only')


# ─────────────────────────────────────────────────────────────────────────────
# F251 — templates in several languages
# ─────────────────────────────────────────────────────────────────────────────

def _same(a, b):
    return ' '.join((a or '').split()) == ' '.join((b or '').split())


def texts_for(campaign, student):
    """(subject, email body, sms body) in the family's language when the campaign uses a translated template unchanged."""
    out = (campaign.subject, campaign.body_email, campaign.body_sms)
    tpl = campaign.template
    if not tpl:
        return out
    lang = language_for(student)
    if lang == 'en':
        return out
    tr = MessageTemplateTranslation.objects.filter(template=tpl, language=lang).first()
    if not tr:
        return out
    subject = tr.subject if tr.subject and _same(campaign.subject, tpl.subject) else campaign.subject
    email = tr.body_email if tr.body_email and _same(campaign.body_email, tpl.body_email) else campaign.body_email
    sms = tr.body_sms if tr.body_sms and _same(campaign.body_sms, tpl.body_sms) else campaign.body_sms
    return subject, email, sms


def save_translation(template, language, data, user, ai=False):
    from .models import LANGUAGES
    if language not in dict(LANGUAGES) or language == 'en':
        raise ConnectError('Choose a language other than English.')
    tr, _ = MessageTemplateTranslation.objects.update_or_create(
        template=template, language=language,
        defaults={'tenant_id': template.tenant_id, 'subject': (data.get('subject') or '').strip()[:300],
                  'body_email': (data.get('body_email') or '').strip(),
                  'body_sms': (data.get('body_sms') or '').strip()[:800], 'updated_by': user, 'ai_drafted': ai})
    return tr


def draft_translation(template, language, tenant, user):
    """Ask the school's AI for a translation of the SMS and email. Raises assistant AIError."""
    from assistant.writers import translate
    subject, sms = translate(tenant, user, template.subject, template.body_sms or template.subject, language)
    out = {'subject': subject, 'body_sms': sms if template.body_sms else ''}
    if template.body_email:
        _s, email = translate(tenant, user, template.subject, template.body_email, language)
        out['body_email'] = email
    return save_translation(template, language, out, user, ai=True)


# ─────────────────────────────────────────────────────────────────────────────
# F242 — parent–teacher chat
# ─────────────────────────────────────────────────────────────────────────────

DAY_CODES = ['MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT', 'SUN']


def chat_teachers(student):
    ids = set()
    cls = student.class_assigned
    if cls:
        ids.update(cls.form_teachers.values_list('pk', flat=True))
    if cls:
        ids.update(StaffSubjectClass.objects.filter(school_class=cls).values_list('staff_id', flat=True))
    return list(Staff.objects.filter(pk__in=ids, user__is_active=True).order_by('full_name'))


def in_office_hours(tenant, when=None):
    from .engage import settings_for
    cfg = settings_for(tenant)
    when = timezone.localtime(when or timezone.now())
    days = [d.strip() for d in cfg.office_days.split(',') if d.strip()]
    return DAY_CODES[when.weekday()] in days and cfg.office_start <= when.time() < cfg.office_end


def office_hours_text(tenant):
    from .engage import settings_for
    cfg = settings_for(tenant)
    days = [d.strip().title() for d in cfg.office_days.split(',') if d.strip()]
    span = f'{days[0]}–{days[-1]}' if len(days) > 2 else ', '.join(days)
    return f'{span}, {cfg.office_start:%H:%M}–{cfg.office_end:%H:%M}'


def is_participant(conv, user):
    return conv.parent_id == user.pk or conv.teacher.user_id == user.pk


def start_conversation(parent, student, teacher, subject, body):
    from .engage import settings_for
    if not settings_for(student.tenant).chat_enabled:
        raise ConnectError('The school has turned chat off.')
    if teacher not in chat_teachers(student):
        raise ConnectError(f'{teacher.full_name} does not teach {student.full_name}.')
    subject = (subject or '').strip()
    if not subject:
        raise ConnectError('Give your message a subject.')
    conv = Conversation.objects.create(tenant_id=student.tenant_id, student=student, teacher=teacher, parent=parent,
                                       subject=subject[:160])
    post(conv, parent, body)
    return conv


def post(conv, user, body):
    body = (body or '').strip()
    if not body:
        raise ConnectError('Write a message.')
    if len(body) > 4000:
        raise ConnectError('Keep the message under 4,000 characters.')
    if not is_participant(conv, user):
        raise ConnectError('You are not part of this conversation.')
    if conv.closed:
        raise ConnectError('This conversation is closed. Start a new one if you need to.')
    msg = ChatMessage.objects.create(tenant_id=conv.tenant_id, conversation=conv, sender=user, body=body)
    conv.last_at = msg.created_at
    conv.save(update_fields=['last_at'])
    _notify(conv, user)
    return msg


def _notify(conv, sender):
    url = f'{conv.tenant.portal_url.rstrip("/")}/communications/chat/{conv.pk}/'
    if sender.pk == conv.parent_id:
        to = conv.teacher.user.email
        # Teachers are not disturbed outside office hours; they see the message when they sign in.
        if to and in_office_hours(conv.tenant):
            _email(to, f'Message from a parent: {conv.subject}',
                   f'<p>A parent of <b>{escape(conv.student.full_name)}</b> sent you a message.</p>'
                   f'<p><a href="{url}">Read and reply</a></p>')
    else:
        to = conv.parent.email or conv.student.parent_email
        if to:
            _email(to, f'Reply from {conv.teacher.full_name}: {conv.subject}',
                   f'<p>{escape(conv.teacher.full_name)} replied about <b>{escape(conv.student.full_name)}</b>.</p>'
                   f'<p><a href="{url}">Read the reply</a></p>')


def mark_read(conv, user):
    conv.messages.filter(read_at__isnull=True).exclude(sender=user).update(read_at=timezone.now())


def unread_for(user, tenant):
    staff = Staff.objects.filter(user=user, tenant=tenant).first()
    q = Q(conversation__parent=user)
    if staff:
        q |= Q(conversation__teacher=staff)
    return ChatMessage.objects.filter(q, tenant=tenant, read_at__isnull=True, hidden=False).exclude(sender=user).count()


def flag(msg, user, reason):
    from .engage import admin_emails
    if not is_participant(msg.conversation, user):
        raise ConnectError('You are not part of this conversation.')
    msg.flagged, msg.flag_reason = True, (reason or '').strip()[:255] or 'Reported'
    msg.save(update_fields=['flagged', 'flag_reason'])
    for email in admin_emails(msg.conversation.tenant):
        _email(email, 'A chat message was reported',
               f'<p>A message in "{escape(msg.conversation.subject)}" was reported: {escape(msg.flag_reason)}.</p>'
               f'<p>Open Communications → Chat to review it.</p>')


def hide(msg, admin, hidden=True):
    msg.hidden, msg.hidden_by = hidden, admin if hidden else None
    msg.save(update_fields=['hidden', 'hidden_by'])


# ─────────────────────────────────────────────────────────────────────────────
# F259 — announcement read receipts
# ─────────────────────────────────────────────────────────────────────────────

def announcement_students(ann):
    if ann.audience == 'students':
        return Student.objects.none()
    qs = Student.objects.filter(tenant_id=ann.tenant_id, status__in=ENROLLED_STATUSES)
    if ann.school_class_id:
        qs = qs.filter(class_assigned_id=ann.school_class_id)
    return qs.select_related('class_assigned').order_by('full_name')


def _reader_keys(ann):
    from announcement.models import AnnouncementRead
    keys = set()
    readers = AnnouncementRead.objects.filter(announcement=ann).select_related('user')
    parent_ids = []
    for r in readers:
        if r.user.email:
            keys.add(r.user.email.strip().lower())
        if r.user.username.startswith('parent_') and r.user.username[7:].isdigit():
            parent_ids.append(int(r.user.username[7:]))
    for st in Student.objects.filter(tenant_id=ann.tenant_id, pk__in=parent_ids):
        keys.add(family_key(st))
    return keys


def read_report(ann):
    """Families the announcement is for, one row each, read or not."""
    keys = _reader_keys(ann)
    families = {}
    for st in announcement_students(ann):
        fam = families.setdefault(family_key(st), {'children': [], 'first': st})
        fam['children'].append(st)
    rows = [{'key': k, 'children': v['children'], 'first': v['first'], 'read': k in keys} for k, v in families.items()]
    rows.sort(key=lambda r: (r['read'], r['first'].full_name))
    read = sum(1 for r in rows if r['read'])
    return {'rows': rows, 'read': read, 'total': len(rows), 'pct': round(100 * read / len(rows)) if rows else 0}


def remind_unread(ann_id, user=None, channel='BOTH'):
    from announcement.models import Announcement
    from users.records import contacts

    from .services import send_sms
    ann = Announcement.objects.select_related('tenant').get(pk=ann_id)
    url = f'{ann.tenant.portal_url.rstrip("/")}/announcement/{ann.pk}/'
    n = 0
    for row in read_report(ann)['rows']:
        if row['read']:
            continue
        st = row['first']
        reached = False
        if channel in ('EMAIL', 'BOTH'):
            for _n, email in contacts(st, 'EMAIL'):
                ok, _ = _email(email, f'Please read: {ann.title}',
                               f'<p>{escape(ann.tenant.name)} posted a notice you have not read yet: '
                               f'<b>{escape(ann.title)}</b>.</p><p><a href="{url}">Read it on the parent portal</a></p>')
                reached = reached or ok
        if channel in ('SMS', 'BOTH'):
            for _n, phone in contacts(st, 'SMS'):
                ok, _ = send_sms(phone, f'{ann.tenant.name}: please read "{ann.title[:80]}" on the parent portal.')
                reached = reached or ok
        n += 1 if reached else 0
    AnnouncementReminder.objects.create(tenant=ann.tenant, announcement=ann, families=n, sent_by=user)
    return n


# ─────────────────────────────────────────────────────────────────────────────
# F246 — SMS units
# ─────────────────────────────────────────────────────────────────────────────

GSM = set('@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !"#¤%&\'()*+,-./0123456789:;<=>?¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿'
          'abcdefghijklmnopqrstuvwxyzäöñüà^{}\\[~]|€')


def sms_pages(text):
    text = text or ''
    if all(ch in GSM for ch in text):
        n = len(text) + sum(1 for ch in text if ch in '^{}\\[~]|€')
        return 1 if n <= 160 else math.ceil(n / 153)
    # Unicode SMS are sent as UTF-16: an emoji (outside the basic plane) takes two of the 70 units.
    units = len(text.encode('utf-16-le')) // 2
    return 1 if units <= 70 else math.ceil(units / 67)


def metering_on():
    return bool(getattr(settings, 'SMS_METERING', False))


def unit_price():
    return Decimal(str(getattr(settings, 'SMS_UNIT_PRICE', '4.00')))


def bundles():
    return list(getattr(settings, 'SMS_BUNDLES', [500, 1000, 5000, 10000]))


def wallet(tenant):
    w = SmsWallet.objects.filter(tenant=tenant).first()
    if not w:
        free = int(getattr(settings, 'SMS_FREE_UNITS', 0) or 0)
        try:
            with transaction.atomic():
                w = SmsWallet.objects.create(tenant=tenant, balance=free)
        except Exception:
            w = SmsWallet.objects.get(tenant=tenant)
    return w


def reserve(tenant, pages):
    """Take `pages` units before an SMS goes. False when the school has run out (only when metering is on)."""
    if not tenant or not metering_on():
        return True
    wallet(tenant)
    return SmsWallet.objects.filter(tenant=tenant, balance__gte=pages).update(balance=F('balance') - pages) == 1


def settle(tenant, pages, ok):
    """After the provider answers: give the units back on failure, count them on success."""
    if not tenant:
        return
    if not ok:
        if metering_on():
            SmsWallet.objects.filter(tenant=tenant).update(balance=F('balance') + pages)
        return
    w = wallet(tenant)
    SmsWallet.objects.filter(pk=w.pk).update(used_total=F('used_total') + pages)
    day, _ = SmsUsageDay.objects.get_or_create(tenant=tenant, date=timezone.localdate())
    SmsUsageDay.objects.filter(pk=day.pk).update(units=F('units') + pages, messages=F('messages') + 1)
    if metering_on():
        _low_alert(tenant)


def _low_alert(tenant):
    from .engage import admin_emails
    w = SmsWallet.objects.get(tenant=tenant)
    low = int(getattr(settings, 'SMS_LOW_UNITS', 200))
    if w.balance >= low or (w.low_alert_sent_at and w.low_alert_sent_at > timezone.now() - timedelta(days=3)):
        return
    w.low_alert_sent_at = timezone.now()
    w.save(update_fields=['low_alert_sent_at'])
    for email in admin_emails(tenant):
        _email(email, f'{tenant.name}: SMS units running low',
               f'<p>Only <b>{w.balance}</b> SMS units are left. Top up under Communications → SMS units so '
               f'messages keep going out.</p>')


def grant_units(tenant, units, user=None, note='Free units'):
    if units <= 0:
        raise ConnectError('Give a number of units above zero.')
    wallet(tenant)
    SmsWallet.objects.filter(tenant=tenant).update(balance=F('balance') + units)
    return SmsTopUp.objects.create(tenant=tenant, units=units, amount=Decimal('0'), kind='GRANT', status='PAID',
                                   paid_at=timezone.now(), created_by=user,
                                   reference=f'GRANT-{uuid.uuid4().hex[:16]}')


def start_topup(tenant, user, units, callback_url):
    """Create a pending top-up and return Paystack's payment page URL."""
    import requests
    if units not in bundles():
        raise ConnectError('Choose one of the bundles.')
    key = getattr(settings, 'PAYSTACK_SECRET_KEY', '')
    if not key:
        raise ConnectError('Online payment is not set up on this platform yet. Contact support to buy units.')
    amount = (unit_price() * units).quantize(Decimal('0.01'))
    ref = f'SMS-{uuid.uuid4().hex[:20]}'
    email = user.email or f'{tenant.subdomain}@{getattr(settings, "ROOT_DOMAIN", "example.com")}'
    try:
        resp = requests.post('https://api.paystack.co/transaction/initialize', timeout=15,
                             headers={'Authorization': f'Bearer {key}'},
                             json={'email': email, 'amount': int(amount * 100), 'currency': 'NGN', 'reference': ref,
                                   'callback_url': callback_url,
                                   'metadata': {'kind': 'sms_units', 'tenant': str(tenant.pk), 'units': units}})
        data = resp.json()
    except Exception as problem:
        logger.error('Paystack SMS top-up init failed: %s', problem)
        raise ConnectError('Could not reach the payment page. Please try again.')
    if not data.get('status'):
        raise ConnectError(data.get('message') or 'The payment page could not be opened.')
    SmsTopUp.objects.create(tenant=tenant, units=units, amount=amount, reference=ref, created_by=user)
    return data['data']['authorization_url']


def verify_topup(reference):
    """Confirm a payment with Paystack and add the units once. Returns the top-up."""
    import requests
    with transaction.atomic():
        top = SmsTopUp.objects.select_for_update().filter(reference=reference).first()
        if not top:
            raise ConnectError('Unknown payment reference.')
        if top.status == SmsTopUp.PAID:
            return top
        try:
            resp = requests.get(f'https://api.paystack.co/transaction/verify/{reference}', timeout=15,
                                headers={'Authorization': f'Bearer {getattr(settings, "PAYSTACK_SECRET_KEY", "")}'})
            data = resp.json()
        except Exception:
            raise ConnectError('Could not confirm the payment yet. Refresh this page in a minute.')
        ok = data.get('status') and data.get('data', {}).get('status') == 'success' and \
            int(data['data'].get('amount') or 0) >= int(top.amount * 100)
        if not ok:
            top.status = SmsTopUp.FAILED
            top.save(update_fields=['status'])
            return top
        top.status, top.paid_at = SmsTopUp.PAID, timezone.now()
        top.save(update_fields=['status', 'paid_at'])
        wallet(top.tenant)
        SmsWallet.objects.filter(tenant_id=top.tenant_id).update(balance=F('balance') + top.units,
                                                                 low_alert_sent_at=None)
        return top


# ─────────────────────────────────────────────────────────────────────────────
# F241 — WhatsApp (Meta WhatsApp Business Cloud API)
# ─────────────────────────────────────────────────────────────────────────────

GRAPH = 'https://graph.facebook.com/v21.0'


def wa_configured():
    return bool(getattr(settings, 'WHATSAPP_TOKEN', '') and getattr(settings, 'WHATSAPP_PHONE_NUMBER_ID', ''))


def wa_number(phone):
    digits = re.sub(r'\D', '', phone or '')
    if digits.startswith('0') and len(digits) == 11:
        digits = '234' + digits[1:]
    elif len(digits) == 10:
        digits = '234' + digits
    return digits


def _wa_post(payload):
    import requests
    try:
        resp = requests.post(f'{GRAPH}/{settings.WHATSAPP_PHONE_NUMBER_ID}/messages', timeout=15, json=payload,
                             headers={'Authorization': f'Bearer {settings.WHATSAPP_TOKEN}'})
        data = resp.json()
    except Exception as problem:
        return False, str(problem)
    if resp.status_code == 200 and data.get('messages'):
        return True, data['messages'][0].get('id', 'wa-ok')
    return False, str((data.get('error') or {}).get('message') or data)[:300]


def send_whatsapp_text(to, text):
    """Free text — WhatsApp only delivers this within 24 hours of the parent's last message (inbox replies)."""
    if not wa_configured():
        return False, 'whatsapp-not-configured'
    return _wa_post({'messaging_product': 'whatsapp', 'to': wa_number(to), 'type': 'text',
                     'text': {'body': text[:4096]}})


def send_whatsapp_template(to, template, language, text):
    """A school-started message: an approved template whose single body variable carries the text."""
    if not wa_configured():
        return False, 'whatsapp-not-configured'
    # Template variables may not contain new lines, tabs or long runs of spaces.
    param = re.sub(r'\s+', ' ', text or '').strip()[:1000]
    return _wa_post({'messaging_product': 'whatsapp', 'to': wa_number(to), 'type': 'template',
                     'template': {'name': template, 'language': {'code': language or 'en'},
                                  'components': [{'type': 'body', 'parameters': [{'type': 'text', 'text': param}]}]}})


def try_whatsapp(tenant, to, text):
    """(sent?, ref) — used by send_sms before falling back to SMS."""
    if not tenant or not wa_configured():
        return False, ''
    from .engage import settings_for
    cfg = settings_for(tenant)
    if not (cfg.whatsapp_enabled and cfg.whatsapp_template):
        return False, ''
    ok, ref = send_whatsapp_template(to, cfg.whatsapp_template, cfg.whatsapp_language, text)
    return ok, (f'wa:{ref}' if ok else ref)


def check_signature(body, header):
    secret = getattr(settings, 'WHATSAPP_APP_SECRET', '')
    if not secret:
        return False        # without the app secret nothing proves a message came from WhatsApp
    expected = 'sha256=' + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header or '')


# ─────────────────────────────────────────────────────────────────────────────
# F258 — inbox
# ─────────────────────────────────────────────────────────────────────────────

def match_student(tenant, phone):
    key = _phone_key(phone)
    if not key:
        return None
    for st in Student.objects.filter(tenant=tenant, status__in=ENROLLED_STATUSES).exclude(parent_phone__isnull=True) \
            .exclude(parent_phone='').only('id', 'parent_phone', 'full_name'):
        if _phone_key(st.parent_phone) == key:
            return st
    for g in Guardian.objects.filter(tenant=tenant).exclude(phone='').select_related('student'):
        if _phone_key(g.phone) == key:
            return g.student
    return None


def route_tenant(phone):
    """Which school a reply on a shared number belongs to: the one that messaged this phone most recently."""
    from tenants.models import Tenant

    from .models import MessageLog
    key = _phone_key(phone)
    if not key:
        return None
    log = MessageLog.objects.filter(recipient__endswith=key[-9:]).order_by('-created_at').first()
    if log:
        return log.tenant
    for t in Tenant.objects.filter(status__in=('ACTIVE', 'TRIAL')):
        if match_student(t, phone):
            return t
    return None


def receive(tenant, channel, from_number, body, provider_id=''):
    body = (body or '').strip()
    if not tenant or not body or not from_number:
        return None
    if provider_id and InboundMessage.objects.filter(tenant=tenant, provider_id=provider_id).exists():
        return None
    return InboundMessage.objects.create(tenant=tenant, channel=channel, from_number=from_number[:30], body=body,
                                         provider_id=(provider_id or '')[:120],
                                         student=match_student(tenant, from_number))


def reply(msg, user, body):
    from .services import send_sms
    body = (body or '').strip()
    if not body:
        raise ConnectError('Write your reply.')
    if msg.channel == 'WA':
        ok, ref = send_whatsapp_text(msg.from_number, body)
        if not ok:   # outside WhatsApp's 24-hour window, or not set up: send it as an SMS instead
            ok, ref = send_sms(msg.from_number, body)
    else:
        ok, ref = send_sms(msg.from_number, body)
    r = InboxReply.objects.create(tenant=msg.tenant, message=msg, body=body, ok=ok, error='' if ok else str(ref)[:255],
                                  sent_by=user)
    if ok:
        msg.handled, msg.handled_by, msg.handled_at = True, user, timezone.now()
        msg.save(update_fields=['handled', 'handled_by', 'handled_at'])
    return r


def parse_sms_inbound(data):
    """(from, text, id) from Africa's Talking (from/text/id), Termii (sender/message) or similar."""
    sender = data.get('from') or data.get('sender') or data.get('msisdn') or data.get('phone') or ''
    text = data.get('text') or data.get('message') or data.get('sms') or data.get('body') or ''
    ref = data.get('id') or data.get('message_id') or data.get('messageId') or ''
    return str(sender), str(text), str(ref)


def parse_whatsapp(payload):
    """([(from, text, id)], [(message id, status)]) from a Cloud API webhook body."""
    msgs, statuses = [], []
    for entry in payload.get('entry', []) or []:
        for change in entry.get('changes', []) or []:
            value = change.get('value', {}) or {}
            for m in value.get('messages', []) or []:
                if m.get('type') == 'text':
                    text = (m.get('text') or {}).get('body', '')
                elif m.get('type') == 'button':
                    text = (m.get('button') or {}).get('text', '')
                else:
                    text = f'[{m.get("type", "message")} received — open WhatsApp to see it]'
                msgs.append((m.get('from', ''), text, m.get('id', '')))
            for s in value.get('statuses', []) or []:
                statuses.append((s.get('id', ''), s.get('status', '')))
    return msgs, statuses
