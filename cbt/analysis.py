"""
cbt/analysis.py
───────────────
Item analysis after a paper: how hard each question was (share answering
correctly), whether it separated stronger from weaker candidates
(discrimination: top 27% minus bottom 27%), and how the options were chosen —
so a wrong answer key or a confusing question stands out.
"""
from collections import Counter, defaultdict

from .models import ExamAttempt, Question, StudentAnswer

GROUP = 0.27
MIN_FOR_FLAGS = 5     # fewer candidates than this and the percentages say little


def item_analysis(exam_part):
    attempts = list(ExamAttempt.objects.filter(exam_part=exam_part, completed=True))
    questions = list(Question.objects.filter(exam_part=exam_part, question_type__in=('MCQ', 'TF'))
                     .prefetch_related('options').order_by('id'))
    if not attempts or not questions:
        return {'rows': [], 'candidates': len(attempts), 'summary': None}
    ranked = sorted(attempts, key=lambda a: a.score, reverse=True)
    k = max(1, round(len(ranked) * GROUP)) if len(ranked) >= 4 else 0
    upper, lower = {a.id for a in ranked[:k]}, {a.id for a in ranked[-k:]} if k else set()
    answers = defaultdict(dict)      # question → {attempt: option id}
    for a in StudentAnswer.objects.filter(attempt__in=attempts, question__in=questions).values(
            'attempt_id', 'question_id', 'selected_option_id'):
        answers[a['question_id']][a['attempt_id']] = a['selected_option_id']
    rows = []
    for q in questions:
        correct = {o.id for o in q.options.all() if o.is_correct}
        seen = [a for a in attempts if not a.question_order or q.id in a.question_order]
        if not seen:
            continue
        chosen = answers.get(q.id, {})
        right = {a.id for a in seen if chosen.get(a.id) in correct}
        p = len(right) / len(seen)
        d = None
        su, sl = [a for a in seen if a.id in upper], [a for a in seen if a.id in lower]
        if su and sl:
            d = len([a for a in su if a.id in right]) / len(su) - len([a for a in sl if a.id in right]) / len(sl)
        dist = Counter(chosen.get(a.id) for a in seen)
        options = [{'option': o, 'count': dist.get(o.id, 0), 'percent': round(100 * dist.get(o.id, 0) / len(seen)),
                    'correct': o.id in correct} for o in q.options.all()]
        blank = dist.get(None, 0)
        top_wrong = max((o for o in options if not o['correct']), key=lambda o: o['count'], default=None)
        flags = []
        if len(seen) < MIN_FOR_FLAGS:
            pass
        elif d is not None and d < 0:
            flags.append(('red', 'Stronger candidates got it wrong more often than weaker ones — check the answer key.'))
        if len(seen) < MIN_FOR_FLAGS:
            pass
        elif p < 0.25:
            msg = 'Very hard: under a quarter got it right.'
            if top_wrong and top_wrong['count'] > len(right):
                msg += f' Most chose "{top_wrong["option"].text}" — is that the real answer?'
            flags.append(('red' if top_wrong and top_wrong['count'] > len(right) else 'amber', msg))
        elif p > 0.95:
            flags.append(('amber', 'Very easy: almost everyone got it right.'))
        if len(seen) >= MIN_FOR_FLAGS and d is not None and 0 <= d < 0.15 and 0.25 <= p <= 0.95:
            flags.append(('amber', "Doesn't separate stronger from weaker candidates — the wording may be unclear."))
        rows.append({'question': q, 'seen': len(seen), 'right': len(right), 'p': round(p * 100),
                     'd': None if d is None else round(d, 2), 'options': options, 'blank': blank, 'flags': flags})
    scores = [a.score for a in attempts]
    summary = {'mean': round(sum(scores) / len(scores), 1), 'highest': max(scores), 'lowest': min(scores),
               'flagged': sum(1 for r in rows if r['flags']), 'groups': k,
               'too_few': len(attempts) < MIN_FOR_FLAGS}
    return {'rows': rows, 'candidates': len(attempts), 'summary': summary}
