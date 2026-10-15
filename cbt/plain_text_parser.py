"""
plain_text_parser.py
──────────────────────────────────────────────────────────────
Plain-text / textarea CBT Question Parser.

Accepts the same flexible formats as word_parser.py but works
on raw text — no Word document required.

SUPPORTED FORMATS
─────────────────
MULTI-LINE (each element on its own line):
    1. The process by which a solid changes directly into gas is called
    A. Condensation
    B. Sublimation
    C. Evaporation
    D. Freezing
    ANSWER: B
    MARK: 0.5

INLINE (whole question on one line):
    1. The process by which a solid changes directly into gas is called A. Condensation B. Sublimation C. Evaporation D. Freezing ANSWER: B MARK: 0.5

MIXED: Both styles can appear in the same paste.

Question numbering accepted: 1.  1)  Q1.  Q1)  (1)  1-  1:
Option letters accepted:      A.  A)  A:  A-  (A)  a.
Answer keywords:  ANSWER: B  |  ANS: B  |  The answer is B  |  Key: B
Marks keywords:   MARK: 0.5  |  MARKS: 1  |  POINTS: 2  |  SCORE: 1/2
"""

import re

# ─────────────────────────────────────────────────────────────
#  COMPILED PATTERNS  (mirrors word_parser.py)
# ─────────────────────────────────────────────────────────────

RE_QUESTION = re.compile(
    r'^(?:\(?\s*Q(?:uestion)?\s*\.?\s*)?'
    r'\(?\s*(\d+)\s*[.):\-]?\s*\)?\s*'
    r'(.+)',
    re.IGNORECASE | re.DOTALL,
)

RE_OPTION_LINE = re.compile(
    r'^\(?([A-Ea-e])[.):\-]\)?\s*(.*)',
    re.IGNORECASE | re.DOTALL,
)

RE_ANSWER = re.compile(
    r'^(?:the\s+)?'
    r'(?:ANSWER|ANS(?:WER)?|CORRECT\s*ANSWER|KEY)'
    r'(?:\s+is)?'
    r'[\s:=\-]*([A-Ea-e])\b'
    r'[\s\(\w\)]*$',
    re.IGNORECASE,
)

RE_ANSWER_SEARCH = re.compile(
    r'(?:ANSWER|ANS(?:WER)?|CORRECT\s*ANSWER|KEY)'
    r'(?:\s+is)?'
    r'[\s:=\-]*([A-Ea-e])\b',
    re.IGNORECASE,
)

RE_MARKS = re.compile(
    r'^(?:MARKS?|POINTS?|SCORE|MARK|WEIGHT)[:\s=]+(.+)',
    re.IGNORECASE,
)

RE_MARKS_SEARCH = re.compile(
    r'(?:MARKS?|POINTS?|SCORE|MARK|WEIGHT)[:\s=]+([0-9/.]+)',
    re.IGNORECASE,
)

# Splits on A.  A)  B.  B)  etc. — used to break an inline option string
RE_OPT_SPLIT = re.compile(r'(?=[A-Ea-e][.)])')

# Finds where inline options START within the question text
RE_INLINE_OPT_SPACED = re.compile(r'(?:^|\s)([A-Ea-e])[.)]\s*\S', re.IGNORECASE)
RE_INLINE_OPT_GLUED  = re.compile(r'(?<=[a-z\d?!,;])([A-Ea-e])[.)]', re.IGNORECASE)


# ─────────────────────────────────────────────────────────────
#  INTERNAL HELPERS
# ─────────────────────────────────────────────────────────────

def _parse_marks(raw):
    if not raw:
        return 1.0
    raw = raw.strip()
    if '/' in raw:
        parts = raw.split('/', 1)
        try:
            return round(float(parts[0].strip()) / float(parts[1].strip()), 4)
        except (ValueError, ZeroDivisionError):
            pass
    try:
        return float(raw)
    except ValueError:
        return 1.0


def _find_inline_options_start(text):
    candidates = []
    for m in RE_INLINE_OPT_SPACED.finditer(text):
        candidates.append((m.start(), m.group(1).upper()))
    for m in RE_INLINE_OPT_GLUED.finditer(text):
        candidates.append((m.start(), m.group(1).upper()))
    if not candidates:
        return -1
    distinct_letters = {c[1] for c in candidates}
    if len(distinct_letters) < 2:
        return -1
    return min(c[0] for c in candidates)


def _is_inline_question(text):
    """Return True when the line contains a question number AND inline options."""
    if not RE_QUESTION.match(text):
        return False
    q_m = RE_QUESTION.match(text)
    rest = text[q_m.start(2):]
    return _find_inline_options_start(rest) >= 0


def _extract_inline_options(options_text):
    """Parse 'A. Opt B. Opt C. Opt D. Opt ANSWER: B MARK: 0.5' into a dict."""
    options = {}
    answer  = None
    marks   = None

    for chunk in RE_OPT_SPLIT.split(options_text):
        chunk = chunk.strip()
        if not chunk:
            continue

        m = re.match(r'^([A-Ea-e])[.)]\s*(.*)', chunk, re.IGNORECASE | re.DOTALL)
        if not m:
            a = RE_ANSWER_SEARCH.search(chunk)
            if a:
                answer = a.group(1).upper()
            mk = RE_MARKS_SEARCH.search(chunk)
            if mk:
                marks = _parse_marks(mk.group(1))
            continue

        letter      = m.group(1).upper()
        option_text = m.group(2).strip()

        mk = RE_MARKS_SEARCH.search(option_text)
        if mk:
            marks = _parse_marks(mk.group(1))

        a = RE_ANSWER_SEARCH.search(option_text)
        if a:
            answer = a.group(1).upper()
            cut = a.start()
            if mk and mk.start() < cut:
                cut = mk.start()
            option_text = option_text[:cut].strip()
        elif mk:
            option_text = option_text[:mk.start()].strip()

        if option_text:
            options[letter] = option_text

    return options, answer, marks


def _new_question(number, text):
    return {
        'question_number': number,
        'question_text':   text,
        'question_type':   'MCQ',
        'marks':           1.0,
        'correct_option':  None,
        'correct_answer':  None,
        'passage':         None,
        'passage_title':   None,
        'passage_group':   None,
        'image_reference': None,
        'image_caption':   None,
        'image_bytes':     None,
        'options':         {},
    }


# ─────────────────────────────────────────────────────────────
#  PUBLIC API
# ─────────────────────────────────────────────────────────────

def parse_plain_text(raw_text):
    """
    Parse a plain-text / textarea string of CBT questions.

    Args:
        raw_text (str): The full text pasted by the user.

    Returns:
        list[dict]: Same structure as WordQuestionParser.parse().
    """
    questions        = []
    current_question = None
    current_options  = {}

    lines = raw_text.splitlines()

    for raw_line in lines:
        text = raw_line.strip()

        # Skip blank lines (they're just separators)
        if not text:
            continue

        # ── Marks line ───────────────────────────────────────────
        mk = RE_MARKS.match(text)
        if mk and current_question:
            current_question['marks'] = _parse_marks(mk.group(1))
            continue

        # ── Short-answer support ─────────────────────────────────
        if re.match(r'^(?:SHORT\s*ANSWER|SA)[:\s]*$', text, re.IGNORECASE) and current_question:
            current_question['question_type'] = 'SA'
            continue

        sa = re.match(r'^(?:ANSWER|ANS)[:\s]+(.+)', text, re.IGNORECASE)
        if sa and current_question and current_question.get('question_type') == 'SA':
            current_question['correct_answer'] = sa.group(1).strip()
            continue

        # ══ INLINE QUESTION (number + options on one line) ════════
        if _is_inline_question(text):
            # Save previous question first
            if current_question:
                current_question['options'] = current_options
                questions.append(current_question)
                current_question = None
                current_options  = {}

            q_m       = RE_QUESTION.match(text)
            full_rest = text[q_m.start(2):]
            opt_idx   = _find_inline_options_start(full_rest)

            question_text = full_rest[:opt_idx].strip() if opt_idx > 0 else full_rest.strip()
            options_text  = full_rest[opt_idx:]         if opt_idx > 0 else ''

            opts, ans, inline_marks = _extract_inline_options(options_text)

            q = _new_question(q_m.group(1), question_text)
            q['options'] = opts
            if ans:
                q['correct_option'] = ans
            if inline_marks is not None:
                q['marks'] = inline_marks

            questions.append(q)
            continue

        # ══ NORMAL QUESTION LINE (number only, options follow) ════
        q_m = RE_QUESTION.match(text)
        if q_m:
            # Save previous
            if current_question:
                current_question['options'] = current_options
                questions.append(current_question)

            current_question = _new_question(q_m.group(1), q_m.group(2).strip())
            current_options  = {}
            continue

        # ── Option line ──────────────────────────────────────────
        opt_m = RE_OPTION_LINE.match(text)
        if opt_m and current_question:
            letter = opt_m.group(1).upper()
            current_options[letter] = opt_m.group(2).strip()
            continue

        # ── Answer line ──────────────────────────────────────────
        ans_m = RE_ANSWER.match(text)
        if ans_m and current_question:
            current_question['correct_option'] = ans_m.group(1).upper()
            if len(current_options) == 2:
                vals = [str(v).lower() for v in current_options.values()]
                if 'true' in vals and 'false' in vals:
                    current_question['question_type'] = 'TF'
            continue

        # ── Unrecognised line: append to current question text ───
        # (handles multi-line question bodies)
        if current_question and not opt_m:
            current_question['question_text'] += ' ' + text

    # Save last question
    if current_question:
        current_question['options'] = current_options
        questions.append(current_question)

    return questions


def validate_plain_text_questions(questions):
    """Same validation logic as WordQuestionParser.validate_questions()."""
    errors = []
    for idx, q in enumerate(questions, 1):
        if not q.get('question_text'):
            errors.append(f"Question {idx}: Missing question text")
        if q['question_type'] in ['MCQ', 'TF']:
            if not q.get('options') or len(q['options']) < 2:
                errors.append(f"Question {idx}: Needs at least 2 options")
            if not q.get('correct_option'):
                errors.append(f"Question {idx}: Missing correct answer")
            if q.get('correct_option') and q['correct_option'] not in q.get('options', {}):
                errors.append(
                    f"Question {idx}: Answer '{q['correct_option']}' "
                    f"not in options {list(q.get('options', {}).keys())}"
                )
        elif q['question_type'] == 'SA':
            if not q.get('correct_answer'):
                errors.append(f"Question {idx}: Missing answer")
    return errors