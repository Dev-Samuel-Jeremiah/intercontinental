"""
curriculum/progression.py
─────────────────────────
Senior-secondary subject streams, end-of-session promotion by the school's
rules, and graduation with verifiable certificates.
"""
import re
from collections import defaultdict
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from users.models import ENROLLED_STATUSES, Class, ClassSubject, Student, StudentSubject

from .models import ClassProgression, PromotionDecision, PromotionPolicy, Stream, StudentStream

CERT_SALT = 'graduation-certificate'


class ProgressionError(ValueError):
    pass


# ─────────────────────────────────────────────────────────────────────────────
# Streams
# ─────────────────────────────────────────────────────────────────────────────

def check_stream(stream):
    """Problems with a stream's own setup (shown to the administrator)."""
    problems = []
    n_comp, n_elec = stream.compulsory.count(), stream.electives.count()
    if stream.min_electives > stream.max_electives:
        problems.append('The minimum number of electives is more than the maximum.')
    if stream.max_electives > n_elec:
        problems.append(f'Students may pick up to {stream.max_electives} electives, but only {n_elec} are listed.')
    if stream.min_subjects > stream.max_subjects:
        problems.append('The minimum number of subjects is more than the maximum.')
    lo, hi = n_comp + stream.min_electives, n_comp + stream.max_electives
    if hi < stream.min_subjects or lo > stream.max_subjects:
        problems.append(f'{n_comp} compulsory subjects plus {stream.min_electives}–{stream.max_electives} electives '
                        f"can't make {stream.min_subjects}–{stream.max_subjects} subjects.")
    both = set(stream.compulsory.values_list('id', flat=True)) & set(stream.electives.values_list('id', flat=True))
    if both:
        problems.append('A subject is both compulsory and an elective.')
    return problems


def check_choice(student, stream, elective_ids):
    """The subject list a choice gives the student, or ProgressionError."""
    cls = student.class_assigned
    if not cls:
        raise ProgressionError(f'{student.full_name} is not in a class.')
    if not stream.applies_to(cls):
        raise ProgressionError(f'The {stream.name} stream is not for {cls.name}.')
    allowed = set(stream.electives.values_list('id', flat=True))
    chosen = {int(i) for i in elective_ids}
    if chosen - allowed:
        raise ProgressionError(f'Pick electives from the {stream.name} list.')
    if not stream.min_electives <= len(chosen) <= stream.max_electives:
        raise ProgressionError(f'{student.full_name}: choose {stream.min_electives} to {stream.max_electives} '
                               f'electives (chose {len(chosen)}).')
    subjects = set(stream.compulsory.values_list('id', flat=True)) | chosen
    if not stream.min_subjects <= len(subjects) <= stream.max_subjects:
        raise ProgressionError(f'{student.full_name}: that makes {len(subjects)} subjects; the {stream.name} stream '
                               f'needs {stream.min_subjects} to {stream.max_subjects}.')
    taught = set(ClassSubject.objects.filter(tenant_id=student.tenant_id, school_class=cls)
                 .values_list('subject_id', flat=True))
    missing = subjects - taught
    if missing:
        from users.models import Subject
        names = ', '.join(Subject.objects.filter(id__in=missing).order_by('name').values_list('name', flat=True))
        raise ProgressionError(f"{names} {'is' if len(missing) == 1 else 'are'} not taught in {cls.name} yet. "
                               f"Add {'it' if len(missing) == 1 else 'them'} to the class's subjects first.")
    return subjects


@transaction.atomic
def choose_stream(student, stream, elective_ids, user=None):
    """
    Put the student in the stream and make their subject list the stream's
    compulsory subjects plus their electives. Results this session for other
    subjects are marked 'not offering' only when no marks have been entered;
    returns how many results were left alone because they have marks.
    """
    from academics.utils import get_active_session
    from results.models import TermResult
    subjects = check_choice(student, stream, elective_ids)
    choice, _ = StudentStream.objects.update_or_create(
        tenant_id=student.tenant_id, student=student, defaults={'stream': stream, 'chosen_by': user})
    choice.electives.set(set(int(i) for i in elective_ids))
    StudentSubject.objects.filter(tenant_id=student.tenant_id, student=student).exclude(subject_id__in=subjects).delete()
    for sid in subjects:
        StudentSubject.objects.get_or_create(tenant_id=student.tenant_id, student=student, subject_id=sid)
    kept = 0
    session = get_active_session()
    if session:
        rows = TermResult.objects.filter(tenant_id=student.tenant_id, student=student, session=session)
        dropped = rows.exclude(subject_id__in=subjects).filter(is_not_offering=False)
        kept = dropped.filter(total_score__gt=0).count()
        for tr in dropped.filter(total_score=0):
            tr.is_not_offering = True
            tr.save()
        for tr in rows.filter(subject_id__in=subjects, is_not_offering=True):
            tr.is_not_offering = False
            tr.save()
    return choice, kept


def offers(student, subject_id):
    """Whether the student takes the subject (everyone takes everything until a list is set)."""
    mine = set(StudentSubject.objects.filter(tenant_id=student.tenant_id, student=student)
               .values_list('subject_id', flat=True))
    return not mine or subject_id in mine


# ─────────────────────────────────────────────────────────────────────────────
# Where each class goes next
# ─────────────────────────────────────────────────────────────────────────────

_STAGE_NEXT = [  # (pattern for the last class of a stage, prefix of the next stage's first class)
    (r'^(primary|basic|pry)\s*6$', 'jss 1'),
    (r'^(jss|jhs)\s*3$', 'ss 1'),
    (r'^basic\s*9$', 'ss 1'),
]
_FINAL = [r'^(ss|sss|shs)\s*3$', r'^grade\s*12$', r'^year\s*12$']


def _split(cls):
    """('jss', 1, 'a') from a class like 'JSS 1A' or level 'JSS 1' + arm 'A'."""
    base = (cls.level or cls.name or '').strip().lower()
    m = re.match(r'^(.*?)(\d+)\s*([a-z]*)$', base)
    if not m:
        return None
    arm = (cls.arm or m.group(3) or '').strip().lower()
    if not cls.level:
        m2 = re.match(r'^(.*?)(\d+)\s*([a-z]*)\s*$', cls.name.strip().lower())
        arm = (cls.arm or m2.group(3)).strip().lower()
    return m.group(1).strip(), int(m.group(2)), arm


def guess_next(cls, classes):
    """(next class or None, graduates?) from the class naming."""
    parts = _split(cls)
    if not parts:
        return None, False
    prefix, n, arm = parts
    stage = f'{prefix} {n}'.strip()
    if any(re.match(p, stage) for p in _FINAL):
        return None, True
    index = {}
    for c in classes:
        p = _split(c)
        if p:
            index.setdefault((p[0], p[1]), []).append((p[2], c))

    def pick(key):
        options = index.get(key, [])
        same_arm = [c for a, c in options if a == arm]
        return (same_arm or [c for _, c in options] or [None])[0]

    nxt = pick((prefix, n + 1))
    if nxt:
        return nxt, False
    for pattern, first in _STAGE_NEXT:
        if re.match(pattern, stage):
            fp, fn = first.rsplit(' ', 1)
            found = pick((fp, int(fn)))
            if not found and fp == 'ss':
                found = pick(('sss', 1)) or pick(('shs', 1))
            return found, False
    return None, False


def progression_map(tenant):
    """[{class, to_class, graduates, saved}] — the school's saved choices, or a guess."""
    classes = list(Class.objects.filter(tenant=tenant).order_by('name'))
    saved = {p.from_class_id: p for p in ClassProgression.objects.filter(tenant=tenant)}
    rows = []
    for c in classes:
        if c.id in saved:
            p = saved[c.id]
            rows.append({'school_class': c, 'to_class': p.to_class, 'graduates': p.graduates, 'saved': True})
        else:
            nxt, grads = guess_next(c, classes)
            rows.append({'school_class': c, 'to_class': nxt, 'graduates': grads, 'saved': False})
    return rows


def next_for(cls):
    row = next((r for r in progression_map(cls.tenant) if r['school_class'].id == cls.id), None)
    return (row['to_class'], row['graduates']) if row else (None, False)


# ─────────────────────────────────────────────────────────────────────────────
# Promotion
# ─────────────────────────────────────────────────────────────────────────────

def policy_for(tenant):
    return PromotionPolicy.objects.filter(tenant=tenant).first() or PromotionPolicy(tenant=tenant)


def standing(student, session, policy):
    """The student's figures for the session: subject averages, overall average, attendance."""
    from academics.models import Term
    from results.models import TermResult
    terms = list(Term.objects.filter(tenant_id=student.tenant_id, session=session).order_by('start_date', 'name'))
    rows = TermResult.objects.filter(tenant_id=student.tenant_id, student=student, session=session,
                                     is_not_offering=False)
    if policy.basis == 'LAST':
        with_results = [t for t in terms if rows.filter(term=t).exists()]
        if with_results:
            rows = rows.filter(term=with_results[-1])
    per_subject = defaultdict(list)
    for tr in rows.select_related('subject'):
        per_subject[tr.subject].append(Decimal(str(tr.total_score or 0)))
    averages = {s: sum(v) / len(v) for s, v in per_subject.items()}
    overall = (sum(averages.values()) / len(averages)) if averages else None
    present = days = 0
    from users.attendance import term_counts
    for t in terms:
        c = term_counts(student, t)
        present, days = present + c['present'], days + c['days']
    return {'averages': averages, 'average': overall,
            'attendance': round(100 * present / days) if days else None}


def assess(student, session, policy, must_pass_ids=None):
    """(suggested outcome, figures, reasons)."""
    figures = standing(student, session, policy)
    must = must_pass_ids if must_pass_ids is not None else set(policy.must_pass.values_list('id', flat=True))
    reasons = []
    avg = figures['average']
    if avg is None:
        return PromotionDecision.REPEAT, figures, ['No results this session.']
    passed = [s for s, a in figures['averages'].items() if a >= policy.subject_pass_mark]
    failed_must = sorted(s.name for s, a in figures['averages'].items()
                         if s.id in must and a < policy.subject_pass_mark)
    figures['passed'] = len(passed)
    hard_fail = False
    if avg < policy.pass_average:
        reasons.append(f'Average {avg:.1f} is below {policy.pass_average:g}.')
    if policy.min_subjects_passed and len(passed) < policy.min_subjects_passed:
        reasons.append(f'Passed {len(passed)} subjects; {policy.min_subjects_passed} needed.')
        hard_fail = True
    if failed_must:
        reasons.append(f"Didn't pass {', '.join(failed_must)}.")
    if policy.min_attendance and figures['attendance'] is not None and figures['attendance'] < policy.min_attendance:
        reasons.append(f"Attended {figures['attendance']}% of days; {policy.min_attendance}% needed.")
    if not reasons:
        return PromotionDecision.PROMOTE, figures, ['Met every rule.']
    near = policy.conditional_band and avg >= policy.pass_average - policy.conditional_band
    only_soft = not hard_fail and len(failed_must) <= 1
    if near and only_soft or (avg >= policy.pass_average and only_soft):
        return PromotionDecision.CONDITIONAL, figures, reasons
    return PromotionDecision.REPEAT, figures, reasons


@transaction.atomic
def prepare(school_class, session, user=None):
    """Work out (or refresh) the suggested decision for every enrolled student in the class."""
    policy = policy_for(school_class.tenant)
    must = set(policy.must_pass.values_list('id', flat=True)) if policy.pk else set()
    to_class, graduates = next_for(school_class)
    out = []
    for student in Student.objects.filter(tenant_id=school_class.tenant_id, class_assigned=school_class,
                                          status__in=ENROLLED_STATUSES).order_by('full_name'):
        existing = PromotionDecision.objects.filter(tenant_id=school_class.tenant_id, session=session,
                                                    student=student).first()
        if existing and existing.applied_at:
            out.append(existing)
            continue
        suggested, figures, reasons = assess(student, session, policy, must)
        if graduates and suggested in (PromotionDecision.PROMOTE, PromotionDecision.CONDITIONAL):
            suggested = PromotionDecision.GRADUATE
        keep_override = existing and existing.overridden and existing.override_reason
        d = existing or PromotionDecision(tenant_id=school_class.tenant_id, session=session, student=student)
        d.from_class = school_class
        d.to_class = to_class
        d.suggested = suggested
        d.outcome = existing.outcome if keep_override else suggested
        d.average = round(figures['average'], 2) if figures['average'] is not None else None
        d.subjects_taken = len(figures['averages'])
        d.subjects_passed = figures.get('passed', 0)
        d.attendance_rate = figures['attendance']
        d.reasons = ' '.join(reasons)
        if not keep_override:
            d.override_reason, d.decided_by = '', user
        d.save()
        out.append(d)
    return out


def override(decision, outcome, reason, user):
    if decision.applied_at:
        raise ProgressionError('This decision has already been applied.')
    if outcome not in dict(PromotionDecision.OUTCOMES):
        raise ProgressionError('Choose a valid outcome.')
    if outcome != decision.suggested and not (reason or '').strip():
        raise ProgressionError(f'Give a reason for changing {decision.student.full_name}\'s outcome.')
    decision.outcome = outcome
    decision.override_reason = (reason or '').strip()[:255] if outcome != decision.suggested else ''
    decision.decided_by = user
    decision.save(update_fields=['outcome', 'override_reason', 'decided_by'])
    return decision


@transaction.atomic
def apply(decisions, user=None, notify=False):
    """Move students: next class, same class, or graduate. Returns counts by outcome."""
    from users.audit import audit_source
    from users.lifecycle import StatusError, change_status
    counts = defaultdict(int)
    for d in decisions:
        if d.applied_at:
            continue
        student = d.student
        if d.outcome in (PromotionDecision.PROMOTE, PromotionDecision.CONDITIONAL):
            if not d.to_class:
                raise ProgressionError(f'Set where {d.from_class.name} goes next before promoting.')
            with audit_source('Promotion', user):
                student.class_assigned = d.to_class
                student.current_term = '1st Term'
                student.save()
        elif d.outcome == PromotionDecision.GRADUATE:
            try:
                change_status(student, 'Graduated', reason=f'Completed {d.from_class.name}', user=user)
            except StatusError as problem:
                raise ProgressionError(str(problem))
        else:
            with audit_source('Promotion', user):
                student.current_term = '1st Term'
                student.save()
        d.applied_at = timezone.now()
        d.save(update_fields=['applied_at'])
        counts[d.outcome] += 1
    if notify and decisions:
        from lms_project.tasks import run_in_background
        run_in_background(_notify_families, [d.id for d in decisions], tenant=decisions[0].student.tenant)
    return dict(counts)


def _notify_families(decision_ids):
    from communications.services import _is_opted_out, send_sms
    from users.records import contacts
    words = {'PROMOTE': 'has been promoted to {to}', 'CONDITIONAL': 'has been promoted to {to} on condition',
             'REPEAT': 'will repeat {frm}', 'GRADUATE': 'has graduated from {frm}. Congratulations'}
    for d in PromotionDecision.objects.filter(id__in=decision_ids).select_related(
            'student', 'student__tenant', 'from_class', 'to_class'):
        if _is_opted_out(d.student, 'SMS'):
            continue
        text = (f"{d.student.tenant.name}: {d.student.full_name} "
                + words[d.outcome].format(to=getattr(d.to_class, 'name', ''), frm=d.from_class.name) + '.')
        for _name, phone in contacts(d.student, 'SMS'):
            send_sms(phone, text)


# ─────────────────────────────────────────────────────────────────────────────
# Graduation certificates
# ─────────────────────────────────────────────────────────────────────────────

def certificate(student):
    from django.core import signing
    if student.status != 'Graduated':
        raise ProgressionError(f'{student.full_name} has not graduated.')
    ref = signing.dumps({'s': student.id, 't': str(student.tenant_id)}, salt=CERT_SALT, compress=True)
    return {'student': student, 'graduated_on': student.left_on, 'from_class': student.leaving_class,
            'reference': ref}


def verify_certificate(reference, tenant):
    from django.core import signing
    try:
        data = signing.loads(reference, salt=CERT_SALT)
    except signing.BadSignature:
        return None
    if data.get('t') != str(tenant.id):
        return None
    student = Student.objects.filter(tenant=tenant, pk=data.get('s'), status='Graduated').first()
    return certificate(student) if student else None
