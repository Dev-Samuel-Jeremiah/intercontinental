"""
assistant/risk.py
─────────────────
At-risk early warning (F164). Puts together attendance over the last 30
days, the latest term's results (and the change since the term before) and,
for people allowed to see fees, the fee balance — and scores each student so
form teachers can see who needs a conversation now.

The score is plain arithmetic so anyone can see why a student is flagged; AI
is only used, on request, to suggest next steps for one student.
"""
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from users.models import ENROLLED_STATUSES, Attendance, Student

DAYS = 30
HIGH, MEDIUM = 50, 25


def _latest_terms(student_ids):
    """{student_id: [(term_id, average, fails)] newest first} from term results."""
    from results.models import TermResult
    per = defaultdict(lambda: defaultdict(list))
    order = {}
    for r in TermResult.objects.filter(student_id__in=student_ids).select_related('term', 'session'):
        per[r.student_id][r.term_id].append(r.total_score or 0)
        order[r.term_id] = (r.session.name if r.session_id else '', r.term.start_date or timezone.localdate(),
                            r.term.name)
    out = {}
    for sid, terms in per.items():
        rows = []
        for tid in sorted(terms, key=lambda t: order[t], reverse=True):
            scores = terms[tid]
            rows.append((tid, sum(scores) / len(scores), sum(1 for x in scores if x < 40)))
        out[sid] = rows
    return out


def _fees(students, tenant):
    from academics.models import AcademicSession, Term
    from finance.models import ChildBilling
    session = AcademicSession.objects.filter(tenant=tenant, is_active=True).first()
    term = Term.objects.filter(tenant=tenant, is_active=True).first()
    if not (session and term):
        return {}
    out = {}
    for b in ChildBilling.objects.filter(tenant=tenant, student__in=students, parent_billing__session=session,
                                         parent_billing__term=term):
        if b.amount_due > 0:
            out[b.student_id] = (b.balance, b.balance / b.amount_due)
    return out


def assess(students, tenant, with_fees=False):
    """[{student, score, level, reasons, fee_reason, facts}] highest risk first."""
    students = list(students)
    ids = [s.id for s in students]
    since = timezone.localdate() - timedelta(days=DAYS)
    att = defaultdict(lambda: [0, 0])
    for a in Attendance.objects.filter(tenant=tenant, student_id__in=ids, subject__isnull=True, date__gte=since) \
            .values('student_id', 'status'):
        att[a['student_id']][1] += 1
        att[a['student_id']][0] += a['status'] == 'A'
    terms = _latest_terms(ids)
    fees = _fees(students, tenant) if with_fees else {}
    rows = []
    for s in students:
        score, reasons, facts = 0, [], {}
        absent, days = att[s.id]
        if days >= 5:
            rate = absent / days
            facts['absence_rate'] = round(100 * rate)
            if rate >= 0.2:
                score += 35
                reasons.append(f'Absent {absent} of the last {days} school days')
            elif rate >= 0.1:
                score += 20
                reasons.append(f'Absent {absent} of the last {days} school days')
        history = terms.get(s.id) or []
        if history:
            _, avg, fails = history[0]
            facts.update(average=round(avg, 1), fails=fails)
            if avg < 40:
                score += 30
                reasons.append(f'Average of {avg:.0f}% last term')
            elif avg < 50:
                score += 15
                reasons.append(f'Average of {avg:.0f}% last term')
            if fails >= 3:
                score += 15
                reasons.append(f'Below 40 in {fails} subjects')
            if len(history) > 1 and history[1][1] - avg >= 10:
                score += 15
                facts['drop'] = round(history[1][1] - avg, 1)
                reasons.append(f'Down {history[1][1] - avg:.0f} points on the term before')
        fee_reason = ''
        if s.id in fees:
            balance, share = fees[s.id]
            if share >= Decimal('0.75'):
                score += 15
                fee_reason = f'₦{balance:,.0f} of this term\'s fees unpaid'
            elif share >= Decimal('0.5'):
                score += 8
                fee_reason = f'₦{balance:,.0f} of this term\'s fees unpaid'
            if fee_reason:
                facts['fees_unpaid_share'] = round(float(share) * 100)
        score = min(score, 100)
        rows.append({'student': s, 'score': score, 'reasons': reasons, 'fee_reason': fee_reason, 'facts': facts,
                     'level': 'high' if score >= HIGH else 'medium' if score >= MEDIUM else 'low'})
    rows.sort(key=lambda r: (-r['score'], r['student'].full_name))
    return rows


def students_for(user, tenant, school_class=None):
    """Administrators see everyone; other staff see the classes they are form teacher of."""
    from users.views_profile import can_manage
    qs = Student.objects.filter(tenant=tenant, status__in=ENROLLED_STATUSES).select_related('class_assigned')
    if not can_manage(user):
        staff = getattr(user, 'staff', None)
        qs = qs.filter(class_assigned__form_teacher=staff) if staff else qs.none()
    if school_class is not None:
        qs = qs.filter(class_assigned=school_class)
    return qs


def suggest(tenant, user, row):
    """AI next steps for one flagged student (first name only is sent, with the numbers)."""
    from .providers import complete
    s = row['student']
    first = (s.full_name or 'The student').split()[0]
    facts = '; '.join(row['reasons'] + ([row['fee_reason']] if row['fee_reason'] else [])) or 'no warning signs'
    prompt = f"""A form teacher in a Nigerian secondary school is worried about {first} ({s.class_assigned}).
Warning signs: {facts}.

Suggest 3 practical next steps the form teacher can take this week (a conversation, contact with the
family, support from the school). Be kind and specific, do not assume reasons, and keep each step to one
or two sentences. Return a JSON object: {{"steps": ["...", "...", "..."]}}"""
    data = complete(tenant, 'early_warning', 'You advise teachers on student wellbeing and progress.', prompt,
                    user=user, json_mode=True, max_tokens=500).data
    steps = data.get('steps') if isinstance(data, dict) else None
    return [str(x).strip()[:400] for x in (steps or []) if str(x).strip()][:5]
