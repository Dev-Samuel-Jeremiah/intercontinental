"""
assistant/writers.py
────────────────────
The prompts behind each AI feature, and checks on what comes back. Every
function returns plain data for a person to read and edit — nothing here
saves, publishes or sends.
"""
from .providers import AIError, complete

LANGUAGES = {'en': 'English', 'ha': 'Hausa', 'yo': 'Yoruba', 'ig': 'Igbo', 'pcm': 'Nigerian Pidgin',
             'fr': 'French'}
TONES = {'warm': 'warm and friendly', 'formal': 'formal and respectful', 'firm': 'firm but kind',
         'encouraging': 'encouraging and upbeat', 'concise': 'brief and to the point'}

SCHOOL_VOICE = ('You write for a Nigerian school. Use clear British English spelling unless asked for another '
                'language, Nigerian school terms (term, session, JSS/SS, form teacher), and never invent facts '
                'that were not given to you.')


def _clip(value, n):
    return str(value or '').strip()[:n]


# ─────────────────────────────────────────────────────────────────────────────
# F161 — lesson notes
# ─────────────────────────────────────────────────────────────────────────────

def lesson_note(tenant, user, subject, school_class, week, topic, minutes=40, subtopics='', objectives='',
                teacher_notes=''):
    if not topic:
        raise AIError('Give the topic (or pick one from the scheme of work) so the AI knows what to write about.')
    minutes = max(20, min(int(minutes or 40), 120))
    teacher_line = f"The teacher's own notes to build on: {teacher_notes}" if teacher_notes else ''
    prompt = f"""Write a lesson note a Nigerian teacher can hand to their head of department.

Subject: {subject}
Class: {school_class}
Week: {week}
Topic: {topic}
Lesson length: {minutes} minutes
{f'Sub-topics from the scheme of work: {subtopics}' if subtopics else ''}
{f'Objectives already set: {objectives}' if objectives else ''}
{teacher_line}

Return a JSON object with:
  "title": a short lesson title,
  "objectives": 3-4 behavioural objectives, one per line, each starting "Students should be able to",
  "content": the lesson note as plain text with these headings on their own lines:
     Previous knowledge, Instructional materials, Introduction (with minutes), Presentation (Step 1, Step 2,
     Step 3 — teacher activity and student activity, with minutes), Evaluation (3-5 questions),
     Summary, Assignment.
The minutes must add up to about {minutes}. Pitch the content at {school_class} level and use Nigerian examples."""
    data = complete(tenant, 'lesson_note', SCHOOL_VOICE, prompt, user=user, json_mode=True,
                    max_tokens=1800).data or {}
    if not isinstance(data, dict) or not data.get('content'):
        raise AIError('The AI reply was incomplete — please try again.')
    return {'title': _clip(data.get('title') or topic, 200), 'objectives': _clip(data.get('objectives'), 2000),
            'content': _clip(data.get('content'), 20000)}


# ─────────────────────────────────────────────────────────────────────────────
# F162 — questions
# ─────────────────────────────────────────────────────────────────────────────

def questions(tenant, user, subject, level, topic, count=5, difficulty='MEDIUM', qtype='MCQ', source=''):
    try:
        count = max(1, min(int(count or 5), 20))
    except (TypeError, ValueError):
        count = 5
    if not (topic or source):
        raise AIError('Give a topic or paste some text for the questions to come from.')
    shape = ('four options, exactly one correct' if qtype == 'MCQ'
             else 'the two options "True" and "False", exactly one correct')
    kind = 'multiple-choice' if qtype == 'MCQ' else 'true/false'
    about = f' on "{topic}"' if topic else ''
    source_block = ('Base every question only on this text:\n<<<\n' + source[:6000] + '\n>>>') if source else ''
    prompt = f"""Write {count} {difficulty.lower()} {kind} questions
for {level or 'secondary school'} {subject}{about}.
{source_block}

Each question has {shape}, and a one or two sentence explanation of the right answer.
Avoid "all of the above" and "none of the above". Vary which option is correct.

Return a JSON object: {{"questions": [{{"text": "...", "options": ["...", "..."], "correct": 0, "explanation": "..."}}]}}
where "correct" is the index of the right option."""
    data = complete(tenant, 'questions', SCHOOL_VOICE, prompt, user=user, json_mode=True,
                    max_tokens=400 + 250 * count, temperature=0.8).data or {}
    out = []
    for q in (data.get('questions') if isinstance(data, dict) else None) or []:
        if not isinstance(q, dict):
            continue
        options = [_clip(o, 255) for o in (q.get('options') or []) if str(o).strip()][:6]
        if qtype == 'TF':
            options = ['True', 'False']
        try:
            correct = int(q.get('correct'))
        except (TypeError, ValueError):
            continue
        text = _clip(q.get('text'), 2000)
        if text and len(options) >= 2 and 0 <= correct < len(options):
            out.append({'text': text, 'options': options, 'correct': correct,
                        'explanation': _clip(q.get('explanation'), 1000)})
    if not out:
        raise AIError('The AI did not return usable questions — please try again.')
    return out[:count]


# ─────────────────────────────────────────────────────────────────────────────
# F166 — messages to parents
# ─────────────────────────────────────────────────────────────────────────────

def parent_message(tenant, user, student, purpose, points, tone='warm', language='en', channel='EMAIL',
                   sender=''):
    points = (points or '').strip()
    if not points:
        raise AIError('Write the main points you want the message to make.')
    lang = LANGUAGES.get(language, 'English')
    first = (student.full_name or '').split()[0] if student.full_name else 'your child'
    short = channel == 'SMS'
    prompt = f"""Draft a message from the school to a parent.

About: {first} ({student.class_assigned or 'no class'})
Parent's name: {student.parent_name or 'not given — use "Dear Parent"'}
Topic: {purpose}
Points to make (use only these facts): {points}
Tone: {TONES.get(tone, TONES['warm'])}
Language: {lang}
Signed by: {sender or 'the school'}, {tenant.name}

{'It goes by SMS: at most 400 characters, no subject needed.' if short else 'It goes by email: 2-4 short paragraphs.'}
Be respectful and calm, never blaming or threatening, and invite the parent to reply or visit if helpful.
Return a JSON object: {{"subject": "...", "body": "..."}} (body as plain text with blank lines between paragraphs)."""
    data = complete(tenant, 'parent_message', SCHOOL_VOICE, prompt, user=user, json_mode=True, max_tokens=900).data
    if not isinstance(data, dict) or not data.get('body'):
        raise AIError('The AI reply was incomplete — please try again.')
    body = _clip(data['body'], 400 if short else 5000)
    return _clip(data.get('subject') or f'About {first}', 200), body


# ─────────────────────────────────────────────────────────────────────────────
# F170 — translating notices
# ─────────────────────────────────────────────────────────────────────────────

def translate(tenant, user, title, message, language):
    if language not in LANGUAGES or language == 'en':
        raise AIError('Choose a language to translate into.')
    prompt = f"""Translate this school notice into {LANGUAGES[language]} for parents.
Keep names, dates, times, amounts and class names exactly as written. Keep it natural, the way a
{LANGUAGES[language]}-speaking parent would say it, not word for word.

Title: {title}
Message:
{message}

Return a JSON object: {{"title": "...", "message": "..."}}"""
    data = complete(tenant, 'translation', SCHOOL_VOICE, prompt, user=user, json_mode=True,
                    max_tokens=300 + len(message) // 2, temperature=0.3).data
    if not isinstance(data, dict) or not data.get('message'):
        raise AIError('The AI reply was incomplete — please try again.')
    return _clip(data.get('title') or title, 255), _clip(data['message'], 20000)
