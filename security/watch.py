"""
security/watch.py
─────────────────
Field-level audit of the changes a school most needs to be able to trace:
scores and results approval, payments, fees and discounts, refunds, exams,
terms, the assessment structure and school settings.

Each watched model is compared before and after every save; when a watched
field changes, one audit event records who changed what, from what, to what
(and creations of money and approval records are recorded too). Auditing
never blocks the save — a failure is only logged.
"""
import logging

from django.db.models.signals import post_save, pre_save

log = logging.getLogger(__name__)

ALL = '__all__'
SKIP = {'id', 'tenant', 'created_at', 'updated_at', 'total_score', 'grade', 'remark', 'raw_exam_score'}


def _student_subject_term(o):
    return f'{o.student.full_name} · {o.subject.name} · {o.term.name}'


# model label → (fields, audit action, how to name the object, record creations?)
WATCHED = {
    'results.TermResult': (['ca1_score', 'ca2_score', 'ca3_score', 'essay_score', 'exam_score', 'is_not_offering',
                            'published'], 'SCORES', _student_subject_term, False),
    'results.SubjectSheet': (['status'], 'APPROVAL', lambda o: f'{o.subject.name} · {o.school_class.name} · {o.term.name}', False),
    'results.ClassApproval': ([], 'APPROVAL', lambda o: f'{o.school_class.name} results for {o.term.name} approved', True),
    'results.AssessmentScheme': (ALL, 'SETTINGS', lambda o: 'Assessment structure', True),
    'results.ScoreLock': (['locks_on', 'reopened_until', 'reopen_reason'], 'SETTINGS',
                          lambda o: f'Score-entry deadline for {o.term.name}', True),
    'results.Grading': (ALL, 'SETTINGS', lambda o: f'Grade {o}', True),
    'finance.Payment': (['status', 'amount'], 'PAYMENT', lambda o: f'Payment {o.reference} (₦{o.amount:,.2f})', True),
    'finance.Refund': (['status', 'amount'], 'PAYMENT', lambda o: f'Refund ₦{o.amount:,.2f}', True),
    'finance.FeeStructure': (ALL, 'FEES', lambda o: f'Fee "{o.name}"', True),
    'finance.FeeDiscount': (ALL, 'FEES', lambda o: f'Discount {o}', True),
    'finance.LateFeeRule': (ALL, 'FEES', lambda o: f'Late fee rule "{o.name}"', True),
    'cbt.Exam': (['title', 'published', 'start_time', 'end_time', 'duration_minutes', 'allow_retake', 'answer_review'],
                 'EXAM', lambda o: f'Exam "{o.title}"', True),
    'academics.Term': (['is_active', 'start_date', 'end_date'], 'SETTINGS', lambda o: f'Term {o.name} ({o.session})', False),
    'academics.AcademicSession': (['is_active'], 'SETTINGS', lambda o: f'Session {o.name}', False),
    'tenants.Tenant': (['name', 'email', 'phone', 'address', 'logo', 'status'], 'SETTINGS', lambda o: 'School details', False),
    'users.Student': (['status', 'admission_number'], 'STUDENT', lambda o: f'{o.full_name} ({o.admission_number})', False),
    'labs.Breakage': (['status', 'charge'], 'PAYMENT', lambda o: f'Lab breakage charge — {o.item}', False),
}


def _fields(model, spec):
    if spec != ALL:
        return spec
    return [f.name for f in model._meta.concrete_fields
            if f.name not in SKIP and not getattr(f, 'auto_now', False) and not getattr(f, 'auto_now_add', False)]


def _value(obj, name):
    field = obj._meta.get_field(name)
    if field.is_relation:
        related = getattr(obj, name, None)
        return str(related) if related is not None else ''
    value = getattr(obj, name, None)
    if hasattr(field, 'choices') and field.choices:
        value = dict(field.flatchoices).get(value, value)
    if value is None:
        return ''
    if hasattr(value, 'name') and hasattr(value, 'url'):          # files
        return 'file' if value else ''
    if isinstance(value, float):
        return f'{value:g}'
    if hasattr(value, 'isoformat'):
        return value.isoformat(sep=' ', timespec='minutes') if hasattr(value, 'hour') else value.isoformat()
    return str(value)


def _snapshot(obj, fields):
    out = {}
    for name in fields:
        try:
            out[name] = _value(obj, name)
        except Exception:
            out[name] = ''
    return out


def _before(sender, instance, **kwargs):
    instance._watch_before = None
    if not instance.pk:
        return
    try:
        fields = _fields(sender, WATCHED[sender._meta.label][0])
        if fields:
            old = sender._base_manager.filter(pk=instance.pk).first()
            if old is not None:
                instance._watch_before = _snapshot(old, fields)
    except Exception:
        log.exception('Audit snapshot failed for %s', sender._meta.label)


def _after(sender, instance, created, raw=False, **kwargs):
    if raw:
        return                                                   # loading fixtures
    from . import services as svc
    spec, action, name, on_create = WATCHED[sender._meta.label]
    try:
        tenant = None
        if sender._meta.label == 'tenants.Tenant':
            tenant = instance
        elif getattr(instance, 'tenant_id', None):
            tenant = instance.tenant
        label = name(instance)
        if created:
            if on_create:
                svc.log_event(action, f'{label} — created', tenant=tenant, obj=instance)
            return
        before = getattr(instance, '_watch_before', None)
        if before is None:
            return
        after = _snapshot(instance, list(before))
        changes = {k: [before[k], after[k]] for k in before if before[k] != after[k]}
        if not changes:
            return
        verbose = {f: str(sender._meta.get_field(f).verbose_name) for f in changes}
        detail = '; '.join(f'{verbose[k]}: {v[0] or "—"} → {v[1] or "—"}' for k, v in changes.items())
        svc.log_event(action, f'{label} — {detail}', tenant=tenant, obj=instance,
                      changes={verbose[k]: v for k, v in changes.items()})
    except Exception:
        log.exception('Audit event failed for %s', sender._meta.label)
    finally:
        instance._watch_before = None


def connect():
    from django.apps import apps
    for label in WATCHED:
        try:
            model = apps.get_model(label)
        except LookupError:
            continue
        pre_save.connect(_before, sender=model, dispatch_uid=f'security-watch-pre-{label}', weak=False)
        post_save.connect(_after, sender=model, dispatch_uid=f'security-watch-post-{label}', weak=False)
