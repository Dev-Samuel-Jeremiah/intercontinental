"""
users/completeness.py
─────────────────────
How complete each student's record is. Every other module reads the student
record, so gaps here turn up everywhere else: a missing parent phone means no
absence SMS, a missing date of birth means no WAEC/NECO registration, no
medical details means the nurse is guessing in an emergency.

`check(student)` answers for one student; `report(students)` answers for many
with a fixed number of queries.
"""
from collections import Counter

# key, label, which profile tab fixes it, why it matters
CHECKS = [
    ('passport', 'Passport photo', 'overview', 'ID cards, result sheets and gate checks'),
    ('dob', 'Date of birth', 'overview', 'WAEC/NECO registration and age checks'),
    ('gender', 'Gender', 'overview', 'Class lists and boarding'),
    ('address', 'Home address', 'overview', 'Transport and emergencies'),
    ('nin', 'NIN', 'overview', 'Government returns and exam registration'),
    ('class', 'Class', 'overview', 'Registers, timetables and results'),
    ('parent_phone', 'Parent phone', 'overview', 'Absence alerts and fee reminders'),
    ('parent_email', 'Parent email', 'overview', 'Results and the parent portal'),
    ('emergency', 'Emergency contact', 'overview', 'Someone to call if the parent cannot be reached'),
    ('medical', 'Medical details', 'health', 'Blood group, genotype and allergies for the nurse'),
    ('birth_cert', 'Birth certificate', 'documents', 'Proof of age'),
    ('immunisation', 'Immunisation record', 'health', 'Health screening and outbreaks'),
    ('custom', 'Required school questions', 'details', 'Questions your school marked as required'),
]
LABELS = {k: label for k, label, _, _ in CHECKS}


def _sets(tenant_id, ids):
    from users.models import Guardian, StudentDocument, StudentField, StudentFieldValue
    out = {'emergency': set(Guardian.objects.filter(tenant_id=tenant_id, student_id__in=ids, emergency_contact=True)
                            .values_list('student_id', flat=True)),
           'birth_cert': set(StudentDocument.objects.filter(tenant_id=tenant_id, student_id__in=ids, doc_type='BIRTH')
                             .values_list('student_id', flat=True)),
           'immunisation': set(StudentDocument.objects.filter(tenant_id=tenant_id, student_id__in=ids,
                                                              doc_type='IMMUNISATION').values_list('student_id', flat=True))}
    try:
        from welfare.models import Immunisation, MedicalProfile
        out['immunisation'] |= set(Immunisation.objects.filter(tenant_id=tenant_id, student_id__in=ids)
                                   .values_list('student_id', flat=True))
        out['medical'] = set(MedicalProfile.objects.filter(tenant_id=tenant_id, student_id__in=ids)
                             .exclude(blood_group='', genotype='', allergies='').values_list('student_id', flat=True))
    except Exception:
        out['medical'] = set()
    required = list(StudentField.objects.filter(tenant_id=tenant_id, required=True, is_active=True)
                    .values_list('pk', flat=True))
    answered = Counter()
    if required:
        for sid in (StudentFieldValue.objects.filter(tenant_id=tenant_id, student_id__in=ids, field_id__in=required)
                    .exclude(value='').values_list('student_id', flat=True)):
            answered[sid] += 1
    out['custom_needed'], out['custom_answered'] = len(required), answered
    return out


def _missing(st, sets):
    gaps = []
    if not st.passport:
        gaps.append('passport')
    if not st.date_of_birth:
        gaps.append('dob')
    if not st.gender:
        gaps.append('gender')
    if not (st.address or '').strip():
        gaps.append('address')
    if not st.nin:
        gaps.append('nin')
    if not st.class_assigned_id:
        gaps.append('class')
    if not (st.parent_phone or '').strip():
        gaps.append('parent_phone')
    if not (st.parent_email or '').strip():
        gaps.append('parent_email')
    for key in ('emergency', 'medical', 'birth_cert', 'immunisation'):
        if st.id not in sets[key]:
            gaps.append(key)
    if sets['custom_needed'] and sets['custom_answered'][st.id] < sets['custom_needed']:
        gaps.append('custom')
    return gaps


def _total(sets):
    return len(CHECKS) - (0 if sets['custom_needed'] else 1)


def check(student):
    """{'percent', 'missing': [{'key','label','tab','why'}], 'done', 'total'} for one student."""
    sets = _sets(student.tenant_id, [student.id])
    gaps = _missing(student, sets)
    total = _total(sets)
    info = {k: (label, tab, why) for k, label, tab, why in CHECKS}
    return {'percent': round(100 * (total - len(gaps)) / total), 'done': total - len(gaps), 'total': total,
            'missing': [{'key': g, 'label': info[g][0], 'tab': info[g][1], 'why': info[g][2]} for g in gaps]}


def report(students):
    """[(student, percent, [missing keys])] plus counts of each missing item."""
    students = list(students)
    if not students:
        return [], Counter(), 0
    sets = _sets(students[0].tenant_id, [s.id for s in students])
    total = _total(sets)
    rows, counts = [], Counter()
    for st in students:
        gaps = _missing(st, sets)
        counts.update(gaps)
        rows.append((st, round(100 * (total - len(gaps)) / total), gaps))
    return rows, counts, total
