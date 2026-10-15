"""
curriculum/events.py
────────────────────
School events with replies (RSVPs), head counts and email reminders.

Reminders go out `reminder_days` before an event to everyone the event is
for — staff, parents or both — except people who have said they are not
coming. Run them daily with `python manage.py send_event_reminders`, or
press "Send reminder now" on the event page.
"""
from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape

from users.models import ENROLLED_STATUSES, Staff, Student

from .models import CalendarEvent, EventRSVP

MAX_GUESTS = 10


class EventError(ValueError):
    pass


def is_staff_user(user):
    return user.is_authenticated and (user.is_superuser or getattr(user, 'is_staff_user', False))


def can_see(user, event):
    return event.audience != 'STAFF' or is_staff_user(user)


def summary(event):
    rows = list(event.rsvps.select_related('user'))
    out = {'YES': 0, 'MAYBE': 0, 'NO': 0, 'headcount': 0, 'rows': rows}
    for r in rows:
        out[r.answer] = out.get(r.answer, 0) + 1
        out['headcount'] += r.headcount
    out['places_left'] = None if event.capacity is None else max(0, event.capacity - out['headcount'])
    return out


def reply(event, user, answer, guests=0, note=''):
    if not event.rsvp:
        raise EventError('This event is not asking for replies.')
    if not event.rsvp_open:
        raise EventError('Replies for this event have closed.')
    if not can_see(user, event):
        raise EventError('This event is for staff only.')
    if answer not in dict(EventRSVP.ANSWERS):
        raise EventError('Choose coming, maybe or not coming.')
    try:
        guests = int(guests or 0)
    except (TypeError, ValueError):
        guests = 0
    guests = max(0, min(guests, MAX_GUESTS)) if answer == EventRSVP.YES else 0
    with transaction.atomic():
        # Lock the event row so two last-minute replies can't both take the final place.
        CalendarEvent.objects.select_for_update().filter(pk=event.pk).first()
        if answer == EventRSVP.YES and event.capacity is not None:
            others = sum(r.headcount for r in event.rsvps.exclude(user=user))
            if others + 1 + guests > event.capacity:
                left = max(0, event.capacity - others)
                raise EventError(f'Only {left} place{"s" if left != 1 else ""} left.' if left else
                                 'Sorry, this event is full.')
        r, _ = EventRSVP.objects.update_or_create(
            event=event, user=user,
            defaults={'tenant_id': event.tenant_id, 'answer': answer, 'guests': guests,
                      'note': (note or '').strip()[:255]})
    return r


# ─────────────────────────────────────────────────────────────────────────────
# Reminders
# ─────────────────────────────────────────────────────────────────────────────

def recipients(event):
    """Email addresses the event is for, without anyone who said they are not coming."""
    emails = {}
    if event.audience in ('ALL', 'STAFF'):
        for s in Staff.objects.filter(tenant_id=event.tenant_id).select_related('user'):
            if s.user.email and s.user.is_active:
                emails.setdefault(s.user.email.lower(), s.full_name)
    if event.audience in ('ALL', 'PARENTS'):
        for st in Student.objects.filter(tenant_id=event.tenant_id, status__in=ENROLLED_STATUSES) \
                .exclude(parent_email__isnull=True).exclude(parent_email=''):
            emails.setdefault(st.parent_email.lower(), st.parent_name or 'Parent')
    declined = {(r.user.email or '').lower() for r in event.rsvps.filter(answer=EventRSVP.NO).select_related('user')}
    return [(e, n) for e, n in sorted(emails.items()) if e not in declined]


def _when(event):
    text = f'{event.start_date:%A %d %B %Y}'
    if event.end_date and event.end_date != event.start_date:
        text += f' to {event.end_date:%A %d %B}'
    if event.start_time:
        text += f', {event.start_time:%H:%M}'
        if event.end_time:
            text += f'–{event.end_time:%H:%M}'
    return text


def event_url(event):
    path = reverse('curriculum:event', args=[event.pk], urlconf='lms_project.tenant_urls')
    return event.tenant.portal_url + path


def send_reminder(event):
    """Email everyone the event is for. Returns how many emails went out."""
    from communications.services import send_email
    sent = 0
    where = f'<br>Where: {escape(event.venue)}' if event.venue else ''
    ask = (f'<p>Please <a href="{event_url(event)}">let us know if you are coming</a>.</p>'
           if event.rsvp and event.rsvp_open else '')
    for email, name in recipients(event):
        ok, _ = send_email(email, f'Reminder: {event.title}',
                           f'<p>Dear {escape(name)},</p><p>A reminder about <b>{escape(event.title)}</b>.<br>'
                           f'When: {escape(_when(event))}{where}</p>'
                           f'{f"<p>{escape(event.description)}</p>" if event.description else ""}{ask}'
                           f'<p>{escape(event.tenant.name)}</p>')
        sent += bool(ok)
    CalendarEvent.objects.filter(pk=event.pk).update(reminded_at=timezone.now())
    return sent


def due_reminders(tenant, today=None):
    today = today or timezone.localdate()
    out = []
    for ev in CalendarEvent.objects.filter(tenant=tenant, reminder_days__gt=0, reminded_at__isnull=True,
                                           start_date__gte=today):
        if (ev.start_date - today).days <= ev.reminder_days:
            out.append(ev)
    return out


def run_reminders(today=None):
    """Every open school's due reminders. Returns {school subdomain: emails sent}."""
    from tenants.middleware import clear_current_tenant, set_current_tenant
    from tenants.models import Tenant
    result = {}
    for tenant in Tenant.objects.filter(status__in=('ACTIVE', 'TRIAL')):
        set_current_tenant(tenant)
        try:
            for ev in due_reminders(tenant, today):
                result[tenant.subdomain] = result.get(tenant.subdomain, 0) + send_reminder(ev)
        finally:
            clear_current_tenant()
    return result
