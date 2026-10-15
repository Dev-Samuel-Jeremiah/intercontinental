"""
results/analysis.py
───────────────────
Grades, class and subject positions (ties shared), the class broadsheet and
subject performance — one place so report cards, broadsheets and analysis
always agree.
"""
from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal

from users.models import ENROLLED_STATUSES, Student

from .models import Grading, TermResult

PRESETS = {
    'waec': ('WAEC / NECO (A1–F9)', [
        ('A1', 75, 'Excellent'), ('B2', 70, 'Very good'), ('B3', 65, 'Good'), ('C4', 60, 'Credit'),
        ('C5', 55, 'Credit'), ('C6', 50, 'Credit'), ('D7', 45, 'Pass'), ('E8', 40, 'Pass'), ('F9', 0, 'Fail')]),
    'letters': ('A–F (70, 60, 50, 45, 40)', [
        ('A', 70, 'Excellent'), ('B', 60, 'Very good'), ('C', 50, 'Good'), ('D', 45, 'Fair'),
        ('E', 40, 'Pass'), ('F', 0, 'Fail')]),
    'primary': ('Primary (A 80+, B 65+, C 50+, D 40+)', [
        ('A', 80, 'Excellent'), ('B', 65, 'Very good'), ('C', 50, 'Good'), ('D', 40, 'Fair'), ('E', 0, 'Needs help')]),
    'bece': ('BECE (Distinction, Credit, Pass, Fail)', [
        ('A', 70, 'Distinction'), ('C', 50, 'Credit'), ('P', 40, 'Pass'), ('F', 0, 'Fail')]),
}


class GradingError(ValueError):
    pass


def whole(score):
    """Round half up (74.5 → 75), unlike Python's round() which gives 74."""
    return int(Decimal(str(score or 0)).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def bands(tenant):
    return list(Grading.objects.filter(tenant=tenant).order_by('-min_score'))


def grade_for(score, band_list):
    s = Decimal(str(score or 0))
    return next((g for g in band_list if s >= g.min_score), None)


def parse_bands(data):
    """[(grade, min, description)] from posted rows grade_N / min_N / desc_N."""
    rows = []
    for i in range(30):
        grade = (data.get(f'grade_{i}') or '').strip().upper()[:2]
        raw = (data.get(f'min_{i}') or '').strip()
        if not grade and not raw:
            continue
        if not grade:
            raise GradingError('Every band needs a grade.')
        try:
            low = Decimal(raw)
        except Exception:
            raise GradingError(f'Grade {grade}: the lowest mark must be a number.')
        if not 0 <= low <= 100:
            raise GradingError(f'Grade {grade}: the lowest mark must be between 0 and 100.')
        rows.append((grade, low, (data.get(f'desc_{i}') or '').strip()[:255]))
    return rows


def check_bands(rows):
    if len(rows) < 2:
        raise GradingError('A grading scale needs at least two bands.')
    grades = [g for g, _, _ in rows]
    if len(set(grades)) != len(grades):
        raise GradingError('Each grade can only appear once.')
    lows = [Decimal(str(m)) for _, m, _ in rows]
    if len(set(lows)) != len(lows):
        raise GradingError('Two bands start at the same mark.')
    if min(lows) != 0:
        raise GradingError('The lowest band must start at 0, so every score gets a grade.')


def save_bands(tenant, rows):
    check_bands(rows)
    Grading.objects.filter(tenant=tenant).delete()
    for grade, low, desc in rows:
        Grading.objects.create(tenant=tenant, grade=grade, min_score=low, description=desc)


def regrade(tenant, term=None):
    """Re-store grade and remark on saved results after the scale changes. Returns how many changed."""
    band_list = bands(tenant)
    qs = TermResult.objects.filter(tenant=tenant, is_not_offering=False)
    if term:
        qs = qs.filter(term=term)
    changed = 0
    for tr in qs.only('id', 'total_score', 'grade', 'remark'):
        g = grade_for(tr.total_score, band_list)
        grade, remark = (g.grade, g.description or g.grade) if g else (None, None)
        if (grade, remark) != (tr.grade, tr.remark):
            TermResult.objects.filter(pk=tr.pk).update(grade=grade, remark=remark)
            changed += 1
    return changed


def ordinal(n):
    if n is None:
        return ''
    suffix = 'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')
    return f'{n}{suffix}'


def rank(values):
    """{key: position} from {key: number}, highest first; ties share a position (1, 2, 2, 4)."""
    ordered = sorted(values.items(), key=lambda kv: kv[1], reverse=True)
    out, prev, pos = {}, None, 0
    for i, (key, value) in enumerate(ordered, start=1):
        if value != prev:
            pos, prev = i, value
        out[key] = pos
    return out


def _class_rows(school_class, term, session):
    return (TermResult.objects.filter(tenant_id=school_class.tenant_id, class_assigned=school_class, term=term,
                                      session=session, is_not_offering=False,
                                      student__status__in=ENROLLED_STATUSES)
            .select_related('student', 'subject'))


def class_positions(school_class, term, session):
    """{student_id: position} by average of offered subjects; plus each average."""
    totals = defaultdict(list)
    for tr in _class_rows(school_class, term, session):
        totals[tr.student_id].append(tr.total_score or 0)
    averages = {sid: round(sum(v) / len(v), 2) for sid, v in totals.items() if v}
    return rank(averages), averages


def subject_positions(school_class, term, session):
    """{(student_id, subject_id): position}."""
    by_subject = defaultdict(dict)
    for tr in _class_rows(school_class, term, session):
        by_subject[tr.subject_id][tr.student_id] = tr.total_score or 0
    out = {}
    for subject_id, scores in by_subject.items():
        for sid, pos in rank(scores).items():
            out[(sid, subject_id)] = pos
    return out


def broadsheet(school_class, term, session):
    """Students × subjects grid with totals, averages and positions."""
    rows = list(_class_rows(school_class, term, session))
    subjects = sorted({tr.subject for tr in rows}, key=lambda s: s.name)
    band_list = bands(school_class.tenant)
    cells = defaultdict(dict)
    for tr in rows:
        g = grade_for(tr.total_score, band_list)
        cells[tr.student_id][tr.subject_id] = {'score': tr.total_score, 'grade': g.grade if g else ''}
    positions, averages = class_positions(school_class, term, session)
    students = Student.objects.filter(tenant_id=school_class.tenant_id, class_assigned=school_class,
                                      status__in=ENROLLED_STATUSES).order_by('full_name')
    out = []
    for st in students:
        mine = cells.get(st.id, {})
        out.append({'student': st, 'cells': [mine.get(s.id) for s in subjects],
                    'total': round(sum(c['score'] for c in mine.values()), 1) if mine else None,
                    'count': len(mine), 'average': averages.get(st.id), 'position': positions.get(st.id)})
    out.sort(key=lambda r: (r['position'] is None, r['position'] or 0, r['student'].full_name))
    return {'subjects': subjects, 'rows': out, 'analysis': subject_analysis(school_class, term, session, rows)}


def subject_analysis(school_class, term, session, rows=None, pass_mark=None):
    """Per subject: entries, mean, highest, lowest, pass rate and grade distribution."""
    if pass_mark is None:
        from curriculum.progression import policy_for
        pass_mark = float(policy_for(school_class.tenant).subject_pass_mark)
    rows = rows if rows is not None else list(_class_rows(school_class, term, session))
    band_list = bands(school_class.tenant)
    by_subject = defaultdict(list)
    for tr in rows:
        by_subject[tr.subject].append(tr.total_score or 0)
    out = []
    for subject, scores in sorted(by_subject.items(), key=lambda kv: kv[0].name):
        dist = defaultdict(int)
        for s in scores:
            g = grade_for(s, band_list)
            dist[g.grade if g else '—'] += 1
        passed = sum(1 for s in scores if s >= pass_mark)
        out.append({'subject': subject, 'entries': len(scores), 'mean': round(sum(scores) / len(scores), 1),
                    'highest': max(scores), 'lowest': min(scores),
                    'pass_rate': round(100 * passed / len(scores)),
                    'distribution': [(g.grade, dist.get(g.grade, 0)) for g in reversed(band_list)] if band_list else []})
    return out
