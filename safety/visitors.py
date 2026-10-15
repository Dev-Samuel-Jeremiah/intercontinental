"""
safety/visitors.py
──────────────────
Visitors: staff expect them in advance and send a QR pass; reception checks
them in (by scanning the pass, or as a walk-in), prints a badge, and checks
them out. Anyone still on site is visible at a glance.
"""
import re

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from safety.models import Visit


class VisitError(ValueError):
    pass


def _clean(data):
    name = (data.get('name') or '').strip()
    if len(name) < 3:
        raise VisitError("Enter the visitor's full name.")
    phone = (data.get('phone') or '').strip()
    if phone and len(re.sub(r'\D', '', phone)) < 10:
        raise VisitError('Enter the full phone number, or leave it blank.')
    purpose = data.get('purpose')
    return {
        'name': name[:120], 'phone': phone[:20], 'email': (data.get('email') or '').strip()[:254],
        'organisation': (data.get('organisation') or '').strip()[:120],
        'purpose': purpose if purpose in dict(Visit.PURPOSES) else 'OTHER',
        'purpose_note': (data.get('purpose_note') or '').strip()[:200],
        'vehicle': (data.get('vehicle') or '').strip().upper()[:20],
    }


def pre_register(tenant, data, *, host=None, student=None, expected_on=None, expected_time=None,
                 user=None):
    fields = _clean(data)
    today = timezone.localdate()
    expected_on = expected_on or today
    if expected_on < today or (expected_on - today).days > 60:
        raise VisitError('Choose a day from today to two months ahead.')
    visit = Visit.objects.create(tenant=tenant, host=host, student=student, expected_on=expected_on,
                                 expected_time=expected_time, registered_by=user, **fields)
    if visit.phone or visit.email:
        from lms_project.tasks import run_in_background
        run_in_background(_send_pass, visit.pk, tenant=tenant)
    return visit


def pass_url(visit):
    from django.urls import reverse
    # Built in a background job too, where no school URLs are active — name them.
    path = reverse('safety:visit_pass', args=[visit.token], urlconf='lms_project.tenant_urls')
    return f"{visit.tenant.portal_url}{path}"


def _send_pass(visit_id):
    from communications.services import send_email, send_sms
    v = Visit.objects.select_related('tenant', 'host').get(pk=visit_id)
    when = f"{v.expected_on:%a %d %b}" + (f" at {v.expected_time:%H:%M}" if v.expected_time else '')
    link = pass_url(v)
    text = (f"{v.tenant.name}: you are expected {when}"
            + (f" to see {v.host.full_name}" if v.host else '') + f". Show this pass at the gate: {link}")
    if v.phone:
        send_sms(v.phone, text)
    if v.email:
        send_email(v.email, f'Your visitor pass — {v.tenant.name}', f'<p>{text}</p>')


def _next_badge(tenant):
    """Badge numbers start at 1 each day."""
    today = timezone.localdate()
    top = Visit.objects.filter(tenant=tenant, checked_in_at__date=today).aggregate(m=Max('badge_number'))['m']
    return (top or 0) + 1


@transaction.atomic
def check_in(visit, user, *, id_type='', id_number='', photo=None):
    if visit.status == Visit.ON_SITE:
        raise VisitError(f'{visit.name} is already checked in.')
    if visit.status in (Visit.CANCELLED, Visit.DENIED, Visit.LEFT):
        raise VisitError(f'This visit is {visit.get_status_display().lower()}.')
    if visit.expected_on != timezone.localdate():
        raise VisitError(f'{visit.name} is expected on {visit.expected_on:%d %b}, not today.')
    visit.status = Visit.ON_SITE
    visit.checked_in_at = timezone.now()
    visit.checked_in_by = user
    visit.badge_number = _next_badge(visit.tenant)
    visit.id_type = (id_type or visit.id_type)[:40]
    visit.id_number = (id_number or visit.id_number)[:40]
    visit.save()
    if photo:
        save_photo(visit, photo)
    if visit.host_id:
        from lms_project.tasks import run_in_background
        run_in_background(_tell_host, visit.pk, tenant=visit.tenant)
    return visit


def walk_in(tenant, data, user, **kw):
    visit = Visit.objects.create(tenant=tenant, registered_by=user, expected_on=timezone.localdate(),
                                 host=kw.get('host'), student=kw.get('student'), **_clean(data))
    return check_in(visit, user, id_type=kw.get('id_type', ''), id_number=kw.get('id_number', ''),
                    photo=kw.get('photo'))


def save_photo(visit, upload):
    from users.roster import RosterError, prepare_passport
    try:
        content, _ = prepare_passport(upload)
    except RosterError as problem:
        raise VisitError(f'Photo not saved: {problem}')
    visit.photo.save('visitor.jpg', content, save=True)


def _tell_host(visit_id):
    """Email the staff member being visited that their visitor is at reception."""
    from communications.services import send_email
    v = Visit.objects.select_related('host', 'host__user', 'tenant').get(pk=visit_id)
    email = getattr(getattr(v.host, 'user', None), 'email', '')
    if email:
        when = timezone.localtime(v.checked_in_at)
        send_email(email, f'Your visitor {v.name} has arrived',
                   f'<p>{v.name}' + (f' ({v.organisation})' if v.organisation else '')
                   + f' arrived at reception at {when:%H:%M} and is waiting for you.</p>')


def check_out(visit, user=None):
    if visit.status != Visit.ON_SITE:
        raise VisitError(f'{visit.name} is not checked in.')
    visit.status = Visit.LEFT
    visit.checked_out_at = timezone.now()
    visit.save(update_fields=['status', 'checked_out_at'])
    return visit


def turn_away(visit, user, reason):
    if visit.status not in (Visit.EXPECTED,):
        raise VisitError('Only an expected visitor can be turned away.')
    if not (reason or '').strip():
        raise VisitError('Say why the visitor was turned away.')
    visit.status = Visit.DENIED
    visit.note = reason.strip()[:255]
    visit.save(update_fields=['status', 'note'])
    return visit
