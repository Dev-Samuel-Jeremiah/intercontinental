"""
admissions/services.py
──────────────────────
The work behind the admissions screens: references, admission numbers, sibling
detection and turning an accepted applicant into a real Student.
"""
from users.models import ENROLLED_STATUSES
import logging
from datetime import date
from decimal import Decimal

from django.db import transaction
from django.db.models import Count
from django.utils.crypto import get_random_string

from users.models import Student, User
from users.passwords import generate_term_password

from .models import AdmissionQuota, AdmissionSettings, Application, ApplicationEvent

logger = logging.getLogger(__name__)


def get_settings(tenant):
    """The school's admission settings, created on first use."""
    settings_obj, _ = AdmissionSettings.objects.get_or_create(tenant=tenant)
    return settings_obj


def _initials(name):
    """'Royal Kidies School' → RKS, 'Intercontinental School Kano' → ISK."""
    words = [w for w in (name or '').replace('-', ' ').split() if w]
    joiners = {'of', 'the', 'and', 'for', 'de', 'la'}
    letters = [w[0] for w in words if w.lower() not in joiners] or [w[0] for w in words]
    return ''.join(letters[:3]).upper() or 'SCH'


def generate_reference(tenant):
    """A short code the parent quotes when asking about their application."""
    year = date.today().year
    for _ in range(20):
        reference = f"APP-{year}-{get_random_string(5, 'ABCDEFGHJKMNPQRSTUVWXYZ23456789')}"
        if not Application.objects.filter(tenant=tenant, reference=reference).exists():
            return reference
    return f"APP-{year}-{get_random_string(8)}"  # pragma: no cover


def build_admission_number(tenant, school_class=None, settings_obj=None):
    """Render the school's admission number format and reserve the next number.

    Format tokens: {prefix} {year} {short_year} {class} {seq}
    """
    settings_obj = settings_obj or get_settings(tenant)
    today = date.today()
    values = {
        'prefix': settings_obj.number_prefix or _initials(getattr(tenant, 'name', '')),
        'year': today.year,
        'short_year': str(today.year)[-2:],
        'class': (getattr(school_class, 'name', '') or '').replace(' ', '').upper(),
    }

    for _ in range(50):
        seq = settings_obj.next_number
        try:
            number = settings_obj.number_format.format(seq=seq, **values)
        except (KeyError, ValueError, IndexError):
            # A broken custom format must not stop an enrolment.
            logger.warning('Bad admission number format %r for %s; using the default.',
                           settings_obj.number_format, tenant)
            number = f"{values['prefix']}/{values['year']}/{seq:04d}"
        settings_obj.next_number = seq + 1
        settings_obj.save(update_fields=['next_number'])
        if not Student.objects.filter(tenant=tenant, admission_number=number).exists():
            return number
    raise ValueError('Could not generate a free admission number')  # pragma: no cover


def _phone_digits(phone):
    """Last 9 digits, so 08031234567, +234 803 123 4567 and 234-803-123-4567 match."""
    return ''.join(ch for ch in (phone or '') if ch.isdigit())[-9:]


def find_sibling(tenant, parent_email='', parent_phone=''):
    """An existing student whose guardian contact matches this applicant's."""
    if parent_email:
        match = Student.objects.filter(tenant=tenant,
                                       parent_email__iexact=parent_email.strip()).first()
        if match:
            return match

    digits = _phone_digits(parent_phone)
    if not digits:
        return None
    # Phone numbers are written every which way, so compare digits only.
    candidates = (Student.objects.filter(tenant=tenant)
                  .exclude(parent_phone__isnull=True).exclude(parent_phone='')
                  .only('id', 'parent_phone'))
    for student in candidates.iterator():
        if _phone_digits(student.parent_phone) == digits:
            return student
    return None


def seats_left(tenant, school_class):
    """Remaining seats for a class, or None when the school sets no limit."""
    quota = AdmissionQuota.objects.filter(tenant=tenant, school_class=school_class).first()
    if not quota or not quota.seats:
        return None
    taken = (Student.objects.filter(tenant=tenant, class_assigned=school_class,
                                    status__in=ENROLLED_STATUSES).count()
             + Application.objects.filter(tenant=tenant, applying_for=school_class,
                                          status__in=[Application.OFFERED,
                                                      Application.ACCEPTED]).count())
    return max(quota.seats - taken, 0)


def record_event(application, actor=None, note='', from_status='', to_status=''):
    return ApplicationEvent.objects.create(
        tenant=application.tenant, application=application, actor=actor,
        note=note, from_status=from_status, to_status=to_status)


def change_status(application, new_status, actor=None, note=''):
    old = application.status
    application.status = new_status
    application.save(update_fields=['status', 'updated_at'])
    record_event(application, actor=actor, note=note, from_status=old, to_status=new_status)
    return application


@transaction.atomic
def enrol_applicant(application, actor=None, school_class=None, send_credentials=False):
    """Turn an accepted applicant into a Student with login details.

    Returns (student, credentials) where credentials holds the plain passwords
    to show once on screen. Safe to call twice: it returns the existing student.
    """
    if application.student_id:
        return application.student, {}

    tenant = application.tenant
    school_class = school_class or application.applying_for
    admission_number = build_admission_number(tenant, school_class)

    student_password = generate_term_password()
    student_user = User.objects.create_user(
        username=admission_number, password=student_password,
        first_name=application.full_name.split(' ')[0],
        last_name=' '.join(application.full_name.split(' ')[1:]),
        is_student=True, tenant=tenant,
    )
    student_user.term_password = student_password
    student_user.save(update_fields=['term_password'])

    parent_password = generate_term_password()
    student = Student.objects.create(
        tenant=tenant, user=student_user, full_name=application.full_name,
        gender=application.gender or None, date_of_birth=application.date_of_birth,
        admission_number=admission_number, class_assigned=school_class,
        passport=application.passport or None,
        parent_name=application.parent_name, parent_phone=application.parent_phone,
        parent_email=application.parent_email or None,
        parent_term_password=parent_password, address=application.address or None,
    )

    application.student = student
    application.status = Application.ENROLLED
    application.save(update_fields=['student', 'status', 'updated_at'])
    record_event(application, actor=actor, from_status=Application.ACCEPTED,
                 to_status=Application.ENROLLED,
                 note=f'Enrolled as {admission_number}')

    credentials = {
        'admission_number': admission_number,
        'student_password': student_password,
        'parent_password': parent_password,
    }

    if send_credentials and application.parent_email:
        from communications.services import send_login_details
        from lms_project.tasks import run_in_background
        run_in_background(send_login_details, student, student_password,
                          tenant=tenant)

    return student, credentials


def funnel_counts(tenant, session=None):
    """Counts per pipeline stage, for the board header and analytics."""
    applications = Application.objects.filter(tenant=tenant)
    if session:
        applications = applications.filter(session=session)
    counts = {status: 0 for status, _ in Application.STATUS_CHOICES}
    for row in applications.values('status').annotate(n=Count('id')):
        counts[row['status']] = row['n']
    counts['TOTAL'] = sum(counts.values())
    return counts


# ─────────────────────────────────────────────────────────────────────────────
# Application fee (Paystack)
# ─────────────────────────────────────────────────────────────────────────────

def paystack_configured():
    from django.conf import settings as dj_settings
    return bool(getattr(dj_settings, 'PAYSTACK_SECRET_KEY', ''))


def start_fee_payment(application, callback_url):
    """Ask Paystack for a checkout link. Returns (url, error)."""
    import requests
    from django.conf import settings as dj_settings

    if not paystack_configured():
        return None, 'Online payment is not set up for this school yet.'
    if not application.parent_email:
        return None, 'An email address is needed to pay online.'
    amount = application.fee_amount or 0
    if amount <= 0:
        return None, 'No application fee is due.'

    try:
        response = requests.post(
            'https://api.paystack.co/transaction/initialize',
            json={
                'email': application.parent_email,
                'amount': int(Decimal(str(amount)) * 100),
                'currency': 'NGN',
                'callback_url': callback_url,
                'metadata': {
                    'purpose': 'admission_application_fee',
                    'application_reference': application.reference,
                    'tenant_id': str(application.tenant_id),
                    'custom_fields': [
                        {'display_name': 'Applicant', 'variable_name': 'applicant',
                         'value': application.full_name},
                        {'display_name': 'Reference', 'variable_name': 'reference',
                         'value': application.reference},
                    ],
                },
            },
            headers={'Authorization': f'Bearer {dj_settings.PAYSTACK_SECRET_KEY}',
                     'Content-Type': 'application/json'},
            timeout=15,
        )
        data = response.json()
        if data.get('status'):
            application.fee_reference = data['data']['reference']
            application.save(update_fields=['fee_reference', 'updated_at'])
            return data['data']['authorization_url'], None
        return None, data.get('message', 'The payment gateway refused the request.')
    except Exception as exc:                      # network, JSON, anything
        logger.exception('Paystack initialise failed for %s', application.reference)
        return None, 'Could not reach the payment gateway. Please try again.'


def verify_fee_payment(application, reference):
    """Confirm with Paystack that the money arrived. Returns (paid, error)."""
    import requests
    from django.conf import settings as dj_settings
    from django.utils import timezone

    if not paystack_configured():
        return False, 'Online payment is not set up for this school yet.'
    try:
        response = requests.get(
            f'https://api.paystack.co/transaction/verify/{reference}',
            headers={'Authorization': f'Bearer {dj_settings.PAYSTACK_SECRET_KEY}'},
            timeout=15,
        )
        data = response.json()
    except Exception:
        logger.exception('Paystack verify failed for %s', reference)
        return False, 'Could not reach the payment gateway.'

    if data.get('status') and data['data'].get('status') == 'success':
        application.fee_paid = True
        application.fee_reference = reference
        application.fee_paid_at = timezone.now()
        application.save(update_fields=['fee_paid', 'fee_reference', 'fee_paid_at',
                                        'updated_at'])
        record_event(application, note=f'Application fee paid online ({reference})')
        return True, None
    return False, 'The payment was not completed.'


# ─────────────────────────────────────────────────────────────────────────────
# Entrance exam scheduling
# ─────────────────────────────────────────────────────────────────────────────

def schedule_exam(application, when, venue='', actor=None, notify=True):
    """Set the entrance exam date and tell the parent."""
    from django.utils import timezone

    application.exam_date = when
    application.exam_venue = venue
    if application.status in (Application.NEW, Application.SCREENING):
        application.status = Application.EXAM
    application.save(update_fields=['exam_date', 'exam_venue', 'status', 'updated_at'])
    record_event(application, actor=actor, to_status=application.status,
                 note=f'Entrance exam scheduled for {when:%d %b %Y, %I:%M %p}'
                      + (f' at {venue}' if venue else ''))

    if notify and (application.parent_email or application.parent_phone):
        from lms_project.tasks import run_in_background
        run_in_background(notify_exam_scheduled, application.pk, tenant=application.tenant)
        application.exam_notified_at = timezone.now()
        application.save(update_fields=['exam_notified_at'])
    return application


def notify_exam_scheduled(application_id):
    """Email and SMS the parent about the entrance exam (runs in the background)."""
    from communications.services import send_email, send_sms

    application = Application.objects.filter(pk=application_id).first()
    if not application:
        return 'application missing'
    school = getattr(application.tenant, 'name', 'the school')
    when = application.exam_date
    when_text = when.strftime('%A, %d %B %Y at %I:%M %p') if when else 'the scheduled date'
    venue = f' at {application.exam_venue}' if application.exam_venue else ''

    if application.parent_email:
        send_email(
            application.parent_email,
            f'{school}: entrance assessment for {application.full_name}',
            f"""<html><body style="font-family:Arial,sans-serif;color:#1c1917">
<div style="max-width:600px;margin:0 auto;padding:20px">
  <h2 style="margin:0 0 4px">{school}</h2>
  <p>Dear {application.parent_name or 'Parent/Guardian'},</p>
  <p>The entrance assessment for <strong>{application.full_name}</strong> has been scheduled for
     <strong>{when_text}</strong>{venue}.</p>
  <p>Please arrive 30 minutes early with this reference: <strong>{application.reference}</strong>.</p>
  <p>Regards,<br><strong>{school}</strong></p>
</div></body></html>""",
            getattr(application, 'student', None))

    if application.parent_phone:
        send_sms(application.parent_phone,
                 f"{school}: entrance assessment for {application.full_name} is on "
                 f"{when_text}{venue}. Ref {application.reference}.")
    return f'notified {application.reference}'


# ─────────────────────────────────────────────────────────────────────────────
# Ranking & waitlist
# ─────────────────────────────────────────────────────────────────────────────

def rank_applicants(tenant, school_class=None, statuses=None):
    """Applicants ordered by total score, best first, for offer decisions."""
    applications = Application.objects.filter(tenant=tenant)
    if school_class:
        applications = applications.filter(applying_for=school_class)
    applications = applications.filter(
        status__in=statuses or [Application.SCREENING, Application.EXAM,
                                Application.INTERVIEW, Application.WAITLIST])
    ranked = sorted(applications.select_related('applying_for'),
                    key=lambda a: (a.total_score is None, -(a.total_score or 0), a.created_at))
    for position, application in enumerate(ranked, start=1):
        application.rank = position
    return ranked
