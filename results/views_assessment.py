"""
results/views_assessment.py
───────────────────────────
The school's continuous-assessment structure.
"""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render

from academics.utils import get_active_term
from users.views_profile import can_manage

from . import assessment
from .models import AssessmentScheme


@login_required
def assessment_settings(request):
    if not can_manage(request.user):
        messages.error(request, 'Only administrators can change the assessment structure.')
        return redirect('dashboard:router')
    term = get_active_term()
    scheme = AssessmentScheme.for_tenant(request.tenant)
    posted = None
    if request.method == 'POST':
        try:
            if request.POST.get('preset') in assessment.PRESETS:
                structure = assessment.PRESETS[request.POST['preset']][1]
            else:
                structure = assessment.parse_form(request.POST)
            posted = structure
            assessment.save(request.tenant, structure, term)
            messages.success(request, 'Assessment structure saved. Score entry and report cards now use it.')
            return redirect('results:assessment')
        except assessment.AssessmentError as problem:
            messages.error(request, str(problem))
    rows = []
    for key in AssessmentScheme.COLUMNS:
        label, top = posted[key] if posted else (getattr(scheme, f'{key}_label'), getattr(scheme, f'{key}_max'))
        rows.append({'key': key, 'label': label, 'max': top,
                     'name': {'ca1': 'CA column 1', 'ca2': 'CA column 2', 'ca3': 'CA column 3',
                              'essay': 'Extra paper (theory, essay or practical)',
                              'exam': 'Examination'}[key]})
    return render(request, 'results/assessment.html', {
        'scheme': scheme, 'rows': rows, 'presets': assessment.PRESETS, 'term': term,
        'saved': scheme.pk is not None,
    })
