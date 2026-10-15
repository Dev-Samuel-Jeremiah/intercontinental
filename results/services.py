from cbt.models import ExamAttempt
from .models import TermResult, SessionResult

def update_term_result(attempt: ExamAttempt):
    """
    Pass a submitted CBT score to the student's result sheet.

    The score is always kept as the CBT reference (raw_exam_score for exams).
    It only fills the result column when that is safe: the column is in use
    and the score fits its maximum, score entry for the term is open, the
    subject sheet isn't with a reviewer or approved, and the student takes the
    subject. Otherwise staff import it later from the CBT scores page, with
    the same checks.
    """
    from .assessment import AssessmentScheme
    from .locks import lock_for
    from .release import may_edit_scores

    exam = attempt.exam_part.exam
    student = attempt.student
    if not (exam.session_id and exam.term_id and student.class_assigned_id):
        return None
    column = {'CA1': 'ca1', 'CA2': 'ca2', 'EXAM': 'exam'}.get(exam.exam_type)
    if not column:
        return None
    term_result, _ = TermResult.objects.get_or_create(
        tenant_id=exam.tenant_id, student=student, subject=exam.subject, session=exam.session, term=exam.term,
        defaults={'class_assigned': student.class_assigned})
    if column == 'exam':
        term_result.raw_exam_score = attempt.score
    scheme = AssessmentScheme.for_tenant(exam.tenant)
    top = getattr(scheme, f'{column}_max')
    lock = lock_for(exam.term)
    from django.contrib.auth.models import AnonymousUser
    # Judged as the student: never a reviewer, so a sheet under review or approved is left alone.
    allowed, _why = may_edit_scores(student.user if student.user_id else AnonymousUser(),
                                    student.class_assigned, exam.subject, exam.term)
    if (top and attempt.score <= top and not term_result.is_not_offering
            and not (lock and lock.is_locked) and allowed):
        setattr(term_result, f'{column}_score', attempt.score)
    term_result.save()          # recalculates total, grade and remark
    return term_result


def generate_session_results(session):
    """
    Aggregate TermResults into SessionResults for a session.
    Reads all three terms and writes first_term / second_term / third_term,
    then saves each SessionResult so the cumulative page is always current.
    """
    results = TermResult.objects.filter(session=session).select_related('term', 'student', 'subject')

    for r in results:
        sr, _ = SessionResult.objects.get_or_create(
            student=r.student,
            subject=r.subject,
            session=session,
            class_assigned=r.class_assigned,
        )

        term_name = r.term.name.lower()
        if '1st' in term_name:
            sr.first_term = r.total_score
        elif '2nd' in term_name:
            sr.second_term = r.total_score
        elif '3rd' in term_name:
            sr.third_term = r.total_score

        # calculate() updates total_score & average_score but doesn't save — fix both
        sr.calculate()
        sr.save()