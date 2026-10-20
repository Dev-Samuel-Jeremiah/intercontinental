from users.models import ENROLLED_STATUSES
import json
from users.support import report_awards
from users.attendance import report_card_days
from datetime import date
from decimal import Decimal
from django.views.decorators.csrf import csrf_exempt
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST
from django.utils import timezone
from django.urls import reverse

from academics.models import AcademicSession, Term
from academics.utils import get_active_session, get_active_term

from users.models import Attendance, Class, ClassSubject, Staff, Student, Subject

from cbt.models import Exam, ExamAttempt

from results.services import generate_session_results, update_term_result
from curriculum.progression import offers
from results.locks import lock_for
from results.models import (
    AssessmentScheme,
    Grading,
    HeadTeacherSignature,
    HosRemark,
    ResultSheetSettings,
    SessionResult,
    Skill,
    SkillAssessment,
    StudentTermAttendance,
    SubjectTeacherRemark,
    TeacherRemark,
    TermResult,
    AIStudentComment,
)
from results.ai_comments import (
    generate_comments_for_student,
    generate_comments_for_class,
)


# ---------------------------------------------------------------------------
# Permission helpers
# ---------------------------------------------------------------------------

def _is_admin_or_super(user):
    if user.is_superuser:
        return True
    staff = getattr(user, 'staff', None)
    return staff and staff.role == 'ADMIN'

def _is_staff_member(user):
    """School staff (or the school admin): the only people who may open any student's results."""
    return user.is_superuser or getattr(user, 'staff', None) is not None

def _can_enter_scores(user):
    if _is_admin_or_super(user):
        return True
    staff = getattr(user, 'staff', None)
    return staff and getattr(staff, 'can_enter_scores', False)

def _can_publish_results(user):
    if _is_admin_or_super(user):
        return True
    staff = getattr(user, 'staff', None)
    return staff and getattr(staff, 'can_publish_results', False)

def _can_mark_attendance(user):
    if _is_admin_or_super(user):
        return True
    staff = getattr(user, 'staff', None)
    return staff and getattr(staff, 'can_mark_attendance', False)



def _get_grading_list():
    from tenants.middleware import get_current_tenant
    tenant = get_current_tenant()
    qs = Grading.objects.filter(tenant=tenant) if tenant else Grading.objects.all()
    return list(qs.order_by('-min_score'))

def _get_result_settings(tenant):
    """Fetch (or lazily create) this school's result-sheet layout preferences."""
    rs, _ = ResultSheetSettings.objects.get_or_create(tenant=tenant)
    return rs


def _resolve_grade(score, grading_list):
    """Return the first Grading whose min_score <= score (highest match)."""
    score_decimal = Decimal(str(score))
    return next((g for g in grading_list if score_decimal >= g.min_score), None)


def _class_age_avg(class_obj):
    dob_list = (
        Student.objects
        .filter(class_assigned=class_obj, date_of_birth__isnull=False)
        .values_list('date_of_birth', flat=True)
    )
    if not dob_list:
        return '-'
    today = date.today()
    total = sum(
        today.year - d.year - ((today.month, today.day) < (d.month, d.day))
        for d in dob_list
    )
    return round(total / len(dob_list), 1)


def _enrich_results(results, grading_list, remark_dict):
    """
    Attach .percent, .grade, .remark to each TermResult object in-place.
    Subject teacher remark overrides the grading description when present.
    """
    from .analysis import whole
    for r in results:
        score   = whole(r.total_score)   # 74.5 → 75 (round() would give 74)
        r.percent = score
        grading = _resolve_grade(score, grading_list)
        r.grade = grading.grade if grading else ''
        subject_remark = remark_dict.get(r.subject_id, '').strip()
        r.remark = subject_remark if subject_remark else (
            grading.description if grading else ''
        )
    return results




def _compute_subject_averages(results, student, term, session):
    """
    For each TermResult in *results*, attach two attributes:
      .subject_avg  — class average total score for that subject this term
      .class_avg    — overall class average (avg of all subject totals) this term

    Only offering students (is_not_offering=False) are counted in both averages.
    """
    from django.db.models import Avg

    if not results:
        return

    class_obj   = student.class_assigned
    subject_ids = [r.subject_id for r in results]

    # For each subject: average total_score across all offering students in the class
    subject_avg_qs = (
        TermResult.objects
        .filter(
            class_assigned=class_obj,
            term=term,
            session=session,
            subject_id__in=subject_ids,
            is_not_offering=False,
        )
        .values('subject_id')
        .annotate(avg=Avg('total_score'))
    )
    subject_avg_map = {row['subject_id']: round(row['avg'] or 0, 1) for row in subject_avg_qs}

    # Overall class average: average total_score across ALL subjects and ALL offering students
    overall = (
        TermResult.objects
        .filter(
            class_assigned=class_obj,
            term=term,
            session=session,
            subject_id__in=subject_ids,
            is_not_offering=False,
        )
        .aggregate(avg=Avg('total_score'))
    )
    class_avg_val = round(overall['avg'] or 0, 1)

    for r in results:
        r.subject_avg = subject_avg_map.get(r.subject_id, 0)
        r.class_avg   = class_avg_val


def _head_teacher_signature():
    sig = HeadTeacherSignature.objects.first()
    return sig.signature.url if sig and sig.signature else None


def _build_pdf_response(request, template, context, filename):
    try:
        import pdfkit
    except ImportError:
        return HttpResponse('<h3>pdfkit is not installed. PDF generation is unavailable.</h3>')

    # wkhtmltopdf's rendering engine evaluates our mobile @media breakpoints
    # as if the viewport were narrow regardless of any viewport-size option,
    # which collapses the multi-column report card (info band, totals band,
    # remarks, skills & behaviour) into a single stacked column. Flagging
    # is_pdf lets the templates skip those responsive rules entirely for the
    # PDF render, so it matches the desktop layout the browser shows.
    pdf_context = {**context, 'is_pdf': True}

    # print-media-type makes wkhtmltopdf honour the @media print rules in the
    # template (which hide the on-screen header/download-button chrome and
    # zero out body padding). Without it wkhtmltopdf renders using the
    # screen stylesheet, so that extra chrome is included in the output and
    # pushes the report card onto a second page. page-size/margins pin the
    # sheet to a single A4 page instead of wkhtmltopdf's Letter default.
    options = {
        'enable-local-file-access': None,
        'quiet':                    '',
        'print-media-type':         None,
        'page-size':                'A4',
        'margin-top':               '5mm',
        'margin-bottom':            '5mm',
        'margin-left':              '5mm',
        'margin-right':             '5mm',
    }
    config  = pdfkit.configuration(wkhtmltopdf='/usr/bin/wkhtmltopdf')
    html    = render(request, template, pdf_context).content.decode()
    try:
        pdf  = pdfkit.from_string(html, False, options=options, configuration=config)
        resp = HttpResponse(pdf, content_type='application/pdf')
        resp['Content-Disposition'] = f'attachment; filename="{filename}"'
        return resp
    except OSError as exc:
        return HttpResponse(f'<h3>PDF generation failed:</h3><p>{exc}</p>')


def _verify_ref(student, term):
    from .release import reference
    return reference(student, term)


def _verify_qr(student, ref):
    """The verification QR as an inline image, so it also prints in PDF downloads."""
    import base64
    from django.urls import reverse as _reverse
    from idcards.utils import qr_png_bytes
    url = student.tenant.portal_url + _reverse('results:verify', args=[ref], urlconf='lms_project.tenant_urls')
    return 'data:image/png;base64,' + base64.b64encode(qr_png_bytes(url)).decode()


def _result_context_for_student(student, term, session, published_only=False):
    """
    Build the shared result context dict used by the admin term_results view,
    parent_result_detail, and student_result_detail.
    Keeps the three views DRY.
    """
    assigned_subject_ids = ClassSubject.objects.filter(
        school_class=student.class_assigned
    ).values_list('subject_id', flat=True)

    # Fetch all results then EXCLUDE not-offering subjects entirely.
    # is_not_offering=True rows must not appear on the report card at all.
    all_results = list(
        TermResult.objects
        .filter(
            student=student,
            term=term,
            session=session,
            subject_id__in=assigned_subject_ids,
            **({'published': True} if published_only else {}),
        )
        .select_related('subject')
    )

    # Keep only subjects the student is actually offering
    results = [r for r in all_results if not getattr(r, 'is_not_offering', False)]

    grading_list = _get_grading_list()
    remark_dict  = {
        r.subject_id: r.remark
        for r in SubjectTeacherRemark.objects.filter(
            student=student, term=term, session=session
        )
    }
    _enrich_results(results, grading_list, remark_dict)
    _compute_subject_averages(results, student, term, session)

    from .analysis import class_positions, ordinal, subject_positions, whole
    positions, _ = class_positions(student.class_assigned, term, session) if student.class_assigned else ({}, {})
    subject_pos = subject_positions(student.class_assigned, term, session) if student.class_assigned else {}
    for r in results:
        r.subject_position = ordinal(subject_pos.get((student.id, r.subject_id)))

    total_obtained   = whole(sum(r.total_score for r in results))
    total_obtainable = len(results) * 100
    overall_percentage = (
        round(sum(r.total_score for r in results) / total_obtainable * 100, 2)
        if total_obtainable else 0
    )

    tr_obj  = TeacherRemark.objects.filter(student=student, term=term, session=session).first()
    hos_obj = HosRemark.objects.filter(student=student, term=term, session=session).first()

    skills = Skill.objects.all().order_by('category')
    assessment_dict = {
        a.skill_id: a.score
        for a in SkillAssessment.objects.filter(student=student, term=term, session=session)
    }

    return {
        'student':               student,
        'term':                  term,
        'session':               session,
        'results':               results,
        'school_days_open':      term.number_of_school_days or 0,
        'total_present':         _get_days_present(student, term, session),
        'total_absent':          _get_days_absent(student, term, session),
        'class_teacher_remark':  tr_obj.remark  if tr_obj  else '',
        'hos_remark':            hos_obj.remark if hos_obj else '',
        'head_teacher_signature': _head_teacher_signature(),
        'gender':                student.gender,
        'age':                   student.age,
        'class_age_avg':         _class_age_avg(student.class_assigned),
        'no_in_class':           Student.objects.filter(status__in=ENROLLED_STATUSES, class_assigned=student.class_assigned).count(),
        'awards':                report_awards(student, term, session),
        'total_obtained':        total_obtained,
        'total_obtainable':      total_obtainable,
        'overall_percentage':    overall_percentage,
        'skills':                skills,
        'assessment_dict':       assessment_dict,
        'ratings':               [5, 4, 3, 2, 1],
        'resumption_date_next_term': term.resumption_date_next_term,
        'class_position':        ordinal(positions.get(student.id)),
        'verify_ref':            (ref := _verify_ref(student, term)),
        'verify_qr':             _verify_qr(student, ref),
        'ranked_in_class':       len(positions),
    }


# ---------------------------------------------------------------------------
# Admin / Staff: Term Results (filterable report card)
# ---------------------------------------------------------------------------

def _get_days_present(student, term, session):
    """Days present this term: the school's manual figure if set, else counted from the register."""
    return report_card_days(student, term, session)[0]


def _get_days_absent(student, term, session):
    """Days absent this term — never the student's whole history."""
    return report_card_days(student, term, session)[1]


@login_required
def my_results(request):
    """Students and parents: their own published report cards, never anyone else's."""
    student = getattr(request.user, 'student', None)
    if student is not None:
        published = TermResult.objects.filter(student=student, published=True)
        wanted = published.filter(term_id=request.GET.get('term') or 0)
        latest = (wanted.first() or published.order_by('-session__start_date', '-term__start_date', '-term_id').first())
        if latest is None:
            messages.info(request, 'Your results have not been published yet.')
            return redirect('dashboard:student')
        return redirect('results:student_result_detail', latest.session_id, latest.term_id)
    if getattr(request.user, 'is_parent', False):
        return redirect('results:dashboard')
    return redirect('dashboard:router')


@login_required
def term_results(request):
    # The filterable report sheet is a staff page: it can open any student.
    if not _is_staff_member(request.user):
        return my_results(request)
    selected_class   = request.GET.get('class')
    selected_student = request.GET.get('student')
    selected_term    = request.GET.get('term')
    selected_session = request.GET.get('session')

    classes  = Class.objects.all()
    students = Student.objects.all()
    terms    = Term.objects.all()
    sessions = AcademicSession.objects.all().order_by('-name')

    # Defaults — fall back to active session / term
    session = get_active_session()
    term    = get_active_term()

    student               = None
    class_teacher_remark  = ''
    hos_remark            = ''
    class_age_avg         = None
    no_in_class           = None
    total_obtained        = 0
    total_obtainable      = 0
    overall_percentage    = 0
    age                   = None
    gender                = None
    total_present         = 0
    total_absent          = 0
    school_days_open      = 0
    skills                = []
    assessment_dict       = {}
    ratings               = [5, 4, 3, 2, 1]
    resumption_date_next_term = None

    selected_class_name   = None
    selected_student_name = None
    selected_term_name    = None
    selected_session_name = None

    results = TermResult.objects.select_related('student', 'subject', 'term', 'session')

    # ── session filter ──────────────────────────────────────────────────────
    if selected_session:
        session = get_object_or_404(AcademicSession, id=selected_session)
        selected_session_name = session.name
        results = results.filter(session=session)

    # ── term filter ─────────────────────────────────────────────────────────
    if selected_term:
        term = get_object_or_404(Term, id=selected_term)
        selected_term_name        = term.name
        school_days_open          = term.number_of_school_days or 0
        resumption_date_next_term = term.resumption_date_next_term
        results = results.filter(term=term)
        # If no explicit session chosen, derive from the term's session
        if not selected_session and term.session_id:
            session = term.session

    # ── class filter ────────────────────────────────────────────────────────
    if selected_class:
        try:
            class_obj           = Class.objects.get(id=selected_class)
            selected_class_name = class_obj.name
            results             = results.filter(class_assigned_id=selected_class)
            students            = students.filter(class_assigned_id=selected_class)
        except Class.DoesNotExist:
            pass

    # ── student filter ──────────────────────────────────────────────────────
    if selected_student and term and session:
        student               = get_object_or_404(Student, id=selected_student)
        selected_student_name = student.full_name

        assigned_subject_ids = ClassSubject.objects.filter(
            school_class=student.class_assigned
        ).values_list('subject_id', flat=True)

        results = results.filter(
            student=student,
            subject_id__in=assigned_subject_ids,
        )

        # Attendance
        total_present = _get_days_present(student, term, session)
        total_absent  = _get_days_absent(student, term, session)

        # Remarks — safe get_or_create now that TeacherRemark has unique_together
        tr_obj, _ = TeacherRemark.objects.get_or_create(
            student=student,
            term=term,
            session=session,
            defaults={'remark': ''},
        )
        class_teacher_remark = tr_obj.remark or ''

        hos_obj    = HosRemark.objects.filter(student=student, term=term, session=session).first()
        hos_remark = hos_obj.remark if hos_obj else ''

        # Student info
        age           = student.age
        gender        = student.gender
        no_in_class   = Student.objects.filter(status__in=ENROLLED_STATUSES, class_assigned=student.class_assigned).count()
        class_age_avg = _class_age_avg(student.class_assigned)

        # Enrich results
        grading_list = _get_grading_list()
        remark_dict  = {
            r.subject_id: r.remark
            for r in SubjectTeacherRemark.objects.filter(
                student=student, term=term, session=session
            )
        }
        results = [r for r in list(results) if not getattr(r, 'is_not_offering', False)]
        _enrich_results(results, grading_list, remark_dict)
        _compute_subject_averages(results, student, term, session)

        total_obtained    = int(round(sum(r.total_score for r in results)))
        total_obtainable  = len(results) * 100
        overall_percentage = (
            int(round((total_obtained / total_obtainable) * 100))
            if total_obtainable else 0
        )

        # Skills & behaviour
        skills = Skill.objects.all().order_by('category')
        assessment_dict = {
            a.skill_id: a.score
            for a in SkillAssessment.objects.filter(student=student, term=term, session=session)
        }

    context = {
        'results':               results,
        'classes':               classes,
        'students':              students,
        'terms':                 terms,
        'sessions':              sessions,
        'selected_class':        selected_class,
        'selected_student':      selected_student,
        'selected_term':         selected_term,
        'selected_session':      selected_session,
        'selected_class_name':   selected_class_name,
        'selected_student_name': selected_student_name,
        'selected_term_name':    selected_term_name,
        'selected_session_name': selected_session_name,
        'student':               student,
        'session':               session,
        'term':                  term,
        'school_days_open':      school_days_open,
        'total_present':         total_present,
        'total_absent':          total_absent,
        'age':                   age,
        'gender':                gender,
        'class_age_avg':         class_age_avg,
        'no_in_class':           no_in_class,
        'awards':                report_awards(student, term, session),
        'total_obtained':        total_obtained,
        'total_obtainable':      total_obtainable,
        'overall_percentage':    overall_percentage,
        'class_teacher_remark':  class_teacher_remark,
        'hos_remark':            hos_remark,
        'head_teacher_signature': _head_teacher_signature(),
        'skills':                skills,
        'assessment_dict':       assessment_dict,
        'ratings':               ratings,
        'resumption_date_next_term': resumption_date_next_term,
        'result_settings':       _get_result_settings(request.tenant),
    }
    return render(request, 'results/term_results.html', context)


# ---------------------------------------------------------------------------
# Admin / Staff: Manual score entry
# ---------------------------------------------------------------------------

def _sheet_context(request, class_id, subject_id, term_id):
    from .release import may_review_subject, settings_for
    if not (class_id and subject_id and term_id) or not settings_for(request.tenant).require_approval:
        return None
    from .models import SubjectSheet
    subject = Subject.objects.filter(pk=subject_id).first()
    sheet = SubjectSheet.objects.filter(school_class_id=class_id, subject_id=subject_id, term_id=term_id).first()
    return {'obj': sheet, 'status': sheet.status if sheet else 'ENTERED',
            'can_review': bool(subject) and may_review_subject(request.user, subject)}


@login_required
def manual_score_entry(request):
    """
    Manual CA + Exam score entry.
    - Toggle students as "Not Offering" (grade=ABS, scores=0, excluded from stats).
    - Not-offering students are excluded from averages, highest, lowest, position.
    - No essay column (as per school config).
    - Accurate class statistics only count offering students.
    """
    # 🔐 PERMISSION CHECK
    if not _can_enter_scores(request.user):
        messages.error(request, "You do not have permission to enter scores.")
        return redirect('dashboard:router')

    active_session = get_active_session()
    scheme   = AssessmentScheme.for_tenant(getattr(request, 'tenant', None))
    classes  = Class.objects.all().order_by('name')
    terms    = Term.objects.all()
    subjects = Subject.objects.all()

    selected_class   = request.POST.get('class')   or request.GET.get('class')
    selected_subject = request.POST.get('subject') or request.GET.get('subject')
    selected_term    = request.POST.get('term')    or request.GET.get('term')

    if selected_class:
        subjects = Subject.objects.filter(
            classsubject__school_class_id=selected_class
        ).distinct().order_by('name')

    rows = []

    if selected_class and selected_subject and selected_term:
        students = Student.objects.filter(
            class_assigned_id=selected_class, status__in=ENROLLED_STATUSES
        ).order_by('full_name')

        for student in students:
            tr, created = TermResult.objects.get_or_create(
                student=student,
                class_assigned_id=selected_class,
                subject_id=selected_subject,
                session=active_session,
                term_id=selected_term,
            )
            if created and not offers(student, int(selected_subject)):
                tr.is_not_offering = True   # not on the student's subject list (stream/electives)
                tr.save()
            rows.append({
                'student': student,
                'tr':      tr,
                'cbt':     round(tr.raw_exam_score or 0, 1),
            })

    # ── POST: save scores + not-offering toggles ──────────────────────
    if request.method == 'POST':
        from .assessment import AssessmentError, clean_score
        from .locks import ScoresLocked, check_open
        back = (f"{request.path}?class={selected_class}"
                f"&subject={selected_subject}&term={selected_term}")
        try:
            check_open(Term.objects.filter(pk=selected_term).first())
        except ScoresLocked as problem:
            messages.error(request, str(problem))
            return redirect(back)
        from .release import may_edit_scores
        allowed, why = may_edit_scores(request.user, Class.objects.get(pk=selected_class),
                                       Subject.objects.get(pk=selected_subject), Term.objects.get(pk=selected_term))
        if not allowed:
            messages.error(request, why)
            return redirect(back)
        tr_ids = set()
        for key in request.POST:
            for prefix in ('ca1_', 'ca2_', 'ca3_', 'essay_', 'exam_', 'not_offering_'):
                if key.startswith(prefix):
                    try:
                        tr_ids.add(int(key.split('_', 2)[-1]))
                    except (ValueError, IndexError):
                        pass

        # Check every score before saving any, so a typo can't half-save a class.
        pending, problems = [], []
        for tr in TermResult.objects.filter(id__in=tr_ids).select_related('student'):
            not_offering = f'not_offering_{tr.id}' in request.POST
            tr.is_not_offering = not_offering
            if not not_offering:
                for col in AssessmentScheme.COLUMNS:
                    top = getattr(scheme, f'{col}_max')
                    if not top:
                        setattr(tr, f'{col}_score', 0)
                        continue
                    try:
                        setattr(tr, f'{col}_score', clean_score(request.POST.get(f'{col}_{tr.id}'), top,
                                                                getattr(scheme, f'{col}_label')))
                    except AssessmentError as problem:
                        problems.append(f'{tr.student.full_name}: {problem}')
            pending.append(tr)
        if problems:
            messages.error(request, 'Nothing was saved. ' + ' '.join(problems[:6])
                           + (f' …and {len(problems) - 6} more.' if len(problems) > 6 else ''))
            return redirect(back)
        saved = 0
        for tr in pending:
            tr.save()  # model zeroes not-offering rows and recalcs total, grade, remark
            saved += 1

        messages.success(request, f'Scores saved for {saved} student(s).')
        return redirect(back)

    # ── Compute stats — offering students only ────────────────────────
    offering = [r for r in rows if not r['tr'].is_not_offering]
    not_off  = [r for r in rows if r['tr'].is_not_offering]

    stats = {
        'total':    len(rows),
        'offering': len(offering),
        'not_offering': len(not_off),
        'highest':  max((r['tr'].total_score for r in offering), default=None),
        'lowest':   min((r['tr'].total_score for r in offering), default=None),
    }

    # Class averages — only offering students
    n = len(offering)
    if n:
        class_avg = {
            'ca1':   round(sum(r['tr'].ca1_score       or 0 for r in offering) / n, 1),
            'ca2':   round(sum(r['tr'].ca2_score       or 0 for r in offering) / n, 1),
            'ca3':   round(sum(r['tr'].ca3_score       or 0 for r in offering) / n, 1),
            'essay': round(sum(r['tr'].essay_score     or 0 for r in offering) / n, 1),
            'exam':  round(sum(r['tr'].exam_score      or 0 for r in offering) / n, 1),
            'cbt':   round(sum(r['cbt']                or 0 for r in offering) / n, 1),
            'total': round(sum(r['tr'].total_score     or 0 for r in offering) / n, 1),
        }
    else:
        class_avg = {}

    # Position ranking — offering students sorted by total_score desc
    sorted_offering = sorted(offering, key=lambda r: r['tr'].total_score or 0, reverse=True)
    positions = {}
    pos = 1
    for i, r in enumerate(sorted_offering):
        if i > 0 and r['tr'].total_score == sorted_offering[i-1]['tr'].total_score:
            positions[r['tr'].id] = positions[sorted_offering[i-1]['tr'].id]
        else:
            positions[r['tr'].id] = pos
        pos += 1

    for r in rows:
        r['position'] = positions.get(r['tr'].id, '—')

    return render(request, 'results/manual_score_entry.html', {
        'classes':          classes,
        'subjects':         subjects,
        'terms':            terms,
        'rows':             rows,
        'selected_class':   selected_class,
        'selected_subject': selected_subject,
        'selected_term':    selected_term,
        'scheme':           scheme,
        'lock':             lock_for(Term.objects.filter(pk=selected_term).first()) if selected_term else None,
        'now':              timezone.now(),
        'sheet':            _sheet_context(request, selected_class, selected_subject, selected_term),
        'class_avg':        class_avg,
        'active_session':   active_session,
        'stats':            stats,
    })



# ---------------------------------------------------------------------------
# CBT Scores Reference page
# ---------------------------------------------------------------------------

@login_required
def cbt_scores_reference(request):
    """
    Read-only view of locked CBT (raw_exam_score) results per class/subject/term.
    Teachers use this to copy scores into manual_score_entry.
    Also provides a one-click "Import CBT → exam_score" action.
    """
    # 🔐 PERMISSION CHECK
    if not _can_enter_scores(request.user):
        messages.error(request, "You do not have permission to view CBT scores.")
        return redirect('dashboard:router')

    active_session = get_active_session()

    classes  = Class.objects.all()
    terms    = Term.objects.all()
    subjects = Subject.objects.all()

    selected_class   = request.GET.get('class')
    selected_subject = request.GET.get('subject')
    selected_term    = request.GET.get('term')

    if selected_class:
        subjects = Subject.objects.filter(
            classsubject__school_class_id=selected_class
        ).distinct()

    rows = []
    session = active_session

    if selected_term:
        try:
            from academics.models import Term as TermModel
            t = TermModel.objects.get(id=selected_term)
            session = t.session
        except Exception:
            pass

    if selected_class and selected_subject and selected_term:
        students = Student.objects.filter(
            class_assigned_id=selected_class
        ).order_by('full_name')

        exams = Exam.objects.filter(
            classes__id=selected_class,
            subject_id=selected_subject,
            session=session,
            term_id=selected_term,
            published=True,
        ).prefetch_related('parts')

        for student in students:
            # Pull CBT score fresh from ExamAttempt
            cbt_score = 0
            for exam in exams:
                for part in exam.parts.all():
                    attempt = ExamAttempt.objects.filter(
                        student=student, exam_part=part, completed=True
                    ).first()
                    if attempt:
                        cbt_score += attempt.score

            tr = TermResult.objects.filter(
                student=student,
                class_assigned_id=selected_class,
                subject_id=selected_subject,
                session=session,
                term_id=selected_term,
            ).first()

            rows.append({
                'student':          student,
                'cbt_score':        round(cbt_score, 1),
                'current_exam':     round(tr.exam_score, 1) if tr else 0,
                'raw_exam_score':   round(tr.raw_exam_score or 0, 1) if tr else 0,
                'tr_id':            tr.id if tr else None,
                'synced':           tr and round(tr.exam_score, 1) == round(cbt_score, 1),
            })

    # One-click import: copy CBT score → exam_score for all rows
    if request.method == 'POST' and request.POST.get('action') == 'import_cbt':
        from .locks import ScoresLocked, check_open
        back = (f"{request.path}?class={selected_class}"
                f"&subject={selected_subject}&term={selected_term}")
        try:
            check_open(Term.objects.filter(pk=selected_term).first())
        except ScoresLocked as problem:
            messages.error(request, str(problem))
            return redirect(back)
        from .release import may_edit_scores
        allowed, why = may_edit_scores(request.user, Class.objects.get(pk=selected_class),
                                       Subject.objects.get(pk=selected_subject), Term.objects.get(pk=selected_term))
        if not allowed:
            messages.error(request, why)
            return redirect(back)
        exam_max = AssessmentScheme.for_tenant(getattr(request, 'tenant', None)).exam_max
        too_high = [r['student'].full_name for r in rows if r['cbt_score'] > exam_max]
        if too_high:
            messages.error(request, f"Nothing imported: {', '.join(too_high[:5])} scored above the exam column's "
                                    f"maximum of {exam_max}. Check the CBT exam's total marks or the assessment structure.")
            return redirect(back)
        imported = 0
        new_scores = {r['tr_id']: r['cbt_score'] for r in rows if r['tr_id'] and r['cbt_score'] > 0}
        # Save each row normally, so totals are recalculated and the change is in the audit trail
        for tr in TermResult.objects.filter(id__in=list(new_scores)):
            tr.exam_score = tr.raw_exam_score = new_scores[tr.id]
            tr.save()
            imported += 1
        messages.success(request, f'Imported CBT scores for {imported} student(s). Totals recalculated.')
        return redirect(
            f"{request.path}?class={selected_class}"
            f"&subject={selected_subject}&term={selected_term}"
        )

    return render(request, 'results/cbt_scores_reference.html', {
        'classes':          classes,
        'subjects':         subjects,
        'terms':            terms,
        'rows':             rows,
        'selected_class':   selected_class,
        'selected_subject': selected_subject,
        'selected_term':    selected_term,
        'session':          session,
    })


# ---------------------------------------------------------------------------
# Admin / Staff: Generate session results
# ---------------------------------------------------------------------------

@login_required
def generate_session_results_view(request):
    if not _can_publish_results(request.user):
        messages.error(request, 'You do not have permission to generate session results.')
        return redirect('dashboard:router')
    session = get_active_session()
    if not session:
        messages.error(request, 'No active session found.')
        return redirect('results:cumulative_results')
    generate_session_results(session)
    messages.success(request, f'Session results for {session.name} generated successfully!')
    return redirect('results:cumulative_results')


# ---------------------------------------------------------------------------
# Admin / Staff: Cumulative (session) results
# ---------------------------------------------------------------------------

@login_required
def cumulative_results(request):
    if not _is_staff_member(request.user):
        return my_results(request)
    selected_class   = request.GET.get('class')
    selected_student = request.GET.get('student')
    selected_session = request.GET.get('session')

    classes  = Class.objects.all()
    sessions = AcademicSession.objects.all().order_by('-name')
    students = Student.objects.all()

    if selected_class:
        students = students.filter(class_assigned_id=selected_class)

    # Resolve session object (default to active session)
    session_obj = None
    if selected_session:
        session_obj = AcademicSession.objects.filter(id=selected_session).first()
    if not session_obj:
        session_obj = get_active_session()

    # ── Auto-generate SessionResults for this session so data is always fresh ──
    if session_obj:
        generate_session_results(session_obj)

    qs = SessionResult.objects.select_related('student', 'class_assigned', 'session', 'subject')
    if selected_class:
        qs = qs.filter(class_assigned_id=selected_class)
    if selected_student:
        qs = qs.filter(student_id=selected_student)
    if session_obj:
        qs = qs.filter(session=session_obj)

    student_obj  = get_object_or_404(Student, id=selected_student) if selected_student else None
    grading_list = _get_grading_list()
    results      = []
    total_obtained = 0

    if student_obj:
        for r in qs.filter(student=student_obj).order_by('subject__name'):
            avg     = r.average_score or 0
            grading = _resolve_grade(avg, grading_list)
            results.append({
                'subject':       r.subject,
                'first_term':    r.first_term  if r.first_term  else '-',
                'second_term':   r.second_term if r.second_term else '-',
                'third_term':    r.third_term  if r.third_term  else '-',
                'total_score':   r.total_score,
                'average_score': round(avg, 2),
                'grade':         grading.grade       if grading else '-',
                'remark':        grading.description if grading else '-',
            })
            total_obtained += avg

    total_obtainable = len(results) * 100

    # ── Class position (cumulative) ───────────────────────────────────────
    cumulative_position = None
    class_size = None
    if student_obj and session_obj:
        class_obj = student_obj.class_assigned
        # Get every student in the class and sum their average_score across subjects
        class_students = Student.objects.filter(status__in=ENROLLED_STATUSES, class_assigned=class_obj)
        student_totals = {}
        for sr in SessionResult.objects.filter(session=session_obj, class_assigned=class_obj):
            student_totals.setdefault(sr.student_id, 0)
            student_totals[sr.student_id] += sr.average_score or 0

        class_size = len(student_totals)
        student_grand_total = student_totals.get(student_obj.id, 0)
        better_than = sum(1 for v in student_totals.values() if v > student_grand_total)
        cumulative_position = better_than + 1

    # ── Teacher remark (session-wide, no term) ────────────────────────────
    teacher_remark_obj = None
    if student_obj and session_obj:
        teacher_remark_obj = TeacherRemark.objects.filter(
            student=student_obj, session=session_obj, term__isnull=True
        ).first()
        if not teacher_remark_obj:
            # Fall back to any remark in this session
            teacher_remark_obj = TeacherRemark.objects.filter(
                student=student_obj, session=session_obj
            ).last()

    return render(request, 'results/cumulative_results.html', {
        'classes':              classes,
        'students':             students,
        'sessions':             sessions,
        'results':              results,
        'selected_class':       selected_class,
        'selected_student':     selected_student,
        'selected_session':     selected_session or (str(session_obj.id) if session_obj else ''),
        'student':              student_obj,
        'session':              session_obj,
        'total_obtained':       round(total_obtained, 2),
        'total_obtainable':     total_obtainable,
        'overall_percentage':   round((total_obtained / total_obtainable) * 100, 2) if total_obtainable else 0,
        'head_teacher_signature': _head_teacher_signature(),
        # Extra student info
        'gender':               getattr(student_obj, 'gender', '-') if student_obj else '-',
        'age':                  getattr(student_obj, 'age', '-')    if student_obj else '-',
        'class_age_avg':        _class_age_avg(student_obj.class_assigned) if student_obj else '-',
        'no_in_class':          class_size or (
                                    Student.objects.filter(status__in=ENROLLED_STATUSES, class_assigned=student_obj.class_assigned).count()
                                    if student_obj else '-'),
        'cumulative_position':  cumulative_position,
        'class_teacher_remark': teacher_remark_obj.remark if teacher_remark_obj else '',
    })


# ---------------------------------------------------------------------------
# Admin / Staff: Publish results
# ---------------------------------------------------------------------------

@login_required
def publish_results_page(request):
    # 🔐 PERMISSION CHECK
    if not _can_publish_results(request.user):
        messages.error(request, "You do not have permission to publish results.")
        return redirect('dashboard:router')

    # ── All sessions and terms for the filter dropdowns ──────────────────
    all_sessions = AcademicSession.objects.all().order_by('-name')
    all_terms    = Term.objects.all().order_by('session__name', 'name')
 
    # ── GET filters ───────────────────────────────────────────────────────
    selected_session_id = request.GET.get('session', '').strip()
    selected_term_id    = request.GET.get('term', '').strip()
    selected_class      = request.GET.get('class', '').strip()
    selected_status     = request.GET.get('status', '').strip()
 
    # Resolve session & term — fall back to active if not selected
    active_session = get_active_session()
    active_term    = get_active_term()
 
    if selected_session_id:
        session = get_object_or_404(AcademicSession, id=selected_session_id)
    else:
        session = active_session
 
    if selected_term_id:
        term = get_object_or_404(Term, id=selected_term_id)
    else:
        term = active_term
 
    # Terms belonging to the selected session (for cascading dropdown)
    session_terms = Term.objects.filter(session=session).order_by('name') if session else Term.objects.none()
 
    # ── Base queryset ─────────────────────────────────────────────────────
    results_qs = TermResult.objects.filter(
        session=session, term=term
    ).select_related('student', 'class_assigned', 'subject') if (session and term) else TermResult.objects.none()
 
    if selected_class:
        results_qs = results_qs.filter(class_assigned_id=selected_class)
 
    # ── POST: publish / unpublish ─────────────────────────────────────────
    if request.method == 'POST':
        action        = request.POST.get('action', '')   # 'publish' or 'unpublish'
        student_ids   = request.POST.getlist('student_ids')
        post_session  = request.POST.get('post_session', '')
        post_term     = request.POST.get('post_term', '')
 
        # Re-build queryset from posted session/term (not GET params)
        post_qs = TermResult.objects.filter(
            session_id=post_session, term_id=post_term
        ) if (post_session and post_term) else TermResult.objects.none()
 
        if not student_ids:
            messages.warning(request, 'No students selected.')
        elif action == 'publish':
            from .release import notify_published, publishable_class_ids
            allowed_classes = publishable_class_ids(request.tenant, Term.objects.filter(pk=post_term).first())
            if allowed_classes is not None:
                held = post_qs.filter(student_id__in=student_ids).exclude(class_assigned_id__in=allowed_classes)
                held_classes = sorted(set(held.values_list('class_assigned__name', flat=True)))
                if held_classes:
                    messages.warning(request, f"Not published — waiting for the principal's approval: "
                                              f"{', '.join(held_classes)}.")
                post_qs = post_qs.filter(class_assigned_id__in=allowed_classes)
            newly = set(post_qs.filter(student_id__in=student_ids, published=False, is_not_offering=False)
                        .values_list('student_id', flat=True))
            updated = post_qs.filter(student_id__in=student_ids).update(published=True)
            done = set(post_qs.filter(student_id__in=student_ids).values_list('student_id', flat=True))
            if done:
                names = ', '.join(Student.objects.filter(id__in=done).values_list('full_name', flat=True))
                messages.success(request, f'Published results for {len(done)} student(s): {names}')
                notify_published(request.tenant, newly, post_term,
                                 request.build_absolute_uri(reverse('users:parent_dashboard')), actor=request.user)
        elif action == 'unpublish':
            updated = post_qs.filter(student_id__in=student_ids).update(published=False)
            names   = ', '.join(
                Student.objects.filter(id__in=student_ids).values_list('full_name', flat=True)
            )
            messages.warning(request, f'Unpublished results for {len(student_ids)} student(s): {names}')
 
        # Preserve filters on redirect
        params = f'?session={post_session}&term={post_term}'
        if selected_class:
            params += f'&class={selected_class}'
        return redirect(f"{request.path}{params}")
 
    # ── Build student list ────────────────────────────────────────────────
    STATUS_MAP = {
        'published':     'Published',
        'not_published': 'Not Published',
        'partial':       'Partially Published',
    }
 
    students = []
    for data in results_qs.values(
        'student_id', 'student__full_name', 'class_assigned__name'
    ).distinct().order_by('class_assigned__name', 'student__full_name'):
        sid        = data['student_id']
        student_qs = results_qs.filter(student_id=sid, is_not_offering=False)
        total      = student_qs.count()
        pub_count  = student_qs.filter(published=True).count()
        incomplete = student_qs.filter(total_score__lte=0).exists()
 
        if pub_count == 0:
            status = 'Not Published'
        elif pub_count == total:
            status = 'Published'
        else:
            status = 'Partially Published'
 
        # Status filter
        if selected_status and status != STATUS_MAP.get(selected_status, ''):
            continue
 
        students.append({
            'id':                    sid,
            'name':                  data['student__full_name'],
            'class':                 data['class_assigned__name'],
            'status':                status,
            'published_count':       pub_count,
            'total_subjects':        total,
            'has_incomplete_scores': incomplete,
        })
 
    # Class dropdown (from current results only)
    classes = results_qs.values_list(
        'class_assigned__id', 'class_assigned__name'
    ).distinct().order_by('class_assigned__name')
 
    # Stats for the info bar
    total_students    = len(students)
    published_count   = sum(1 for s in students if s['status'] == 'Published')
    unpublished_count = sum(1 for s in students if s['status'] == 'Not Published')
    partial_count     = sum(1 for s in students if s['status'] == 'Partially Published')
 
    return render(request, 'results/publish_results.html', {
        'students':            students,
        'classes':             classes,
        'all_sessions':        all_sessions,
        'all_terms':           all_terms,
        'session_terms':       session_terms,
        'selected_session_id': selected_session_id or (str(session.id) if session else ''),
        'selected_term_id':    selected_term_id    or (str(term.id)    if term    else ''),
        'selected_class':      selected_class,
        'selected_status':     selected_status,
        'session':             session,
        'term':                term,
        'total_students':      total_students,
        'published_count':     published_count,
        'unpublished_count':   unpublished_count,
        'partial_count':       partial_count,
    })
 

# ---------------------------------------------------------------------------
# Parent: result list dashboard
# ---------------------------------------------------------------------------

@login_required
def parent_view_results(request):
    """
    Lists all published term results available to view for the parent's
    linked student(s), grouped by session and term.
    """
    if not getattr(request.user, 'is_parent', False):
        return redirect('results:my_results' if getattr(request.user, 'student', None) else 'dashboard:router')
    parent_email = getattr(request.user, 'email', None)
    if not parent_email:
        return render(request, 'parents/dashboard.html', {
            'error': 'Your account does not have a valid email address.'
        })

    students = Student.objects.filter(parent_email=parent_email)
    if not students.exists():
        return render(request, 'parents/dashboard.html', {
            'error': 'No student is linked to your parent account.'
        })

    selected_student_id = request.GET.get('student')
    if selected_student_id:
        student = get_object_or_404(students, id=selected_student_id)
    else:
        student = students.first()

    # Build list of (session, term) pairs that have at least one published result
    published_pairs = (
        TermResult.objects
        .filter(student=student, published=True)
        .select_related('session', 'term')
        .values('session__id', 'session__name', 'term__id', 'term__name')
        .distinct()
        .order_by('-session__name', 'term__name')
    )

    return render(request, 'parents/dashboard.html', {
        'students':        students,
        'student':         student,
        'published_pairs': published_pairs,
    })


# ---------------------------------------------------------------------------
# Parent: result detail  (read-only, published only)
# ---------------------------------------------------------------------------

@login_required
def parent_result_detail(request, student_id, session_id, term_id):
    """A parent's view of one child's published report card."""
    student = get_object_or_404(Student, id=student_id)
    if not (_is_admin_or_super(request.user) or getattr(request.user, 'is_staff_user', False)):
        from users.views_achievements import _parent_children
        if not _parent_children(request).filter(pk=student.pk).exists():
            return HttpResponse('Unauthorized', status=403)
    term    = get_object_or_404(Term,            id=term_id)
    session = get_object_or_404(AcademicSession, id=session_id)
    from .release import ReleaseError, access, redeem
    if request.method == 'POST' and request.POST.get('pin') is not None:
        try:
            redeem(request, request.POST.get('pin'), student, term)
        except ReleaseError as problem:
            messages.error(request, str(problem))
        return redirect(request.path)
    blocked = access(request, student, term)
    if blocked:
        return render(request, 'results/access_gate.html', {'student': student, 'term': term, 'session': session,
                                                             'blocked': blocked, 'school': request.tenant})
    context = _result_context_for_student(student, term, session, published_only=True)
    context.update({'readonly': True, 'result_settings': _get_result_settings(request.tenant)})
    if not context['results']:
        context.update({'results': None, 'message': 'Results have not been published yet.'})
        return render(request, 'parents/result_detail.html', context)
    if request.GET.get('download') == 'pdf':
        return _build_pdf_response(request, 'parents/result_detail.html', context, f'{student.full_name}_result.pdf')
    return render(request, 'parents/result_detail.html', context)


# ---------------------------------------------------------------------------
# Student: result detail  (read-only, published only)
# ---------------------------------------------------------------------------

@login_required
def student_result_detail(request, session_id, term_id):
    """A student's view of their own published report card."""
    student = getattr(request.user, 'student', None)
    if student is None:
        return HttpResponse('Unauthorized', status=403)
    term    = get_object_or_404(Term,            id=term_id)
    session = get_object_or_404(AcademicSession, id=session_id)
    from .release import ReleaseError, access, redeem
    if request.method == 'POST' and request.POST.get('pin') is not None:
        try:
            redeem(request, request.POST.get('pin'), student, term)
        except ReleaseError as problem:
            messages.error(request, str(problem))
        return redirect(request.path)
    blocked = access(request, student, term)
    if blocked:
        return render(request, 'results/access_gate.html', {'student': student, 'term': term, 'session': session,
                                                             'blocked': blocked, 'school': request.tenant})
    context = _result_context_for_student(student, term, session, published_only=True)
    context.update({'readonly': True, 'result_settings': _get_result_settings(request.tenant)})
    if not context['results']:
        context.update({'results': None, 'message': 'Results have not been published yet.'})
        return render(request, 'students/result_detail.html', context)
    if request.GET.get('download') == 'pdf':
        return _build_pdf_response(request, 'students/result_detail.html', context, f'{student.full_name}_result.pdf')
    return render(request, 'students/result_detail.html', context)


# ---------------------------------------------------------------------------
# Skills & Behaviour (standalone view)
# ---------------------------------------------------------------------------

@login_required
def skills_behaviour_view(request, student_id, term_id, session_id):
    student = get_object_or_404(Student,         id=student_id)
    term    = get_object_or_404(Term,            id=term_id)
    session = get_object_or_404(AcademicSession, id=session_id)

    assessment_map = {
        a.skill_id: a.score
        for a in SkillAssessment.objects.filter(student=student, term=term, session=session)
    }

    return render(request, 'results/skills_behaviour.html', {
        'student':        student,
        'term':           term,
        'session':        session,
        'skills':         Skill.objects.filter(category='skill'),
        'behaviours':     Skill.objects.filter(category='behaviour'),
        'assessment_map': assessment_map,
        'ratings':        [5, 4, 3, 2, 1],
    })


# ---------------------------------------------------------------------------
# Teacher: score view (class scoreboard)
# ---------------------------------------------------------------------------

@login_required
def teacher_score_view(request):
    # 🔐 PERMISSION CHECK
    if not _can_enter_scores(request.user):
        messages.error(request, "You do not have permission to view or enter scores.")
        return redirect('dashboard:router')

    active_session = get_active_session()

    selected_class   = request.GET.get('class')
    selected_subject = request.GET.get('subject')
    selected_term    = request.GET.get('term')
    selected_student = request.GET.get('student')
    sort_by          = request.GET.get('sort', 'name')

    classes  = Class.objects.all().order_by('name')
    terms    = Term.objects.all().order_by('-id')
    subjects = Subject.objects.all().order_by('name')
    students = Student.objects.all().order_by('full_name')

    if selected_class:
        subjects = Subject.objects.filter(
            classsubject__school_class_id=selected_class
        ).distinct().order_by('name')
        students = students.filter(class_assigned_id=selected_class)

    term    = get_active_term()
    session = active_session

    if selected_term:
        term    = get_object_or_404(Term, id=selected_term)
        session = term.session

    rows        = []
    class_stats = {}
    grading_list = _get_grading_list()
    subject_obj  = None
    class_obj    = None

    if selected_class and selected_subject and selected_term:
        subject_obj = get_object_or_404(Subject, id=selected_subject)
        class_obj   = get_object_or_404(Class,   id=selected_class)

        qs = TermResult.objects.filter(
            class_assigned_id=selected_class,
            subject_id=selected_subject,
            term=term,
            session=session,
        ).select_related('student', 'subject')

        if selected_student:
            qs = qs.filter(student_id=selected_student)

        order_map = {'score_desc': '-total_score', 'score_asc': 'total_score'}
        qs = qs.order_by(order_map.get(sort_by, 'student__full_name'))

        for r in qs:
            score   = r.total_score or 0
            grading = _resolve_grade(score, grading_list)
            display_exam = (
                r.raw_exam_score
                if (r.raw_exam_score is not None and r.raw_exam_score > 0)
                else r.exam_score or 0
            )
            rows.append({
                'student':   r.student,
                'exam':      display_exam,
                'total':     int(round(score)),
                'grade':     grading.grade       if grading else '-',
                'remark':    grading.description if grading else '-',
                'published': r.published,
            })

        if rows:
            totals = [r['total'] for r in rows]
            passed = [t for t in totals if t >= 40]
            class_stats = {
                'count':     len(rows),
                'highest':   max(totals),
                'lowest':    min(totals),
                'average':   round(sum(totals) / len(totals), 1),
                'pass_rate': round(len(passed) / len(totals) * 100, 1),
            }

    return render(request, 'results/teacher_score_view.html', {
        'classes':          classes,
        'subjects':         subjects,
        'terms':            terms,
        'students':         students,
        'rows':             rows,
        'class_stats':      class_stats,
        'selected_class':   selected_class,
        'selected_subject': selected_subject,
        'selected_term':    selected_term,
        'selected_student': selected_student,
        'sort_by':          sort_by,
        'term':             term,
        'session':          session,
        'subject_obj':      subject_obj,
        'class_obj':        class_obj,
    })


# ---------------------------------------------------------------------------
# AJAX: Save remarks and skills
# ---------------------------------------------------------------------------

@csrf_exempt
@login_required
def save_teacher_remark(request):
    """Auto-save class teacher remark. Uses csrf_exempt so the JS
    X-CSRFToken header approach works regardless of middleware quirks."""
    if request.method != 'POST':
        return JsonResponse({'status': 'method_not_allowed'}, status=405)

    # 🔐 PERMISSION CHECK
    if not _can_enter_scores(request.user):
        return JsonResponse({'status': 'unauthorized', 'detail': 'No permission to enter scores.'}, status=403)

    student_id  = request.POST.get('student_id', '').strip()
    term_id     = request.POST.get('term_id', '').strip()
    session_id  = request.POST.get('session_id', '').strip()
    remark_text = request.POST.get('remark', '').strip()

    if not all([student_id, term_id, session_id]):
        return JsonResponse(
            {'status': 'invalid', 'detail': 'Missing student_id, term_id or session_id'},
            status=400,
        )

    try:
        obj, _ = TeacherRemark.objects.get_or_create(
            student_id=int(student_id),
            term_id=int(term_id),
            session_id=int(session_id),
            defaults={'remark': remark_text},
        )
        if obj.remark != remark_text:
            obj.remark = remark_text
            obj.save(update_fields=['remark'])
        return JsonResponse({'status': 'success'})
    except Exception as e:
        return JsonResponse({'status': 'error', 'detail': str(e)}, status=500)


@require_POST
@login_required
def save_hos_remark(request):
    """Auto-save Head of School remark (staff / superuser only)."""
    if not (getattr(request.user, 'is_staff_user', False) or request.user.is_superuser):
        return JsonResponse({'status': 'unauthorized'}, status=403)

    student_id  = request.POST.get('student_id')
    term_id     = request.POST.get('term_id')
    session_id  = request.POST.get('session_id')
    remark_text = request.POST.get('remark', '').strip()

    if not all([student_id, term_id, session_id]):
        return JsonResponse({'status': 'invalid'}, status=400)

    with transaction.atomic():
        obj, created = HosRemark.objects.get_or_create(
            student_id=student_id,
            term_id=term_id,
            session_id=session_id,
            defaults={'remark': remark_text},
        )
        if not created and obj.remark != remark_text:
            obj.remark = remark_text
            obj.save(update_fields=['remark'])

    return JsonResponse({'status': 'success', 'created': created})


@require_POST
@login_required
def save_subject_remark(request):
    """Auto-save subject teacher remark."""
    # 🔐 PERMISSION CHECK
    if not _can_enter_scores(request.user):
        return JsonResponse({'status': 'unauthorized', 'detail': 'No permission to enter scores.'}, status=403)

    student_id  = request.POST.get('student_id')
    subject_id  = request.POST.get('subject_id')
    term_id     = request.POST.get('term_id')
    session_id  = request.POST.get('session_id')
    remark_text = request.POST.get('remark', '').strip()

    if not all([student_id, subject_id, term_id, session_id]):
        return JsonResponse({'status': 'invalid'}, status=400)

    obj, _ = SubjectTeacherRemark.objects.get_or_create(
        student_id=student_id,
        subject_id=subject_id,
        term_id=term_id,
        session_id=session_id,
        defaults={'remark': ''},
    )
    obj.remark = remark_text
    obj.save(update_fields=['remark', 'updated_at'])
    return JsonResponse({'status': 'saved'})


@require_POST
@login_required
def save_skill_assessment(request):
    """Auto-save a skill/behaviour rating."""
    # 🔐 PERMISSION CHECK
    if not _can_enter_scores(request.user):
        return JsonResponse({'status': 'unauthorized', 'detail': 'No permission to enter scores.'}, status=403)

    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'status': 'invalid'}, status=400)

    required = ('student', 'skill', 'term', 'session', 'score')
    if not all(k in data for k in required):
        return JsonResponse({'status': 'missing fields'}, status=400)

    SkillAssessment.objects.update_or_create(
        student_id=data['student'],
        skill_id=data['skill'],
        term_id=data['term'],
        session_id=data['session'],
        defaults={'score': data['score']},
    )
    return JsonResponse({'status': 'saved'})


# ---------------------------------------------------------------------------
# Manual Term Attendance Entry
# ---------------------------------------------------------------------------

@login_required
def manual_term_attendance(request):
    """
    Page to manually enter days-present for every student in a class for a term.
    Supports both GET (display) and POST (save).
    Auto-detects active term/session; also allows manual filter.
    """
    # 🔐 PERMISSION CHECK
    if not _can_mark_attendance(request.user):
        messages.error(request, "You do not have permission to manage attendance.")
        return redirect('dashboard:router')

    from django.contrib import messages as django_messages

    terms    = Term.objects.all().order_by('-start_date')
    sessions = AcademicSession.objects.all().order_by('-start_date')
    classes  = Class.objects.all().order_by('name')

    active_term    = get_active_term()
    active_session = get_active_session()

    selected_term_id    = request.GET.get('term')    or request.POST.get('term')    or (str(active_term.id)    if active_term    else '')
    selected_session_id = request.GET.get('session') or request.POST.get('session') or (str(active_session.id) if active_session else '')
    selected_class_id   = request.GET.get('class')   or request.POST.get('class')   or ''

    selected_term    = Term.objects.filter(id=selected_term_id).first()    if selected_term_id    else None
    selected_session = AcademicSession.objects.filter(id=selected_session_id).first() if selected_session_id else None
    selected_class   = Class.objects.filter(id=selected_class_id).first()  if selected_class_id   else None

    students = []
    attendance_map = {}

    if selected_term and selected_session and selected_class:
        students = Student.objects.filter(
            class_assigned=selected_class
        ).select_related('user').order_by('user__last_name', 'user__first_name')

        # Build a dict: student_id -> days_present
        attendance_map = {
            a.student_id: a.days_present
            for a in StudentTermAttendance.objects.filter(
                term=selected_term,
                session=selected_session,
                student__in=students,
            )
        }

    if request.method == 'POST' and selected_term and selected_session and selected_class:
        saved = 0
        for student in students:
            key = f'days_{student.id}'
            raw = request.POST.get(key, '').strip()
            if raw == '':
                continue
            try:
                days = int(raw)
                if days < 0:
                    days = 0
            except ValueError:
                continue
            StudentTermAttendance.objects.update_or_create(
                student=student,
                term=selected_term,
                session=selected_session,
                defaults={'days_present': days},
            )
            saved += 1
        django_messages.success(request, f'Attendance saved for {saved} student(s).')
        # Redirect to same filter so user sees updated data
        base = request.path
        return redirect(
            f"{base}?term={selected_term_id}&session={selected_session_id}&class={selected_class_id}"
        )

    # Attach days_present to each student for easy template use
    for student in students:
        student.days_present = attendance_map.get(student.id, '')

    return render(request, 'results/manual_term_attendance.html', {
        'terms':              terms,
        'sessions':           sessions,
        'classes':            classes,
        'selected_term':      selected_term,
        'selected_session':   selected_session,
        'selected_class':     selected_class,
        'selected_term_id':   selected_term_id,
        'selected_session_id': selected_session_id,
        'selected_class_id':  selected_class_id,
        'students':           students,
        'school_days_open':   selected_term.number_of_school_days if selected_term else 0,
    })


# ---------------------------------------------------------------------------
# Batch Results — all students in a class, one result card per student
# ---------------------------------------------------------------------------

@login_required
def batch_results(request):
    """
    Filter by class + term + session and display every student's full
    result card (identical layout to result_detail.html) on one page.
    Teachers can scroll through or print all at once.
    """
    if not _is_staff_member(request.user):
        return my_results(request)
    classes  = Class.objects.all().order_by('name')
    terms    = Term.objects.all()
    sessions = AcademicSession.objects.all().order_by('-name')

    selected_class   = request.GET.get('class',   '').strip()
    selected_term    = request.GET.get('term',    '').strip()
    selected_session = request.GET.get('session', '').strip()

    session_obj = None
    term_obj    = None
    class_obj   = None
    student_cards = []   # list of context dicts, one per student

    if selected_session:
        session_obj = AcademicSession.objects.filter(id=selected_session).first()
    if not session_obj:
        session_obj = get_active_session()

    if selected_term:
        term_obj = Term.objects.filter(id=selected_term).first()
    if not term_obj:
        term_obj = get_active_term()

    if selected_class and session_obj and term_obj:
        class_obj = Class.objects.filter(id=selected_class).first()
        if class_obj:
            students = Student.objects.filter(
                class_assigned=class_obj,
                status__in=ENROLLED_STATUSES,
            ).order_by('full_name')

            for student in students:
                ctx = _result_context_for_student(student, term_obj, session_obj)
                # skip students with no results at all
                if not ctx['results']:
                    continue
                student_cards.append(ctx)

    context = {
        'classes':         classes,
        'terms':           terms,
        'sessions':        sessions,
        'selected_class':  selected_class,
        'selected_term':   selected_term,
        'selected_session': selected_session,
        'session_obj':     session_obj,
        'term_obj':        term_obj,
        'class_obj':       class_obj,
        'student_cards':   student_cards,
        'total_students':  len(student_cards),
        'result_settings': _get_result_settings(request.tenant),
    }
    return render(request, 'results/batch_results.html', context)

import logging
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# AI Comment: Generate for a single student (AJAX POST)
# ---------------------------------------------------------------------------

@require_POST
@login_required
def generate_ai_comment(request):
    if not _can_enter_scores(request.user):
        return JsonResponse(
            {'status': 'error', 'detail': 'You do not have permission to generate AI comments.'},
            status=403,
        )

    student_id = request.POST.get('student_id', '').strip()
    term_id    = request.POST.get('term_id',    '').strip()
    session_id = request.POST.get('session_id', '').strip()

    if not all([student_id, term_id, session_id]):
        return JsonResponse(
            {'status': 'error', 'detail': 'Missing student_id, term_id, or session_id.'},
            status=400,
        )

    try:
        student = get_object_or_404(Student, id=int(student_id))
        term    = get_object_or_404(Term,    id=int(term_id))
        session = get_object_or_404(AcademicSession, id=int(session_id))

        assigned_subject_ids = ClassSubject.objects.filter(
            school_class=student.class_assigned
        ).values_list('subject_id', flat=True)

        results = list(
            TermResult.objects.filter(
                student=student,
                term=term,
                session=session,
                subject_id__in=assigned_subject_ids,
            ).select_related('subject')
        )
        results = [r for r in results if not getattr(r, 'is_not_offering', False)]

        if not results:
            return JsonResponse(
                {'status': 'error',
                 'detail': 'No results found for this student in the selected term/session. '
                           'Please enter scores before generating comments.'},
                status=400,
            )

        total_obtained   = sum(r.total_score or 0 for r in results)
        total_obtainable = len(results) * 100
        overall_pct = (
            round((total_obtained / total_obtainable) * 100, 1)
            if total_obtainable else 0.0
        )

        comment_obj = generate_comments_for_student(
            student, term, session, results, overall_pct, tone=request.POST.get('tone') or 'warm'
        )

        if not comment_obj.generation_ok:
            return JsonResponse(
                {'status': 'error', 'detail': comment_obj.error_message},
                status=500,
            )

        return JsonResponse({
            'status':             'success',
            'teacher_comment':    comment_obj.teacher_comment,
            'hos_comment':        comment_obj.hos_comment,
            'overall_summary':    comment_obj.overall_summary,
            'overall_percentage': comment_obj.overall_percentage,
            'generated_at':       comment_obj.generated_at.strftime('%d %b %Y, %I:%M %p')
                                  if comment_obj.generated_at else '',
        })

    except Exception as e:
        logger.error("generate_ai_comment view error: %s", e, exc_info=True)
        return JsonResponse({'status': 'error', 'detail': str(e)}, status=500)


# ---------------------------------------------------------------------------
# AI Comment: Generate for a whole class (AJAX POST)
# ---------------------------------------------------------------------------

@require_POST
@login_required
def generate_ai_comments_bulk(request):
    if not _is_admin_or_super(request.user):
        return JsonResponse(
            {'status': 'error', 'detail': 'Only admins can run bulk AI comment generation.'},
            status=403,
        )

    class_id   = request.POST.get('class_id',   '').strip()
    term_id    = request.POST.get('term_id',     '').strip()
    session_id = request.POST.get('session_id',  '').strip()

    if not all([class_id, term_id, session_id]):
        return JsonResponse(
            {'status': 'error', 'detail': 'Missing class_id, term_id, or session_id.'},
            status=400,
        )

    try:
        class_obj = get_object_or_404(Class, id=int(class_id))
        term      = get_object_or_404(Term,  id=int(term_id))
        session   = get_object_or_404(AcademicSession, id=int(session_id))

        summary = generate_comments_for_class(class_obj, term, session, tone=request.POST.get('tone') or 'warm')

        return JsonResponse({
            'status':  'done',
            'total':   summary['total'],
            'success': summary['success'],
            'failed':  summary['failed'],
            'errors':  summary['errors'],
        })

    except Exception as e:
        logger.error("generate_ai_comments_bulk view error: %s", e, exc_info=True)
        return JsonResponse({'status': 'error', 'detail': str(e)}, status=500)


# ---------------------------------------------------------------------------
# AI Comment: Get existing comments for a student (GET, for page load prefill)
# ---------------------------------------------------------------------------

@login_required
def get_ai_comment(request):
    if not _is_staff_member(request.user):
        return JsonResponse({'status': 'unauthorized'}, status=403)
    student_id = request.GET.get('student_id', '').strip()
    term_id    = request.GET.get('term_id',    '').strip()
    session_id = request.GET.get('session_id', '').strip()

    if not all([student_id, term_id, session_id]):
        return JsonResponse({'status': 'none'})

    try:
        obj = AIStudentComment.objects.filter(
            student_id=int(student_id),
            term_id=int(term_id),
            session_id=int(session_id),
            generation_ok=True,
        ).first()

        if not obj:
            return JsonResponse({'status': 'none'})

        return JsonResponse({
            'status':             'found',
            'teacher_comment':    obj.teacher_comment,
            'hos_comment':        obj.hos_comment,
            'overall_summary':    obj.overall_summary,
            'overall_percentage': obj.overall_percentage,
            'generated_at':       obj.generated_at.strftime('%d %b %Y, %I:%M %p')
                                  if obj.generated_at else '',
        })

    except Exception as e:
        logger.error("get_ai_comment view error: %s", e)
        return JsonResponse({'status': 'error', 'detail': str(e)}, status=500)