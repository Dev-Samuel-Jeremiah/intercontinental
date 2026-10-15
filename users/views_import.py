"""
users/views_import.py
─────────────────────
Bulk registration of students and staff from Excel:
upload → preview (every row checked, nothing saved) → import → logins.

Students: administrators (the same people who can register a student).
Staff: whoever may create staff (superusers and staff with "can create staff").
"""
from datetime import datetime

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone

from . import bulk_import as bi
from .models import Class

XLSX = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
PREVIEW_HOURS = 2


def _allowed(user, kind):
    staff = user.staff
    if user.is_superuser:
        return True
    if staff is None:
        return False
    return staff.role == 'ADMIN' if kind == 'students' else staff.can_create_staff


def _guard(request, kind):
    if kind not in bi.COLUMNS:
        raise Http404
    if not _allowed(request.user, kind):
        messages.error(request, f'You do not have permission to register {kind}.')
        return redirect('dashboard:router')
    return None


def _labels(kind):
    return {'kind': kind, 'one': 'student' if kind == 'students' else 'staff member', 'many': kind,
            'title': 'Students' if kind == 'students' else 'Staff',
            'single_url': reverse('users:register_student' if kind == 'students' else 'users:create_staff'),
            'list_url': reverse('users:all_students') if kind == 'students' else reverse('hr:directory')}


def _validate(request, kind, rows, create_classes):
    if kind == 'students':
        return bi.validate_students(request.tenant, rows, create_classes)
    return bi.validate_staff(request.tenant, rows), []


@login_required
def import_page(request, kind):
    blocked = _guard(request, kind)
    if blocked:
        return blocked
    ctx = {**_labels(kind), 'columns': bi.COLUMNS[kind], 'max_rows': bi.MAX_ROWS[kind]}
    if kind == 'students':
        ctx['classes'] = Class.objects.filter(tenant=request.tenant).order_by('name')
    if request.method == 'POST':
        upload = request.FILES.get('file')
        create_classes = bool(request.POST.get('create_classes')) and kind == 'students'
        if not upload:
            messages.error(request, 'Choose the Excel file to upload.')
            return render(request, 'imports/upload.html', ctx)
        try:
            rows, unknown = bi.read_rows(upload, kind)
        except bi.ImportFileError as problem:
            messages.error(request, str(problem))
            return render(request, 'imports/upload.html', ctx)
        checks, new_classes = _validate(request, kind, rows, create_classes)
        request.session[f'import_{kind}'] = {'rows': rows, 'create_classes': create_classes, 'file': upload.name,
                                             'at': timezone.now().isoformat()}
        ready = [c for c in checks if c.ok]
        return render(request, 'imports/preview.html', {
            **ctx, 'checks': checks, 'ready': len(ready), 'failed': len(checks) - len(ready),
            'warned': sum(1 for c in ready if c.warnings), 'unknown': unknown, 'file_name': upload.name,
            'new_classes': new_classes, 'preview_columns': [label for _k, label in _preview_columns(kind)],
            'preview_rows': [(c, [_cell(c, key) for key, _l in _preview_columns(kind)]) for c in checks],
        })
    return render(request, 'imports/upload.html', ctx)


def _cell(check, key):
    """What to show for one column: the cleaned value when it read correctly (so fixes such as a restored
    leading 0 are visible), otherwise what was typed."""
    typed = check.raw.get(key, '')
    value = check.data.get(key)
    if key == 'admission_number' and not typed:
        return 'auto'
    if hasattr(value, 'strftime'):
        return value.strftime('%d/%m/%Y')
    if key == 'role' and value:
        return dict(bi.Staff.ROLE_CHOICES).get(value, value)
    return value if value not in (None, '') else typed


def _preview_columns(kind):
    if kind == 'students':
        return [('full_name', 'Name'), ('admission_number', 'Admission no.'), ('class_name', 'Class'),
                ('gender', 'Gender'), ('date_of_birth', 'Born'), ('parent_name', 'Parent'),
                ('parent_phone', 'Parent phone')]
    return [('full_name', 'Name'), ('role', 'Role'), ('username', 'Username'), ('email', 'Email'),
            ('phone', 'Phone'), ('section', 'Section')]


@login_required
def import_confirm(request, kind):
    blocked = _guard(request, kind)
    if blocked:
        return blocked
    if request.method != 'POST':
        return redirect(f'users:import_{kind}')
    pending = request.session.get(f'import_{kind}')
    stale = pending and (timezone.now() - datetime.fromisoformat(pending['at'])).total_seconds() > PREVIEW_HOURS * 3600
    if not pending or stale:
        messages.error(request, 'That preview has expired — upload the file again.')
        return redirect(f'users:import_{kind}')
    rows = [tuple(r) for r in pending['rows']]
    checks, new_classes = _validate(request, kind, rows, pending['create_classes'])   # re-checked against today's data
    if not any(c.ok for c in checks):
        messages.error(request, 'No rows are ready to import — fix the file and upload it again.')
        return redirect(f'users:import_{kind}')
    try:
        if kind == 'students':
            created = bi.import_students(request.tenant, checks, new_classes)
        else:
            created = bi.import_staff(request.tenant, checks)
    except bi.ImportFileError as problem:
        messages.error(request, str(problem))
        return redirect(f'users:import_{kind}')
    skipped = sum(1 for c in checks if not c.ok)
    del request.session[f'import_{kind}']
    request.session[f'import_{kind}_done'] = {'created': created, 'skipped': skipped, 'file': pending['file'],
                                              'new_classes': new_classes, 'at': timezone.now().isoformat()}
    messages.success(request, f'{len(created)} {kind if len(created) != 1 else _labels(kind)["one"]} registered.')
    return redirect(f'users:import_{kind}_done')


@login_required
def import_done(request, kind):
    blocked = _guard(request, kind)
    if blocked:
        return blocked
    done = request.session.get(f'import_{kind}_done')
    if not done:
        return redirect(f'users:import_{kind}')
    return render(request, 'imports/result.html', {**_labels(kind), **done, 'count': len(done['created'])})


@login_required
def import_template(request, kind):
    blocked = _guard(request, kind)
    if blocked:
        return blocked
    resp = HttpResponse(bi.template_workbook(kind, request.tenant), content_type=XLSX)
    resp['Content-Disposition'] = f'attachment; filename="{kind}-import-template.xlsx"'
    return resp


@login_required
def import_credentials(request, kind):
    blocked = _guard(request, kind)
    if blocked:
        return blocked
    done = request.session.get(f'import_{kind}_done')
    if not done:
        messages.error(request, 'Those logins are no longer available — they are only kept until you sign out.')
        return redirect(f'users:import_{kind}')
    resp = HttpResponse(bi.credentials_workbook(kind, done['created'], request.tenant), content_type=XLSX)
    resp['Content-Disposition'] = f'attachment; filename="new-{kind}-logins-{timezone.localdate():%Y%m%d}.xlsx"'
    resp['Cache-Control'] = 'no-store'
    return resp
