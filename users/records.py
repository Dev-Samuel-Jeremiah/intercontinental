"""
users/records.py
────────────────
The rules behind a student's wider record: guardians and who gets messages,
NIN checks, uploaded documents, and the extra fields a school adds itself.
"""
import re
from datetime import date, timedelta

from django.utils import timezone
from django.utils.text import slugify

from users.models import Guardian, StudentDocument, StudentField, StudentFieldValue


class RecordError(ValueError):
    """Something the user entered cannot be saved; the message says why."""


# ─────────────────────────────────────────────────────────────────────────────
# NIN
# ─────────────────────────────────────────────────────────────────────────────

def clean_nin(raw):
    """'123 4567 8901' → '12345678901'. Blank stays blank. Anything else must be 11 digits."""
    digits = re.sub(r'[\s-]', '', raw or '')
    if not digits:
        return ''
    if not digits.isdigit() or len(digits) != 11:
        raise RecordError('A NIN is 11 digits.')
    return digits


def nin_taken_by(student, nin):
    """Another student at this school already recorded with this NIN, if any."""
    from users.models import Student
    if not nin:
        return None
    return (Student.objects.filter(tenant_id=student.tenant_id, nin=nin)
            .exclude(pk=student.pk).first())


# ─────────────────────────────────────────────────────────────────────────────
# Who a message reaches
# ─────────────────────────────────────────────────────────────────────────────

def _phone_key(phone):
    digits = re.sub(r'\D', '', phone or '')
    return digits[-10:] if len(digits) >= 10 else digits


def contacts(student, channel, fees=False):
    """
    Everyone the school should message about this student on a channel
    ('SMS' or 'EMAIL'): the main contact on the record first, then each
    guardian who has agreed to that channel — for fee messages, only the
    guardians marked to receive them. Nobody is messaged twice.
    """
    out, seen = [], set()

    def add(name, address):
        if not address:
            return
        key = _phone_key(address) if channel == 'SMS' else address.strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append((name or '', address.strip()))

    add(student.parent_name, student.parent_phone if channel == 'SMS' else student.parent_email)
    for g in Guardian.objects.filter(tenant_id=student.tenant_id, student=student):
        if not g.wants(channel) or (fees and not g.gets_fee_messages):
            continue
        add(g.full_name, g.phone if channel == 'SMS' else g.email)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Documents
# ─────────────────────────────────────────────────────────────────────────────

MAX_DOCUMENT_BYTES = 10 * 1024 * 1024
# The file must really be what its name says, not just be named that way.
_SIGNATURES = {
    '.pdf': [b'%PDF'],
    '.jpg': [b'\xff\xd8\xff'], '.jpeg': [b'\xff\xd8\xff'],
    '.png': [b'\x89PNG\r\n\x1a\n'],
    '.webp': [b'RIFF'],
}
CONTENT_TYPES = {'.pdf': 'application/pdf', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
                 '.png': 'image/png', '.webp': 'image/webp'}


def check_upload(upload):
    """Return the lower-case extension, or raise RecordError explaining what is wrong."""
    import os
    if not upload:
        raise RecordError('Choose the file to upload.')
    ext = os.path.splitext(upload.name or '')[1].lower()
    if ext not in _SIGNATURES:
        raise RecordError('Upload a PDF or a photo (JPG, PNG or WEBP).')
    if upload.size > MAX_DOCUMENT_BYTES:
        raise RecordError('The file is larger than 10 MB.')
    head = upload.read(12)
    upload.seek(0)
    if not any(head.startswith(sig) for sig in _SIGNATURES[ext]) or \
            (ext == '.webp' and head[8:12] != b'WEBP'):
        raise RecordError("That file isn't a real PDF or image — it may be damaged or renamed.")
    return ext


def expiring_documents(tenant, days=30):
    """Documents already expired or expiring within `days`, soonest first."""
    limit = timezone.localdate() + timedelta(days=days)
    return (StudentDocument.objects.filter(tenant=tenant, expires_on__isnull=False,
                                           expires_on__lte=limit,
                                           student__status__in=('Active', 'Suspended'))
            .select_related('student', 'student__class_assigned').order_by('expires_on'))


# ─────────────────────────────────────────────────────────────────────────────
# Custom fields
# ─────────────────────────────────────────────────────────────────────────────

NIGERIAN_STATES = [
    'Abia', 'Adamawa', 'Akwa Ibom', 'Anambra', 'Bauchi', 'Bayelsa', 'Benue', 'Borno',
    'Cross River', 'Delta', 'Ebonyi', 'Edo', 'Ekiti', 'Enugu', 'FCT (Abuja)', 'Gombe', 'Imo',
    'Jigawa', 'Kaduna', 'Kano', 'Katsina', 'Kebbi', 'Kogi', 'Kwara', 'Lagos', 'Nasarawa',
    'Niger', 'Ogun', 'Ondo', 'Osun', 'Oyo', 'Plateau', 'Rivers', 'Sokoto', 'Taraba', 'Yobe',
    'Zamfara', 'Not Nigerian',
]

# Fields many Nigerian schools ask for, offered with one click.
SUGGESTED_FIELDS = [
    {'label': 'State of origin', 'field_type': 'CHOICE', 'choices': '\n'.join(NIGERIAN_STATES)},
    {'label': 'LGA', 'field_type': 'TEXT', 'help_text': 'Local government area of origin'},
    {'label': 'Religion', 'field_type': 'CHOICE', 'sensitive': True,
     'choices': 'Christianity\nIslam\nTraditional\nOther\nPrefer not to say',
     'help_text': 'Optional — only administrators can see it'},
    {'label': 'Blood group', 'field_type': 'CHOICE',
     'choices': 'A+\nA−\nB+\nB−\nAB+\nAB−\nO+\nO−'},
    {'label': 'Genotype', 'field_type': 'CHOICE', 'sensitive': True,
     'choices': 'AA\nAS\nAC\nSS\nSC\nCC'},
    {'label': 'Allergies', 'field_type': 'TEXT', 'help_text': 'Food, medicine or other'},
    {'label': 'Medical conditions', 'field_type': 'TEXT', 'sensitive': True,
     'help_text': 'Asthma, sickle cell, epilepsy… — only administrators can see it'},
]


def unique_key(tenant, label):
    base = slugify(label)[:50] or 'field'
    key, n = base, 2
    while StudentField.objects.filter(tenant=tenant, key=key).exists():
        key, n = f'{base}-{n}', n + 1
    return key


def add_suggested_fields(tenant):
    """Create any suggested fields the school does not have yet. Returns how many."""
    existing = {f.lower() for f in StudentField.objects.filter(tenant=tenant)
                .values_list('label', flat=True)}
    order = (StudentField.objects.filter(tenant=tenant).count() + 1) * 10
    created = 0
    for spec in SUGGESTED_FIELDS:
        if spec['label'].lower() in existing:
            continue
        StudentField.objects.create(tenant=tenant, key=unique_key(tenant, spec['label']),
                                    order=order, **spec)
        order += 10
        created += 1
    return created


def clean_value(field, raw):
    """Normalise an answer for a field, or raise RecordError."""
    value = (raw or '').strip()
    if not value:
        if field.required:
            raise RecordError(f'{field.label} is required.')
        return ''
    if field.field_type == StudentField.NUMBER:
        try:
            float(value.replace(',', ''))
        except ValueError:
            raise RecordError(f'{field.label} must be a number.')
        return value.replace(',', '')
    if field.field_type == StudentField.DATE:
        try:
            return date.fromisoformat(value).isoformat()
        except ValueError:
            raise RecordError(f'{field.label} must be a date.')
    if field.field_type == StudentField.CHOICE:
        if value not in field.options:
            raise RecordError(f'Choose one of the options for {field.label}.')
        return value
    if field.field_type == StudentField.YES_NO:
        if value not in ('Yes', 'No'):
            raise RecordError(f'Answer yes or no for {field.label}.')
        return value
    return value[:2000]


def answers(student, include_sensitive):
    """[(field, value)] for the active fields, in the school's order."""
    values = {v.field_id: v.value for v in StudentFieldValue.objects.filter(
        tenant_id=student.tenant_id, student=student)}
    fields = StudentField.objects.filter(tenant_id=student.tenant_id, is_active=True)
    if not include_sensitive:
        fields = fields.exclude(sensitive=True)
    return [(f, values.get(f.id, '')) for f in fields]


def save_answers(student, data, user=None):
    """
    Save answers from a form (`field_<id>` → value). All are checked before
    any is saved, so one bad answer does not leave the record half updated.
    """
    from users.models import StudentChange
    fields = list(StudentField.objects.filter(tenant_id=student.tenant_id, is_active=True))
    cleaned, problems = {}, []
    for f in fields:
        if f'field_{f.id}' not in data:
            continue
        try:
            cleaned[f] = clean_value(f, data.get(f'field_{f.id}'))
        except RecordError as e:
            problems.append(str(e))
    if problems:
        raise RecordError(' '.join(problems))
    current = {v.field_id: v for v in StudentFieldValue.objects.filter(
        tenant_id=student.tenant_id, student=student)}
    changed = 0
    for f, value in cleaned.items():
        row = current.get(f.id)
        old = row.value if row else ''
        if old == value:
            continue
        if row:
            row.value = value
            row.save(update_fields=['value'])
        else:
            StudentFieldValue.objects.create(tenant_id=student.tenant_id, student=student,
                                             field=f, value=value)
        StudentChange.objects.create(
            tenant_id=student.tenant_id, student=student, field=f'custom:{f.key}',
            label=f.label, old_value='(hidden)' if f.sensitive and old else old,
            new_value='(hidden)' if f.sensitive and value else value,
            changed_by=user, source='More details')
        changed += 1
    return changed
