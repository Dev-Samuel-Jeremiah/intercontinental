"""
users/views_profile.py
──────────────────────
The 360° student profile, changing a student's status, and the alumni
register of graduates.

Any staff member can open a profile. Changing status, and seeing the change
history, is for school administrators; fees are shown to administrators and
the accounts office.
"""
import csv
from datetime import date

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.text import slugify

from academics.utils import get_active_session, get_active_term

from . import profile as student_profile
from .lifecycle import StatusError, change_status
from .models import ENROLLED_STATUSES, STATUS_OPTIONS, Student, mask_nin


def _role(user):
    staff = getattr(user, 'staff', None)
    return getattr(staff, 'role', None)


def can_view(user):
    return user.is_authenticated and (user.is_superuser or getattr(user, 'is_staff_user', False))


def can_manage(user):
    return user.is_authenticated and (user.is_superuser or _role(user) == 'ADMIN')


def can_see_fees(user):
    return user.is_authenticated and (user.is_superuser or _role(user) in ('ADMIN', 'ACCOUNT', 'PROPRIETOR'))


def _date(raw):
    try:
        return date.fromisoformat(raw) if raw else None
    except ValueError:
        return None


@login_required
def student_profile_view(request, student_id):
    if not can_view(request.user):
        messages.error(request, 'Only staff can open student profiles.')
        return redirect('dashboard:router')
    student = get_object_or_404(Student.objects.select_related('class_assigned', 'current_session',
                                                               'user', 'house'),
                                pk=student_id)
    session, term = get_active_session(), get_active_term()
    fees_allowed = can_see_fees(request.user)
    manage = can_manage(request.user)
    from . import completeness, history
    spells = history.history(student)
    health = student_profile.health(student, request.user)
    timeline = student_profile.timeline(student, include_fees=fees_allowed,
                                        include_health=bool(health and health['nurse']), include_documents=manage)

    return render(request, 'students/profile.html', {
        'student': student,
        'can_manage': manage, 'can_see_fees': fees_allowed,
        'siblings': student_profile.siblings(student),
        'results': student_profile.results_history(student),
        'attendance': student_profile.attendance_summary(student, term),
        'fees': student_profile.fees_summary(student, session, term) if fees_allowed else None,
        'timeline': timeline,
        'class_history': spells, 'sections_attended': history.sections_attended(spells),
        'section': history.section_of(student.class_assigned),
        'completeness': completeness.check(student),
        'health': health,
        'ai_outlook': _ai_outlook(request, student),
        'status_history': student.status_changes.select_related('changed_by')[:20],
        'changes': student_profile.change_history(student) if manage else [],
        'statuses': [s for s in STATUS_OPTIONS if s[0] != student.status],
        'session': session, 'term': term, 'today': timezone.localdate(),
        'guardians': student.guardians.all(),
        'relationships': Guardian.RELATIONSHIPS, 'contact_choices': Guardian.CONTACT_CHOICES,
        'documents': student.documents.select_related('uploaded_by') if manage else None,
        'document_types': StudentDocument.TYPES,
        'answers': records.answers(student, include_sensitive=manage),
        'nin_display': student.nin if manage else mask_nin(student.nin),
        'awards': student.awards.select_related('recorded_by'),
        'roles': student.roles.select_related('appointed_by'),
        'award_categories': Award.CATEGORIES, 'award_levels': Award.LEVELS,
        'role_kinds': LeadershipRole.KINDS,
        'support': SupportNeeds.objects.filter(student=student).first(),
        'extra_time_choices': SupportNeeds.EXTRA_TIME,
        'pending_corrections': student.change_requests.filter(status='PENDING').count(),
        'exports': student.data_exports.select_related('exported_by')[:10] if manage else [],
        **_attendance_context(student, term),
    })


def _ai_outlook(request, student):
    """The school AI's view of this student — administrators and the student's form teacher only."""
    try:
        from schoolai.models import Prediction
    except Exception:
        return None
    user = request.user
    form_teacher = bool(student.class_assigned and student.class_assigned.has_form_teacher(user))
    if not (can_manage(user) or form_teacher):
        return None
    preds = {p.kind: p for p in Prediction.objects.filter(tenant_id=student.tenant_id, student=student)}
    return preds or None


def _attendance_context(student, term):
    from . import attendance as att
    if not term or not term.start_date:
        return {'heat_weeks': [], 'term_counts': None}
    end = min(term.end_date or timezone.localdate(), timezone.localdate())
    return {'heat_weeks': att.student_heatmap(student, term.start_date, end),
            'term_counts': att.term_counts(student, term)}


@login_required
def change_student_status(request, student_id):
    if request.method != 'POST':
        return redirect('users:student_profile', student_id=student_id)
    if not can_manage(request.user):
        messages.error(request, "Only a school administrator can change a student's status.")
        return redirect('users:student_profile', student_id=student_id)
    student = get_object_or_404(Student, pk=student_id)
    if request.POST.get('status') in ('Withdrawn', 'Graduated') and not request.POST.get('library_override'):
        from library.services import clearance_problem
        problem = clearance_problem(student)
        if problem:
            messages.error(request, f'Library clearance: {student.full_name} has {problem}. Clear it at the library, '
                                    f'or tick "Leave anyway" to record the change regardless.')
            return redirect('users:student_profile', student_id=student_id)
    try:
        change_status(student, request.POST.get('status') or '',
                      reason=request.POST.get('reason') or '',
                      effective_on=_date(request.POST.get('effective_on')),
                      until=_date(request.POST.get('until')),
                      destination=request.POST.get('destination') or '',
                      user=request.user)
    except StatusError as problem:
        messages.error(request, str(problem))
    else:
        note = {
            'Active': 'is back on the roll and can sign in again',
            'Suspended': 'is suspended. They stay on the roll',
            'Withdrawn': 'has been withdrawn. Their record and history are kept',
            'Graduated': 'has graduated and moved to the alumni register',
            'Deceased': 'has been recorded as deceased. Their record is kept',
        }[student.status]
        messages.success(request, f'{student.full_name} {note}.')
    return redirect('users:student_profile', student_id=student_id)


def _alumni(request):
    rows = Student.objects.filter(status='Graduated').order_by('-left_on', 'full_name')
    year = request.GET.get('year') or ''
    if year.isdigit():
        rows = rows.filter(left_on__year=int(year))
    q = (request.GET.get('q') or '').strip()
    if q:
        rows = rows.filter(Q(full_name__icontains=q) | Q(admission_number__icontains=q) |
                           Q(destination__icontains=q) | Q(leaving_class__icontains=q))
    return rows, year, q


@login_required
def alumni_register(request):
    if not can_view(request.user):
        messages.error(request, 'Only staff can open the alumni register.')
        return redirect('dashboard:router')
    rows, year, q = _alumni(request)
    years = sorted({d.year for d in Student.objects.filter(status='Graduated')
                    .exclude(left_on=None).values_list('left_on', flat=True)}, reverse=True)
    return render(request, 'students/alumni.html', {
        'alumni': rows[:500], 'count': rows.count(), 'years': years, 'year': year, 'q': q,
        'left_counts': {s: Student.objects.filter(status=s).count()
                        for s in ('Graduated', 'Withdrawn')},
        'can_manage': can_manage(request.user),
    })


@login_required
def alumni_export(request):
    if not can_manage(request.user):
        messages.error(request, 'Only a school administrator can download the alumni list.')
        return redirect('users:alumni_register')
    rows, year, _ = _alumni(request)
    response = HttpResponse(content_type='text/csv; charset=utf-8')
    school = slugify(getattr(request.tenant, 'name', '') or 'school')[:40]
    response['Content-Disposition'] = \
        f'attachment; filename="{school}-alumni{"-" + year if year else ""}.csv"'
    writer = csv.writer(response)
    writer.writerow(['Name', 'Admission number', 'Gender', 'Graduated on', 'Final class',
                     'Destination', 'Parent name', 'Parent phone', 'Parent email'])
    for s in rows:
        writer.writerow([s.full_name, s.admission_number, s.gender or '',
                         s.left_on.isoformat() if s.left_on else '', s.leaving_class,
                         s.destination, s.parent_name or '', s.parent_phone or '',
                         s.parent_email or ''])
    return response


# ─────────────────────────────────────────────────────────────────────────────
# Guardians, documents, extra details and NIN
# ─────────────────────────────────────────────────────────────────────────────

from django.http import FileResponse, Http404  # noqa: E402

from . import records  # noqa: E402
from .models import (Award, Guardian, LeadershipRole, StudentDocument,  # noqa: E402
                     StudentField, SupportNeeds)


def _back_to(student, tab):
    from django.urls import reverse
    return redirect(reverse('users:student_profile', args=[student.id]) + f'?tab={tab}#{tab}')


def _guardian_from(post, guardian):
    guardian.full_name = (post.get('full_name') or '').strip()[:160]
    rel = post.get('relationship')
    guardian.relationship = rel if rel in dict(Guardian.RELATIONSHIPS) else 'GUARDIAN'
    guardian.phone = (post.get('phone') or '').strip()[:20]
    guardian.email = (post.get('email') or '').strip()[:254]
    guardian.address = (post.get('address') or '').strip()[:255]
    guardian.occupation = (post.get('occupation') or '').strip()[:120]
    contact = post.get('contact_by')
    guardian.contact_by = contact if contact in dict(Guardian.CONTACT_CHOICES) else 'BOTH'
    guardian.gets_fee_messages = bool(post.get('gets_fee_messages'))
    guardian.can_pick_up = bool(post.get('can_pick_up'))
    guardian.emergency_contact = bool(post.get('emergency_contact'))
    guardian.note = (post.get('note') or '').strip()[:255]
    guardian.nin = records.clean_nin(post.get('nin'))
    if not guardian.full_name:
        raise records.RecordError("Enter the guardian's name.")
    if not guardian.phone and not guardian.email:
        raise records.RecordError('Give a phone number or an email, so the school can reach them.')
    if guardian.email:
        from django.core.validators import validate_email
        from django.core.exceptions import ValidationError
        try:
            validate_email(guardian.email)
        except ValidationError:
            raise records.RecordError('That email address does not look right.')
    return guardian


def _guardian_photo(request, guardian):
    """Save an uploaded photo, cropped to the same shape as student photos."""
    upload = request.FILES.get('photo')
    if not upload:
        return
    from .roster import RosterError, prepare_passport
    try:
        content, _warnings = prepare_passport(upload)
    except RosterError as problem:
        raise records.RecordError(f'Photo not saved: {problem}')
    old = guardian.photo.name if guardian.photo else None
    guardian.photo.save(f'guardian-{guardian.id}.jpg', content, save=True)
    if old and old != guardian.photo.name:
        guardian.photo.storage.delete(old)


def _guardian_summary(g):
    flags = [label for flag, label in (('emergency_contact', 'emergency contact'), ('can_pick_up', 'may collect'))
             if getattr(g, flag)]
    return ' · '.join(x for x in (f'{g.full_name} ({g.get_relationship_display()})', g.phone, g.email, ', '.join(flags)) if x)


def _log_guardian(request, student, before, guardian):
    """Guardians are part of the record's history: who was added, changed or removed, by whom."""
    from users.models import StudentChange
    after = _guardian_summary(guardian) if guardian else ''
    if before != after:
        StudentChange.objects.create(tenant_id=student.tenant_id, student=student, field='guardian', label='Guardian',
                                     old_value=before, new_value=after, changed_by=request.user, source='Guardians')


@login_required
def student_guardians(request, student_id):
    student = get_object_or_404(Student, pk=student_id)
    if request.method != 'POST' or not can_manage(request.user):
        if request.method == 'POST':
            messages.error(request, 'Only a school administrator can change guardians.')
        return _back_to(student, 'overview')
    action = request.POST.get('action')
    try:
        if action == 'add':
            g = _guardian_from(request.POST, Guardian(tenant_id=student.tenant_id, student=student))
            g.save()
            _guardian_photo(request, g)
            _log_guardian(request, student, '', g)
            messages.success(request, f'{g.full_name} added as {g.get_relationship_display().lower()}.')
        elif action == 'edit':
            g = get_object_or_404(Guardian, pk=request.POST.get('guardian_id'), student=student)
            before = _guardian_summary(g)
            _guardian_from(request.POST, g).save()
            _guardian_photo(request, g)
            _log_guardian(request, student, before, g)
            messages.success(request, f'{g.full_name} updated.')
        elif action == 'delete':
            g = get_object_or_404(Guardian, pk=request.POST.get('guardian_id'), student=student)
            name, before = g.full_name, _guardian_summary(g)
            g.delete()
            _log_guardian(request, student, before, None)
            messages.success(request, f'{name} removed.')
    except records.RecordError as problem:
        messages.error(request, str(problem))
    return _back_to(student, 'overview')


@login_required
def student_documents(request, student_id):
    student = get_object_or_404(Student, pk=student_id)
    if request.method != 'POST' or not can_manage(request.user):
        if request.method == 'POST':
            messages.error(request, 'Only a school administrator can manage documents.')
        return _back_to(student, 'documents')
    action = request.POST.get('action')
    if action == 'upload':
        upload = request.FILES.get('file')
        try:
            records.check_upload(upload)
            doc_type = request.POST.get('doc_type')
            expires = _date(request.POST.get('expires_on'))
            issued = _date(request.POST.get('issued_on'))
            if issued and expires and expires <= issued:
                raise records.RecordError('The expiry date must be after the issue date.')
            doc = StudentDocument(
                tenant_id=student.tenant_id, student=student,
                doc_type=doc_type if doc_type in dict(StudentDocument.TYPES) else 'OTHER',
                title=(request.POST.get('title') or '').strip()[:160],
                original_name=(upload.name or '')[:255], size=upload.size,
                issued_on=issued, expires_on=expires,
                note=(request.POST.get('note') or '').strip()[:255], uploaded_by=request.user)
            doc.file.save(upload.name, upload, save=False)
            doc.save()
            messages.success(request, f'{doc.display_name} uploaded.')
        except records.RecordError as problem:
            messages.error(request, str(problem))
    elif action == 'delete':
        doc = get_object_or_404(StudentDocument, pk=request.POST.get('document_id'), student=student)
        name = doc.display_name
        doc.file.delete(save=False)
        doc.delete()
        messages.success(request, f'{name} deleted.')
    return _back_to(student, 'documents')


@login_required
def document_file(request, document_id):
    """Serve a document to an administrator. There is no public URL for these files."""
    if not can_manage(request.user):
        raise Http404
    doc = get_object_or_404(StudentDocument, pk=document_id)   # this school's only
    try:
        handle = doc.file.open('rb')
    except (FileNotFoundError, OSError):
        raise Http404('The file is missing from storage.')
    import os
    ext = os.path.splitext(doc.file.name)[1].lower()
    name = slugify(f'{doc.student.admission_number}-{doc.display_name}')[:80] + ext
    response = FileResponse(handle, content_type=records.CONTENT_TYPES.get(ext, 'application/octet-stream'),
                            as_attachment=request.GET.get('download') == '1', filename=name)
    # Never cached by a shared computer's browser or a proxy.
    response['Cache-Control'] = 'private, no-store'
    response['X-Content-Type-Options'] = 'nosniff'
    return response


@login_required
def student_details(request, student_id):
    """Save the school's extra fields and the NIN."""
    student = get_object_or_404(Student, pk=student_id)
    if request.method != 'POST' or not can_manage(request.user):
        if request.method == 'POST':
            messages.error(request, 'Only a school administrator can change these details.')
        return _back_to(student, 'details')
    try:
        nin = records.clean_nin(request.POST.get('nin'))
        other = records.nin_taken_by(student, nin)
        if other:
            raise records.RecordError(f'That NIN is already recorded for {other.full_name} '
                                      f'({other.admission_number}).')
        changed = records.save_answers(student, request.POST, request.user)
        if nin != student.nin:
            student.nin = nin
            from .audit import audit_source
            with audit_source('More details', request.user):
                student.save(update_fields=['nin'])
            changed += 1
        messages.success(request, 'Details saved.' if changed else 'Nothing changed.')
    except records.RecordError as problem:
        messages.error(request, str(problem))
    return _back_to(student, 'details')


@login_required
def student_fields(request):
    """The school's own extra questions on every student record."""
    if not can_manage(request.user):
        messages.error(request, 'Only a school administrator can set up student fields.')
        return redirect('dashboard:router')
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'suggested':
            n = records.add_suggested_fields(request.tenant)
            messages.success(request, f'{n} field(s) added.' if n else 'You already have them all.')
        elif action == 'add':
            label = (request.POST.get('label') or '').strip()[:80]
            ftype = request.POST.get('field_type')
            choices = (request.POST.get('choices') or '').strip()
            if not label:
                messages.error(request, 'Give the field a name.')
            elif StudentField.objects.filter(label__iexact=label).exists():
                messages.error(request, f'There is already a field called "{label}".')
            elif ftype == StudentField.CHOICE and len([c for c in choices.splitlines() if c.strip()]) < 2:
                messages.error(request, 'A list needs at least two options, one per line.')
            else:
                StudentField.objects.create(
                    tenant=request.tenant, label=label,
                    key=records.unique_key(request.tenant, label),
                    field_type=ftype if ftype in dict(StudentField.TYPES) else StudentField.TEXT,
                    choices=choices, help_text=(request.POST.get('help_text') or '').strip()[:160],
                    required=bool(request.POST.get('required')),
                    sensitive=bool(request.POST.get('sensitive')),
                    order=(StudentField.objects.count() + 1) * 10)
                messages.success(request, f'"{label}" added to every student record.')
        elif action == 'update':
            f = get_object_or_404(StudentField, pk=request.POST.get('field_id'))
            f.is_active = bool(request.POST.get('is_active'))
            f.required = bool(request.POST.get('required'))
            f.sensitive = bool(request.POST.get('sensitive'))
            try:
                f.order = max(0, int(request.POST.get('order') or f.order))
            except ValueError:
                pass
            f.save(update_fields=['is_active', 'required', 'sensitive', 'order'])
            messages.success(request, f'"{f.label}" saved.')
        return redirect('users:student_fields')

    from django.db.models import Count
    fields = StudentField.objects.annotate(answered=Count('values')).order_by('order', 'label')
    return render(request, 'students/fields.html', {
        'fields': fields, 'types': StudentField.TYPES,
        'students': Student.objects.filter(status__in=ENROLLED_STATUSES).count(),
    })


@login_required
def expiring_documents(request):
    if not can_manage(request.user):
        messages.error(request, 'Only a school administrator can see student documents.')
        return redirect('dashboard:router')
    try:
        days = max(1, min(365, int(request.GET.get('days') or 30)))
    except ValueError:
        days = 30
    docs = list(records.expiring_documents(request.tenant, days))
    return render(request, 'students/expiring_documents.html', {
        'expired': [d for d in docs if d.is_expired],
        'soon': [d for d in docs if not d.is_expired], 'days': days,
    })
