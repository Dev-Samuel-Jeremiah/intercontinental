"""
cbt/bank.py
───────────
The question bank: adding and finding questions, saving an exam's questions
into the bank, and putting bank questions on a paper.
"""
from django.db import transaction
from django.db.models import Count, Q

from .models import BankOption, BankQuestion, Option, Question


class BankError(ValueError):
    pass


def options_from(data):
    """[(text, is_correct)] from posted option_N / correct fields."""
    texts = [(data.get(f'option_{i}') or '').strip()[:255] for i in range(8)]
    try:
        correct = int(data.get('correct', -1))
    except (TypeError, ValueError):
        correct = -1
    out = [(t, i == correct) for i, t in enumerate(texts) if t]
    return out


@transaction.atomic
def save_question(tenant, data, staff=None, question=None, subject=None):
    text = (data.get('text') or '').strip()
    if not text:
        raise BankError('Write the question.')
    qtype = data.get('question_type') if data.get('question_type') in ('MCQ', 'TF') else 'MCQ'
    options = [('True', data.get('correct') == '0'), ('False', data.get('correct') == '1')] if qtype == 'TF' \
        else options_from(data)
    if len(options) < 2:
        raise BankError('Give at least two options.')
    if sum(1 for _, c in options if c) != 1:
        raise BankError('Mark exactly one option as correct.')
    q = question or BankQuestion(tenant=tenant, created_by=staff)
    q.subject = subject or q.subject
    q.level = (data.get('level') or '').strip()[:40]
    q.topic = (data.get('topic') or '').strip()[:120]
    q.outcome = (data.get('outcome') or '').strip()[:200]
    q.difficulty = data.get('difficulty') if data.get('difficulty') in dict(BankQuestion.DIFFICULTY) else 'MEDIUM'
    q.question_type, q.text = qtype, text
    q.passage = (data.get('passage') or '').strip()
    q.explanation = (data.get('explanation') or '').strip()
    try:
        q.marks = max(0.5, min(100, float(data.get('marks') or 1)))
    except ValueError:
        q.marks = 1
    q.for_practice = bool(data.get('for_practice'))
    q.save()
    q.options.all().delete()
    BankOption.objects.bulk_create([BankOption(question=q, text=t, is_correct=c) for t, c in options])
    return q


def search(tenant, subject=None, level='', topic='', difficulty='', text=''):
    qs = (BankQuestion.objects.filter(tenant=tenant).select_related('subject', 'created_by')
          .prefetch_related('options').annotate(n_uses=Count('uses')))
    if subject:
        qs = qs.filter(subject=subject)
    if level:
        qs = qs.filter(level__iexact=level)
    if topic:
        qs = qs.filter(topic__icontains=topic)
    if difficulty:
        qs = qs.filter(difficulty=difficulty)
    if text:
        qs = qs.filter(Q(text__icontains=text) | Q(outcome__icontains=text))
    return qs


@transaction.atomic
def to_bank(question, staff=None, level='', topic=''):
    """Copy an exam question into the bank (once)."""
    if question.question_type not in ('MCQ', 'TF'):
        return None
    if question.bank_question_id:
        return question.bank_question
    exam = question.exam_part.exam
    b = BankQuestion.objects.create(tenant=exam.tenant, subject=exam.subject, level=level, topic=topic,
                                    question_type=question.question_type, text=question.question_text,
                                    passage=question.passage or '', explanation=question.explanation or '',
                                    marks=question.marks or 1, created_by=staff, for_practice=False)
    if question.image:
        b.image = question.image
        b.save(update_fields=['image'])
    BankOption.objects.bulk_create([BankOption(question=b, text=o.text, is_correct=o.is_correct)
                                    for o in question.options.all()])
    question.bank_question = b
    question.save(update_fields=['bank_question'])
    return b


@transaction.atomic
def add_to_part(bank_questions, exam_part):
    """Copy bank questions onto a paper. Ones already on it are skipped. Returns how many were added."""
    if exam_part.part_type != 'CBT':
        raise BankError('Bank questions go into a CBT section.')
    have = set(Question.objects.filter(exam_part=exam_part, bank_question__isnull=False)
               .values_list('bank_question_id', flat=True))
    added = 0
    for b in bank_questions:
        if b.id in have:
            continue
        if b.subject_id != exam_part.exam.subject_id:
            raise BankError(f'"{b.text[:40]}…" is a {b.subject.name} question; this exam is {exam_part.exam.subject.name}.')
        q = Question(exam_part=exam_part, question_text=b.text, question_type=b.question_type, marks=b.marks,
                     passage=b.passage or None, explanation=b.explanation, bank_question=b)
        if b.image:
            q.image = b.image
        q.save()
        Option.objects.bulk_create([Option(question=q, text=o.text, is_correct=o.is_correct) for o in b.options.all()])
        added += 1
    return added
