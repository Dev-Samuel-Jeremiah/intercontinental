"""
family/services.py
──────────────────
    F263  daily digest for each family
    F265  fee statement (PDF)
    F266  appointments with teachers and the principal
    F267  PTA: executives, meetings and minutes, levies
    F268  volunteer sign-up
    F269  feedback and complaints
    F276  each child's teachers, with office hours
    F277  password-free sign-in (emailed link or SMS code)
    F280  satisfaction score (NPS) each term
"""
import hashlib
import secrets
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
from io import BytesIO

from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone
from django.utils.html import escape

from users.models import ENROLLED_STATUSES, Staff, Student

from .models import (DAYS, Appointment, Availability, DigestLog, FamilySettings, LoginToken, Opportunity,
                     PtaPayment, SatisfactionResponse, Signup, Ticket, TicketMessage)

ZERO = Decimal('0')
DAY_CODES = ['MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT', 'SUN']


class FamilyError(ValueError):
    pass


def settings_for(tenant):
    obj = FamilySettings.objects.filter(tenant=tenant).first()
    return obj or FamilySettings.objects.create(tenant=tenant)


def family_key(student):
    from communications.connect import family_key as key
    return key(student)


def children_of(user, tenant):
    """The same rule the parent portal uses."""
    from timetable.ical import parent_children
    if not getattr(user, 'is_parent', False):
        return []
    return list(parent_children(user, tenant).select_related('class_assigned').order_by('id'))


def _email(to, subject, html):
    from communications.services import send_email
    return send_email(to, subject, html)


def _admins(tenant):
    from hr.services import admins_emails
    return admins_emails(tenant)


def _family_emails(student):
    from users.records import contacts
    return [e for _n, e in contacts(student, 'EMAIL')]


def _portal(tenant, path):
    return f'{tenant.portal_url.rstrip("/")}{path}'


# ─────────────────────────────────────────────────────────────────────────────
# F263 — daily digest
# ─────────────────────────────────────────────────────────────────────────────

def child_day(student, day):
    from curriculum.models import Homework
    from finance.models import Sale
    from users.models import Attendance, Award, HousePoint
    mark = Attendance.objects.filter(student=student, date=day, subject__isnull=True).first()
    if not mark:
        attendance = None
    elif mark.status == 'A':
        attendance = 'Absent'
    elif mark.minutes_late:
        attendance = f'Present — {mark.minutes_late} minutes late'
    else:
        attendance = 'Present'
    homework = list(Homework.objects.filter(school_class_id=student.class_assigned_id, set_on=day)
                    .select_related('subject')) if student.class_assigned_id else []
    due = list(Homework.objects.filter(school_class_id=student.class_assigned_id, due_on=day + timedelta(days=1))
               .select_related('subject')) if student.class_assigned_id else []
    awards = list(Award.objects.filter(student=student, awarded_on=day))
    points = HousePoint.objects.filter(student=student, awarded_on=day).aggregate(s=Sum('points'))['s'] or 0
    spent = Sale.objects.filter(student=student, created_at__date=day, status=Sale.COMPLETED) \
        .aggregate(s=Sum('total'))['s'] or ZERO
    try:
        from teaching.models import BehaviourEntry
        behaviour = list(BehaviourEntry.objects.filter(student=student, date=day))
    except Exception:
        behaviour = []
    return {'student': student, 'attendance': attendance, 'homework': homework, 'due': due, 'awards': awards,
            'points': points, 'spent': spent, 'behaviour': behaviour}


def family_wallet_balance(student):
    try:
        from finance.views import _get_family_wallet_for_student
        w = _get_family_wallet_for_student(student)
        return w.balance if w else None
    except Exception:
        return None


def digest(children, day):
    items = [child_day(c, day) for c in children]
    news = any(i['attendance'] or i['homework'] or i['due'] or i['awards'] or i['points'] or i['spent'] or
               i['behaviour'] for i in items)
    return {'day': day, 'items': items, 'has_news': news,
            'wallet': family_wallet_balance(children[0]) if children else None}


def digest_html(tenant, d):
    rows = []
    for i in d['items']:
        lines = [f"<b>{escape(i['student'].full_name)}</b>"]
        if i['attendance']:
            lines.append(f"Attendance: {escape(i['attendance'])}")
        for h in i['homework']:
            lines.append(f"Homework set — {escape(h.subject.name)}: {escape(h.title)} (due {h.due_on:%a %d %b})")
        for h in i['due']:
            lines.append(f"Due tomorrow — {escape(h.subject.name)}: {escape(h.title)}")
        for a in i['awards']:
            lines.append(f"Award: {escape(a.title)}")
        if i['points']:
            lines.append(f"House points today: {i['points']}")
        if i['spent']:
            lines.append(f"Spent at the shop/canteen: ₦{i['spent']:,.2f}")
        for b in i['behaviour']:
            lines.append(f"{'Praise' if b.points > 0 else 'Concern'}: {escape(b.get_category_display())}"
                         f"{' — ' + escape(b.note) if b.note else ''}")
        rows.append('<p>' + '<br>'.join(lines) + '</p>')
    wallet = f"<p>Family wallet balance: <b>₦{d['wallet']:,.2f}</b></p>" if d['wallet'] is not None else ''
    return (f"<p>Here is today at {escape(tenant.name)} ({d['day']:%A %d %B}).</p>{''.join(rows)}{wallet}"
            f"<p style='color:#666;font-size:12px'>Turn these summaries off under Message preferences on the parent "
            f"portal.</p>")


def families(tenant):
    groups = defaultdict(list)
    for st in Student.objects.filter(tenant=tenant, status__in=ENROLLED_STATUSES).select_related('class_assigned') \
            .order_by('id'):
        groups[family_key(st)].append(st)
    return groups


def send_digests(tenant, day=None):
    from communications.connect import wants_topic
    day = day or timezone.localdate()
    if not settings_for(tenant).digest_enabled:
        return 0
    sent = 0
    for key, kids in families(tenant).items():
        if not wants_topic(kids[0], 'DIGEST'):
            continue
        d = digest(kids, day)
        if not d['has_news']:
            continue
        emails = _family_emails(kids[0])
        if not emails:
            continue
        try:
            with transaction.atomic():
                DigestLog.objects.create(tenant=tenant, family=key, date=day)
        except IntegrityError:
            continue
        html = digest_html(tenant, d)
        for e in emails:
            _email(e, f'{tenant.name}: today for {", ".join(k.full_name.split()[0] for k in kids)}', html)
        sent += 1
    return sent


def run_digests(day=None):
    from tenants.middleware import clear_current_tenant, set_current_tenant
    from tenants.models import Tenant
    total = 0
    for tenant in Tenant.objects.filter(status__in=('ACTIVE', 'TRIAL')):
        set_current_tenant(tenant)
        try:
            total += send_digests(tenant, day)
        finally:
            clear_current_tenant()
    return total


# ─────────────────────────────────────────────────────────────────────────────
# F265 — fee statement
# ─────────────────────────────────────────────────────────────────────────────

def statement(student):
    from finance.models import ChildBilling, ChildPaymentAllocation, FeePayment
    bills = list(ChildBilling.objects.filter(student=student).select_related(
        'parent_billing__session', 'parent_billing__term').order_by('parent_billing__session__name', 'parent_billing_id'))
    allocations = list(ChildPaymentAllocation.objects.filter(child_billing__student=student).select_related(
        'payment', 'child_billing__parent_billing__term').order_by('created_at'))
    items = list(FeePayment.objects.filter(student=student).select_related('fee_structure').order_by('id'))
    billed = sum((b.amount_due for b in bills), ZERO) + sum((f.fee_structure.amount for f in items
                                                             if f.status != 'WAIVED'), ZERO)
    paid = sum((b.paid for b in bills), ZERO) + sum((f.amount_paid for f in items), ZERO)
    return {'student': student, 'bills': bills, 'allocations': allocations, 'items': items, 'billed': billed,
            'paid': paid, 'balance': max(ZERO, billed - paid), 'today': timezone.localdate()}


def statement_pdf(tenant, st):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=1.8 * cm, rightMargin=1.8 * cm, topMargin=1.6 * cm,
                            bottomMargin=1.6 * cm, title='Fee statement')
    ss = getSampleStyleSheet()
    s = st['student']
    out = [Paragraph(f'<b>{escape(tenant.name)}</b>', ss['Title']),
           Paragraph('Fee statement', ss['Heading2']),
           Paragraph(f'{escape(s.full_name)} · {escape(s.admission_number)} · {escape(str(s.class_assigned or ""))}'
                     f' · printed {st["today"]:%d %B %Y}', ss['Normal']), Spacer(1, 12)]

    def table(head, rows, widths):
        t = Table([head] + rows, colWidths=widths, repeatRows=1)
        t.setStyle(TableStyle([('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1b1b24')),
                               ('TEXTCOLOR', (0, 0), (-1, 0), colors.white), ('FONTSIZE', (0, 0), (-1, -1), 9),
                               ('GRID', (0, 0), (-1, -1), 0.25, colors.HexColor('#cccccc')),
                               ('ALIGN', (2, 1), (-1, -1), 'RIGHT')]))
        return t

    def n(v):
        return f'{v:,.2f}'

    if st['bills']:
        out += [Paragraph('Term bills', ss['Heading3']),
                table(['Session', 'Term', 'Fee', 'Discount', 'Late fee', 'Due', 'Paid', 'Balance'],
                      [[str(b.parent_billing.session), str(b.parent_billing.term), n(b.gross_fee), n(b.discount),
                        n(b.late_fee), n(b.amount_due), n(b.paid), n(b.balance)] for b in st['bills']],
                      [2.3 * cm, 2 * cm] + [2 * cm] * 6), Spacer(1, 10)]
    if st['allocations']:
        out += [Paragraph('Payments', ss['Heading3']),
                table(['Date', 'Reference', 'Term', 'Amount'],
                      [[timezone.localtime(a.created_at).strftime('%d %b %Y'), a.payment.reference,
                        str(a.child_billing.parent_billing.term), n(a.amount_allocated)] for a in st['allocations']],
                      [3 * cm, 6 * cm, 4 * cm, 3 * cm]), Spacer(1, 10)]
    if st['items']:
        out += [Paragraph('Fee items', ss['Heading3']),
                table(['Item', 'Status', 'Amount', 'Paid'],
                      [[f.fee_structure.name, f.get_status_display(), n(f.fee_structure.amount), n(f.amount_paid)]
                       for f in st['items']], [7 * cm, 3 * cm, 3 * cm, 3 * cm]), Spacer(1, 10)]
    out += [Spacer(1, 6), Paragraph(f'<b>Total billed: ₦{n(st["billed"])} · Paid: ₦{n(st["paid"])} · '
                                    f'Balance: ₦{n(st["balance"])}</b>', ss['Normal'])]
    doc.build(out)
    return buf.getvalue()


# ─────────────────────────────────────────────────────────────────────────────
# F276 — teachers, and who a parent can meet
# ─────────────────────────────────────────────────────────────────────────────

def teachers_of(student):
    """[(staff, [subjects], is_form_teacher)]"""
    from users.models import StaffSubjectClass
    out = {}
    cls = student.class_assigned
    if not cls:
        return []
    for link in StaffSubjectClass.objects.filter(school_class=cls).select_related('staff__user', 'subject'):
        if link.staff.user.is_active:
            out.setdefault(link.staff_id, [link.staff, [], False])[1].append(link.subject.name)
    for ft in cls.form_teachers.select_related('user'):
        if ft.user.is_active:
            out.setdefault(ft.pk, [ft, [], False])[2] = True
    rows = [tuple(v) for v in out.values()]
    return sorted(rows, key=lambda r: (not r[2], r[0].full_name))


def meetable(student):
    """The child's teachers and the school's administrators (principal, vice-principal)."""
    ids = [t[0].pk for t in teachers_of(student)]
    ids += list(Staff.objects.filter(tenant_id=student.tenant_id, role='ADMIN', user__is_active=True)
                .values_list('pk', flat=True))
    return list(Staff.objects.filter(pk__in=set(ids)).filter(availability__isnull=False).distinct()
                .order_by('full_name'))


# ─────────────────────────────────────────────────────────────────────────────
# F266 — appointments
# ─────────────────────────────────────────────────────────────────────────────

def set_availability(staff, day, start, end, slot_minutes, location):
    if day not in dict(DAYS):
        raise FamilyError('Choose a day.')
    if not (start and end) or end <= start:
        raise FamilyError('The end time must be after the start time.')
    if slot_minutes not in (10, 15, 20, 30, 45, 60):
        raise FamilyError('Choose a meeting length.')
    return Availability.objects.create(tenant_id=staff.tenant_id, staff=staff, day=day, start=start, end=end,
                                       slot_minutes=slot_minutes, location=(location or '')[:120])


def free_slots(staff, day, now=None):
    from academics.models import PublicHoliday
    now = timezone.localtime(now or timezone.now())
    if day < now.date() or PublicHoliday.objects.filter(tenant_id=staff.tenant_id, date=day).exists():
        return []
    taken = set(Appointment.objects.filter(staff=staff, date=day, status=Appointment.BOOKED)
                .values_list('start', flat=True))
    out = []
    for a in Availability.objects.filter(staff=staff, day=DAY_CODES[day.weekday()]):
        t = datetime.combine(day, a.start)
        stop = datetime.combine(day, a.end)
        while t + timedelta(minutes=a.slot_minutes) <= stop:
            if t.time() not in taken and (day > now.date() or t.time() > now.time()):
                out.append({'start': t.time(), 'end': (t + timedelta(minutes=a.slot_minutes)).time(),
                            'location': a.location})
            t += timedelta(minutes=a.slot_minutes)
    return sorted(out, key=lambda s: s['start'])


def upcoming(staff, days=14):
    today = timezone.localdate()
    out = []
    for i in range(days):
        d = today + timedelta(days=i)
        slots = free_slots(staff, d)
        if slots:
            out.append((d, slots))
    return out


def book(parent, student, staff, day, start, mode, reason):
    if staff not in meetable(student):
        raise FamilyError(f'{staff.full_name} is not taking appointments about {student.full_name}.')
    reason = (reason or '').strip()
    if not reason:
        raise FamilyError('Say briefly what you would like to discuss.')
    if mode not in ('IN_PERSON', 'PHONE', 'VIDEO'):
        raise FamilyError('Choose how you would like to meet.')
    with transaction.atomic():
        Staff.objects.select_for_update().get(pk=staff.pk)
        slot = next((s for s in free_slots(staff, day) if s['start'] == start), None)
        if not slot:
            raise FamilyError('That time has just been taken. Please choose another.')
        appt = Appointment.objects.create(tenant_id=staff.tenant_id, staff=staff, parent=parent, student=student,
                                          date=day, start=slot['start'], end=slot['end'], mode=mode,
                                          reason=reason[:255], note=slot['location'])
    when = f'{day:%A %d %B} at {slot["start"]:%-I:%M %p}'
    if staff.user.email:
        _email(staff.user.email, f'Parent appointment: {when}',
               f'<p>A parent of <b>{escape(student.full_name)}</b> booked {appt.get_mode_display().lower()} with you '
               f'on {when}.</p><p>About: {escape(reason)}</p>')
    to = parent.email or student.parent_email
    if to:
        _email(to, f'Appointment confirmed: {when}',
               f'<p>Your appointment with {escape(staff.full_name)} about {escape(student.full_name)} is on '
               f'<b>{when}</b> ({appt.get_mode_display().lower()}{", " + escape(slot["location"]) if slot["location"] else ""}).'
               f'</p><p>To cancel, open Appointments on the parent portal.</p>')
    return appt


def cancel(appt, user, note=''):
    if appt.status != Appointment.BOOKED:
        raise FamilyError('This appointment is not booked.')
    staff_side = appt.staff.user_id == user.pk
    if not (staff_side or appt.parent_id == user.pk):
        raise FamilyError('Only the parent or the member of staff can cancel.')
    appt.status, appt.note = Appointment.CANCELLED, (note or '')[:255]
    appt.save(update_fields=['status', 'note'])
    to = (appt.parent.email or appt.student.parent_email) if staff_side else appt.staff.user.email
    if to:
        who = appt.staff.full_name if staff_side else 'The parent'
        _email(to, 'Appointment cancelled', f'<p>{escape(who)} cancelled the appointment on '
                                            f'{appt.date:%A %d %B} at {appt.start:%-I:%M %p}.'
                                            f'{" Note: " + escape(note) if note else ""}</p>')


# ─────────────────────────────────────────────────────────────────────────────
# F267 — PTA
# ─────────────────────────────────────────────────────────────────────────────

def levy_status(levy, key):
    return PtaPayment.objects.filter(levy=levy, family=key).first()


def pay_levy_from_wallet(levy, student, user):
    from finance.views import _get_family_wallet_for_student
    key = family_key(student)
    if not levy.active:
        raise FamilyError('This levy is closed.')
    if levy_status(levy, key):
        raise FamilyError('Your family has already paid this levy.')
    wallet = _get_family_wallet_for_student(student)
    if not wallet:
        raise FamilyError('No family wallet found. Please pay at the bursary.')
    with transaction.atomic():
        try:
            txn = wallet.debit(levy.amount, f'PTA: {levy.title}', performed_by=user, category='OTHER')
        except ValueError as problem:
            raise FamilyError(str(problem))
        try:
            return PtaPayment.objects.create(tenant_id=levy.tenant_id, levy=levy, family=key, student=student,
                                             amount=levy.amount, method='WALLET', transaction=txn, received_by=user)
        except IntegrityError:
            raise FamilyError('Your family has already paid this levy.')


def record_levy(levy, student, method, user):
    if method not in ('CASH', 'TRANSFER', 'WAIVED'):
        raise FamilyError('Choose how it was paid.')
    key = family_key(student)
    try:
        return PtaPayment.objects.create(tenant_id=levy.tenant_id, levy=levy, family=key, student=student,
                                         amount=ZERO if method == 'WAIVED' else levy.amount, method=method,
                                         received_by=user)
    except IntegrityError:
        raise FamilyError(f'{student.full_name}\'s family has already paid.')


def levy_report(levy):
    groups = families(levy.tenant)
    paid = {p.family: p for p in levy.payments.all()}
    rows = [{'key': k, 'children': kids, 'payment': paid.get(k)} for k, kids in groups.items()]
    rows.sort(key=lambda r: (r['payment'] is not None, r['children'][0].full_name))
    return {'rows': rows, 'paid': sum(1 for r in rows if r['payment']), 'total': len(rows),
            'collected': sum((p.amount for p in paid.values()), ZERO)}


# ─────────────────────────────────────────────────────────────────────────────
# F268 — volunteering
# ─────────────────────────────────────────────────────────────────────────────

def places_left(opp):
    return None if opp.places is None else max(0, opp.places - opp.signups.count())


def volunteer(opp, user, name, phone, note=''):
    if opp.cancelled or opp.date < timezone.localdate():
        raise FamilyError('This is no longer open.')
    name = (name or '').strip()
    if not name:
        raise FamilyError('Give your name.')
    with transaction.atomic():
        Opportunity.objects.select_for_update().get(pk=opp.pk)
        if places_left(opp) == 0:
            raise FamilyError('All places have been taken. Thank you for offering!')
        try:
            return Signup.objects.create(tenant_id=opp.tenant_id, opportunity=opp, user=user, name=name[:120],
                                         phone=(phone or '')[:20], note=(note or '')[:255])
        except IntegrityError:
            raise FamilyError('You have already signed up.')


# ─────────────────────────────────────────────────────────────────────────────
# F269 — feedback and complaints
# ─────────────────────────────────────────────────────────────────────────────

def _next_reference(tenant):
    last = Ticket.objects.filter(tenant=tenant).order_by('-id').first()
    n = int(last.reference.split('-')[-1]) + 1 if last and last.reference.split('-')[-1].isdigit() else 1
    return f'FB-{n:05d}'


def open_ticket(tenant, parent, student, kind, category, subject, body):
    if kind not in dict(Ticket.KINDS) or category not in dict(Ticket.CATEGORIES):
        raise FamilyError('Choose what kind of message this is and what it is about.')
    subject, body = (subject or '').strip(), (body or '').strip()
    if not subject or not body:
        raise FamilyError('Give a subject and tell us what happened.')
    for _ in range(3):
        try:
            with transaction.atomic():
                t = Ticket.objects.create(tenant=tenant, reference=_next_reference(tenant), parent=parent,
                                          student=student, kind=kind, category=category, subject=subject[:160],
                                          body=body[:8000])
            break
        except IntegrityError:
            continue
    else:
        raise FamilyError('Please try again.')
    for e in _admins(tenant):
        _email(e, f'New {t.get_kind_display().lower()} {t.reference}: {t.subject}',
               f'<p>{escape(t.get_category_display())} — {escape(t.subject)}</p><p>{escape(body[:600])}</p>'
               f'<p>Open Parent experience → Feedback to reply.</p>')
    return t


def ticket_reply(ticket, user, body, internal=False, status=None):
    body = (body or '').strip()
    if body:
        TicketMessage.objects.create(tenant_id=ticket.tenant_id, ticket=ticket, author=user, body=body[:8000],
                                     internal=internal)
    staff_side = user.pk != ticket.parent_id
    if status and staff_side and status in dict(Ticket.STATUSES):
        ticket.status = status
        if status == Ticket.RESOLVED and not ticket.resolved_at:
            ticket.resolved_at = timezone.now()
    elif not staff_side and ticket.status in (Ticket.RESOLVED, Ticket.CLOSED) and body:
        ticket.status, ticket.resolved_at = Ticket.OPEN, None   # parent says it is not sorted
    elif staff_side and body and not internal and ticket.status == Ticket.OPEN:
        ticket.status = Ticket.PROGRESS
    ticket.save()
    if staff_side and (body and not internal or status == Ticket.RESOLVED):
        to = ticket.parent.email if ticket.parent else None
        to = to or (ticket.student.parent_email if ticket.student else None)
        if to:
            _email(to, f'Update on {ticket.reference}: {ticket.subject}',
                   f'<p>The school has updated your {ticket.get_kind_display().lower()} '
                   f'<b>{escape(ticket.subject)}</b> — now: {ticket.get_status_display()}.</p>'
                   f'<p>Read it under Feedback on the parent portal.</p>')
    return ticket


def rate(ticket, user, stars, comment=''):
    if ticket.parent_id != user.pk:
        raise FamilyError('Only the parent who wrote it can rate it.')
    if ticket.status not in (Ticket.RESOLVED, Ticket.CLOSED):
        raise FamilyError('You can rate it once it is resolved.')
    if str(stars) not in ('1', '2', '3', '4', '5'):
        raise FamilyError('Choose from 1 to 5 stars.')
    ticket.rating, ticket.rating_comment, ticket.status = int(stars), (comment or '')[:255], Ticket.CLOSED
    ticket.save(update_fields=['rating', 'rating_comment', 'status', 'updated_at'])


def overdue(ticket, days):
    return ticket.status in (Ticket.OPEN, Ticket.PROGRESS) and ticket.created_at < timezone.now() - timedelta(days=days)


# ─────────────────────────────────────────────────────────────────────────────
# F277 — password-free sign-in
# ─────────────────────────────────────────────────────────────────────────────

LINK_MINUTES, CODE_MINUTES, MAX_REQUESTS = 20, 10, 3


def _hash(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


def find_family(tenant, contact):
    """(primary student, 'EMAIL'|'SMS', normalised contact) or (None, None, None)."""
    from users.records import _phone_key
    contact = (contact or '').strip()
    qs = Student.objects.filter(tenant=tenant, status__in=ENROLLED_STATUSES).order_by('id')
    if '@' in contact:
        st = qs.filter(parent_email__iexact=contact).first()
        return (st, 'EMAIL', contact.lower()) if st else (None, None, None)
    key = _phone_key(contact)
    if len(key) < 10:
        return None, None, None
    for st in qs.exclude(parent_phone__isnull=True).exclude(parent_phone='').only('id', 'parent_phone', 'tenant_id'):
        if _phone_key(st.parent_phone) == key:
            return Student.objects.get(pk=st.pk), 'SMS', key
    return None, None, None


def request_login(tenant, contact):
    """Send a link or code if the contact is on file. Returns the channel, or None (the page says the same either way)."""
    if not settings_for(tenant).magic_login:
        raise FamilyError('Password-free sign-in is switched off. Use your admission number and password.')
    student, channel, sent_to = find_family(tenant, contact)
    if not student:
        return None
    recent = LoginToken.objects.filter(tenant=tenant, sent_to=sent_to,
                                       created_at__gte=timezone.now() - timedelta(minutes=15)).count()
    if recent >= MAX_REQUESTS:
        return channel     # quietly stop sending; the page still says "check your phone/email"
    if channel == 'EMAIL':
        raw = secrets.token_urlsafe(32)
        LoginToken.objects.create(tenant=tenant, token_hash=_hash(raw), student=student, channel='EMAIL',
                                  sent_to=sent_to, expires_at=timezone.now() + timedelta(minutes=LINK_MINUTES))
        link = _portal(tenant, f'/family/login/{raw}/')
        _email(student.parent_email, f'Sign in to {tenant.name}',
               f'<p>Tap to sign in to the parent portal:</p><p><a href="{link}">{link}</a></p>'
               f'<p>The link works once, for {LINK_MINUTES} minutes. If you did not ask for it, ignore this email.</p>')
    else:
        from communications.services import send_sms
        code = f'{secrets.randbelow(1000000):06d}'
        LoginToken.objects.create(tenant=tenant, token_hash=_hash(f'{sent_to}:{code}'), student=student,
                                  channel='SMS', sent_to=sent_to,
                                  expires_at=timezone.now() + timedelta(minutes=CODE_MINUTES))
        send_sms(student.parent_phone, f'{tenant.name} sign-in code: {code}. It expires in {CODE_MINUTES} minutes. '
                                       f'Do not share it.')
    return channel


def parent_user(student):
    from users.models import User
    user, created = User.objects.get_or_create(
        username=f'parent_{student.id}',
        defaults={'email': student.parent_email or '', 'is_parent': True, 'is_student': False,
                  'is_staff_user': False, 'tenant': student.tenant, 'first_name': student.parent_name or ''})
    if created:
        user.set_unusable_password()
        user.save(update_fields=['password'])
    return user


def use_link(tenant, raw):
    with transaction.atomic():
        tok = LoginToken.objects.select_for_update().filter(tenant=tenant, token_hash=_hash(raw or ''),
                                                            channel='EMAIL').first()
        if not tok or not tok.live:
            raise FamilyError('This sign-in link has expired or was already used. Ask for a new one.')
        tok.used_at = timezone.now()
        tok.save(update_fields=['used_at'])
    return parent_user(tok.student)


def use_code(tenant, contact, code):
    from users.records import _phone_key
    key = _phone_key(contact or '')
    with transaction.atomic():
        tok = LoginToken.objects.select_for_update().filter(tenant=tenant, sent_to=key, channel='SMS',
                                                            used_at__isnull=True).order_by('-created_at').first()
        if not tok or not tok.live:
            raise FamilyError('That code has expired. Ask for a new one.')
        if tok.token_hash != _hash(f'{key}:{(code or "").strip()}'):
            tok.attempts += 1
            tok.save(update_fields=['attempts'])
            raise FamilyError('That code is not right. Check the SMS and try again.')
        tok.used_at = timezone.now()
        tok.save(update_fields=['used_at'])
    return parent_user(tok.student)


# ─────────────────────────────────────────────────────────────────────────────
# F280 — satisfaction score
# ─────────────────────────────────────────────────────────────────────────────

def period_for(day=None):
    """Terms run roughly Sep–Dec, Jan–Apr, May–Aug: '2026/27 T1'."""
    day = day or timezone.localdate()
    if day.month >= 9:
        y, t = day.year, 1
    elif day.month <= 4:
        y, t = day.year - 1, 2
    else:
        y, t = day.year - 1, 3
    return f'{y}/{str(y + 1)[2:]} T{t}'


def score_due(tenant, student):
    return settings_for(tenant).satisfaction_enabled and not SatisfactionResponse.objects.filter(
        tenant=tenant, family=family_key(student), period=period_for()).exists()


def record_score(tenant, user, student, score, comment=''):
    if str(score) not in [str(i) for i in range(11)]:
        raise FamilyError('Choose a score from 0 to 10.')
    try:
        with transaction.atomic():
            return SatisfactionResponse.objects.create(tenant=tenant, user=user, family=family_key(student),
                                                       period=period_for(), score=int(score),
                                                       comment=(comment or '').strip()[:2000])
    except IntegrityError:
        raise FamilyError('Your family has already given a score this term. Thank you!')


def nps(responses):
    scores = [r.score for r in responses]
    if not scores:
        return {'count': 0, 'nps': None, 'promoters': 0, 'passives': 0, 'detractors': 0, 'average': None}
    pro = sum(1 for s in scores if s >= 9)
    det = sum(1 for s in scores if s <= 6)
    return {'count': len(scores), 'nps': round(100 * (pro - det) / len(scores)), 'promoters': pro,
            'passives': len(scores) - pro - det, 'detractors': det, 'average': round(sum(scores) / len(scores), 1)}


def nps_report(tenant):
    rows = list(SatisfactionResponse.objects.filter(tenant=tenant))
    by_period = defaultdict(list)
    for r in rows:
        by_period[r.period].append(r)
    trend = [{'period': p, **nps(rs)} for p, rs in sorted(by_period.items())]
    current = period_for()
    return {'current': {'period': current, **nps(by_period.get(current, []))}, 'trend': trend,
            'comments': [r for r in rows if r.comment][:30]}
