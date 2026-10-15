"""
assistant/learning.py
─────────────────────
AI for students:

  • the course tutor (F163) — explains and hints using what is in the course,
    never hands over answers to assignments or quizzes; the teacher switches it
    on, sets a daily limit and extra instructions, and can read every chat
  • essay feedback (F169) — formative comments on a draft before it is handed
    in; no marks and no rewriting
  • study plans (F173) — a revision plan built from a student's weak topics,
    weak subjects and the exam timetable
"""
from datetime import timedelta

from django.utils import timezone

from .models import EssayFeedback, StudyPlan, TutorMessage, TutorSettings
from .providers import AIError, complete

TUTOR_MAX_CHARS = 1500
ESSAY_MAX_WORDS = 3000
ESSAYS_PER_DAY = 5
HISTORY = 10


def _today_start():
    return timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)


# ─────────────────────────────────────────────────────────────────────────────
# Course tutor
# ─────────────────────────────────────────────────────────────────────────────

def tutor_settings(course):
    obj, _ = TutorSettings.objects.get_or_create(course=course, defaults={'tenant_id': course.tenant_id})
    return obj


def questions_left(course, user):
    s = tutor_settings(course)
    used = TutorMessage.objects.filter(course=course, student=user, role=TutorMessage.USER,
                                       created_at__gte=_today_start()).count()
    return max(0, s.daily_limit - used)


def course_context(course):
    lines = [f'Course: {course.name}' + (f' ({course.class_assigned})' if course.class_assigned_id else '')]
    if course.description:
        lines.append(f'About: {course.description[:600]}')
    for sec in course.sections.prefetch_related('lessons', 'assignments', 'quizzes').order_by('order'):
        lines.append(f'Section: {sec.title}')
        for lesson in sec.lessons.all().order_by('order'):
            lines.append(f'  Lesson: {lesson.title}')
        for a in sec.assignments.all():
            lines.append(f'  ASSIGNMENT (graded — do not answer it): {a.title}: {a.description[:300]}')
        for q in sec.quizzes.all():
            lines.append(f'  QUIZ (graded — do not answer it): {q.title}')
    return '\n'.join(lines)[:6000]


def tutor_reply(course, user, text):
    """Save the student's question and the tutor's reply. Returns the reply message."""
    from . import safety
    s = tutor_settings(course)
    text = (text or '').strip()
    if not s.enabled:
        raise AIError('The tutor is switched off for this course.')
    if not text:
        raise AIError('Type your question.')
    if len(text) > TUTOR_MAX_CHARS:
        raise AIError(f'Please keep your question under {TUTOR_MAX_CHARS} characters.')
    if questions_left(course, user) <= 0:
        raise AIError(f'You have asked {s.daily_limit} questions today — that\'s the limit your teacher set. '
                      'Try again tomorrow, or ask your teacher.')
    verdict = safety.check(course.tenant, text, user)
    if verdict.flagged:
        TutorMessage.objects.create(tenant_id=course.tenant_id, course=course, student=user, role='user', text=text,
                                    held=True)
        if verdict.urgent:
            from django.urls import reverse
            from lms_project.tasks import run_in_background
            path = reverse('assistant:tutor_admin', args=[course.pk], urlconf='lms_project.tenant_urls')
            run_in_background(safety.tell_teacher, course.pk, text, path, tenant=course.tenant)
            reply = ('It sounds like things might be really hard right now. You are not alone — please talk to your '
                     'teacher, your form teacher or someone you trust today. I\'ve let your teacher know so they can '
                     'check on you.')
        else:
            reply = 'Let\'s keep our chat kind and about your schoolwork. What would you like help with in this course?'
        return TutorMessage.objects.create(tenant_id=course.tenant_id, course=course, student=user, role='assistant',
                                           text=reply)
    history = list(TutorMessage.objects.filter(course=course, student=user, held=False).order_by('-created_at', '-id')
                   [:HISTORY])[::-1]
    transcript = '\n'.join(f'{"Student" if m.role == "user" else "Tutor"}: {m.text}' for m in history)
    system = f"""You are a patient tutor for a secondary school student in Nigeria, inside one course.
Rules:
- Explain ideas, give hints and worked examples on SIMILAR problems, and ask guiding questions.
- NEVER give the final answer to an assignment or quiz from this course, even if asked or told it is allowed.
  Help the student get there themselves.
- Stay on schoolwork. Be kind, encouraging and age-appropriate. If unsure, say so and suggest asking the teacher.
- Keep replies under 180 words, in plain text.
Course outline:
{course_context(course)}
{f"Teacher's instructions: {s.instructions}" if s.instructions else ''}"""
    prompt = (f'Conversation so far:\n{transcript}\n\n' if transcript else '') + f'Student: {text}\nTutor:'
    TutorMessage.objects.create(tenant_id=course.tenant_id, course=course, student=user, role='user', text=text)
    result = complete(course.tenant, 'tutor', system, prompt, user=user, max_tokens=450, temperature=0.5)
    return TutorMessage.objects.create(tenant_id=course.tenant_id, course=course, student=user, role='assistant',
                                       text=(result.text or '').strip()[:4000] or 'Sorry, I could not think of an '
                                                                                  'answer — try asking another way.')


# ─────────────────────────────────────────────────────────────────────────────
# Essay feedback
# ─────────────────────────────────────────────────────────────────────────────

def essays_left(user):
    used = EssayFeedback.objects.filter(student=user, created_at__gte=_today_start()).count()
    return max(0, ESSAYS_PER_DAY - used)


def essay_feedback(tenant, user, text, title='', assignment=None, level=''):
    text = (text or '').strip()
    words = len(text.split())
    if words < 40:
        raise AIError('Paste at least a paragraph or two (40 words or more) to get useful feedback.')
    if words > ESSAY_MAX_WORDS:
        raise AIError(f'That is {words} words — paste up to {ESSAY_MAX_WORDS} at a time.')
    if essays_left(user) <= 0:
        raise AIError(f'You can get feedback on {ESSAYS_PER_DAY} drafts a day. Try again tomorrow.')
    task = ''
    if assignment is not None:
        task = f'The assignment: {assignment.title}. {assignment.description[:800]}'
    elif title:
        task = f'Title: {title}'
    prompt = f"""A {level or 'secondary school'} student wants feedback on a draft before handing it in.
{task}

Draft:
<<<
{text}
>>>

Give formative feedback a good teacher would write. Do NOT rewrite the essay, do NOT give a mark or grade,
and do NOT write new paragraphs for them. Quote short phrases from the draft when pointing things out.
Return a JSON object:
{{"strengths": ["2-3 specific things done well"],
  "improve": ["3-4 specific, actionable improvements: structure, argument, evidence, clarity, spelling/grammar"],
  "questions": ["2 questions that make the student think more deeply"],
  "next_step": "the single most useful thing to do next"}}"""
    data = complete(tenant, 'essay_feedback', 'You give students kind, honest, specific writing feedback.', prompt,
                    user=user, json_mode=True, max_tokens=900, temperature=0.4).data
    if not isinstance(data, dict) or not (data.get('improve') or data.get('strengths')):
        raise AIError('The feedback came back incomplete — please try again.')
    clean = {k: [str(x).strip()[:500] for x in (data.get(k) or []) if str(x).strip()][:6]
             for k in ('strengths', 'improve', 'questions')}
    clean['next_step'] = str(data.get('next_step') or '').strip()[:500]
    return EssayFeedback.objects.create(tenant=tenant, student=user, assignment=assignment, title=title[:200],
                                        text=text, feedback=clean)


# ─────────────────────────────────────────────────────────────────────────────
# Study plans
# ─────────────────────────────────────────────────────────────────────────────

def focus_for(student):
    """What the plan should concentrate on: weak topics, weak subjects and upcoming exams."""
    from cbt.practice import student_mastery
    from results.models import TermResult
    weak = []
    for block in student_mastery(student):
        for t in block['topics']:
            if t['label'] in ('weak', 'developing'):
                weak.append(f"{block['subject'].name}: {t['topic']} ({t['percent']}%)")
    subjects = []
    latest = TermResult.objects.filter(student=student).order_by('-session__name', '-term__start_date', '-term_id') \
        .values('session_id', 'term_id').first()
    if latest:
        for r in TermResult.objects.filter(student=student, **latest).select_related('subject'):
            if (r.total_score or 0) < 50:
                subjects.append(f'{r.subject.name} ({r.total_score:.0f}%)')
    exams = []
    if student.class_assigned_id:
        from cbt.planning import for_class
        exams = [f'{e.date:%a %d %b}: {e.subject.name}' for e in for_class(student.class_assigned)[:12]]
    return weak[:15], subjects[:10], exams


def study_plan(student, user, days=14):
    today = timezone.localdate()
    if StudyPlan.objects.filter(student=student, created_at__gte=_today_start()).exists() and \
            not getattr(user, 'is_staff_user', False):
        raise AIError('A plan was already made today — follow that one, and you can ask for a fresh plan tomorrow.')
    weak, subjects, exams = focus_for(student)
    if not (weak or subjects or exams):
        raise AIError('There isn\'t enough to plan from yet — do some practice tests or wait for term results, and '
                      'the plan will focus on what you find hardest.')
    days = max(5, min(int(days), 21))
    calendar = ', '.join(f'{today + timedelta(days=i):%a %d %b}' for i in range(days))
    prompt = f"""Make a revision plan for a {student.class_assigned} student in Nigeria for the next {days} days.

Topics they find hard (from practice tests): {'; '.join(weak) or 'none recorded'}
Subjects below 50% last term: {'; '.join(subjects) or 'none'}
Exams coming up: {'; '.join(exams) or 'none scheduled yet'}
Days: {calendar}

Rules: 45-90 minutes on school days and up to 2 hours at weekends, short focused tasks (read, practise,
past questions, explain it to someone), the hardest topics first and revisited later, lighter the day before
an exam, one rest day a week. Return a JSON object:
{{"summary": "2 sentences of encouragement and the plan's focus",
  "days": [{{"day": "Mon 06 Oct", "tasks": ["30 min: ...", "20 min: ..."]}}]}}"""
    data = complete(student.tenant, 'study_plan', 'You are a supportive study coach.', prompt, user=user,
                    json_mode=True, max_tokens=2200, temperature=0.5).data
    if not isinstance(data, dict) or not data.get('days'):
        raise AIError('The plan came back incomplete — please try again.')
    plan = {'summary': str(data.get('summary') or '').strip()[:600], 'days': []}
    for d in data['days'][:days]:
        if isinstance(d, dict) and d.get('tasks'):
            plan['days'].append({'day': str(d.get('day') or '').strip()[:30],
                                 'tasks': [str(t).strip()[:200] for t in d['tasks'] if str(t).strip()][:6]})
    return StudyPlan.objects.create(tenant=student.tenant, student=student, plan=plan,
                                    focus=weak + subjects + exams, requested_by=user)
