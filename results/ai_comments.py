"""
results/ai_comments.py
─────────────────────────────────
AI comments for student term results, written through the school's chosen
AI provider (OpenAI, Gemini or Claude — see assistant/providers.py).

Generates three distinct comments per student per term:
  • teacher_comment   — class teacher's end-of-term remark
  • hos_comment       — Head of School's formal remark
  • overall_summary   — brief holistic summary of the student's performance

Variety (F165): a tone can be chosen (warm, formal, encouraging, concise),
and the openings already used for classmates this term are passed to the AI
so a class's report cards don't all start the same way. If a reply still
repeats an opening, it is asked once more.

Settings (.env): GROQ_API_KEY (or OPENAI_API_KEY / GEMINI_API_KEY / ANTHROPIC_API_KEY).
"""

from users.models import ENROLLED_STATUSES
import logging
import re

from django.utils import timezone

logger = logging.getLogger(__name__)

MAX_TOKENS  = 700
TEMPERATURE = 0.9
TONES = {
    'warm': 'warm, personal and encouraging',
    'formal': 'formal and measured, as on an official report',
    'encouraging': 'upbeat and motivating, focused on next steps',
    'concise': 'short and direct, no flourishes',
}
OPENING_WORDS = 5


def opening(text):
    """The first few words of a comment, lower-cased, for spotting repeats."""
    words = re.findall(r"[a-z']+", (text or '').lower())
    return ' '.join(words[:OPENING_WORDS])


def _performance_label(percentage: float) -> str:
    if percentage >= 80: return "Excellent"
    if percentage >= 70: return "Very Good"
    if percentage >= 60: return "Good"
    if percentage >= 50: return "Average"
    if percentage >= 40: return "Below Average"
    return "Poor"


def _grade_distribution(results) -> dict:
    dist = {}
    for r in results:
        grade = getattr(r, 'grade', '') or 'N/A'
        dist[grade] = dist.get(grade, 0) + 1
    return dist


def _strongest_weakest(results):
    valid = [r for r in results if not getattr(r, 'is_not_offering', False)]
    if not valid:
        return None, None
    strongest = max(valid, key=lambda r: r.total_score or 0)
    weakest   = min(valid, key=lambda r: r.total_score or 0)
    return (
        str(strongest.subject),
        str(weakest.subject) if weakest.subject != strongest.subject else None,
    )


def _build_prompt(student, term, session, results, overall_percentage: float, tone='warm', avoid=()) -> str:
    perf_label         = _performance_label(overall_percentage)
    grade_dist         = _grade_distribution(results)
    strongest, weakest = _strongest_weakest(results)
    gender             = getattr(student, 'gender', '') or 'the student'
    first_name         = (student.full_name or '').split()[0] if student.full_name else 'the student'

    subject_lines = []
    for r in results:
        if getattr(r, 'is_not_offering', False):
            continue
        subject_lines.append(
            f"  - {r.subject}: {int(round(r.total_score or 0))}/100 (Grade: {r.grade or 'N/A'})"
        )
    subject_table = "\n".join(subject_lines) if subject_lines else "  No results recorded."
    grade_summary = ", ".join(f"{c}x {g}" for g, c in sorted(grade_dist.items())) or "N/A"
    strongest_line = f"Strongest subject: {strongest}" if strongest else ""
    weakest_line   = f"Needs most attention: {weakest}" if weakest else ""

    pronoun_sub = "He"  if gender == "Male" else ("She"  if gender == "Female" else "They")
    pronoun_obj = "him" if gender == "Male" else ("her"  if gender == "Female" else "them")
    pronoun_pos = "his" if gender == "Male" else ("her"  if gender == "Female" else "their")

    avoid_block = ''
    if avoid:
        listed = '\n'.join(f'  - "{a}..."' for a in list(avoid)[:30])
        avoid_block = ('Classmates\' report cards already open like this — start teacher_comment and hos_comment '
                       f'differently and avoid these stock phrases:\n{listed}')
    return f"""You are an experienced Nigerian school report writer generating professional, \
personalised end-of-term comments for a student result card.

STUDENT
Name: {student.full_name} | First name: {first_name} | Gender: {gender}
Term: {term} | Session: {session} | Class: {student.class_assigned}

PERFORMANCE
Overall: {overall_percentage:.1f}% ({perf_label}) | Subjects: {len(results)}
Grades: {grade_summary}
{strongest_line}
{weakest_line}

Subjects:
{subject_table}

Pronouns: subject={pronoun_sub}, object={pronoun_obj}, possessive={pronoun_pos}

TASK: Return ONLY a valid JSON object with no preamble, no markdown fences, no extra text.

{{
  "teacher_comment": "...",
  "hos_comment": "...",
  "overall_summary": "..."
}}

RULES:
teacher_comment (2-3 sentences, max 60 words):
- Class teacher writing to parent/guardian
- Acknowledge performance, mention {pronoun_pos} strongest area
- End with encouragement or specific advice
- Use the student first name at least once, warm and personal tone

hos_comment (1-2 sentences, max 40 words):
- Head of School / Principal voice
- Formal, brief, authoritative, motivational
- Reference overall performance level
- Must NOT repeat teacher_comment wording

overall_summary (1 sentence, max 20 words):
- Concise holistic assessment for report card summary box
- Factual and positive

IMPORTANT: Sound human-written, vary sentence structure, Nigerian context (this term, promotion, new session).
TONE: {TONES.get(tone, TONES['warm'])}.
{avoid_block}"""


def _ask(student, prompt):
    from assistant.providers import complete
    result = complete(student.tenant, 'report_comments', 'You write school report card comments.', prompt,
                      json_mode=True, max_tokens=MAX_TOKENS, temperature=TEMPERATURE)
    data = result.data if isinstance(result.data, dict) else {}
    return data, f'{result.provider}:{result.model}'[:60]


def generate_comments_for_student(student, term, session, results,
                                   overall_percentage: float = 0.0, tone: str = 'warm', avoid=None):
    """
    Generate and persist AI comments for one student.
    `avoid` is a set of openings already used for classmates (it is updated).
    Returns AIStudentComment instance. Never raises.
    """
    from results.models import AIStudentComment

    if avoid is None:
        avoid = class_openings(student, term, session)
    teacher_comment = hos_comment = overall_summary = error_message = ''
    model_used = ''
    generation_ok = False

    try:
        prompt = _build_prompt(student, term, session, results, overall_percentage, tone, sorted(avoid))
        data, model_used = _ask(student, prompt)
        if opening(data.get('teacher_comment')) in avoid:
            # Same opening as a classmate's — ask once more, naming it.
            retry = prompt + f'\nYour last teacher_comment began "{opening(data.get("teacher_comment"))}" — ' \
                             'that is taken. Start differently.'
            data, model_used = _ask(student, retry)
        teacher_comment = str(data.get('teacher_comment', '')).strip()
        hos_comment     = str(data.get('hos_comment',     '')).strip()
        overall_summary = str(data.get('overall_summary', '')).strip()
        if not teacher_comment:
            raise ValueError("The AI returned an empty teacher_comment.")
        avoid.update({opening(teacher_comment), opening(hos_comment)} - {''})
        generation_ok = True
        logger.info("AI comments generated for student=%s term=%s session=%s (%.1f%%)",
                    student.full_name, term, session, overall_percentage)
    except Exception as e:  # AIError, bad JSON, network — recorded on the comment, never raised
        error_message = str(e)
        logger.error("AI comment failed for student %s: %s", student.id, e)

    obj, _ = AIStudentComment.objects.update_or_create(
        student=student,
        term=term,
        session=session,
        defaults={
            'teacher_comment':    teacher_comment,
            'hos_comment':        hos_comment,
            'overall_summary':    overall_summary,
            'overall_percentage': overall_percentage,
            'total_subjects':     len([r for r in results
                                       if not getattr(r, 'is_not_offering', False)]),
            'generated_at':       timezone.now(),
            'model_used':         model_used or 'not generated',
            'generation_ok':      generation_ok,
            'error_message':      error_message,
        },
    )
    return obj


def class_openings(student, term, session):
    """Openings of comments already written for this student's classmates this term."""
    from results.models import AIStudentComment
    out = set()
    if not student.class_assigned_id:
        return out
    for c in AIStudentComment.objects.filter(student__class_assigned_id=student.class_assigned_id, term=term,
                                             session=session, generation_ok=True).exclude(student=student):
        out.update({opening(c.teacher_comment), opening(c.hos_comment)} - {''})
    return out


def generate_comments_for_class(class_obj, term, session, tone: str = 'warm') -> dict:
    """Generate AI comments for every active student in a class."""
    from users.models import Student
    from results.models import TermResult

    students = Student.objects.filter(
        class_assigned=class_obj, status__in=ENROLLED_STATUSES,
    ).order_by('full_name')

    summary = {'total': 0, 'success': 0, 'failed': 0, 'errors': []}
    avoid = None

    for student in students:
        summary['total'] += 1
        results = list(
            TermResult.objects.filter(
                student=student, term=term, session=session,
            ).select_related('subject')
        )
        results = [r for r in results if not getattr(r, 'is_not_offering', False)]

        total_obtained   = sum(r.total_score or 0 for r in results)
        total_obtainable = len(results) * 100
        overall_pct = round((total_obtained / total_obtainable) * 100, 1) if total_obtainable else 0.0

        if avoid is None:
            avoid = class_openings(student, term, session)
        obj = generate_comments_for_student(student, term, session, results, overall_pct, tone, avoid)

        if obj.generation_ok:
            summary['success'] += 1
        else:
            summary['failed'] += 1
            summary['errors'].append(f"{student.full_name}: {obj.error_message}")

    logger.info(
        "Bulk AI generation complete — class=%s term=%s: %d/%d succeeded",
        class_obj, term, summary['success'], summary['total']
    )
    return summary