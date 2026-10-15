"""
analytics/metrics.py
────────────────────
The numbers behind the analytics pages, worked out from the school's own
records. Every function takes the school (tenant) and returns plain Python
data, so pages, emails, the board pack and tests all share one source.
"""
import re
from collections import Counter, defaultdict
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Max, Q
from django.utils import timezone

from users.models import ENROLLED_STATUSES, LEFT_STATUSES, Attendance, Class, Student

ZERO = Decimal('0')


def pct(part, whole):
    return round(100 * float(part) / float(whole), 1) if whole else None


def active_period(tenant):
    from academics.models import AcademicSession, Term
    return (AcademicSession.objects.filter(tenant=tenant, is_active=True).first(),
            Term.objects.filter(tenant=tenant, is_active=True).first())


def on_roll(tenant):
    return Student.objects.filter(tenant=tenant, status__in=ENROLLED_STATUSES)


# ─────────────────────────────────────────────────────────────────────────────
# Attendance
# ─────────────────────────────────────────────────────────────────────────────

def attendance_today(tenant, day=None):
    day = day or timezone.localdate()
    roll = on_roll(tenant)
    records = Attendance.objects.filter(tenant=tenant, date=day, subject__isnull=True, student__in=roll)
    present = records.filter(status='P').count()
    absent = records.filter(status='A').count()
    late = records.filter(status='P', minutes_late__gt=0).count()
    marked_classes = set(records.values_list('school_class_id', flat=True))
    class_ids = set(roll.exclude(class_assigned__isnull=True).values_list('class_assigned_id', flat=True))
    unmarked = list(Class.objects.filter(pk__in=class_ids - marked_classes).order_by('name'))
    return {'day': day, 'roll': roll.count(), 'present': present, 'absent': absent, 'late': late,
            'rate': pct(present, present + absent), 'unmarked': unmarked}


def attendance_rate(tenant, days=30):
    since = timezone.localdate() - timedelta(days=days)
    qs = Attendance.objects.filter(tenant=tenant, subject__isnull=True, date__gte=since)
    total = qs.count()
    return pct(qs.filter(status='P').count(), total)


# ─────────────────────────────────────────────────────────────────────────────
# Fees
# ─────────────────────────────────────────────────────────────────────────────

def _billings(tenant):
    from finance.models import ChildBilling
    session, term = active_period(tenant)
    if not (session and term):
        return ChildBilling.objects.none(), term
    return (ChildBilling.objects.filter(tenant=tenant, parent_billing__session=session, parent_billing__term=term)
            .select_related('student__class_assigned', 'parent_billing'), term)


def fee_position(tenant):
    billings, term = _billings(tenant)
    expected = paid = outstanding = ZERO
    debtors = 0
    for b in billings:
        expected += b.amount_due
        paid += min(b.paid, b.amount_due)
        outstanding += b.balance
        debtors += b.balance > 0
    return {'term': term, 'expected': expected, 'paid': paid, 'outstanding': outstanding, 'debtors': debtors,
            'rate': pct(paid, expected)}


def collections(tenant, weeks=8):
    """Approved payments: today, this week, by method and week by week."""
    from finance.models import Payment
    now = timezone.localtime()
    today = now.date()
    start = today - timedelta(days=today.weekday()) - timedelta(weeks=weeks - 1)
    qs = Payment.objects.filter(tenant=tenant, status=Payment.APPROVED)
    rows = list(qs.filter(created_at__date__gte=start).values('amount', 'method', 'created_at'))
    by_week = defaultdict(lambda: ZERO)
    by_method = defaultdict(lambda: ZERO)
    for r in rows:
        d = timezone.localtime(r['created_at']).date()
        by_week[d - timedelta(days=d.weekday())] += r['amount']
        by_method[r['method']] += r['amount']
    methods = dict(Payment.METHOD_CHOICES)
    weeks_list = [start + timedelta(weeks=i) for i in range(weeks)]
    peak = max([by_week[w] for w in weeks_list] + [ZERO]) or Decimal('1')
    return {
        'today': sum((r['amount'] for r in rows if timezone.localtime(r['created_at']).date() == today), ZERO),
        'this_week': by_week[today - timedelta(days=today.weekday())],
        'weeks': [{'week': w, 'amount': by_week[w], 'width': round(100 * float(by_week[w] / peak))} for w in weeks_list],
        'methods': sorted(((methods.get(k, k), v) for k, v in by_method.items()), key=lambda x: -x[1]),
        'pending': Payment.objects.filter(tenant=tenant, status=Payment.PENDING).count(),
    }


def collection_by_class(tenant):
    billings, _ = _billings(tenant)
    rows = defaultdict(lambda: {'expected': ZERO, 'paid': ZERO, 'outstanding': ZERO, 'students': 0, 'debtors': 0})
    ids = {}
    for b in billings:
        cls = b.student.class_assigned
        key = cls.name if cls else 'No class'
        ids[key] = cls.pk if cls else None
        r = rows[key]
        r['expected'] += b.amount_due
        r['paid'] += min(b.paid, b.amount_due)
        r['outstanding'] += b.balance
        r['students'] += 1
        r['debtors'] += b.balance > 0
    return [dict(name=k, class_id=ids[k], rate=pct(v['paid'], v['expected']), **v) for k, v in sorted(rows.items())]


AGE_BUCKETS = [('0–30 days', 30), ('31–60 days', 60), ('61–90 days', 90), ('Over 90 days', None)]


def debt_ageing(tenant):
    """Unpaid balances by how long since the family last paid anything (or since the term began)."""
    from finance.models import Payment
    billings, term = _billings(tenant)
    owing = [b for b in billings if b.balance > 0]
    last_paid = dict(Payment.objects.filter(tenant=tenant, status=Payment.APPROVED,
                                            wallet_id__in={b.parent_billing.wallet_id for b in owing})
                     .values('wallet_id').annotate(last=Max('created_at')).values_list('wallet_id', 'last'))
    today = timezone.localdate()
    since_term = (term.start_date if term and term.start_date else today)
    buckets = [{'label': label, 'amount': ZERO, 'families': 0, 'never': 0} for label, _ in AGE_BUCKETS]
    for b in owing:
        last = last_paid.get(b.parent_billing.wallet_id)
        days = (today - (timezone.localtime(last).date() if last else since_term)).days
        i = next(n for n, (_, limit) in enumerate(AGE_BUCKETS) if limit is None or days <= limit)
        buckets[i]['amount'] += b.balance
        buckets[i]['families'] += 1
        buckets[i]['never'] += last is None
    return buckets


# ─────────────────────────────────────────────────────────────────────────────
# Results
# ─────────────────────────────────────────────────────────────────────────────

def latest_result_period(tenant):
    from results.models import TermResult
    return TermResult.objects.filter(tenant=tenant).order_by('-session__name', '-term__start_date', '-term_id') \
        .values('session_id', 'term_id').first()


def results_summary(tenant):
    from results.models import TermResult
    period = latest_result_period(tenant)
    if not period:
        return None
    per_student = defaultdict(list)
    per_class = defaultdict(list)
    term = None
    for r in TermResult.objects.filter(tenant=tenant, **period).select_related('class_assigned', 'term', 'session'):
        per_student[r.student_id].append(r.total_score or 0)
        term = term or f'{r.term.name}, {r.session.name}'
        per_class[r.class_assigned.name].append(r.total_score or 0)
    averages = [sum(v) / len(v) for v in per_student.values()]
    return {'term': term, 'students': len(averages),
            'average': round(sum(averages) / len(averages), 1) if averages else None,
            'pass_rate': pct(sum(1 for a in averages if a >= 50), len(averages)),
            'by_class': [{'name': k, 'average': round(sum(v) / len(v), 1)} for k, v in sorted(per_class.items())]}


# ─────────────────────────────────────────────────────────────────────────────
# KPI targets
# ─────────────────────────────────────────────────────────────────────────────

def kpis(tenant):
    from .models import METRICS, KPITarget
    results = results_summary(tenant) or {}
    values = {'attendance': attendance_rate(tenant), 'fees': fee_position(tenant)['rate'],
              'pass_rate': results.get('pass_rate'), 'average': results.get('average'),
              'enrolment': on_roll(tenant).count()}
    targets = {k.metric: float(k.target) for k in KPITarget.objects.filter(tenant=tenant)}
    out = []
    for key, label in METRICS:
        value, target = values.get(key), targets.get(key)
        progress = min(100, round(100 * value / target)) if (value is not None and target) else None
        out.append({'metric': key, 'label': label, 'value': value, 'target': target, 'progress': progress,
                    'met': value is not None and target is not None and value >= target})
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Enrolment and retention
# ─────────────────────────────────────────────────────────────────────────────

def month_starts(months):
    """The first day of each of the last `months` months, oldest first (this month last)."""
    first = timezone.localdate().replace(day=1)
    out = [first]
    while len(out) < months:
        out.append((out[-1] - timedelta(days=1)).replace(day=1))
    return out[::-1]


def month_end(start):
    nxt = (start + timedelta(days=32)).replace(day=1)
    return min(nxt - timedelta(days=1), timezone.localdate())


def _left_on(s):
    return s.left_on or s.status_changed_on


def enrolment_series(tenant, months=24):
    students = list(Student.objects.filter(tenant=tenant).only('created_at', 'status', 'left_on', 'status_changed_on'))
    points = []
    for start in month_starts(months):
        end = month_end(start)
        n = 0
        for s in students:
            joined = timezone.localtime(s.created_at).date() if s.created_at else end
            gone = _left_on(s) if s.status in LEFT_STATUSES else None
            if s.status in LEFT_STATUSES and gone is None:
                gone = joined          # left on an unknown day: don't count them as ever on roll in the past
            if joined <= end and (gone is None or gone > end):
                n += 1
        points.append({'month': start, 'count': n})
    ahead = forecast(points)
    # One scale for real and projected bars, so the trend is drawn in proportion.
    peak = max([p['count'] for p in points + ahead] + [1])
    for p in points + ahead:
        p['height'] = round(100 * p['count'] / peak)
    return points, ahead


def forecast(points, ahead=3):
    """Straight-line trend over the last 12 months, projected `ahead` months."""
    recent = points[-12:]
    n = len(recent)
    if n < 3:
        return []
    xs, ys = range(n), [p['count'] for p in recent]
    mx, my = (n - 1) / 2, sum(ys) / n
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sum((x - mx) ** 2 for x in xs) or 1)
    last = recent[-1]['month']
    out = []
    for i in range(1, ahead + 1):
        y, m = last.year + (last.month - 1 + i) // 12, (last.month - 1 + i) % 12 + 1
        out.append({'month': date(y, m, 1), 'count': max(0, round(my + slope * (n - 1 + i - mx)))})
    return out


def by_level(tenant):
    counts = Counter()
    for s in on_roll(tenant).select_related('class_assigned'):
        c = s.class_assigned
        counts[(c.level or re.sub(r'\s*[A-Z]$', '', c.name)) if c else 'No class'] += 1
    return sorted(counts.items())


def joiners_and_leavers(tenant, months=12):
    starts = month_starts(months)
    since = starts[0]
    joined = Counter(timezone.localtime(s.created_at).date().replace(day=1)
                     for s in Student.objects.filter(tenant=tenant, created_at__date__gte=since).only('created_at'))
    left = Counter()
    for s in Student.objects.filter(tenant=tenant, status__in=LEFT_STATUSES):
        d = _left_on(s)
        if d and d >= since:
            left[d.replace(day=1)] += 1
    return [{'month': m, 'joined': joined.get(m, 0), 'left': left.get(m, 0)} for m in starts]


def retention(tenant, months=12):
    since = timezone.localdate() - timedelta(days=30 * months)
    leavers = [s for s in Student.objects.filter(tenant=tenant, status__in=LEFT_STATUSES).select_related('class_assigned')
               if (_left_on(s) or date.min) >= since]
    withdrawn = [s for s in leavers if s.status == 'Withdrawn']
    roll = on_roll(tenant).count()
    by_class = Counter(s.leaving_class or (s.class_assigned.name if s.class_assigned else 'Unknown') for s in withdrawn)
    reasons = Counter((s.status_reason or 'No reason recorded').strip().capitalize() for s in withdrawn)
    destinations = Counter(s.destination.strip() for s in withdrawn if s.destination.strip())
    return {'months': months, 'leavers': len(leavers), 'by_status': Counter(s.status for s in leavers).most_common(),
            'withdrawn': len(withdrawn), 'withdrawal_rate': pct(len(withdrawn), roll + len(withdrawn)),
            'by_class': by_class.most_common(), 'reasons': reasons.most_common(8),
            'destinations': destinations.most_common(8), 'recent': sorted(withdrawn, key=lambda s: _left_on(s) or date.min,
                                                                         reverse=True)[:15]}


# ─────────────────────────────────────────────────────────────────────────────
# Demographics
# ─────────────────────────────────────────────────────────────────────────────

AGE_BANDS = [('Under 10', 0, 9), ('10–12', 10, 12), ('13–15', 13, 15), ('16–18', 16, 18), ('19+', 19, 200)]


def age_band(age):
    if age is None:
        return 'Unknown'
    return next(label for label, lo, hi in AGE_BANDS if lo <= age <= hi)


def demographics(tenant):
    rows = defaultdict(lambda: {'Male': 0, 'Female': 0, 'Unspecified': 0, 'ages': Counter(), 'total': 0, 'id': None})
    for s in on_roll(tenant).select_related('class_assigned'):
        name = s.class_assigned.name if s.class_assigned else 'No class'
        r = rows[name]
        r['id'] = s.class_assigned_id
        r[s.gender if s.gender in ('Male', 'Female') else 'Unspecified'] += 1
        r['ages'][age_band(s.age)] += 1
        r['total'] += 1
    bands = [b[0] for b in AGE_BANDS] + ['Unknown']
    table = [{'name': k, 'class_id': v['id'], 'male': v['Male'], 'female': v['Female'], 'unspecified': v['Unspecified'],
              'total': v['total'], 'ages': [v['ages'].get(b, 0) for b in bands]} for k, v in sorted(rows.items())]
    totals = {'male': sum(r['male'] for r in table), 'female': sum(r['female'] for r in table),
              'unspecified': sum(r['unspecified'] for r in table), 'total': sum(r['total'] for r in table),
              'ages': [sum(r['ages'][i] for r in table) for i in range(len(bands))]}
    summary = [{'band': b, 'count': n, 'share': pct(n, totals['total']) or 0} for b, n in zip(bands, totals['ages'])]
    return {'bands': bands, 'rows': table, 'totals': totals, 'summary': summary}


# ─────────────────────────────────────────────────────────────────────────────
# Cohort comparison
# ─────────────────────────────────────────────────────────────────────────────

def _squash(text):
    return re.sub(r'[\s\-_/]', '', str(text or '')).lower()


def levels(tenant):
    names = set()
    for c in Class.objects.filter(tenant=tenant):
        names.add(c.level or re.sub(r'\s*[A-Z]$', '', c.name))
    return sorted(n for n in names if n)


def cohort(tenant, level, term_name):
    """The same year group at the same point (term) in each session, side by side."""
    from results.models import TermResult
    key = _squash(level)
    rows = TermResult.objects.filter(tenant=tenant, term__name=term_name).filter(
        Q(class_assigned__level__iexact=level) | Q(class_assigned__name__istartswith=level)
    ).select_related('session', 'subject', 'class_assigned')
    data = defaultdict(lambda: {'students': defaultdict(list), 'subjects': defaultdict(list)})
    for r in rows:
        c = r.class_assigned
        if _squash(c.level) != key and not _squash(c.name).startswith(key):
            continue
        d = data[r.session.name]
        d['students'][r.student_id].append(r.total_score or 0)
        d['subjects'][r.subject.name].append(r.total_score or 0)
    sessions = sorted(data)[-4:]
    subjects = sorted({s for name in sessions for s in data[name]['subjects']})
    out = []
    for name in sessions:
        d = data[name]
        averages = [sum(v) / len(v) for v in d['students'].values()]
        out.append({'session': name, 'students': len(averages),
                    'average': round(sum(averages) / len(averages), 1) if averages else None,
                    'pass_rate': pct(sum(1 for a in averages if a >= 50), len(averages)),
                    'subjects': [round(sum(d['subjects'][s]) / len(d['subjects'][s]), 1) if d['subjects'].get(s)
                                 else None for s in subjects]})
    return {'level': level, 'term': term_name, 'sessions': out, 'subjects': subjects}


# ─────────────────────────────────────────────────────────────────────────────
# Activity feed
# ─────────────────────────────────────────────────────────────────────────────

def activity(tenant, limit=40, days=14):
    from announcement.models import Announcement
    from finance.models import Payment
    from users.models import User
    since = timezone.now() - timedelta(days=days)
    items = []
    for u in User.objects.filter(tenant=tenant, last_login__gte=since).order_by('-last_login')[:15]:
        who = 'staff' if u.is_staff_user else 'parent' if u.is_parent else 'student' if u.is_student else 'user'
        items.append((u.last_login, 'bi-box-arrow-in-right', f'{u.get_full_name() or u.username} ({who}) signed in'))
    for p in Payment.objects.filter(tenant=tenant, created_at__gte=since).select_related('wallet__student')[:20]:
        verb = {'APPROVED': 'received', 'PENDING': 'waiting for approval', 'REJECTED': 'rejected'}.get(p.status, p.status)
        items.append((p.created_at, 'bi-cash-coin',
                      f'₦{p.amount:,.0f} payment {verb} — {p.wallet.student.full_name} family ({p.get_method_display()})'))
    for s in Student.objects.filter(tenant=tenant, created_at__gte=since).select_related('class_assigned')[:15]:
        items.append((s.created_at, 'bi-person-plus', f'{s.full_name} joined {s.class_assigned or "the school"}'))
    for a in Announcement.objects.filter(tenant=tenant, published=True, published_at__gte=since)[:10]:
        items.append((a.published_at, 'bi-megaphone', f'Announcement published: {a.title}'))
    try:
        from admissions.models import Application
        for a in Application.objects.filter(tenant=tenant, created_at__gte=since)[:10]:
            items.append((a.created_at, 'bi-inbox', f'New application: {a.full_name}'))
    except Exception:
        pass
    from results.models import ClassApproval
    for c in ClassApproval.objects.filter(tenant=tenant, approved_at__gte=since).select_related('school_class', 'term')[:10]:
        items.append((c.approved_at, 'bi-patch-check', f'Results approved for {c.school_class.name} ({c.term.name})'))
    items.sort(key=lambda x: x[0], reverse=True)
    return [{'when': w, 'icon': i, 'text': t} for w, i, t in items[:limit]]
