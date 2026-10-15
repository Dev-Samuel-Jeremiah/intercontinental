"""
results/assessment.py
─────────────────────
The school's continuous-assessment template: checking a new structure before
it is saved, and checking scores against it when they are entered.
"""
from django.db.models import Q

from .models import AssessmentScheme, TermResult


class AssessmentError(ValueError):
    pass


PRESETS = {
    'default': ('TRT 10 + Homework 10 + Class participation 10 + Exam 70',
                {'ca1': ('TRT', 10), 'ca2': ('Homework', 10), 'ca3': ('Class Part.', 10),
                 'essay': ('Essay', 0), 'exam': ('Exam', 70)}),
    'two_tests': ('1st test 20 + 2nd test 20 + Exam 60',
                  {'ca1': ('1st Test', 20), 'ca2': ('2nd Test', 20), 'ca3': ('', 0),
                   'essay': ('Essay', 0), 'exam': ('Exam', 60)}),
    'tests_assignment': ('2 tests 10 each + Assignment 10 + Exam 70',
                         {'ca1': ('1st Test', 10), 'ca2': ('2nd Test', 10), 'ca3': ('Assignment', 10),
                          'essay': ('Essay', 0), 'exam': ('Exam', 70)}),
    'waec_style': ('CA 40 (3 parts) + Exam 60',
                   {'ca1': ('1st CA', 15), 'ca2': ('2nd CA', 15), 'ca3': ('Project', 10),
                    'essay': ('Essay', 0), 'exam': ('Exam', 60)}),
    'cbt_theory': ('CA 30 + Objective (CBT) 40 + Theory 30',
                   {'ca1': ('1st CA', 15), 'ca2': ('2nd CA', 15), 'ca3': ('', 0),
                    'essay': ('Theory', 30), 'exam': ('Objective', 40)}),
}


def parse_form(data):
    """{column: (label, max)} from posted fields, with clear errors."""
    out = {}
    for key in AssessmentScheme.COLUMNS:
        label = (data.get(f'{key}_label') or '').strip()[:24]
        try:
            top = int(data.get(f'{key}_max') or 0)
        except (TypeError, ValueError):
            raise AssessmentError('Maximum marks must be whole numbers.')
        if not 0 <= top <= 100:
            raise AssessmentError('Each column is out of 0 to 100.')
        if top and not label:
            raise AssessmentError('Give every column you use a name.')
        out[key] = (label or AssessmentScheme.DEFAULTS[key][0], top)
    return out


def check(structure):
    total = sum(top for _, top in structure.values())
    if total != 100:
        raise AssessmentError(f'The columns add up to {total}. They must add up to 100.')
    if not structure['exam'][1]:
        raise AssessmentError('The exam column must be used.')
    labels = [label.lower() for label, top in structure.values() if top]
    if len(labels) != len(set(labels)):
        raise AssessmentError('Two columns have the same name.')


def conflicts(tenant, term, structure):
    """
    Results already entered this term that wouldn't fit the new structure:
    {column label: number of results}. A column can't be switched off or
    lowered below marks students already have.
    """
    if not term:
        return {}
    qs = TermResult.objects.filter(tenant=tenant, term=term, is_not_offering=False)
    out = {}
    for key, (label, top) in structure.items():
        n = qs.filter(**{f'{key}_score__gt': top}).count()
        if n:
            out[label] = n
    return out


def save(tenant, structure, term=None):
    check(structure)
    clash = conflicts(tenant, term, structure)
    if clash:
        detail = ', '.join(f'{label} ({n})' for label, n in clash.items())
        raise AssessmentError(f'Some scores entered this term are higher than the new maximum: {detail}. '
                              'Correct those scores first, or change the structure at the start of next term.')
    scheme = AssessmentScheme.objects.filter(tenant=tenant).first() or AssessmentScheme(tenant=tenant)
    for key, (label, top) in structure.items():
        setattr(scheme, f'{key}_label', label)
        setattr(scheme, f'{key}_max', top)
    scheme.save()
    return scheme


def clean_score(raw, top, label):
    """A posted score as a number within the column's maximum."""
    if raw in (None, ''):
        return 0.0
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise AssessmentError(f'{label}: "{raw}" is not a number.')
    if value < 0:
        raise AssessmentError(f'{label} cannot be negative.')
    if value > top:
        raise AssessmentError(f'{label} is out of {top}, so {value:g} is too high.')
    return value
