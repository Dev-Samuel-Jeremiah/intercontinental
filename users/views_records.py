"""
users/views_records.py
──────────────────────
Records health — which students' records are missing what, by section and
class — and the printable cumulative record card (admission to graduation).
"""
import csv

from django.contrib import messages
from django.core.paginator import Paginator
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.contrib.auth.decorators import login_required

from . import completeness, history
from . import profile as student_profile
from .models import ENROLLED_STATUSES, Class, Student
from .views_profile import can_manage, can_view


@login_required
def records_health(request):
    if not can_view(request.user):
        messages.error(request, 'Only staff can see student records.')
        return redirect('dashboard:router')
    from saas_admin.models import Campus
    tenant = request.tenant
    sections = Campus.objects.filter(tenant=tenant, active=True)
    section = sections.filter(pk=request.GET.get('section') or request.session.get('campus_id')).first() \
        if (request.GET.get('section') or request.session.get('campus_id')) else None
    classes = Class.objects.filter(tenant=tenant).order_by('name')
    if section:
        classes = classes.filter(pk__in=section.classes.values('pk'))
    school_class = classes.filter(pk=request.GET.get('class')).first() if request.GET.get('class') else None
    missing = request.GET.get('missing') if request.GET.get('missing') in completeness.LABELS else ''
    students = (Student.objects.filter(tenant=tenant, status__in=ENROLLED_STATUSES)
                .select_related('class_assigned').order_by('class_assigned__name', 'full_name'))
    if school_class:
        students = students.filter(class_assigned=school_class)
    elif section:
        students = students.filter(class_assigned__in=classes)
    rows, counts, total = completeness.report(students)
    by_class = {}
    for st, pct, gaps in rows:
        key = st.class_assigned.name if st.class_assigned else 'No class'
        c = by_class.setdefault(key, {'name': key, 'id': st.class_assigned_id, 'students': 0, 'complete': 0, 'sum': 0})
        c['students'] += 1
        c['sum'] += pct
        c['complete'] += not gaps
    class_rows = sorted(({**c, 'avg': round(c['sum'] / c['students'])} for c in by_class.values()), key=lambda c: c['avg'])
    shown = [r for r in rows if r[2] and (not missing or missing in r[2])]
    shown.sort(key=lambda r: (r[1], r[0].full_name))
    if request.GET.get('export') == 'csv':
        resp = HttpResponse(content_type='text/csv')
        resp['Content-Disposition'] = 'attachment; filename="incomplete-student-records.csv"'
        w = csv.writer(resp)
        w.writerow(['Admission number', 'Name', 'Class', 'Complete %', 'Missing'])
        for st, pct, gaps in shown:
            w.writerow([st.admission_number, st.full_name, st.class_assigned.name if st.class_assigned else '', pct,
                        '; '.join(completeness.LABELS[g] for g in gaps)])
        return resp
    page = Paginator(shown, 50).get_page(request.GET.get('page'))
    n = len(rows)
    return render(request, 'students/records_health.html', {
        'sections': sections, 'section': section, 'classes': classes, 'school_class': school_class,
        'missing': missing, 'checks': completeness.CHECKS, 'labels': completeness.LABELS,
        'page': page, 'class_rows': class_rows, 'total_students': n, 'need_attention': len(shown),
        'missing_label': completeness.LABELS.get(missing, ''),
        'complete': sum(1 for r in rows if not r[2]),
        'average': round(sum(r[1] for r in rows) / n) if n else None,
        'gaps': [{'key': k, 'label': completeness.LABELS[k], 'count': counts[k],
                  'percent': round(100 * counts[k] / n) if n else 0}
                 for k, _, _, _ in completeness.CHECKS if counts.get(k)],
        'query': '&'.join(f'{k}={v}' for k, v in (('section', section.pk if section else ''),
                                                  ('class', school_class.pk if school_class else ''),
                                                  ('missing', missing)) if v),
    })


@login_required
def record_card(request, student_id):
    """The cumulative record card: one printable file from admission to leaving."""
    if not can_view(request.user):
        messages.error(request, 'Only staff can print student records.')
        return redirect('dashboard:router')
    student = get_object_or_404(Student.objects.select_related('class_assigned', 'house', 'user'), pk=student_id,
                                tenant=request.tenant)
    manage = can_manage(request.user)
    from .models import mask_nin
    health = student_profile.health(student, request.user)
    return render(request, 'students/record_card.html', {
        'student': student, 'school': request.tenant, 'spells': history.history(student),
        'results': [r for r in student_profile.results_history(student) if r['published']],
        'guardians': student.guardians.all(), 'awards': student.awards.all(), 'roles': student.roles.all(),
        'status_history': student.status_changes.all()[:20], 'health': health,
        'section': history.section_of(student.class_assigned),
        'nin_display': student.nin if manage else mask_nin(student.nin),
        'completeness': completeness.check(student),
    })
