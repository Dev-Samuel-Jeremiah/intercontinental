"""
schoolai/memory.py
──────────────────
The school's long-term memory.

  snapshot()  — today's vital signs (enrolment, attendance, results, risk,
                knowledge), one row a day, kept for years.
  collect()   — every student's every term as numbers (FeatureRow), plus what
                happened the following term (the labels the models learn from).
                Runs every night; past terms are only recomputed when their
                data changes, so the history keeps growing.
"""
from collections import defaultdict
from datetime import date, timedelta

from django.db.models import Count, Q
from django.utils import timezone

from users.models import ENROLLED_STATUSES, Attendance, Student

from .models import FeatureRow, Snapshot

PASS_MARK = 50
FAIL_MARK = 40
TERM_INDEX = {'1st Term': 1, '2nd Term': 2, '3rd Term': 3}

# feature key → (label for people, format, which way is "worse" for a student: -1 lower is worse, 1 higher is worse)
FEATURES = [
    ('avg', 'Term average', '{:.0f}%', -1),
    ('min_subject', 'Weakest subject', '{:.0f}%', -1),
    ('fails', 'Subjects below 40%', '{:.0f}', 1),
    ('trend', 'Change since the term before', '{:+.0f} points', -1),
    ('attendance', 'Attendance', '{:.0f}%', -1),
    ('late', 'Late arrivals', '{:.0f}% of days', 1),
    ('practice', 'Practice quizzes taken', '{:.0f}', -1),
    ('practice_acc', 'Practice quiz accuracy', '{:.0f}%', -1),
    ('labs', 'Practical attendance', '{:.0f}%', -1),
    ('sick', 'Sick-bay visits', '{:.0f}', 1),
    ('fees_owing', 'Fees outstanding', '{:.0f}% of the bill', 1),
]
FEATURE_KEYS = [f[0] for f in FEATURES]
FEATURE_INFO = {f[0]: f for f in FEATURES}


# ─────────────────────────────────────────────────────────────────────────────
# Terms in order
# ─────────────────────────────────────────────────────────────────────────────

def term_window(term):
    start = term.start_date or getattr(term.session, 'start_date', None)
    end = term.end_date
    if start and not end:
        end = start + timedelta(days=100)
    return start, end


def ordered_terms(tenant):
    """Every term, oldest first, with its position (1, 2, 3…)."""
    from academics.models import Term

    def key(t):
        start = t.start_date or getattr(t.session, 'start_date', None) or date(1900, 1, 1)
        return (getattr(t.session, 'start_date', None) or start, t.session.name, TERM_INDEX.get(t.name, 9), start)
    terms = sorted(Term.objects.filter(tenant=tenant).select_related('session'), key=key)
    return terms


# ─────────────────────────────────────────────────────────────────────────────
# Daily snapshot
# ─────────────────────────────────────────────────────────────────────────────

def snapshot(tenant, on=None):
    from users.models import Staff
    from .models import AskedQuestion, KnowledgeItem, Prediction
    on = on or timezone.localdate()
    students = Student.objects.filter(tenant=tenant, status__in=ENROLLED_STATUSES)
    week = Attendance.objects.filter(tenant=tenant, date__gt=on - timedelta(days=7), date__lte=on)
    marked = week.count()
    m = {
        'enrolled': students.count(),
        'girls': students.filter(gender='Female').count(),
        'boys': students.filter(gender='Male').count(),
        'staff': Staff.objects.filter(tenant=tenant).count(),
        'attendance_7d': round(100 * week.filter(status='P').count() / marked, 1) if marked else None,
        'joined_30d': Student.objects.filter(tenant=tenant, created_at__date__gt=on - timedelta(days=30)).count(),
        'left_30d': Student.objects.filter(tenant=tenant, left_on__gt=on - timedelta(days=30)).count(),
        'at_risk': Prediction.objects.filter(tenant=tenant, kind='FAIL_RISK', band='HIGH').count(),
        'knowledge': KnowledgeItem.objects.filter(tenant=tenant, active=True).count(),
        'questions_7d': AskedQuestion.objects.filter(tenant=tenant, created_at__date__gt=on - timedelta(days=7)).count(),
    }
    try:
        from results.models import TermResult
        latest = TermResult.objects.filter(tenant=tenant, is_not_offering=False).order_by('-term__start_date', '-id').first()
        if latest:
            rows = TermResult.objects.filter(tenant=tenant, term=latest.term, is_not_offering=False)
            scores = [r.total_score or 0 for r in rows]
            m['latest_term'] = f'{latest.term.name} {latest.session.name}'
            m['latest_average'] = round(sum(scores) / len(scores), 1) if scores else None
    except Exception:
        pass
    obj, _ = Snapshot.objects.update_or_create(tenant=tenant, date=on, defaults={'metrics': m})
    return obj


def enrolment_history(tenant, months=36):
    """[(month end, students on the roll)] for the last `months` months, rebuilt from admission and leaving dates."""
    today = timezone.localdate()
    rows = list(Student.objects.filter(tenant=tenant).values_list('created_at', 'left_on', 'status'))
    out = []
    for i in range(months, -1, -1):
        y, mth = today.year, today.month - i
        while mth <= 0:
            mth += 12
            y -= 1
        nxt = date(y + (mth == 12), mth % 12 + 1, 1)
        end = min(nxt - timedelta(days=1), today)
        count = 0
        for created, left_on, status in rows:
            if created and timezone.localdate(created) > end:
                continue
            if left_on and left_on <= end:
                continue
            if not left_on and status in ('Withdrawn', 'Graduated', 'Deceased'):
                continue                                          # left, date unknown — leave them out
            count += 1
        out.append((end, count))
    first = next((i for i, (_, c) in enumerate(out) if c), len(out))
    return out[first:]


# ─────────────────────────────────────────────────────────────────────────────
# Per-student, per-term features
# ─────────────────────────────────────────────────────────────────────────────

def _results_by_term(tenant):
    from results.models import TermResult
    per = defaultdict(lambda: defaultdict(list))                  # term_id → student_id → [scores]
    for term_id, student_id, score in (TermResult.objects.filter(tenant=tenant, is_not_offering=False)
                                       .values_list('term_id', 'student_id', 'total_score')):
        per[term_id][student_id].append(float(score or 0))
    return per


def _window_counts(qs, field, start, end):
    if not (start and end):
        return {}
    return dict(qs.filter(**{f'{field}__gte': start, f'{field}__lte': end}).values('student_id')
                .annotate(n=Count('id')).values_list('student_id', 'n'))


def _term_extras(tenant, term, student_ids):
    """Attendance, lateness, practice, labs, sick bay and fees for one term. {student_id: {key: value}}."""
    start, end = term_window(term)
    out = defaultdict(dict)
    if start and end:
        att = Attendance.objects.filter(tenant=tenant, student_id__in=student_ids, date__gte=start, date__lte=end)
        totals = dict(att.values('student_id').annotate(n=Count('id')).values_list('student_id', 'n'))
        present = dict(att.filter(status='P').values('student_id').annotate(n=Count('id')).values_list('student_id', 'n'))
        late = dict(att.filter(minutes_late__gt=0).values('student_id').annotate(n=Count('id')).values_list('student_id', 'n'))
        for sid, n in totals.items():
            out[sid]['attendance'] = 100 * present.get(sid, 0) / n
            out[sid]['late'] = 100 * late.get(sid, 0) / n
        try:
            from cbt.models import PracticeSession
            for sid, n in _window_counts(PracticeSession.objects.filter(tenant=tenant, student_id__in=student_ids,
                                                                        submitted_at__isnull=False),
                                         'submitted_at__date', start, end).items():
                out[sid]['practice'] = n
            acc = defaultdict(list)
            for sid, score, total in (PracticeSession.objects.filter(
                    tenant=tenant, student_id__in=student_ids, submitted_at__date__gte=start,
                    submitted_at__date__lte=end, total__gt=0).values_list('student_id', 'score', 'total')):
                acc[sid].append(100 * score / total)
            for sid, vals in acc.items():
                out[sid]['practice_acc'] = sum(vals) / len(vals)
        except Exception:
            pass
        try:
            from hostel.models import SickBayVisit
            for sid, n in _window_counts(SickBayVisit.objects.filter(tenant=tenant, student_id__in=student_ids),
                                         'admitted_at__date', start, end).items():
                out[sid]['sick'] = n
        except Exception:
            pass
    try:
        from labs.models import PracticalRecord
        recs = PracticalRecord.objects.filter(tenant=tenant, student_id__in=student_ids, session__term=term,
                                              session__status='DONE').exclude(attendance='E')
        tot = dict(recs.values('student_id').annotate(n=Count('id')).values_list('student_id', 'n'))
        came = dict(recs.filter(attendance__in=('P', 'L')).values('student_id').annotate(n=Count('id'))
                    .values_list('student_id', 'n'))
        for sid, n in tot.items():
            out[sid]['labs'] = 100 * came.get(sid, 0) / n
    except Exception:
        pass
    try:
        from finance.models import ChildBilling
        for sid, total, paid in (ChildBilling.objects.filter(tenant=tenant, student_id__in=student_ids,
                                                             parent_billing__term=term)
                                 .values_list('student_id', 'total_fee', 'paid')):
            if total and total > 0:
                out[sid]['fees_owing'] = max(0.0, float(100 * (total - paid) / total))
    except Exception:
        pass
    return out


def collect(tenant, recent_only=False):
    """Build (or refresh) every FeatureRow and its labels. Returns how many rows were written."""
    terms = ordered_terms(tenant)
    results = _results_by_term(tenant)
    terms = [t for t in terms if results.get(t.id)]               # only terms with results
    if recent_only:
        known = set(FeatureRow.objects.filter(tenant=tenant).values_list('term_id', flat=True))
        refresh = {t.id for t in terms[-2:]} | {t.id for t in terms if t.id not in known}
    else:
        refresh = {t.id for t in terms}
    written = 0
    previous_avg = {}
    for i, term in enumerate(terms):
        per_student = results[term.id]
        averages = {sid: sum(v) / len(v) for sid, v in per_student.items() if v}
        nxt = terms[i + 1] if i + 1 < len(terms) else None
        if term.id in refresh or (nxt and nxt.id in refresh):
            extras = _term_extras(tenant, term, list(per_student))
            next_results = results.get(nxt.id, {}) if nxt else {}
            for sid, scores in per_student.items():
                avg = averages[sid]
                feats = {'avg': avg, 'min_subject': min(scores), 'fails': sum(1 for s in scores if s < FAIL_MARK),
                         'trend': avg - previous_avg[sid] if sid in previous_avg else None}
                feats.update(extras.get(sid, {}))
                labels = {}
                if sid in next_results and next_results[sid]:
                    nav = sum(next_results[sid]) / len(next_results[sid])
                    labels = {'next_avg': round(nav, 2), 'fail': int(nav < PASS_MARK)}
                FeatureRow.objects.update_or_create(
                    tenant=tenant, student_id=sid, term=term,
                    defaults={'order': i + 1, 'features': {k: (round(v, 3) if isinstance(v, float) else v)
                                                           for k, v in feats.items()}, 'labels': labels})
                written += 1
        for sid, avg in averages.items():
            previous_avg[sid] = avg
    return written


def memory_stats(tenant):
    from .models import AskedQuestion, KnowledgeItem
    terms = FeatureRow.objects.filter(tenant=tenant).values('term').distinct().count()
    first = Snapshot.objects.filter(tenant=tenant).order_by('date').first()
    rows = FeatureRow.objects.filter(tenant=tenant)
    try:
        from results.models import TermResult
        results = TermResult.objects.filter(tenant=tenant).count()
    except Exception:
        results = 0
    return {
        'terms': terms, 'student_terms': rows.count(), 'labelled': rows.exclude(labels={}).count(),
        'students': rows.values('student').distinct().count(), 'results': results,
        'attendance': Attendance.objects.filter(tenant=tenant).count(),
        'snapshots': Snapshot.objects.filter(tenant=tenant).count(), 'since': first.date if first else None,
        'knowledge': KnowledgeItem.objects.filter(tenant=tenant, active=True).count(),
        'questions': AskedQuestion.objects.filter(tenant=tenant).count(),
        'taught': AskedQuestion.objects.filter(tenant=tenant, status='RESOLVED').count(),
    }
