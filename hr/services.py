"""
hr/services.py
──────────────
The rules behind the HR pages: onboarding checklists (F221), contracts and
expiry reminders (F230), appraisals (F229), training (F232), disciplinary
cases (F236), exits with final settlement (F240) and recruitment (F231).
"""
import calendar
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone
from django.utils.html import escape

from users.models import Staff

from .models import (Appraisal, AppraisalCycle, Contract, DisciplinaryCase, ExitCase, JobApplication,
                     OnboardingStep, OnboardingTask, StaffRecord, TrainingRecord)

ZERO = Decimal('0')
DEFAULT_TASKS = [
    ('Offer letter signed and returned', True), ('Contract signed', True), ('Passport photo', True),
    ('Copy of certificates', True), ('Means of identification', True), ('Guarantor form', True),
    ('Bank account details added to payroll', False), ('Pension PIN and Tax ID recorded', False),
    ('Portal account created and password given', False), ('Staff ID card issued', False),
    ('Timetable and classes assigned', False), ('Staff handbook and safeguarding policy read', False),
]
DEFAULT_HANDOVER = ['Class registers and records handed over', 'Lesson notes and schemes of work handed over',
                    'Scores for the term entered', 'Keys returned', 'School laptop / devices returned',
                    'Staff ID card returned', 'Library books returned', 'Portal access to be switched off']


class HRError(ValueError):
    pass


def check_file(upload):
    from users import records
    try:
        records.check_upload(upload)
    except records.RecordError as problem:
        raise HRError(str(problem))


def admins_emails(tenant):
    from users.models import User
    out = set()
    for u in User.objects.filter(tenant=tenant, is_active=True).exclude(email=''):
        role = getattr(u.staff_profiles.filter(tenant=tenant).first(), 'role', None)
        if u.is_superuser or role == 'ADMIN':
            out.add(u.email)
    return sorted(out)


def record_for(staff):
    obj, _ = StaffRecord.objects.get_or_create(staff=staff, defaults={'tenant_id': staff.tenant_id})
    return obj


# ─────────────────────────────────────────────────────────────────────────────
# Onboarding
# ─────────────────────────────────────────────────────────────────────────────

def ensure_tasks(tenant):
    if not OnboardingTask.objects.filter(tenant=tenant).exists():
        OnboardingTask.objects.bulk_create([OnboardingTask(tenant=tenant, title=t, needs_file=f, order=i)
                                            for i, (t, f) in enumerate(DEFAULT_TASKS)])
    return OnboardingTask.objects.filter(tenant=tenant, active=True)


def checklist(staff):
    tasks = list(ensure_tasks(staff.tenant))
    have = {s.task_id: s for s in OnboardingStep.objects.filter(staff=staff)}
    out = []
    for t in tasks:
        step = have.get(t.pk) or OnboardingStep.objects.create(tenant_id=staff.tenant_id, staff=staff, task=t)
        out.append(step)
    return out


def tick(step, user, upload=None, note='', done=True):
    if upload:
        check_file(upload)
        step.file = upload
    if done and step.task.needs_file and not step.file:
        raise HRError(f'Upload the document for "{step.task.title}" first.')
    step.done = done
    step.done_at, step.done_by = (timezone.now(), user) if done else (None, None)
    step.note = (note or step.note or '').strip()[:200]
    step.save()
    return step


def onboarding_progress(staff_list):
    """{staff_id: (done, total)}."""
    total = OnboardingTask.objects.filter(tenant_id=staff_list[0].tenant_id, active=True).count() if staff_list else 0
    done = {}
    for row in OnboardingStep.objects.filter(staff__in=staff_list, done=True, task__active=True).values('staff_id'):
        done[row['staff_id']] = done.get(row['staff_id'], 0) + 1
    return {s.pk: (done.get(s.pk, 0), total) for s in staff_list}


# ─────────────────────────────────────────────────────────────────────────────
# Contracts
# ─────────────────────────────────────────────────────────────────────────────

def add_contract(staff, kind, start, end, user, upload=None, note=''):
    if kind not in dict(Contract.KINDS):
        raise HRError('Choose the kind of contract.')
    if not start:
        raise HRError('Give the start date.')
    if end and end <= start:
        raise HRError('The contract must end after it starts.')
    if kind != 'PERMANENT' and not end:
        raise HRError('Give the end date for a contract that is not permanent.')
    c = Contract(tenant_id=staff.tenant_id, staff=staff, kind=kind, start=start, end=end, note=(note or '')[:200],
                 created_by=user)
    if upload:
        check_file(upload)
        c.file = upload
    c.save()
    return c


def current_contract(staff, day=None):
    day = day or timezone.localdate()
    return Contract.objects.filter(staff=staff, start__lte=day).filter(end__isnull=True).first() or \
        Contract.objects.filter(staff=staff, start__lte=day, end__gte=day).order_by('-end').first()


def expiring(tenant, days=60):
    today = timezone.localdate()
    latest = {}
    for c in Contract.objects.filter(tenant=tenant, end__isnull=False, end__gte=today - timedelta(days=30)) \
            .select_related('staff').order_by('staff_id', '-end'):
        latest.setdefault(c.staff_id, c)
    renewed = set(Contract.objects.filter(tenant=tenant).filter(end__isnull=True).values_list('staff_id', flat=True))
    out = [c for c in latest.values() if c.end <= today + timedelta(days=days) and c.staff_id not in renewed]
    return sorted(out, key=lambda c: c.end)


def send_reminders(tenant, days=30):
    """Email administrators once about each contract ending within `days`. Returns contracts reminded."""
    from communications.services import send_email
    due = [c for c in expiring(tenant, days) if not c.reminded_at and c.end >= timezone.localdate()]
    if not due:
        return []
    rows = ''.join(f'<li>{escape(c.staff.full_name)} — {c.get_kind_display()} ends {c.end:%d %b %Y}</li>' for c in due)
    for email in admins_emails(tenant):
        send_email(email, f'{tenant.name}: staff contracts ending soon',
                   f'<p>These contracts end in the next {days} days:</p><ul>{rows}</ul>'
                   f'<p>Renew them or start the exit process in HR.</p>')
    Contract.objects.filter(pk__in=[c.pk for c in due]).update(reminded_at=timezone.now())
    return due


# ─────────────────────────────────────────────────────────────────────────────
# Appraisals
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_CRITERIA = [('Lesson planning and preparation', 20), ('Teaching and classroom management', 25),
                    ('Student progress and assessment', 20), ('Punctuality and attendance', 15),
                    ('Teamwork and professional conduct', 10), ('Communication with parents', 10)]


def parse_criteria(text):
    out = []
    for line in (text or '').splitlines():
        line = line.strip()
        if not line:
            continue
        name, _, weight = line.rpartition(':')
        if not name:
            name, weight = weight, '1'
        try:
            w = max(1, int(float(weight.strip())))
        except ValueError:
            name, w = line, 1
        out.append({'name': name.strip()[:80], 'weight': w})
    if not out:
        raise HRError('List at least one thing to appraise, one per line, e.g. "Lesson planning: 20".')
    return out[:12]


@transaction.atomic
def start_cycle(tenant, title, opens, closes, criteria_text, user):
    if not (title and opens and closes) or closes < opens:
        raise HRError('Give a title and dates, closing after opening.')
    cycle = AppraisalCycle.objects.create(tenant=tenant, title=title[:120], opens=opens, closes=closes,
                                          criteria=parse_criteria(criteria_text), created_by=user)
    for staff in Staff.objects.filter(tenant=tenant, role__in=('TEACHER', 'ADMIN', 'ACCOUNT', 'LIB')):
        rec = StaffRecord.objects.filter(staff=staff).select_related('department__hod').first()
        hod = rec.department.hod if rec and rec.department_id and rec.department.hod_id != staff.pk else None
        Appraisal.objects.create(tenant=tenant, cycle=cycle, staff=staff, reviewer=hod)
    return cycle


def _scores(cycle, data, prefix):
    out = {}
    for i, c in enumerate(cycle.criteria):
        raw = str(data.get(f'{prefix}{i}') or '')
        if raw not in ('1', '2', '3', '4', '5'):
            raise HRError(f'Score "{c["name"]}" from 1 to 5.')
        out[c['name']] = int(raw)
    return out


def weighted(cycle, scores):
    total = sum(c['weight'] for c in cycle.criteria) or 1
    return Decimal(sum(scores.get(c['name'], 0) * c['weight'] for c in cycle.criteria) / total).quantize(Decimal('0.1'))


def self_review(appraisal, data):
    if appraisal.status != Appraisal.SELF or not appraisal.cycle.open:
        raise HRError('Your self-review has already been sent.')
    appraisal.self_scores = _scores(appraisal.cycle, data, 's')
    appraisal.self_comment = (data.get('self_comment') or '').strip()[:4000]
    appraisal.status, appraisal.submitted_at = Appraisal.REVIEW, timezone.now()
    appraisal.save()


def may_review(user, appraisal):
    from users.views_profile import can_manage
    staff = getattr(user, 'staff', None)
    if staff and staff.pk == appraisal.staff_id:
        return False
    return can_manage(user) or bool(staff and appraisal.reviewer_id == staff.pk)


def review(appraisal, user, data):
    if not may_review(user, appraisal):
        raise HRError('Only the reviewer or an administrator can review this appraisal.')
    if appraisal.status == Appraisal.DONE:
        raise HRError('This appraisal is already complete.')
    appraisal.scores = _scores(appraisal.cycle, data, 'r')
    appraisal.reviewer_comment = (data.get('reviewer_comment') or '').strip()[:4000]
    appraisal.overall = weighted(appraisal.cycle, appraisal.scores)
    appraisal.status, appraisal.reviewed_at = Appraisal.DONE, timezone.now()
    if not appraisal.reviewer_id:
        appraisal.reviewer = getattr(user, 'staff', None)
    appraisal.save()


def indicators(staff, cycle):
    """Facts from the school's records for the appraisal period, to inform the scores."""
    from curriculum.models import CoverAssignment, LessonNote
    start, end = cycle.opens - timedelta(days=120), cycle.closes
    notes = LessonNote.objects.filter(teacher=staff, created_at__date__range=(start, end))
    training = TrainingRecord.objects.filter(staff=staff, date__range=(start, end))
    return {
        'lesson_notes': notes.count(), 'notes_approved': notes.filter(status=LessonNote.APPROVED).count(),
        'notes_returned': notes.filter(status=LessonNote.RETURNED).count(),
        'cover_lessons': CoverAssignment.objects.filter(cover_teacher=staff, date__range=(start, end)).count(),
        'training_hours': sum((t.hours for t in training), ZERO),
        'queries': DisciplinaryCase.objects.filter(staff=staff, date__range=(start, end)).count(),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Training
# ─────────────────────────────────────────────────────────────────────────────

def add_training(staff, data, user, upload=None):
    from datetime import datetime
    title = (data.get('title') or '').strip()[:160]
    try:
        day = datetime.strptime(data.get('date') or '', '%Y-%m-%d').date()
        hours = Decimal(str(data.get('hours') or '0'))
        cost = Decimal(str(data.get('cost') or '0').replace(',', ''))
    except Exception:
        raise HRError('Give the date, hours and cost as numbers.')
    if not title:
        raise HRError('Name the training.')
    if hours < 0 or cost < 0:
        raise HRError('Hours and cost cannot be negative.')
    t = TrainingRecord(tenant_id=staff.tenant_id, staff=staff, title=title, date=day, hours=hours, cost=cost,
                       provider=(data.get('provider') or '').strip()[:120], added_by=user)
    if upload:
        check_file(upload)
        t.certificate = upload
    t.save()
    return t


# ─────────────────────────────────────────────────────────────────────────────
# Discipline
# ─────────────────────────────────────────────────────────────────────────────

def issue(staff, kind, allegation, respond_by, user):
    from communications.services import send_email
    allegation = (allegation or '').strip()
    if kind not in dict(DisciplinaryCase.KINDS) or not allegation:
        raise HRError('Choose the kind of letter and write what it is about.')
    case = DisciplinaryCase.objects.create(tenant_id=staff.tenant_id, staff=staff, kind=kind, allegation=allegation,
                                           date=timezone.localdate(), respond_by=respond_by, issued_by=user)
    by = f' and reply by {respond_by:%d %B %Y}' if respond_by else ' and reply'
    if staff.user.email:
        send_email(staff.user.email, f'{case.get_kind_display()} from {staff.tenant.name}',
                   f'<p>Dear {escape(staff.full_name)},</p><p>You have been issued a '
                   f'{case.get_kind_display().lower()}. Please sign in to the staff portal (My HR) to read it'
                   f'{by}.</p>')
    return case


def respond(case, staff, text):
    if case.staff_id != staff.pk:
        raise HRError('This is not your case.')
    if case.closed_at:
        raise HRError('This case is closed.')
    text = (text or '').strip()
    if not text:
        raise HRError('Write your reply.')
    case.response, case.responded_at = text[:6000], timezone.now()
    case.save(update_fields=['response', 'responded_at'])


def close_case(case, outcome, note):
    if outcome not in dict(DisciplinaryCase.OUTCOMES):
        raise HRError('Choose the outcome.')
    case.outcome, case.outcome_note, case.closed_at = outcome, (note or '').strip()[:4000], timezone.now()
    case.save(update_fields=['outcome', 'outcome_note', 'closed_at'])


# ─────────────────────────────────────────────────────────────────────────────
# Exits and final settlement
# ─────────────────────────────────────────────────────────────────────────────

def open_exit(staff, kind, notice_date, last_day, reason, user):
    if kind not in dict(ExitCase.KINDS) or not (notice_date and last_day):
        raise HRError('Choose the reason for leaving and give the notice date and last day.')
    if last_day < notice_date:
        raise HRError('The last day cannot be before the notice date.')
    if ExitCase.objects.filter(staff=staff, completed_at__isnull=True).exists():
        raise HRError(f'{staff.full_name} already has an exit in progress.')
    return ExitCase.objects.create(tenant_id=staff.tenant_id, staff=staff, kind=kind, notice_date=notice_date,
                                   last_day=last_day, reason=(reason or '')[:255],
                                   handover=[{'item': i, 'done': False} for i in DEFAULT_HANDOVER])


def settlement(case):
    """Pay for the days worked in the last month, plus or minus other amounts, less outstanding loans."""
    from payroll.models import StaffPayroll
    from safety.staff_attendance import working_days
    pr = StaffPayroll.objects.filter(staff=case.staff).first()
    if not pr:
        return {'note': 'Not on payroll.', 'total': float(case.other_amounts)}
    first = case.last_day.replace(day=1)
    last = case.last_day.replace(day=calendar.monthrange(case.last_day.year, case.last_day.month)[1])
    month_days = len(working_days(first, last, case.staff.tenant))
    worked = len(working_days(first, case.last_day, case.staff.tenant))
    final_pay = (pr.gross_monthly * worked / month_days).quantize(Decimal('0.01')) if month_days else ZERO
    loans = sum((ln.balance for ln in pr.loans.filter(active=True)), ZERO)
    total = final_pay + case.other_amounts - loans
    return {'monthly': float(pr.gross_monthly), 'worked_days': worked, 'month_days': month_days,
            'final_pay': float(final_pay), 'other': float(case.other_amounts), 'loans': float(loans),
            'total': float(total)}


@transaction.atomic
def complete_exit(case, user, disable_login=True):
    from payroll.models import StaffPayroll
    if case.completed_at:
        raise HRError('This exit is already complete.')
    pending = [h['item'] for h in case.handover if not h.get('done')]
    if pending:
        raise HRError('Finish the handover first: ' + '; '.join(pending[:3]) + ('…' if len(pending) > 3 else '.'))
    case.settlement = settlement(case)
    case.completed_at, case.completed_by = timezone.now(), user
    case.save()
    pr = StaffPayroll.objects.filter(staff=case.staff).first()
    if pr:
        pr.is_active = False
        pr.save(update_fields=['is_active'])
        pr.loans.filter(active=True).update(active=False, note='Settled from final pay')
    rec = record_for(case.staff)
    rec.left_on = case.last_day
    rec.save(update_fields=['left_on'])
    if disable_login and case.staff.user_id != getattr(user, 'id', None):
        case.staff.user.is_active = False
        case.staff.user.save(update_fields=['is_active'])


# ─────────────────────────────────────────────────────────────────────────────
# Recruitment
# ─────────────────────────────────────────────────────────────────────────────

def apply(vacancy, data, upload):
    if not vacancy.published or vacancy.closing_date < timezone.localdate():
        raise HRError('Applications for this job have closed.')
    name, email, phone = ((data.get(k) or '').strip() for k in ('full_name', 'email', 'phone'))
    if not (name and '@' in email and phone):
        raise HRError('Give your name, email address and phone number.')
    if not upload:
        raise HRError('Attach your CV as a PDF.')
    check_file(upload)
    if not upload.name.lower().endswith('.pdf'):
        raise HRError('Attach your CV as a PDF.')
    if JobApplication.objects.filter(vacancy=vacancy, email__iexact=email).exists():
        raise HRError('You have already applied for this job.')
    return JobApplication.objects.create(tenant_id=vacancy.tenant_id, vacancy=vacancy, full_name=name[:160],
                                         email=email[:254], phone=phone[:20], cv=upload,
                                         cover_letter=(data.get('cover_letter') or '').strip()[:6000])


def book_interview(app, when, venue):
    from communications.services import send_email
    if not when:
        raise HRError('Give the interview date and time.')
    app.interview_at, app.interview_venue, app.status = when, (venue or '').strip()[:160], 'INTERVIEW'
    app.save(update_fields=['interview_at', 'interview_venue', 'status'])
    local = timezone.localtime(when)
    where = f' at {escape(app.interview_venue)}' if app.interview_venue else ''
    send_email(app.email, f'Interview: {app.vacancy.title} at {app.tenant.name}',
               f'<p>Dear {escape(app.full_name)},</p><p>Thank you for applying for <b>{escape(app.vacancy.title)}</b>. '
               f'We would like to invite you to an interview on <b>{local:%A %d %B %Y at %H:%M}</b>{where}.</p>'
               f'<p>Please reply to confirm.</p><p>{escape(app.tenant.name)}</p>')
