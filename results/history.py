"""
results/history.py
──────────────────
A class's report cards as one download, a student's progress across terms,
families asking for a result to be checked, and multi-year transcripts.
"""
import io
import zipfile
from collections import OrderedDict, defaultdict

from django.core.files.base import ContentFile
from django.utils import timezone
from django.utils.text import slugify

from users.models import ENROLLED_STATUSES, Student

from .models import ReportBundle, ResultQuery, TermResult

TRANSCRIPT_SALT = 'transcript'


class HistoryError(ValueError):
    pass


# ─────────────────────────────────────────────────────────────────────────────
# Whole-class downloads
# ─────────────────────────────────────────────────────────────────────────────

def html_to_pdf(html):
    """Report card HTML → PDF bytes (wkhtmltopdf, same options as single downloads)."""
    from lms_project.pdf import A4, html_to_pdf as safe_pdf
    return safe_pdf(html, **A4)


def request_bundle(school_class, term, user, published_only=False):
    if ReportBundle.objects.filter(tenant_id=school_class.tenant_id, school_class=school_class, term=term,
                                   status__in=(ReportBundle.PENDING, ReportBundle.RUNNING)).exists():
        raise HistoryError(f'Report cards for {school_class.name} are already being prepared.')
    if not TermResult.objects.filter(tenant_id=school_class.tenant_id, class_assigned=school_class, term=term).exists():
        raise HistoryError(f'{school_class.name} has no results for {term.name}.')
    bundle = ReportBundle.objects.create(tenant_id=school_class.tenant_id, school_class=school_class, term=term,
                                         created_by=user, published_only=published_only)
    from lms_project.tasks import run_in_background
    run_in_background(build_bundle, bundle.id, tenant=school_class.tenant)
    return bundle


def build_bundle(bundle_id):
    from django.template.loader import render_to_string

    from .views import _get_result_settings, _result_context_for_student
    from django.urls import get_urlconf, set_urlconf
    bundle = ReportBundle.objects.select_related('school_class', 'term__session', 'tenant').get(pk=bundle_id)
    bundle.status = ReportBundle.RUNNING
    bundle.save(update_fields=['status'])
    previous_urlconf = get_urlconf()
    set_urlconf('lms_project.tenant_urls')      # report cards link to school pages; no request here to say so
    try:
        settings_obj = _get_result_settings(bundle.tenant)
        students = Student.objects.filter(tenant_id=bundle.tenant_id, class_assigned=bundle.school_class,
                                          status__in=ENROLLED_STATUSES).order_by('full_name')
        buf, n = io.BytesIO(), 0
        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
            for st in students:
                ctx = _result_context_for_student(st, bundle.term, bundle.term.session,
                                                  published_only=bundle.published_only)
                if not ctx['results']:
                    continue
                ctx.update({'readonly': True, 'result_settings': settings_obj, 'is_pdf': True})
                html = render_to_string('parents/result_detail.html', ctx)
                z.writestr(f"{slugify(st.full_name) or st.id}-{slugify(st.admission_number)}.pdf", html_to_pdf(html))
                n += 1
        if not n:
            raise HistoryError('No student in the class had results to print.')
        name = slugify(f'{bundle.school_class.name}-{bundle.term.name}-{bundle.term.session.name}') + '.zip'
        bundle.file.save(name, ContentFile(buf.getvalue()), save=False)
        bundle.count, bundle.status = n, ReportBundle.DONE
    except Exception as problem:     # report any failure on the page rather than leaving it "running"
        bundle.status, bundle.error = ReportBundle.FAILED, str(problem)[:255]
    finally:
        set_urlconf(previous_urlconf)
    bundle.finished_at = timezone.now()
    bundle.save()
    return bundle


# ─────────────────────────────────────────────────────────────────────────────
# Progress across terms
# ─────────────────────────────────────────────────────────────────────────────

def progress(student, published_only=True):
    """
    Each term the student has results for, oldest first, with their average,
    and each subject's total per term — the data for the progress chart.
    """
    qs = TermResult.objects.filter(tenant_id=student.tenant_id, student=student, is_not_offering=False)
    if published_only:
        qs = qs.filter(published=True)
    terms, per_subject = OrderedDict(), defaultdict(dict)
    for tr in qs.select_related('term', 'term__session', 'subject').order_by('term__start_date', 'term__name'):
        terms.setdefault(tr.term_id, {'term': tr.term, 'scores': []})['scores'].append(tr.total_score or 0)
        per_subject[tr.subject][tr.term_id] = tr.total_score or 0
    term_list = list(terms.values())
    for t in term_list:
        t['average'] = round(sum(t['scores']) / len(t['scores']), 1)
    subjects = []
    for subject, by_term in sorted(per_subject.items(), key=lambda kv: kv[0].name):
        series = [by_term.get(t['term'].id) for t in term_list]
        known = [v for v in series if v is not None]
        change = round(known[-1] - known[-2], 1) if len(known) >= 2 else None
        subjects.append({'subject': subject, 'series': series, 'latest': known[-1] if known else None,
                         'change': change})
    return {'terms': term_list, 'subjects': subjects}


def chart_points(series, width=600, height=160, pad=12):
    """'x,y x,y …' for an SVG polyline (scores 0–100), skipping terms with no score."""
    n = len(series)
    if not n:
        return ''
    step = (width - 2 * pad) / max(1, n - 1)
    pts = []
    for i, v in enumerate(series):
        if v is None:
            continue
        x = pad + i * step if n > 1 else width / 2
        y = height - pad - (height - 2 * pad) * min(100, max(0, v)) / 100
        pts.append(f'{x:.1f},{y:.1f}')
    return ' '.join(pts)


def label_positions(terms, width=600, pad=12):
    """[(x, term)] matching chart_points' spacing, for the term labels under the chart."""
    n = len(terms)
    step = (width - 2 * pad) / max(1, n - 1)
    return [(round(pad + i * step if n > 1 else width / 2, 1), t['term']) for i, t in enumerate(terms)]


# ─────────────────────────────────────────────────────────────────────────────
# Asking for a result to be checked
# ─────────────────────────────────────────────────────────────────────────────

def raise_query(student, term, subject, user, message):
    tr = TermResult.objects.filter(tenant_id=student.tenant_id, student=student, term=term, subject=subject,
                                   published=True).first()
    if not tr:
        raise HistoryError(f'There is no published {subject.name} result to check.')
    if not (message or '').strip() or len(message.strip()) < 10:
        raise HistoryError('Say what you think is wrong, so the teacher knows what to check.')
    if ResultQuery.objects.filter(tenant_id=student.tenant_id, student=student, term=term, subject=subject,
                                  status=ResultQuery.OPEN).exists():
        raise HistoryError(f'You already asked about {subject.name}. The teacher will reply soon.')
    return ResultQuery.objects.create(tenant_id=student.tenant_id, student=student, term=term, subject=subject,
                                      raised_by=user, message=message.strip(), score_when_raised=tr.total_score)


def may_answer(user, query):
    from curriculum.services import may_review
    from users.models import StaffSubjectClass
    staff = getattr(user, 'staff', None)
    if may_review(user, query.subject):
        return True
    return bool(staff) and StaffSubjectClass.objects.filter(
        tenant_id=query.tenant_id, staff=staff, subject=query.subject,
        school_class_id=query.student.class_assigned_id).exists()


def answer(query, user, response, corrected=False):
    if query.status != ResultQuery.OPEN:
        raise HistoryError('This request has already been answered.')
    if not may_answer(user, query):
        raise HistoryError("Only the subject's teacher, its head of department or an administrator can answer.")
    if not (response or '').strip():
        raise HistoryError('Write a reply for the family.')
    tr = TermResult.objects.filter(tenant_id=query.tenant_id, student=query.student, term=query.term,
                                   subject=query.subject).first()
    after = tr.total_score if tr else None
    if corrected and after == query.score_when_raised:
        raise HistoryError('The score is unchanged. Correct it in score entry first, then mark this corrected.')
    query.status = ResultQuery.CORRECTED if corrected else ResultQuery.ANSWERED
    query.response, query.score_after = response.strip(), after
    query.responded_by, query.responded_at = user, timezone.now()
    query.save()
    from lms_project.tasks import run_in_background
    run_in_background(_tell_family, query.id, tenant=query.student.tenant)
    return query


def _tell_family(query_id):
    from communications.services import send_email
    from users.records import contacts
    q = ResultQuery.objects.select_related('student', 'student__tenant', 'subject', 'term').get(pk=query_id)
    change = (f' The score has been corrected from {q.score_when_raised:g} to {q.score_after:g}.'
              if q.status == ResultQuery.CORRECTED else '')
    body = (f"<p>The school has replied about {q.student.full_name}'s {q.subject.name} result for {q.term.name}.</p>"
            f"<blockquote>{q.response}</blockquote><p>{change}</p><p>{q.student.tenant.name}</p>")
    for _name, email in contacts(q.student, 'EMAIL'):
        send_email(email, f'Reply about {q.student.full_name}\'s {q.subject.name} result', body, q.student)


# ─────────────────────────────────────────────────────────────────────────────
# Transcripts
# ─────────────────────────────────────────────────────────────────────────────

def transcript(student):
    """Every published result, grouped by session and term, with averages."""
    from .analysis import bands, grade_for
    band_list = bands(student.tenant)
    rows = (TermResult.objects.filter(tenant_id=student.tenant_id, student=student, published=True,
                                      is_not_offering=False)
            .select_related('term', 'session', 'subject', 'class_assigned')
            .order_by('session__start_date', 'session__name', 'term__start_date', 'term__name', 'subject__name'))
    sessions = OrderedDict()
    for tr in rows:
        s = sessions.setdefault(tr.session_id, {'session': tr.session, 'terms': OrderedDict()})
        t = s['terms'].setdefault(tr.term_id, {'term': tr.term, 'class': tr.class_assigned, 'rows': []})
        g = grade_for(tr.total_score, band_list)
        t['rows'].append({'subject': tr.subject, 'score': tr.total_score, 'grade': g.grade if g else ''})
    out, all_scores = [], []
    for s in sessions.values():
        terms = []
        for t in s['terms'].values():
            scores = [r['score'] for r in t['rows']]
            all_scores += scores
            t['average'] = round(sum(scores) / len(scores), 2)
            terms.append(t)
        out.append({'session': s['session'], 'terms': terms,
                    'average': round(sum(t['average'] for t in terms) / len(terms), 2)})
    return {'student': student, 'sessions': out,
            'overall': round(sum(all_scores) / len(all_scores), 2) if all_scores else None}


def transcript_reference(student):
    from django.core import signing
    count = TermResult.objects.filter(tenant_id=student.tenant_id, student=student, published=True).count()
    return signing.dumps({'s': student.id, 't': str(student.tenant_id), 'n': count, 'd': timezone.localdate().isoformat()},
                         salt=TRANSCRIPT_SALT, compress=True)


def verify_transcript(ref, tenant):
    from django.core import signing
    try:
        data = signing.loads(ref, salt=TRANSCRIPT_SALT)
    except signing.BadSignature:
        return None
    if data.get('t') != str(tenant.id):
        return None
    student = Student.objects.filter(tenant=tenant, pk=data.get('s')).first()
    if not student:
        return None
    t = transcript(student)
    t['issued_on'] = data.get('d')
    t['changed'] = TermResult.objects.filter(tenant=tenant, student=student, published=True).count() != data.get('n')
    return t
