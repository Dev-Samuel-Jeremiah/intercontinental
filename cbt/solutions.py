"""
Worked solutions for the answer review.

Each objective question gets one step-by-step solution, written by the
school's AI (Groq, or whichever provider the school uses) and stored, so every
student who reviews the question sees the same solution and the AI is asked
only once. The AI is always given the official answer and asked to explain it;
it never decides the answer. If it believes the marked answer is wrong, the
solution is held back from students and flagged for the teacher instead.

    from cbt import solutions
    have = solutions.existing(questions)            # {question_id: QuestionSolution}
    made = solutions.write(tenant, batch, user, subject='Mathematics', level='JSS 1')
"""
import hashlib
import html
import json
import re
import string

from django.utils.html import strip_tags

from assistant.providers import complete

from .models import QuestionSolution

BATCH = 6
SOLVABLE = ('MCQ', 'TF', 'SA')
SYSTEM = (
    "You are an experienced, encouraging {subject} teacher at a Nigerian school, writing worked solutions for "
    "{level} students who are reviewing an exam they have just taken.\n"
    "Each question comes with the OFFICIAL correct answer. Explain, step by step, how to reach exactly that answer.\n"
    "Rules:\n"
    "- Never change the official answer. If you are sure it is wrong, set \"disagree\": true and explain in \"note\" "
    "(one sentence), but still do not invent a different answer.\n"
    "- 2 to 6 short steps in simple English for a student of that level. Show the working, not just the result.\n"
    "- Write any mathematics or chemistry in LaTeX between single dollar signs, e.g. $\\frac{{3}}{{4}}$.\n"
    "- For each wrong option, one short sentence on the mistake that leads to it, keyed by its letter.\n"
    "- Check every number you write by working it out again before you reply: each step and each reason must be "
    "arithmetically exact (e.g. 345 - 225 = 120, so 345 minutes is 120 minutes too many).\n"
    "- One short tip to remember for next time.\n"
    "- If a question mentions a diagram you cannot see, work from the official answer and the text.\n"
    "Reply with JSON only, in this shape:\n"
    '{{"solutions": [{{"id": 12, "steps": ["..."], "why_not": {{"B": "..."}}, "tip": "...", '
    '"disagree": false, "note": ""}}]}}'
)


def _plain(text):
    return html.unescape(strip_tags(text or '')).strip()


def _options(q):
    """The question's options in a fixed order (by id), whatever order the student saw them in."""
    return sorted(q.options.all(), key=lambda o: o.id)


def fingerprint(q):
    """Changes whenever the question, its options, its answer or the teacher's note change."""
    parts = [q.question_text or '', q.passage or '', q.explanation or '']
    parts += [f'{o.id}:{o.text}:{int(o.is_correct)}' for o in _options(q)]
    return hashlib.sha1('\n'.join(parts).encode(), usedforsecurity=False).hexdigest()


def solvable(q):
    return q.question_type in SOLVABLE and any(o.is_correct for o in _options(q))


def existing(questions):
    """Stored solutions that still match their question."""
    by_id = {q.id: q for q in questions}
    found = QuestionSolution.objects.filter(question_id__in=list(by_id))
    return {s.question_id: s for s in found if s.key == fingerprint(by_id[s.question_id])}


def _brief(q):
    opts = _options(q)
    letters = dict(zip(string.ascii_uppercase, opts))
    item = {'id': q.id, 'question': _plain(q.question_text)}
    if q.passage:
        item['passage'] = _plain(q.passage)[:1500]
    if q.image:
        item['has_diagram'] = True
    if q.question_type == 'SA':
        item['accepted_answers'] = [o.text for o in opts if o.is_correct]
    else:
        item['options'] = {k: o.text for k, o in letters.items()}
        item['official_answer'] = [k for k, o in letters.items() if o.is_correct]
    if q.explanation:
        item['teacher_note'] = _plain(q.explanation)[:500]
    return item, {k: o.id for k, o in letters.items()}


def _clean(raw, letter_ids, q):
    def text(v, n):
        t = str(v or '').strip()
        while t.endswith('}') and t.count('}') > t.count('{'):     # a brace the model left behind
            t = t[:-1].rstrip()
        return t[:n]
    steps = [text(s, 600) for s in (raw.get('steps') or []) if text(s, 600)][:8]
    why_not = {}
    for letter, reason in (raw.get('why_not') or {}).items():
        oid = letter_ids.get(str(letter).strip().upper()[:1])
        if oid and text(reason, 400):
            why_not[str(oid)] = text(reason, 400)
    # The answer shown is always the teacher's key, never the model's wording.
    answer = ' / '.join(o.text for o in _options(q) if o.is_correct)
    return {'steps': steps, 'answer': answer[:300], 'why_not': why_not, 'tip': text(raw.get('tip'), 300)}


def write(tenant, questions, user=None, subject='', level=''):
    """Ask the AI for the given questions (at most BATCH) and store the answers. Raises AIError."""
    questions = [q for q in questions if solvable(q)][:BATCH]
    if not questions:
        return {}
    briefs, letters = [], {}
    for q in questions:
        item, letter_ids = _brief(q)
        briefs.append(item)
        letters[q.id] = letter_ids
    system = SYSTEM.format(subject=subject or 'subject', level=level or 'secondary school')
    result = complete(tenant, 'exam_solutions', system, json.dumps({'questions': briefs}, ensure_ascii=False),
                      user=user, json_mode=True, max_tokens=3500, temperature=0.2)
    rows = (result.data or {}).get('solutions') if isinstance(result.data, dict) else None
    made = {}
    for raw in rows or []:
        try:
            qid = int(raw.get('id'))
        except (TypeError, ValueError, AttributeError):
            continue
        q = next((q for q in questions if q.id == qid), None)
        if q is None:
            continue
        data = _clean(raw, letters[qid], q)
        if not data['steps']:
            continue
        sol, _ = QuestionSolution.objects.update_or_create(question=q, defaults={
            'tenant': tenant, 'key': fingerprint(q), 'data': data, 'provider': result.provider, 'model': result.model,
            'flagged': bool(raw.get('disagree')), 'flag_note': str(raw.get('note') or '')[:300]})
        made[qid] = sol
    return made


def retry_after(message):
    """Seconds to wait when the provider says we hit its per-minute limit; None for any other failure."""
    if '429' not in message and 'rate limit' not in message.lower():
        return None
    m = re.search(r'try again in (?:(\d+)m)?([\d.]+)s', message)
    seconds = (int(m.group(1) or 0) * 60 + float(m.group(2))) if m else 15
    return min(90, max(2, round(seconds + 1)))


def public(sol):
    """What the student's page shows. A flagged solution is held back until a teacher checks the question."""
    if sol.flagged:
        return {'held': True}
    return dict(sol.data, held=False)
