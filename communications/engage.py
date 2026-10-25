"""
communications/engage.py
────────────────────────
Message approval (F250), surveys and polls (F252), consent forms (F253),
birthday greetings (F254), emergency broadcasts (F249), delivery reports
(F247), the scheduled-messages calendar (F260) and the reception TV notice
board (F256).
"""
import calendar
import secrets
from collections import Counter
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.html import escape

from users.models import ENROLLED_STATUSES, Staff, Student

from .models import (BirthdayGreeting, Campaign, CommSettings, ConsentForm, ConsentResponse, EmergencyBroadcast,
                     MessageLog, Survey, SurveyResponse)


class CommError(ValueError):
    pass


def settings_for(tenant):
    obj = CommSettings.objects.filter(tenant=tenant).first()
    if not obj:
        obj = CommSettings.objects.create(tenant=tenant)
    return obj


def is_admin(user):
    from users.views_profile import can_manage
    return can_manage(user)


def admin_emails(tenant):
    from hr.services import admins_emails
    return admins_emails(tenant)


def _email(to, subject, html):
    from .services import send_email
    return send_email(to, subject, html)


def _sms(to, text):
    from .services import send_sms
    return send_sms(to, text)


# ─────────────────────────────────────────────────────────────────────────────
# F250 — message approval
# ─────────────────────────────────────────────────────────────────────────────

def needs_approval(user, tenant):
    return settings_for(tenant).require_approval and not is_admin(user)


def submit(campaign, user, tenant):
    """Called right after a campaign is created. Returns True if it now waits for approval."""
    if not needs_approval(user, tenant):
        return False
    campaign.status = 'PENDING'
    campaign.save(update_fields=['status'])
    for email in admin_emails(tenant):
        _email(email, f'Message waiting for approval: {campaign.title}',
               f'<p>{escape(user.get_full_name() or user.username)} wants to send '
               f'<b>{escape(campaign.title)}</b> ({campaign.get_channel_display()}, '
               f'{campaign.get_audience_display()}).</p><p>Open Communications → Approvals to approve or '
               f'send it back.</p>')
    return True


def may_send(campaign, user, tenant):
    """A junior member of staff can only send a campaign an administrator approved."""
    return not needs_approval(user, tenant) or bool(campaign.approved_at)


def approve(campaign, user):
    from lms_project.tasks import run_in_background

    from .services import send_campaign
    if campaign.status != 'PENDING':
        raise CommError('This message is not waiting for approval.')
    campaign.approved_by, campaign.approved_at = user, timezone.now()
    later = campaign.scheduled_at and campaign.scheduled_at > timezone.now()
    campaign.status = 'SCHEDULED' if later else 'DRAFT'
    campaign.save(update_fields=['approved_by', 'approved_at', 'status'])
    if not later:
        run_in_background(send_campaign, campaign.id, user=user, tenant=campaign.tenant)
    _tell_author(campaign, 'approved' + (f' and will go out on {timezone.localtime(campaign.scheduled_at):%d %b %Y at %-I:%M %p}'
                                         if later else ' and is being sent now'))
    return 'scheduled' if later else 'sending'


def reject(campaign, user, note):
    if campaign.status != 'PENDING':
        raise CommError('This message is not waiting for approval.')
    note = (note or '').strip()
    if not note:
        raise CommError('Say what needs changing, so the sender can fix it.')
    campaign.status, campaign.review_note = 'REJECTED', note[:255]
    campaign.approved_by = user
    campaign.save(update_fields=['status', 'review_note', 'approved_by'])
    _tell_author(campaign, f'sent back: {escape(note)}')


def _tell_author(campaign, what):
    author = campaign.created_by
    if author and author.email:
        _email(author.email, f'Your message "{campaign.title}"', f'<p>Your message <b>{escape(campaign.title)}</b> '
                                                                 f'was {what}.</p>')


# ─────────────────────────────────────────────────────────────────────────────
# F252 — surveys and polls
# ─────────────────────────────────────────────────────────────────────────────

KINDS = {'choice': 'CHOICE', 'multi': 'MULTI', 'rating': 'RATING', 'text': 'TEXT'}


def parse_questions(text):
    """
    One question per line. Options follow the question, separated by |.
        How satisfied are you with the new timetable? | Very | Somewhat | Not at all
        [multi] Which clubs should we add? | Chess | Coding | Drama
        [rating] Rate the end-of-term concert
        [text] Anything else you would like to tell us?
    A line with options and no tag is single choice; a line without options is free text.
    """
    out = []
    for raw in (text or '').splitlines():
        line = raw.strip()
        if not line:
            continue
        kind = None
        if line.startswith('[') and ']' in line:
            tag, line = line[1:line.index(']')].strip().lower(), line[line.index(']') + 1:].strip()
            kind = KINDS.get(tag)
            if not kind:
                raise CommError(f'Unknown question type [{tag}] — use [multi], [rating] or [text].')
        parts = [p.strip() for p in line.split('|')]
        question, options = parts[0][:300], [p[:120] for p in parts[1:] if p][:12]
        if not question:
            continue
        kind = kind or ('CHOICE' if options else 'TEXT')
        if kind in ('CHOICE', 'MULTI') and len(options) < 2:
            raise CommError(f'"{question}" needs at least two options separated by |.')
        out.append({'text': question, 'kind': kind, 'options': options if kind in ('CHOICE', 'MULTI') else []})
    if not out:
        raise CommError('Add at least one question.')
    return out[:30]


def create_survey(tenant, user, title, description, audience, anonymous, closes, questions_text):
    if not (title or '').strip():
        raise CommError('Give the survey a title.')
    if audience not in dict(Survey.AUDIENCES):
        raise CommError('Choose who should answer.')
    if not closes or closes < timezone.localdate():
        raise CommError('Choose a closing date from today on.')
    return Survey.objects.create(tenant=tenant, title=title.strip()[:160], description=(description or '').strip(),
                                 audience=audience, anonymous=bool(anonymous), closes=closes,
                                 questions=parse_questions(questions_text), created_by=user)


def _is_parent(user):
    return bool(getattr(user, 'is_parent', False))


def _is_staff(user, tenant):
    return Staff.objects.filter(user=user, tenant=tenant).exists()


def may_answer(survey, user):
    if not user.is_authenticated:
        return False
    if survey.audience == Survey.PARENTS:
        return _is_parent(user)
    if survey.audience == Survey.STAFF:
        return _is_staff(user, survey.tenant)
    if survey.audience == Survey.STUDENTS:
        return bool(getattr(user, 'is_student', False))
    return _is_parent(user) or _is_staff(user, survey.tenant)


def is_open(survey):
    return survey.published and survey.closes >= timezone.localdate()


def open_surveys(user, tenant):
    """Surveys this person can still answer."""
    done = set(SurveyResponse.objects.filter(user=user, survey__tenant=tenant).values_list('survey_id', flat=True))
    return [s for s in Survey.objects.filter(tenant=tenant, published=True, closes__gte=timezone.localdate())
            if s.pk not in done and may_answer(s, user)]


def answer(survey, user, data):
    """`data` is a QueryDict-like object with q0, q1… (getlist for multi-choice)."""
    if not is_open(survey):
        raise CommError('This survey has closed.')
    if not may_answer(survey, user):
        raise CommError('This survey is not for you.')
    out = {}
    for i, q in enumerate(survey.questions):
        key = f'q{i}'
        if q['kind'] == 'MULTI':
            picked = [o for o in (data.getlist(key) if hasattr(data, 'getlist') else data.get(key, []))
                      if o in q['options']]
            if picked:
                out[str(i)] = picked
        elif q['kind'] == 'CHOICE':
            v = data.get(key)
            if v in q['options']:
                out[str(i)] = v
        elif q['kind'] == 'RATING':
            v = str(data.get(key) or '')
            if v in ('1', '2', '3', '4', '5'):
                out[str(i)] = int(v)
        else:
            v = (data.get(key) or '').strip()
            if v:
                out[str(i)] = v[:2000]
    if not out:
        raise CommError('Answer at least one question.')
    try:
        with transaction.atomic():
            return SurveyResponse.objects.create(tenant_id=survey.tenant_id, survey=survey, user=user, answers=out)
    except IntegrityError:
        raise CommError('You have already answered this survey. Thank you!')


def results(survey):
    responses = list(survey.responses.select_related('user'))
    out = []
    for i, q in enumerate(survey.questions):
        key = str(i)
        given = [r.answers[key] for r in responses if key in r.answers]
        item = {'text': q['text'], 'kind': q['kind'], 'answered': len(given)}
        if q['kind'] in ('CHOICE', 'MULTI'):
            counts = Counter(v for g in given for v in (g if isinstance(g, list) else [g]))
            item['bars'] = [{'label': o, 'count': counts.get(o, 0),
                             'pct': round(100 * counts.get(o, 0) / len(given)) if given else 0} for o in q['options']]
        elif q['kind'] == 'RATING':
            counts = Counter(given)
            item['average'] = round(sum(given) / len(given), 1) if given else None
            item['bars'] = [{'label': f'{n} ★', 'count': counts.get(n, 0),
                             'pct': round(100 * counts.get(n, 0) / len(given)) if given else 0} for n in (5, 4, 3, 2, 1)]
        else:
            item['texts'] = [(None if survey.anonymous else _who(r.user), r.answers[key]) for r in responses
                             if key in r.answers]
        out.append(item)
    return {'count': len(responses), 'questions': out}


def _who(user):
    if not user:
        return 'Someone'
    return user.get_full_name() or user.username


def export_rows(survey):
    header = ['Submitted'] + ([] if survey.anonymous else ['Name']) + [q['text'] for q in survey.questions]
    rows = []
    for r in survey.responses.select_related('user').order_by('created_at'):
        cells = [timezone.localtime(r.created_at).strftime('%Y-%m-%d %H:%M')]
        if not survey.anonymous:
            cells.append(_who(r.user))
        for i in range(len(survey.questions)):
            v = r.answers.get(str(i), '')
            cells.append(', '.join(v) if isinstance(v, list) else v)
        rows.append(cells)
    return header, rows


def invite(survey, tenant):
    """Email the people the survey is for. Returns emails sent."""
    url = f'{tenant.portal_url.rstrip("/")}/communications/surveys/{survey.pk}/'
    to = set()
    if survey.audience in (Survey.PARENTS, Survey.BOTH):
        from users.records import contacts

        from .connect import wants_topic
        for st in Student.objects.filter(tenant=tenant, status__in=ENROLLED_STATUSES):
            if wants_topic(st, 'SURVEYS'):
                to.update(e.lower() for _n, e in contacts(st, 'EMAIL'))
    if survey.audience in (Survey.STAFF, Survey.BOTH):
        to.update(s.user.email.lower() for s in Staff.objects.filter(tenant=tenant, user__is_active=True)
                  .select_related('user') if s.user.email)
    sent = 0
    for email in sorted(to):
        ok, _ = _email(email, f'{tenant.name}: {survey.title}',
                       f'<p>{escape(tenant.name)} would like your views: <b>{escape(survey.title)}</b>.</p>'
                       f'<p>{escape(survey.description)}</p><p>Sign in to the portal and open Surveys, or go to '
                       f'<a href="{url}">{url}</a>. Closes {survey.closes:%d %B %Y}.</p>')
        sent += 1 if ok else 0
    return sent


# ─────────────────────────────────────────────────────────────────────────────
# F253 — consent forms
# ─────────────────────────────────────────────────────────────────────────────

def create_consent(tenant, user, title, details, deadline, cost, class_ids, event=None):
    from users.models import Class
    if not (title or '').strip() or not (details or '').strip():
        raise CommError('Give the title and the details parents are agreeing to.')
    if not deadline or deadline < timezone.localdate():
        raise CommError('Choose a reply-by date from today on.')
    try:
        cost = Decimal(str(cost or '0').replace(',', '')).quantize(Decimal('0.01'))
    except InvalidOperation:
        raise CommError('Give the cost as a number (0 if free).')
    form = ConsentForm.objects.create(tenant=tenant, title=title.strip()[:160], details=details.strip(),
                                      deadline=deadline, cost=max(cost, Decimal('0')), event=event, created_by=user)
    form.classes.set(Class.objects.filter(tenant=tenant, pk__in=[int(c) for c in class_ids if str(c).isdigit()]))
    return form


def consent_students(form):
    qs = Student.objects.filter(tenant_id=form.tenant_id, status__in=ENROLLED_STATUSES)
    ids = list(form.classes.values_list('pk', flat=True))
    if ids:
        qs = qs.filter(class_assigned_id__in=ids)
    return qs.select_related('class_assigned').order_by('class_assigned__name', 'full_name')


def consent_open(form):
    return not form.closed and form.deadline >= timezone.localdate()


def give_consent(form, student, user, answer, signed_name, note='', ip=''):
    if not consent_open(form):
        raise CommError('Replies for this form have closed. Please contact the school.')
    if not consent_students(form).filter(pk=student.pk).exists():
        raise CommError(f'This form is not for {student.full_name}.')
    if answer not in (ConsentResponse.YES, ConsentResponse.NO):
        raise CommError('Choose whether you give consent.')
    signed_name = ' '.join((signed_name or '').split())
    if len(signed_name) < 3:
        raise CommError('Type your full name to sign.')
    resp, _ = ConsentResponse.objects.update_or_create(
        form=form, student=student,
        defaults={'tenant_id': form.tenant_id, 'answer': answer, 'signed_name': signed_name[:160],
                  'signed_by': user, 'ip_address': (ip or '')[:45], 'note': (note or '').strip()[:255]})
    return resp


def consent_summary(form):
    replies = {r.student_id: r for r in form.responses.select_related('signed_by')}
    rows = [{'student': s, 'reply': replies.get(s.pk)} for s in consent_students(form)]
    return {'rows': rows, 'yes': sum(1 for r in rows if r['reply'] and r['reply'].answer == 'YES'),
            'no': sum(1 for r in rows if r['reply'] and r['reply'].answer == 'NO'),
            'waiting': sum(1 for r in rows if not r['reply'])}


def notify_consent(form_id, reminder=False, channel='BOTH'):
    """Ask (or remind) the parents who have not replied. Returns {'emails': n, 'sms': n}."""
    from users.records import contacts
    form = ConsentForm.objects.select_related('tenant').get(pk=form_id)
    replied = set(form.responses.values_list('student_id', flat=True))
    url = f'{form.tenant.portal_url.rstrip("/")}/communications/consent/'
    word = 'Reminder: ' if reminder else ''
    sent = {'emails': 0, 'sms': 0}
    for st in consent_students(form):
        if st.pk in replied:
            continue
        if channel in ('EMAIL', 'BOTH'):
            for _n, email in contacts(st, 'EMAIL'):
                ok, _ = _email(email, f'{word}consent needed — {form.title}',
                               f'<p>{escape(form.tenant.name)} needs your consent for <b>{escape(st.full_name)}</b>: '
                               f'<b>{escape(form.title)}</b>.</p><p>{escape(form.details)}</p>'
                               f'<p>Please reply by {form.deadline:%d %B %Y} on the parent portal: '
                               f'<a href="{url}">{url}</a></p>')
                sent['emails'] += 1 if ok else 0
        if channel in ('SMS', 'BOTH'):
            for _n, phone in contacts(st, 'SMS'):
                ok, _ = _sms(phone, f'{word}{form.tenant.name}: please give or refuse consent for {st.full_name} '
                                    f'- {form.title} - by {form.deadline:%d %b} on the parent portal.')
                sent['sms'] += 1 if ok else 0
    return sent


# ─────────────────────────────────────────────────────────────────────────────
# F254 — birthday greetings
# ─────────────────────────────────────────────────────────────────────────────

def _is_birthday(dob, today):
    if not dob:
        return False
    if (dob.month, dob.day) == (today.month, today.day):
        return True
    # 29 February birthdays are greeted on 28 February in other years.
    return dob.month == 2 and dob.day == 29 and not calendar.isleap(today.year) and (today.month, today.day) == (2, 28)


def birthdays_today(tenant, today=None):
    """([students], [staff]) with a birthday today."""
    from hr.models import StaffRecord
    today = today or timezone.localdate()
    near = [today] + ([date(today.year, 2, 28)] if today.month == 2 else [])
    students = [s for s in Student.objects.filter(tenant=tenant, status__in=ENROLLED_STATUSES,
                                                   date_of_birth__month=today.month)
                .select_related('class_assigned') if _is_birthday(s.date_of_birth, today)]
    staff = [r for r in StaffRecord.objects.filter(tenant=tenant, date_of_birth__month__in={d.month for d in near},
                                                     left_on__isnull=True, staff__user__is_active=True)
             .select_related('staff__user') if _is_birthday(r.date_of_birth, today)]
    return students, staff


def _greeted(tenant, person, year):
    try:
        with transaction.atomic():
            BirthdayGreeting.objects.create(tenant=tenant, person=person, year=year)
        return False
    except IntegrityError:
        return True


def send_birthdays(tenant, today=None):
    """Greet today's birthdays once. Returns {'students': n, 'staff': n}."""
    from users.records import contacts

    from .services import _build_context, _render_body
    cfg = settings_for(tenant)
    today = today or timezone.localdate()
    students, staff = birthdays_today(tenant, today)
    done = {'students': 0, 'staff': 0}
    if cfg.birthday_students:
        for st in students:
            from .connect import wants_topic
            if not wants_topic(st, 'BIRTHDAY') or _greeted(tenant, f'student:{st.pk}', today.year):
                continue
            text = _render_body(cfg.birthday_student_sms, {**_build_context(st), 'school_name': tenant.name})
            for _n, phone in contacts(st, 'SMS'):
                _sms(phone, text)
            for _n, email in contacts(st, 'EMAIL'):
                _email(email, f'Happy birthday, {st.full_name.split()[0]}!', f'<p>{escape(text)}</p>')
            done['students'] += 1
    if cfg.birthday_staff:
        for rec in staff:
            if _greeted(tenant, f'staff:{rec.staff_id}', today.year):
                continue
            text = _render_body(cfg.birthday_staff_sms, {'name': rec.staff.full_name.split()[0],
                                                         'school_name': tenant.name})
            if rec.phone:
                _sms(rec.phone, text)
            if rec.staff.user.email:
                _email(rec.staff.user.email, 'Happy birthday!', f'<p>{escape(text)}</p>')
            done['staff'] += 1
    return done


def run_birthdays(today=None):
    from tenants.middleware import clear_current_tenant, set_current_tenant
    from tenants.models import Tenant
    out = {}
    for tenant in Tenant.objects.filter(status__in=('ACTIVE', 'TRIAL')):
        set_current_tenant(tenant)
        try:
            n = send_birthdays(tenant, today)
            if n['students'] or n['staff']:
                out[tenant.subdomain] = n
        finally:
            clear_current_tenant()
    return out


# ─────────────────────────────────────────────────────────────────────────────
# F249 — emergency broadcast
# ─────────────────────────────────────────────────────────────────────────────

SMS_LIMIT = 459  # three SMS pages


def start_broadcast(tenant, user, subject, message, to_parents=True, to_staff=True):
    from lms_project.tasks import run_in_background
    subject, message = (subject or '').strip(), (message or '').strip()
    if not is_admin(user):
        raise CommError('Only administrators can send an emergency broadcast.')
    if not subject or not message:
        raise CommError('Write the subject and the message.')
    if len(message) > SMS_LIMIT:
        raise CommError(f'Keep the message under {SMS_LIMIT} characters so it fits in an SMS.')
    if not (to_parents or to_staff):
        raise CommError('Choose parents, staff or both.')
    b = EmergencyBroadcast.objects.create(tenant=tenant, subject=subject[:160], message=message,
                                          to_parents=to_parents, to_staff=to_staff, sent_by=user)
    run_in_background(deliver_broadcast, b.pk, tenant=tenant)
    return b


def broadcast_contacts(b):
    """(phones, emails) — everyone once. Emergencies reach parents who opted out of ordinary messages."""
    from hr.models import StaffRecord
    phones, emails = {}, {}

    def add_phone(p):
        from users.records import _phone_key
        if p and _phone_key(p):
            phones.setdefault(_phone_key(p), p)

    def add_email(e):
        if e and '@' in e:
            emails.setdefault(e.strip().lower(), e.strip())

    if b.to_parents:
        from users.models import Guardian
        for st in Student.objects.filter(tenant_id=b.tenant_id, status__in=ENROLLED_STATUSES):
            add_phone(st.parent_phone)
            add_email(st.parent_email)
        for g in Guardian.objects.filter(tenant_id=b.tenant_id, student__status__in=ENROLLED_STATUSES):
            add_phone(g.phone)
            add_email(g.email)
    if b.to_staff:
        for s in Staff.objects.filter(tenant_id=b.tenant_id, user__is_active=True).select_related('user'):
            add_email(s.user.email)
        for r in StaffRecord.objects.filter(tenant_id=b.tenant_id, left_on__isnull=True):
            add_phone(r.phone)
    return list(phones.values()), list(emails.values())


def deliver_broadcast(pk):
    b = EmergencyBroadcast.objects.select_related('tenant').get(pk=pk)
    phones, emails = broadcast_contacts(b)
    text = f'URGENT - {b.tenant.name}: {b.message}'
    for p in phones:
        ok, _ = _sms(p, text)
        if ok:
            b.sms_sent += 1
        else:
            b.sms_failed += 1
    html = (f'<div style="border-left:4px solid #dc2626;padding:8px 14px"><p><b>Urgent message from '
            f'{escape(b.tenant.name)}</b></p><p>{escape(b.message)}</p></div>')
    for e in emails:
        ok, _ = _email(e, f'URGENT: {b.subject}', html)
        if ok:
            b.emails_sent += 1
        else:
            b.emails_failed += 1
    b.finished_at = timezone.now()
    b.save()
    return {'sms': b.sms_sent, 'emails': b.emails_sent}


# ─────────────────────────────────────────────────────────────────────────────
# F247 — delivery reports
# ─────────────────────────────────────────────────────────────────────────────

DLR_MAP = {'delivered': 'DELIVERED', 'delivrd': 'DELIVERED', 'success': 'DELIVERED',
           'failed': 'FAILED', 'undelivered': 'FAILED', 'rejected': 'FAILED', 'expired': 'FAILED',
           'dnd active': 'FAILED', 'message failed': 'FAILED', 'bounced': 'BOUNCED', 'sent': 'SENT',
           'read': 'DELIVERED'}


def apply_dlr(payload):
    """
    Update a message from an SMS provider's delivery report. Accepts Termii
    (message_id/status) and Africa's Talking (id/status) shapes. Returns the
    number of messages updated.
    """
    ref = str(payload.get('message_id') or payload.get('messageId') or payload.get('id') or '').strip()
    raw = str(payload.get('status') or payload.get('Status') or '').strip().lower()
    status = DLR_MAP.get(raw)
    if not ref or not status:
        return 0
    logs = MessageLog.objects.filter(provider_ref=ref)
    n = 0
    for log in logs:
        log.status = status
        if status == 'FAILED':
            log.error_message = (payload.get('reason') or payload.get('failureReason') or raw)[:500]
        log.save(update_fields=['status', 'error_message'])
        n += 1
    return n


def report(campaign):
    logs = list(campaign.logs.select_related('student').order_by('channel', 'recipient'))
    counts = Counter(log.status for log in logs)
    return {'logs': logs, 'counts': {k: counts.get(k, 0) for k in ('SENT', 'DELIVERED', 'FAILED', 'BOUNCED')},
            'total': len(logs)}


def retry_failed(campaign):
    """Send again every failed message of a campaign. Returns (now sent, still failing)."""
    from .services import send_email, send_sms
    ok_n = bad_n = 0
    for log in campaign.logs.filter(status__in=('FAILED', 'BOUNCED')):
        if log.channel == 'EMAIL':
            ok, ref = send_email(log.recipient, log.subject, log.body, log.student)
        else:
            ok, ref = send_sms(log.recipient, log.body)
        log.status = 'SENT' if ok else 'FAILED'
        log.provider_ref = ref if ok else log.provider_ref
        log.error_message = '' if ok else ref
        log.sent_at = timezone.now() if ok else log.sent_at
        log.save(update_fields=['status', 'provider_ref', 'error_message', 'sent_at'])
        if ok:
            ok_n += 1
        else:
            bad_n += 1
    if ok_n:
        field_ok = {'EMAIL': 'emails_sent', 'SMS': 'sms_sent'}
        for ch, f in field_ok.items():
            setattr(campaign, f, campaign.logs.filter(channel=ch, status__in=('SENT', 'DELIVERED')).count())
            setattr(campaign, f.replace('sent', 'failed'),
                    campaign.logs.filter(channel=ch, status__in=('FAILED', 'BOUNCED')).count())
        campaign.save(update_fields=['emails_sent', 'emails_failed', 'sms_sent', 'sms_failed'])
    return ok_n, bad_n


# ─────────────────────────────────────────────────────────────────────────────
# F260 — scheduled messages calendar
# ─────────────────────────────────────────────────────────────────────────────

def month_items(tenant, year, month):
    """{date: [{'kind', 'title', 'time', 'url_name', 'pk', 'tone'}]} for the month."""
    first = date(year, month, 1)
    last = date(year, month, calendar.monthrange(year, month)[1])
    items = {}

    def add(day, **kw):
        items.setdefault(day, []).append(kw)

    for c in Campaign.objects.filter(tenant=tenant).exclude(status='CANCELLED'):
        when = c.scheduled_at if c.status in ('SCHEDULED', 'PENDING', 'DRAFT') and c.scheduled_at else c.sent_at
        if not when:
            continue
        local = timezone.localtime(when)
        if first <= local.date() <= last:
            tone = {'SCHEDULED': 'gold', 'PENDING': 'amber', 'SENT': 'green', 'FAILED': 'red'}.get(c.status, '')
            add(local.date(), kind=c.get_status_display(), title=c.title, time=local.strftime('%-I:%M %p'),
                link=f'/communications/campaigns/{c.pk}/', tone=tone)
    for f in ConsentForm.objects.filter(tenant=tenant, deadline__range=(first, last)):
        add(f.deadline, kind='Consent due', title=f.title, time='', link=f'/communications/consent/manage/{f.pk}/',
            tone='')
    for s in Survey.objects.filter(tenant=tenant, closes__range=(first, last)):
        add(s.closes, kind='Survey closes', title=s.title, time='', link=f'/communications/surveys/{s.pk}/results/',
            tone='')
    for b in EmergencyBroadcast.objects.filter(tenant=tenant, created_at__date__range=(first, last)):
        add(timezone.localtime(b.created_at).date(), kind='Emergency', title=b.subject,
            time=timezone.localtime(b.created_at).strftime('%-I:%M %p'), link='/communications/emergency/', tone='red')
    return items


def month_grid(year, month, items):
    """Weeks (Monday first) of {'day': date|None, 'items': [...]}."""
    weeks = []
    for week in calendar.Calendar(firstweekday=0).monthdatescalendar(year, month):
        weeks.append([{'day': d if d.month == month else None, 'items': items.get(d, []) if d.month == month else []}
                      for d in week])
    return weeks


# ─────────────────────────────────────────────────────────────────────────────
# F256 — reception TV notice board
# ─────────────────────────────────────────────────────────────────────────────

def signage_token(tenant, renew=False):
    cfg = settings_for(tenant)
    if renew or not cfg.signage_token:
        cfg.signage_token = secrets.token_urlsafe(24)
        cfg.save(update_fields=['signage_token'])
    return cfg.signage_token


def _short(name):
    parts = (name or '').split()
    return f'{parts[0]} {parts[-1][0]}.' if len(parts) > 1 else (parts[0] if parts else '')


def signage_slides(tenant, today=None):
    from announcement.models import Announcement
    from curriculum.models import CalendarEvent
    from django.db.models import Q
    today = today or timezone.localdate()
    now = timezone.now()
    slides = []
    for a in Announcement.objects.filter(tenant=tenant, published=True).filter(
            Q(expires_at__isnull=True) | Q(expires_at__gte=now)).order_by('-published_at')[:8]:
        slides.append({'kind': 'Announcement', 'title': a.title, 'body': a.message[:600]})
    events = CalendarEvent.objects.filter(tenant=tenant, start_date__range=(today, today + timedelta(days=21))) \
        .exclude(audience='STAFF').order_by('start_date')[:8]
    if events:
        slides.append({'kind': 'Coming up', 'title': 'Dates for your diary',
                       'items': [f'{e.start_date:%a %d %b} — {e.title}' for e in events]})
    students, staff = birthdays_today(tenant, today)
    # A public screen: first name and initial only.
    names = [_short(s.full_name) + (f' ({s.class_assigned.name})' if s.class_assigned_id else '') for s in students] + \
        [_short(r.staff.full_name) for r in staff]
    if names:
        slides.append({'kind': 'Happy birthday', 'title': 'Happy birthday today!', 'items': names[:20]})
    if not slides:
        slides.append({'kind': 'Welcome', 'title': f'Welcome to {tenant.name}', 'body': ''})
    return slides
