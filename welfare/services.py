"""
welfare/services.py
───────────────────
Health, welfare and discipline logic, kept out of the views so it can be
tested: who may see what, medical profiles, immunisations, medication,
screening, accidents, discipline incidents and points thresholds,
detentions, suspension cases, restorative meetings, counselling,
safeguarding, support plans, sports, the resource hub, wellbeing surveys,
conduct reports for parents and the daily reminders.
"""
import uuid
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from django.db import IntegrityError, transaction
from django.db.models import Count, Q, Sum
from django.utils import timezone
from django.utils.html import escape

from users.models import ENROLLED_STATUSES, Guardian, Staff, Student

from .models import (Accident, Concern, ConcernAction, CounsellingCase, CounsellingNote, Detention, FitnessTest,
                     Immunisation, Incident, MedicalProfile, MedicationDose, MedicationPlan, PlanReview, Resource,
                     RestorativeMeeting, Screening, SupportPlan, SuspensionCase, TeamMember, Threshold, ThresholdHit,
                     WelfareSettings)


class WelfareError(ValueError):
    pass


def settings_for(tenant):
    cfg, _ = WelfareSettings.objects.get_or_create(tenant=tenant)
    return cfg


def staff_of(user, tenant):
    if not getattr(user, 'is_authenticated', False):
        return None
    return Staff.objects.filter(user=user, tenant=tenant).first()


def _is_admin(user):
    from users.views_profile import can_manage
    return can_manage(user)


def _in(user, tenant, field):
    staff = staff_of(user, tenant)
    return bool(staff and getattr(settings_for(tenant), field).filter(pk=staff.pk).exists())


def is_nurse(user, tenant):
    return _is_admin(user) or _in(user, tenant, 'nurses')


def is_counsellor(user, tenant):
    """Counselling notes are for the counsellors named in settings only — not even administrators read them."""
    return _in(user, tenant, 'counsellors')


def is_lead(user, tenant):
    return _is_admin(user) or _in(user, tenant, 'safeguarding_leads')


def is_discipline(user, tenant):
    return _is_admin(user) or _in(user, tenant, 'discipline_team')


def _emails(tenant, field=None, admins=True):
    from hr.services import admins_emails
    out = set(admins_emails(tenant)) if admins else set()
    if field:
        for s in getattr(settings_for(tenant), field).select_related('user'):
            if s.user.email and s.user.is_active:
                out.add(s.user.email)
    return sorted(out)


def _email(to, subject, html):
    from communications.services import send_email
    return send_email(to, subject, html)


def tell_family(student, subject, text, sms=None):
    from users.records import contacts
    for _n, e in contacts(student, 'EMAIL'):
        _email(e, subject, f'<p>{escape(text)}</p>')
    if sms:
        from communications.services import send_sms
        for _n, p in contacts(student, 'SMS'):
            send_sms(p, sms)


def _date(raw, default=None):
    try:
        return datetime.strptime(raw or '', '%Y-%m-%d').date()
    except ValueError:
        return default


def _dec(raw, label):
    raw = str(raw or '').strip()
    if not raw:
        return None
    try:
        return Decimal(raw)
    except InvalidOperation:
        raise WelfareError(f'{label} must be a number.')


def current_term(tenant):
    from teaching.services import current_term as ct
    return ct(tenant)


# ─────────────────────────────────────────────────────────────────────────────
# F382 / F400 — medical profile and health plan
# ─────────────────────────────────────────────────────────────────────────────

def profile_for(student):
    p, _ = MedicalProfile.objects.get_or_create(student=student, defaults={'tenant_id': student.tenant_id})
    return p


PARENT_FIELDS = ('blood_group', 'genotype', 'allergies', 'conditions', 'emergency_medication', 'diet',
                 'doctor_name', 'doctor_phone', 'hospital', 'other_insurance')


def save_profile(student, data, user, by_nurse=True):
    from .models import HealthPlan
    p = profile_for(student)
    for f in PARENT_FIELDS:
        if f in data:
            max_len = MedicalProfile._meta.get_field(f).max_length
            val = (data.get(f) or '').strip()
            setattr(p, f, val[:max_len] if max_len else val)
    if p.blood_group not in dict(MedicalProfile.BLOOD):
        raise WelfareError('Choose a blood group from the list.')
    if p.genotype not in dict(MedicalProfile.GENOTYPE):
        raise WelfareError('Choose a genotype from the list.')
    p.consent_first_aid = bool(data.get('consent_first_aid'))
    p.consent_hospital = bool(data.get('consent_hospital'))
    if by_nurse:
        p.notes = (data.get('notes') or '').strip()
        p.plan = HealthPlan.objects.filter(tenant_id=student.tenant_id, pk=data.get('plan') or 0).first()
        p.member_number = (data.get('member_number') or '').strip()[:60]
        p.cover_expires = _date(data.get('cover_expires'))
    p.updated_by = user
    p.save()
    if not by_nurse:
        for e in _emails(student.tenant, 'nurses', admins=False):
            _email(e, f'Medical details updated: {student.full_name}',
                   f'<p>A parent updated the medical details for {escape(student.full_name)}. '
                   f'Please check them before the next trip or sports day.</p>')
    return p


def plan_members(plan):
    return MedicalProfile.objects.filter(plan=plan).select_related('student__class_assigned') \
        .order_by('student__full_name')


# ─────────────────────────────────────────────────────────────────────────────
# F383 — immunisations
# ─────────────────────────────────────────────────────────────────────────────

def add_immunisation(student, data, user, proof=None, verified=False):
    vaccine = data.get('vaccine')
    if vaccine not in dict(Immunisation.VACCINES):
        raise WelfareError('Choose the vaccine.')
    if vaccine == 'OTHER' and not (data.get('other_name') or '').strip():
        raise WelfareError('Name the vaccine.')
    given, due = _date(data.get('given_on')), _date(data.get('next_due'))
    if not (given or due):
        raise WelfareError('Give the date it was given, or when it is due.')
    if given and given > timezone.localdate():
        raise WelfareError('The date given cannot be in the future.')
    if proof:
        from users.records import check_upload
        try:
            check_upload(proof)
        except ValueError as problem:
            raise WelfareError(str(problem))
    return Immunisation.objects.create(
        tenant_id=student.tenant_id, student=student, vaccine=vaccine,
        other_name=(data.get('other_name') or '').strip()[:80], dose=(data.get('dose') or '').strip()[:30],
        given_on=given, next_due=due, proof=proof or '', verified=verified, recorded_by=user)


def immunisations_due(tenant, days=None):
    days = settings_for(tenant).immunisation_reminder_days if days is None else days
    return Immunisation.objects.filter(tenant=tenant, next_due__isnull=False,
                                       next_due__lte=timezone.localdate() + timedelta(days=days),
                                       student__status__in=ENROLLED_STATUSES) \
        .select_related('student__class_assigned').order_by('next_due')


def send_immunisation_reminders(tenant):
    n = 0
    for imm in immunisations_due(tenant).filter(reminded_at__isnull=True):
        when = 'is due' if imm.next_due >= timezone.localdate() else 'was due'
        tell_family(imm.student, f'Vaccination reminder: {imm.student.full_name}',
                    f'{imm.student.full_name}\'s {imm.name}{" " + imm.dose if imm.dose else ""} {when} on '
                    f'{imm.next_due:%d %B %Y}. Once it is done, please add it on the parent portal so the school '
                    f'record is up to date.')
        imm.reminded_at = timezone.now()
        imm.save(update_fields=['reminded_at'])
        n += 1
    return n


# ─────────────────────────────────────────────────────────────────────────────
# F384 — medication
# ─────────────────────────────────────────────────────────────────────────────

def add_plan(student, data, user):
    medicine, dose, times = ((data.get(k) or '').strip() for k in ('medicine', 'dose', 'times'))
    if not (medicine and dose and times):
        raise WelfareError('Give the medicine, the dose and when it is given.')
    starts = _date(data.get('starts'), timezone.localdate())
    ends = _date(data.get('ends'))
    if ends and ends < starts:
        raise WelfareError('The end date is before the start.')
    return MedicationPlan.objects.create(tenant_id=student.tenant_id, student=student, medicine=medicine[:120],
                                         dose=dose[:60], times=times[:60], starts=starts, ends=ends,
                                         reason=(data.get('reason') or '').strip()[:160],
                                         parent_consent=bool(data.get('parent_consent')), added_by=user)


def plans_today(tenant, day=None):
    day = day or timezone.localdate()
    plans = list(MedicationPlan.objects.filter(tenant=tenant, active=True, starts__lte=day)
                 .filter(Q(ends__isnull=True) | Q(ends__gte=day)).select_related('student__class_assigned'))
    given = set(MedicationDose.objects.filter(tenant=tenant, given_at__date=day, plan__isnull=False)
                .values_list('plan_id', flat=True))
    for p in plans:
        p.given_today = p.pk in given
    return plans


def give_dose(student, user, data, plan=None):
    medicine = (data.get('medicine') or (plan.medicine if plan else '')).strip()
    dose = (data.get('dose') or (plan.dose if plan else '')).strip()
    if not (medicine and dose):
        raise WelfareError('Give the medicine and the dose.')
    if plan and not plan.parent_consent:
        raise WelfareError('There is no parent consent on file for this medicine.')
    if not plan and not profile_for(student).consent_first_aid:
        raise WelfareError(f'{student.full_name}\'s parents have not agreed to first-aid medicine.')
    d = MedicationDose.objects.create(tenant_id=student.tenant_id, student=student, plan=plan, medicine=medicine[:120],
                                      dose=dose[:60], given_at=timezone.now(),
                                      reason=(data.get('reason') or '').strip()[:160],
                                      refused=bool(data.get('refused')), note=(data.get('note') or '').strip()[:255],
                                      given_by=user)
    if not plan:
        tell_family(student, f'Medicine given at school: {student.full_name}',
                    f'{student.full_name} was given {medicine} ({dose}) at {timezone.localtime(d.given_at):%-I:%M %p}'
                    f'{" for " + d.reason if d.reason else ""}.')
    return d


# ─────────────────────────────────────────────────────────────────────────────
# F385 — screening
# ─────────────────────────────────────────────────────────────────────────────

def save_screening(student, data, user):
    s = Screening(tenant_id=student.tenant_id, student=student, by=user,
                  date=_date(data.get('date'), timezone.localdate()), term=current_term(student.tenant),
                  height_cm=_dec(data.get('height_cm'), 'Height'), weight_kg=_dec(data.get('weight_kg'), 'Weight'),
                  vision_left=(data.get('vision_left') or '').strip()[:10],
                  vision_right=(data.get('vision_right') or '').strip()[:10],
                  hearing_ok={'1': True, '0': False}.get(str(data.get('hearing_ok'))),
                  dental_ok={'1': True, '0': False}.get(str(data.get('dental_ok'))),
                  notes=(data.get('notes') or '').strip()[:255], refer=bool(data.get('refer')))
    if s.height_cm is not None and not 40 <= s.height_cm <= 230:
        raise WelfareError('Height looks wrong — give it in centimetres.')
    if s.weight_kg is not None and not 5 <= s.weight_kg <= 200:
        raise WelfareError('Weight looks wrong — give it in kilograms.')
    s.save()
    if s.refer:
        tell_family(student, f'Health check: {student.full_name}',
                    f'At the school health check on {s.date:%d %B}, the nurse suggested {student.full_name} sees a '
                    f'doctor{": " + s.notes if s.notes else ""}.')
    return s


def screening_sheet(cls, term=None):
    """Latest screening per student in a class, for a class-by-class health check."""
    rows = []
    for st in Student.objects.filter(class_assigned=cls, status__in=ENROLLED_STATUSES).order_by('full_name'):
        qs = st.screenings.all()
        if term:
            qs = qs.filter(term=term)
        rows.append((st, qs.first()))
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# F393 — accidents
# ─────────────────────────────────────────────────────────────────────────────

def report_accident(tenant, user, data, student=None):
    if not (student or (data.get('other_person') or '').strip()):
        raise WelfareError('Choose the student, or name the person who was hurt.')
    if data.get('place') not in dict(Accident.PLACES) or not (data.get('injury') or '').strip():
        raise WelfareError('Say where it happened and what the injury is.')
    try:
        at = timezone.make_aware(datetime.strptime(data.get('happened_at') or '', '%Y-%m-%dT%H:%M'))
    except ValueError:
        at = timezone.now()
    severity = data.get('severity') if data.get('severity') in dict(Accident.SEVERITY) else 'MINOR'
    a = Accident.objects.create(
        tenant=tenant, student=student, other_person=(data.get('other_person') or '').strip()[:160],
        happened_at=at, place=data['place'], activity=(data.get('activity') or '').strip()[:160],
        injury=data['injury'].strip()[:255], body_part=(data.get('body_part') or '').strip()[:80],
        first_aid=(data.get('first_aid') or '').strip(), severity=severity,
        hospital=bool(data.get('hospital')) or severity == 'SERIOUS',
        witnesses=(data.get('witnesses') or '').strip()[:255], reported_by=user)
    if student:
        text = (f'{student.full_name} had an accident at school ({a.get_place_display().lower()}) at '
                f'{timezone.localtime(at):%-I:%M %p}: {a.injury}.'
                f'{" First aid: " + a.first_aid if a.first_aid else ""}')
        sms = None
        if severity != 'MINOR':
            sms = f'{tenant.name}: {student.full_name} was hurt at school ({a.injury[:60]}). Please call the school.'
        tell_family(student, f'Accident report: {student.full_name}', text, sms=sms)
        a.parent_told = True
        a.save(update_fields=['parent_told'])
    if severity != 'MINOR':
        for e in _emails(tenant, 'nurses'):
            _email(e, f'Accident ({a.get_severity_display()}): {a.who}',
                   f'<p>{escape(a.who)} — {escape(a.injury)} at the {escape(a.get_place_display().lower())}.</p>')
    return a


# ─────────────────────────────────────────────────────────────────────────────
# F394 — emergency contacts
# ─────────────────────────────────────────────────────────────────────────────

def emergency_card(student):
    guardians = list(Guardian.objects.filter(student=student).order_by('-emergency_contact', 'full_name'))
    profile = MedicalProfile.objects.filter(student=student).first()
    phones = []
    if student.parent_phone:
        phones.append((student.parent_name or 'Parent', 'Main contact', student.parent_phone))
    for g in guardians:
        if g.phone:
            phones.append((g.full_name, g.get_relationship_display() + (' · emergency' if g.emergency_contact
                                                                          else ''), g.phone))
    return {'student': student, 'phones': phones, 'medical': profile,
            'alerts': profile.alerts if profile else []}


def find_students(tenant, q, limit=20):
    q = (q or '').strip()
    if not q:
        return []
    return list(Student.objects.filter(tenant=tenant, status__in=ENROLLED_STATUSES)
                .filter(Q(full_name__icontains=q) | Q(admission_number__iexact=q))
                .select_related('class_assigned').order_by('full_name')[:limit])


# ─────────────────────────────────────────────────────────────────────────────
# F386 / F387 — incidents, points and thresholds
# ─────────────────────────────────────────────────────────────────────────────

def report_incident(tenant, user, students, data):
    if not students:
        raise WelfareError('Choose at least one student.')
    if data.get('category') not in dict(Incident.CATEGORIES) or not (data.get('description') or '').strip():
        raise WelfareError('Choose what happened and describe it.')
    severity = int(data['severity']) if str(data.get('severity')) in ('1', '2', '3', '4') else 1
    action = data.get('action') if data.get('action') in dict(Incident.ACTIONS) else ''
    demerits = str(data.get('demerits') or '0')
    demerits = min(int(demerits), 50) if demerits.isdigit() else 0
    day = _date(data.get('date'), timezone.localdate())
    if day > timezone.localdate():
        raise WelfareError('The date cannot be in the future.')
    group = uuid.uuid4().hex[:12] if len(students) > 1 else ''
    visible = str(data.get('parent_visible', '1')) != '0'
    cfg = settings_for(tenant)
    out = []
    for st in students:
        inc = Incident.objects.create(
            tenant=tenant, student=st, group=group, date=day, place=(data.get('place') or '').strip()[:120],
            category=data['category'], severity=severity, description=data['description'].strip(), action=action,
            action_note=(data.get('action_note') or '').strip()[:255], demerits=demerits, parent_visible=visible,
            reported_by=user)
        if cfg.tell_parents_incidents and visible:
            tell_family(st, f'Behaviour report: {st.full_name}',
                        f'{st.full_name} was involved in an incident at school on {day:%d %B} '
                        f'({inc.get_category_display().lower()}).'
                        f'{" Action: " + inc.get_action_display() + "." if action else ""}'
                        f'{" " + inc.action_note if inc.action_note else ""} '
                        f'Please speak with your child; contact the school if you would like to discuss it.')
            inc.parents_told_at = timezone.now()
            inc.save(update_fields=['parents_told_at'])
        if severity >= 3:
            for e in _emails(tenant, 'discipline_team'):
                _email(e, f'Serious incident: {st.full_name}',
                       f'<p>{escape(st.full_name)} — {escape(inc.get_category_display())} '
                       f'({escape(inc.get_severity_display())}).</p><p>{escape(inc.description)}</p>')
        check_thresholds(st)
        out.append(inc)
    return out


def term_window(tenant):
    term = current_term(tenant)
    if term and term.start_date:
        return term, term.start_date, term.end_date or timezone.localdate()
    today = timezone.localdate()
    return term, today - timedelta(days=90), today


def points(student, start=None, end=None):
    from teaching.models import BehaviourEntry
    if start is None:
        _t, start, end = term_window(student.tenant)
    entries = BehaviourEntry.objects.filter(student=student, date__gte=start, date__lte=end)
    merits = entries.filter(points__gt=0).aggregate(s=Sum('points'))['s'] or 0
    minus = -(entries.filter(points__lt=0).aggregate(s=Sum('points'))['s'] or 0)
    minus += Incident.objects.filter(student=student, date__gte=start, date__lte=end) \
        .aggregate(s=Sum('demerits'))['s'] or 0
    return {'merits': merits, 'demerits': minus}


def check_thresholds(student):
    """Record (once a term) and announce every merit or demerit threshold the student has now reached."""
    tenant = student.tenant
    term, start, end = term_window(tenant)
    pts = points(student, start, end)
    hits = []
    for th in Threshold.objects.filter(tenant=tenant):
        total = pts['merits'] if th.kind == 'MERIT' else pts['demerits']
        if total < th.points or ThresholdHit.objects.filter(threshold=th, student=student, term=term).exists():
            continue
        try:
            with transaction.atomic():
                hit = ThresholdHit.objects.create(tenant=tenant, threshold=th, student=student, term=term,
                                                  total=total)
        except IntegrityError:
            continue
        hits.append(hit)
        word = 'merit' if th.kind == 'MERIT' else 'demerit'
        if th.tell_parents:
            tell_family(student, f'{th.name}: {student.full_name}',
                        f'{student.full_name} has reached {total} {word} points this term — {th.name}. '
                        f'{th.action}')
        cls = student.class_assigned
        to = set(_emails(tenant, 'discipline_team') if th.kind == 'DEMERIT' else [])
        if cls:
            to.update(s.user.email for s in cls.form_teachers.select_related('user') if s.user.email)
        for e in to:
            _email(e, f'{th.name}: {student.full_name}',
                   f'<p>{escape(student.full_name)} has {total} {word} points this term. Next step: '
                   f'{escape(th.action)}</p>')
    return hits


def leaderboard(tenant, kind='MERIT', limit=20):
    _t, start, end = term_window(tenant)
    rows = []
    for st in Student.objects.filter(tenant=tenant, status__in=ENROLLED_STATUSES).select_related('class_assigned'):
        p = points(st, start, end)
        n = p['merits'] if kind == 'MERIT' else p['demerits']
        if n:
            rows.append((st, n))
    rows.sort(key=lambda r: (-r[1], r[0].full_name))
    return rows[:limit]


# ─────────────────────────────────────────────────────────────────────────────
# F388 — detentions
# ─────────────────────────────────────────────────────────────────────────────

def set_detention(session, student, reason, user, incident=None):
    if not (reason or '').strip():
        raise WelfareError('Give the reason for the detention.')
    if session.date < timezone.localdate():
        raise WelfareError('That session has passed.')
    if session.detentions.count() >= session.capacity:
        raise WelfareError('That session is full.')
    try:
        with transaction.atomic():
            d = Detention.objects.create(tenant_id=session.tenant_id, session=session, student=student,
                                         reason=reason.strip()[:255], incident=incident, set_by=user)
    except IntegrityError:
        raise WelfareError(f'{student.full_name} is already in that detention.')
    tell_family(student, f'Detention: {student.full_name}',
                f'{student.full_name} has a detention on {session.date:%A %d %B} from {session.starts:%-I:%M %p} to '
                f'{session.ends:%-I:%M %p} in {session.room}. Reason: {d.reason}. Please arrange collection after it '
                f'ends.', sms=f'{student.tenant.name}: {student.full_name} has detention {session.date:%a %d %b} '
                              f'{session.starts:%-I:%M %p}-{session.ends:%-I:%M %p}.')
    return d


def mark_detentions(session, statuses):
    """statuses: {detention_id: 'ATTENDED'|'MISSED'|'EXCUSED'}."""
    n = 0
    for d in session.detentions.select_related('student'):
        new = statuses.get(str(d.pk))
        if new not in ('ATTENDED', 'MISSED', 'EXCUSED') or new == d.status:
            continue
        d.status = new
        d.save(update_fields=['status'])
        n += 1
        if new == 'MISSED':
            tell_family(d.student, f'Detention missed: {d.student.full_name}',
                        f'{d.student.full_name} did not attend the detention on {session.date:%d %B}. The school '
                        f'will be in touch about what happens next.')
    return n


# ─────────────────────────────────────────────────────────────────────────────
# F389 — suspensions
# ─────────────────────────────────────────────────────────────────────────────

def propose_suspension(student, user, data, incident=None):
    starts, ends = _date(data.get('starts')), _date(data.get('ends'))
    if not (starts and ends) or ends < starts:
        raise WelfareError('Give the first and last day of the suspension.')
    if not (data.get('reason') or '').strip():
        raise WelfareError('Give the reason.')
    if SuspensionCase.objects.filter(student=student, status__in=('PROPOSED', 'APPROVED')).exists():
        raise WelfareError(f'{student.full_name} already has a suspension in progress.')
    case = SuspensionCase.objects.create(tenant_id=student.tenant_id, student=student, incident=incident,
                                         reason=data['reason'].strip(), starts=starts, ends=ends, proposed_by=user)
    for e in _emails(student.tenant):
        _email(e, f'Suspension to approve: {student.full_name}',
               f'<p>{escape(student.full_name)}, {starts:%d %b} to {ends:%d %b} ({case.days} days): '
               f'{escape(case.reason)}</p>')
    return case


def letter_text(case):
    st = case.student
    return (f'Dear {st.parent_name or "Parent/Guardian"},\n\n'
            f'This letter is to inform you that {st.full_name} ({st.class_assigned or ""}) has been suspended from '
            f'school from {case.starts:%A %d %B %Y} to {case.ends:%A %d %B %Y} inclusive ({case.days} school '
            f'day{"s" if case.days != 1 else ""}).\n\nReason: {case.reason}\n\n'
            f'During the suspension your child should not come to school and should complete any work set. '
            f'{st.full_name.split()[0]} may return after a meeting with you and the school'
            f'{" on " + timezone.localtime(case.meeting_at).strftime("%A %d %B at %-I:%M %p") if case.meeting_at else ""}.'
            f'\n\nYours faithfully,\n\nThe Principal\n{st.tenant.name}')


@transaction.atomic
def decide_suspension(case, user, approve, note='', meeting_at=None):
    if case.status != 'PROPOSED':
        raise WelfareError('This suspension has already been decided.')
    case.decided_by, case.decision_note = user, (note or '')[:255]
    if meeting_at:
        case.meeting_at = meeting_at
    if not approve:
        case.status = 'DECLINED'
        case.save()
        return case
    from users.lifecycle import StatusError, change_status
    try:
        change_status(case.student, 'Suspended', reason=case.reason[:255], effective_on=case.starts,
                      until=case.ends + timedelta(days=1), user=user)
    except StatusError as problem:
        raise WelfareError(str(problem))
    case.status = 'APPROVED'
    case.save()
    st = case.student
    from users.records import contacts
    html = '<p>' + escape(letter_text(case)).replace('\n\n', '</p><p>').replace('\n', '<br>') + '</p>'
    for _n, e in contacts(st, 'EMAIL'):
        _email(e, f'Suspension letter: {st.full_name}', html)
    return case


@transaction.atomic
def record_return(case, user, notes):
    if case.status != 'APPROVED':
        raise WelfareError('Only an approved suspension can end with a return meeting.')
    if not (notes or '').strip():
        raise WelfareError('Note what was agreed at the return meeting.')
    case.meeting_notes = notes.strip()
    case.meeting_at = case.meeting_at or timezone.now()
    case.status = 'RETURNED'
    case.save()
    from users.lifecycle import StatusError, change_status
    if case.student.status == 'Suspended':
        try:
            change_status(case.student, 'Active', reason='Returned after suspension meeting', user=user)
        except StatusError as problem:
            raise WelfareError(str(problem))
    return case


# ─────────────────────────────────────────────────────────────────────────────
# F399 — restorative conversations
# ─────────────────────────────────────────────────────────────────────────────

def save_restorative(tenant, user, students, data, incident=None):
    if len(students) < 1 or not (data.get('what_happened') or '').strip() or not (data.get('agreement') or '').strip():
        raise WelfareError('Choose the students and record what happened and what was agreed.')
    m = RestorativeMeeting.objects.create(
        tenant=tenant, incident=incident, date=_date(data.get('date'), timezone.localdate()),
        others=(data.get('others') or '').strip()[:255], what_happened=data['what_happened'].strip(),
        who_was_affected=(data.get('who_was_affected') or '').strip(), agreement=data['agreement'].strip(),
        outcome=data.get('outcome') if data.get('outcome') in dict(RestorativeMeeting.OUTCOMES) else 'RESOLVED',
        follow_up_on=_date(data.get('follow_up_on')), facilitator=user)
    m.students.set(students)
    return m


# ─────────────────────────────────────────────────────────────────────────────
# F390 — counselling
# ─────────────────────────────────────────────────────────────────────────────

def request_help(student, reason, text=''):
    if CounsellingCase.objects.filter(student=student, status='REQUESTED').exists():
        raise WelfareError('You have already asked — a counsellor will find you soon.')
    case = CounsellingCase.objects.create(
        tenant_id=student.tenant_id, student=student, status='REQUESTED', self_referred=True,
        reason=reason if reason in dict(CounsellingCase.REASONS) else 'OTHER', summary=(text or '').strip()[:255],
        opened_by=student.user)
    for e in _emails(student.tenant, 'counsellors', admins=False):
        _email(e, 'A student would like to talk',
               f'<p>{escape(student.full_name)} ({escape(str(student.class_assigned or ""))}) asked to see a '
               f'counsellor. Open the counselling page for details.</p>')
    return case


def open_case(student, user, data):
    staff = staff_of(user, student.tenant)
    return CounsellingCase.objects.create(
        tenant_id=student.tenant_id, student=student, opened_by=user, counsellor=staff,
        reason=data.get('reason') if data.get('reason') in dict(CounsellingCase.REASONS) else 'OTHER',
        summary=(data.get('summary') or '').strip()[:255],
        risk=data.get('risk') if data.get('risk') in ('LOW', 'MEDIUM', 'HIGH') else 'LOW')


def add_note(case, user, data):
    if not (data.get('text') or '').strip():
        raise WelfareError('Write the note.')
    if case.status == 'REQUESTED':
        case.status = 'OPEN'
        case.counsellor = case.counsellor or staff_of(user, case.tenant)
        case.save(update_fields=['status', 'counsellor'])
    if data.get('risk') in ('LOW', 'MEDIUM', 'HIGH') and data['risk'] != case.risk:
        case.risk = data['risk']
        case.save(update_fields=['risk'])
    return CounsellingNote.objects.create(tenant_id=case.tenant_id, case=case, author=user,
                                          date=_date(data.get('date'), timezone.localdate()),
                                          text=data['text'].strip(), next_session=_date(data.get('next_session')))


def close_case(case):
    case.status, case.closed_at = 'CLOSED', timezone.now()
    case.save(update_fields=['status', 'closed_at'])


# ─────────────────────────────────────────────────────────────────────────────
# F391 — safeguarding
# ─────────────────────────────────────────────────────────────────────────────

def raise_concern(student, user, data):
    if data.get('category') not in dict(Concern.CATEGORIES) or not (data.get('description') or '').strip():
        raise WelfareError('Choose the kind of concern and describe what you saw or heard.')
    c = Concern.objects.create(tenant_id=student.tenant_id, student=student, category=data['category'],
                               description=data['description'].strip(),
                               immediate_danger=bool(data.get('immediate_danger')), raised_by=user)
    urgent = 'URGENT — ' if c.immediate_danger else ''
    for e in _emails(student.tenant, 'safeguarding_leads'):
        _email(e, f'{urgent}Safeguarding concern raised',
               f'<p>A {escape(c.get_category_display().lower())} concern about {escape(student.full_name)} was raised by '
               f'{escape(user.get_full_name() or user.username)}.{" The child may be in immediate danger." if c.immediate_danger else ""}'
               f'</p><p>Open the safeguarding page to read it. Do not forward this email.</p>')
    return c


def act_on_concern(concern, user, text, status='', lead=None, referred_to=''):
    if not (text or '').strip():
        raise WelfareError('Record what was done.')
    if status and status not in dict(Concern.STATUSES):
        raise WelfareError('Choose a status.')
    ConcernAction.objects.create(tenant_id=concern.tenant_id, concern=concern, text=text.strip(),
                                 status=status or '', by=user)
    fields = []
    if status and status != concern.status:
        concern.status = status
        fields.append('status')
    if lead and lead != concern.lead:
        concern.lead = lead
        fields.append('lead')
    if referred_to:
        concern.referred_to = referred_to.strip()[:160]
        fields.append('referred_to')
    if fields:
        concern.save(update_fields=fields)
    return concern


def concerns_for(user, tenant):
    qs = Concern.objects.filter(tenant=tenant).select_related('student__class_assigned', 'lead', 'raised_by')
    return qs if is_lead(user, tenant) else qs.filter(raised_by=user)


# ─────────────────────────────────────────────────────────────────────────────
# F397 — support plans
# ─────────────────────────────────────────────────────────────────────────────

def save_plan(student, user, data, plan=None):
    for f in ('needs', 'goals', 'strategies'):
        if not (data.get(f) or '').strip():
            raise WelfareError('Fill in the needs, goals and strategies.')
    starts = _date(data.get('starts'), timezone.localdate())
    review = _date(data.get('review_on'))
    if not review or review <= starts:
        raise WelfareError('Choose a review date after the start.')
    plan = plan or SupportPlan(tenant_id=student.tenant_id, student=student, created_by=user)
    plan.title = (data.get('title') or 'Individual support plan').strip()[:160]
    plan.needs, plan.goals, plan.strategies = (data[f].strip() for f in ('needs', 'goals', 'strategies'))
    plan.strengths = (data.get('strengths') or '').strip()
    plan.owner = Staff.objects.filter(tenant_id=student.tenant_id, pk=data.get('owner') or 0).first()
    if plan.review_on != review:
        plan.reminded_at = None
    plan.starts, plan.review_on = starts, review
    plan.parent_agreed = bool(data.get('parent_agreed'))
    plan.save()
    return plan


def review_plan(plan, user, data):
    if not (data.get('progress') or '').strip():
        raise WelfareError('Describe the progress.')
    r = PlanReview.objects.create(tenant_id=plan.tenant_id, plan=plan, by=user,
                                  date=_date(data.get('date'), timezone.localdate()), progress=data['progress'].strip(),
                                  rating=data.get('rating') if data.get('rating') in ('BEHIND', 'SOME', 'MET') else 'SOME',
                                  next_steps=(data.get('next_steps') or '').strip())
    nxt = _date(data.get('next_review'))
    if data.get('close'):
        plan.status = 'CLOSED'
    elif nxt:
        plan.review_on, plan.reminded_at = nxt, None
    plan.save()
    return r


def plans_due(tenant, days=7):
    return SupportPlan.objects.filter(tenant=tenant, status='ACTIVE',
                                      review_on__lte=timezone.localdate() + timedelta(days=days)) \
        .select_related('student', 'owner__user')


# ─────────────────────────────────────────────────────────────────────────────
# F396 — sports and fitness
# ─────────────────────────────────────────────────────────────────────────────

def add_member(team, student, position='', captain=False):
    try:
        with transaction.atomic():
            m = TeamMember.objects.create(tenant_id=team.tenant_id, team=team, student=student,
                                          position=(position or '')[:40], captain=captain)
    except IntegrityError:
        raise WelfareError(f'{student.full_name} is already in the team.')
    if captain:
        team.members.exclude(pk=m.pk).update(captain=False)
    return m


def record_fitness(student, test, value, user, day=None):
    if test not in dict((t[0], t[1]) for t in FitnessTest.TESTS):
        raise WelfareError('Choose the test.')
    v = _dec(value, 'The result')
    if v is None or v <= 0:
        raise WelfareError('Give the result.')
    return FitnessTest.objects.create(tenant_id=student.tenant_id, student=student, test=test, value=v,
                                      date=day or timezone.localdate(), by=user)


def personal_bests(student):
    out = {}
    for t in FitnessTest.TESTS:
        qs = student.fitness_tests.filter(test=t[0]).order_by('value' if t[3] else '-value')
        best = qs.first()
        if best:
            out[t[1]] = (best, qs.count())
    return out


def fitness_board(tenant, test, limit=15):
    lower = FitnessTest.lower_is_better(test)
    best = {}
    for f in FitnessTest.objects.filter(tenant=tenant, test=test).select_related('student__class_assigned'):
        cur = best.get(f.student_id)
        if cur is None or (f.value < cur.value if lower else f.value > cur.value):
            best[f.student_id] = f
    return sorted(best.values(), key=lambda f: f.value if lower else -f.value)[:limit]


# ─────────────────────────────────────────────────────────────────────────────
# F398 — resource hub; F395 — wellbeing survey
# ─────────────────────────────────────────────────────────────────────────────

STARTERS = [
    ('STRESS', 'BOTH', 'Feeling stressed about exams', 'Stress before exams is normal. Break revision into short '
     'sessions with breaks, sleep well the night before, and talk to a teacher if you feel stuck. Nobody expects you '
     'to know everything.'),
    ('MOOD', 'STUDENTS', 'When you feel low or worried', 'Tell someone you trust — a parent, your form teacher, the '
     'school counsellor or a friend. You can ask to see the counsellor privately from "My wellbeing". Feelings change, '
     'and help works.'),
    ('FRIENDS', 'BOTH', 'Bullying: what to do', 'Bullying is never your fault. Keep a note of what happened, tell an '
     'adult, and use the school\'s "Report bullying" page (you can stay anonymous). The school will act.'),
    ('ONLINE', 'BOTH', 'Staying safe online', 'Never share passwords or photos you would not want everyone to see. '
     'Block and report anyone who makes you uncomfortable, and tell an adult. Parents: keep devices in shared rooms '
     'at night.'),
    ('SLEEP', 'BOTH', 'Sleep, food and exercise', 'Teenagers need 8–10 hours of sleep. Breakfast, water and some '
     'exercise every day help concentration and mood more than late-night cramming.'),
    ('PARENTING', 'PARENTS', 'Talking with your child', 'Ask open questions ("What was the best part of today?"), '
     'listen without rushing to fix things, and let the school know early if something changes at home.'),
    ('HELP', 'BOTH', 'Getting help at school', 'The school nurse, counsellor, form teachers and principal are here '
     'to help. In an emergency, call the school office or go straight to the nearest hospital.'),
]


def ensure_starters(tenant):
    if Resource.objects.filter(tenant=tenant).exists():
        return 0
    for i, (topic, aud, title, body) in enumerate(STARTERS):
        Resource.objects.create(tenant=tenant, topic=topic, audience=aud, title=title, body=body, order=i)
    return len(STARTERS)


def resources_for(tenant, audience):
    ensure_starters(tenant)
    return Resource.objects.filter(tenant=tenant, published=True, audience__in=('BOTH', audience))


WELLBEING_QUESTIONS = """[rating] How happy are you at school at the moment?
[rating] How safe do you feel at school?
[rating] How well are you sleeping?
[rating] How manageable is your schoolwork?
Do you have at least one adult at school you could talk to? | Yes | Not sure | No
Have you been bullied or left out this term? | No | Once or twice | Often
[text] Is there anything the school should know or do? (No names are recorded.)"""


def start_wellbeing_survey(tenant, user, closes):
    from communications.engage import CommError, create_survey
    term = current_term(tenant)
    try:
        title = f'Wellbeing check — {term.name}' if term else f'Wellbeing check — {timezone.localdate():%B %Y}'
        return create_survey(tenant, user, title,
                             'A short anonymous check on how you are doing. Your name is not shown with your '
                             'answers.', 'STUDENTS', True, closes, WELLBEING_QUESTIONS)
    except CommError as problem:
        raise WelfareError(str(problem))


# ─────────────────────────────────────────────────────────────────────────────
# F392 — conduct report for parents
# ─────────────────────────────────────────────────────────────────────────────

def conduct_report(student):
    from teaching.models import BehaviourEntry
    term, start, end = term_window(student.tenant)
    pts = points(student, start, end)
    entries = BehaviourEntry.objects.filter(student=student, date__gte=start, date__lte=end)
    praise = list(entries.filter(points__gt=0).values('category').annotate(n=Count('id')).order_by('-n')[:5])
    labels = dict(BehaviourEntry.POSITIVE + BehaviourEntry.NEGATIVE)
    for p in praise:
        p['label'] = labels.get(p['category'], p['category'])
    return {
        'term': term, 'start': start, 'end': end, 'merits': pts['merits'], 'demerits': pts['demerits'],
        'praise': praise,
        'incidents': list(Incident.objects.filter(student=student, parent_visible=True, date__gte=start,
                                                  date__lte=end)),
        'detentions': list(Detention.objects.filter(student=student, session__date__gte=start,
                                                    session__date__lte=end).select_related('session')),
        'hits': list(ThresholdHit.objects.filter(student=student, reached_at__date__gte=start)
                     .select_related('threshold')),
        'restorative': RestorativeMeeting.objects.filter(students=student, date__gte=start, date__lte=end).count(),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Daily reminders
# ─────────────────────────────────────────────────────────────────────────────

def send_reminders(tenant):
    counts = {'immunisations': send_immunisation_reminders(tenant), 'plans': 0, 'follow_ups': 0}
    now = timezone.now()
    for plan in plans_due(tenant).filter(reminded_at__isnull=True):
        to = plan.owner.user.email if plan.owner and plan.owner.user.email else None
        for e in ([to] if to else _emails(tenant)):
            _email(e, f'Support plan review due: {plan.student.full_name}',
                   f'<p>The support plan "{escape(plan.title)}" for {escape(plan.student.full_name)} is due for '
                   f'review on {plan.review_on:%d %B}.</p>')
        plan.reminded_at = now
        plan.save(update_fields=['reminded_at'])
        counts['plans'] += 1
    for m in RestorativeMeeting.objects.filter(tenant=tenant, followed_up=False,
                                               follow_up_on=timezone.localdate()).select_related('facilitator'):
        if m.facilitator and m.facilitator.email:
            names = ', '.join(s.full_name for s in m.students.all())
            _email(m.facilitator.email, 'Restorative follow-up today',
                   f'<p>Check in with {escape(names)} about the agreement made on {m.date:%d %b}: '
                   f'{escape(m.agreement[:300])}</p>')
            counts['follow_ups'] += 1
    return counts


def run_reminders():
    from tenants.middleware import clear_current_tenant, set_current_tenant
    from tenants.models import Tenant
    total = {'immunisations': 0, 'plans': 0, 'follow_ups': 0}
    for t in Tenant.objects.filter(status__in=('ACTIVE', 'TRIAL')):
        set_current_tenant(t)
        try:
            for k, v in send_reminders(t).items():
                total[k] += v
        finally:
            clear_current_tenant()
    return total
