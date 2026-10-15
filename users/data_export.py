"""
users/data_export.py
────────────────────
Everything the school holds about one child, in one ZIP file — for a parent
exercising their right of access under the Nigeria Data Protection Act, or
for a school handing records to the child's next school.

The archive holds a readable summary, the full record as JSON, CSV sheets
for results, attendance, payments and messages, and the uploaded documents
and photo. Every export is logged with who made it and why.
"""
import csv
import io
import json
import os
import zipfile

from django.utils import timezone

from users.models import (Attendance, Award, DataExport, Guardian, LeadershipRole,
                          ProfileChangeRequest, StudentChange, StudentDocument,
                          StudentStatusChange, SupportNeeds)


def _csv(rows, headers):
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(headers)
    w.writerows(rows)
    return out.getvalue()


def _iso(v):
    return v.isoformat() if hasattr(v, 'isoformat') else ('' if v is None else v)


def build(student, user, reason):
    """Return (filename, bytes) and log the export."""
    from users import records
    t = student.tenant_id

    record = {
        'student': {
            'full_name': student.full_name, 'admission_number': student.admission_number,
            'gender': student.gender, 'date_of_birth': _iso(student.date_of_birth),
            'class': getattr(student.class_assigned, 'name', None),
            'house': getattr(student.house, 'name', None),
            'status': student.status, 'status_reason': student.status_reason,
            'left_on': _iso(student.left_on), 'destination': student.destination,
            'address': student.address, 'nin': student.nin,
            'enrolled_on': _iso(student.created_at),
            'main_contact': {'name': student.parent_name, 'phone': student.parent_phone,
                             'email': student.parent_email},
        },
        'guardians': [{'name': g.full_name, 'relationship': g.get_relationship_display(),
                       'phone': g.phone, 'email': g.email, 'address': g.address,
                       'occupation': g.occupation, 'nin': g.nin,
                       'messages': g.get_contact_by_display(),
                       'may_collect': g.can_pick_up, 'emergency_contact': g.emergency_contact}
                      for g in Guardian.objects.filter(tenant_id=t, student=student)],
        'more_details': {f.label: v for f, v in records.answers(student, include_sensitive=True)},
        'support_needs': None,
        'awards': [{'title': a.title, 'category': a.get_category_display(),
                    'level': a.get_level_display(), 'position': a.position,
                    'date': _iso(a.awarded_on), 'description': a.description}
                   for a in Award.objects.filter(tenant_id=t, student=student)],
        'leadership_roles': [{'title': r.title, 'kind': r.get_kind_display(), 'group': r.group,
                              'from': _iso(r.start_date), 'to': _iso(r.end_date)}
                             for r in LeadershipRole.objects.filter(tenant_id=t, student=student)],
        'status_history': [{'from': c.from_status, 'to': c.to_status, 'on': _iso(c.effective_on),
                            'reason': c.reason}
                           for c in StudentStatusChange.objects.filter(tenant_id=t, student=student)],
        'change_history': [{'when': _iso(c.changed_at), 'what': c.label, 'before': c.old_value,
                            'after': c.new_value, 'source': c.source}
                           for c in StudentChange.objects.filter(tenant_id=t, student=student)],
        'correction_requests': [{'what': r.get_field_display(), 'asked_for': r.new_value,
                                 'status': r.get_status_display(), 'on': _iso(r.created_at)}
                                for r in ProfileChangeRequest.objects.filter(tenant_id=t, student=student)],
        'exported_at': timezone.now().isoformat(),
    }
    support = SupportNeeds.objects.filter(tenant_id=t, student=student).first()
    if support:
        record['support_needs'] = {'adjustments': support.adjustments,
                                   'classroom_notes': support.classroom_notes,
                                   'private_notes': support.private_notes,
                                   'review_on': _iso(support.review_on)}

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('record.json', json.dumps(record, indent=2, ensure_ascii=False, default=str))

        try:
            from results.models import TermResult
            rows = [(r.session.name if r.session else '', r.term.name if r.term else '',
                     r.subject.name, r.ca1_score, r.ca2_score, r.ca3_score, r.exam_score,
                     r.total_score, r.grade or '', r.remark or '')
                    for r in TermResult.objects.filter(tenant_id=t, student=student)
                    .select_related('session', 'term', 'subject')]
            z.writestr('results.csv', _csv(rows, ['Session', 'Term', 'Subject', 'CA1', 'CA2',
                                                  'CA3', 'Exam', 'Total', 'Grade', 'Remark']))
        except Exception:
            pass

        rows = [(a.date.isoformat(), 'Present' if a.status == 'P' else 'Absent',
                 a.subject.name if a.subject else '', a.remarks or '')
                for a in Attendance.objects.filter(tenant_id=t, student=student).select_related('subject')]
        z.writestr('attendance.csv', _csv(rows, ['Date', 'Mark', 'Subject', 'Remarks']))

        try:
            from users.profile import family_wallet
            from finance.models import Payment
            wallet = family_wallet(student)
            rows = []
            if wallet:
                rows = [(p.created_at.date().isoformat(), p.reference, p.get_method_display(),
                         f'{p.amount:.2f}', p.get_status_display())
                        for p in Payment.objects.filter(tenant_id=t, wallet=wallet)]
            z.writestr('payments.csv', _csv(rows, ['Date', 'Reference', 'Method', 'Amount (NGN)',
                                                   'Status']))
        except Exception:
            pass

        try:
            from communications.models import MessageLog
            rows = [(m.created_at.isoformat() if getattr(m, 'created_at', None) else '',
                     m.channel, m.recipient, m.subject or '', m.status)
                    for m in MessageLog.objects.filter(student=student)]
            z.writestr('messages_sent.csv', _csv(rows, ['When', 'Channel', 'To', 'Subject', 'Status']))
        except Exception:
            pass

        if student.passport:
            try:
                with student.passport.open('rb') as f:
                    z.writestr('photo' + os.path.splitext(student.passport.name)[1].lower(), f.read())
            except OSError:
                pass
        for d in StudentDocument.objects.filter(tenant_id=t, student=student):
            try:
                with d.file.open('rb') as f:
                    ext = os.path.splitext(d.file.name)[1].lower()
                    z.writestr(f'documents/{d.id}-{d.get_doc_type_display()}{ext}', f.read())
            except OSError:
                pass

        z.writestr('README.txt', (
            f"Records held by {student.tenant.name} about {student.full_name} "
            f"({student.admission_number}).\n"
            f"Exported {timezone.localtime():%d %B %Y %H:%M}.\n\n"
            "record.json        - personal details, guardians, extra details, support needs,\n"
            "                     awards, roles, status and change history\n"
            "results.csv        - every subject score recorded\n"
            "attendance.csv     - every register mark\n"
            "payments.csv       - payments into the family wallet\n"
            "messages_sent.csv  - messages the school sent about the child\n"
            "documents/         - uploaded certificates and forms\n"))

    DataExport.objects.create(tenant_id=t, student=student, exported_by=user, reason=reason[:255])
    safe = ''.join(ch if ch.isalnum() or ch in '-_' else '-' for ch in student.admission_number)
    return f'{safe}-records-{timezone.localdate():%Y%m%d}.zip', buf.getvalue()
