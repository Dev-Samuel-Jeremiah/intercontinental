"""
saas_admin/services.py
──────────────────────
School-level tools that came from the old platform layer: branding, the in-app
help centre and tours, notices, data export, nightly backups and restore, and
sections (stored as campuses: Nursery & Primary, Secondary, Darun).

The SaaS parts — sign-up, plans, billing, coupons, agents, add-ons, custom
domains, support tickets to the vendor, impersonation and offboarding — have
been removed from this dedicated installation.
"""
import io
import json
import logging
import re
import zipfile
from datetime import date

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.html import escape

from tenants.models import Tenant

from .models import Article, Backup, Branding, Campus, DataExport, Notice, Tour, TourSeen

log = logging.getLogger(__name__)


class PlatformError(ValueError):
    pass


def is_platform(user):
    """Server administrators: superusers with no school record (created with createsuperuser)."""
    return bool(getattr(user, 'is_authenticated', False) and user.is_superuser and not user.tenant_id)


def _email(to, subject, html):
    from communications.services import send_email
    return send_email(to, subject, html)


def platform_emails():
    """Who gets technical and security alerts: ADMIN_EMAIL, IT_ALERT_EMAILS and server administrators."""
    from users.models import User
    raw = getattr(settings, 'IT_ALERT_EMAILS', '') or getattr(settings, 'PLATFORM_SUPPORT_EMAILS', '')
    extra = [e.strip() for e in raw.split(',') if e.strip()] if isinstance(raw, str) else []
    extra += [email for _name, email in getattr(settings, 'ADMINS', []) if email]
    staff = list(User.objects.filter(is_superuser=True, tenant__isnull=True, is_active=True).exclude(email='')
                 .values_list('email', flat=True))
    return sorted(set(extra + staff))


def school_admin_emails(tenant):
    from hr.services import admins_emails
    out = set(admins_emails(tenant))
    if tenant.owner_email:
        out.add(tenant.owner_email)
    return sorted(out)


# ─────────────────────────────────────────────────────────────────────────────
# Branding
# ─────────────────────────────────────────────────────────────────────────────

HEX = re.compile(r'^#[0-9a-fA-F]{6}$')


def save_branding(tenant, data, favicon=None):
    b, _ = Branding.objects.get_or_create(tenant=tenant)
    for f in ('primary', 'accent'):
        v = (data.get(f) or '').strip()
        if v and not HEX.match(v):
            raise PlatformError('Colours must look like #1f6feb.')
        setattr(b, f, v)
    b.brand_name = (data.get('brand_name') or '').strip()[:80]
    b.login_message = (data.get('login_message') or '').strip()[:255]
    b.email_from_name = (data.get('email_from_name') or '').strip()[:80]
    b.hide_powered_by = True
    if favicon:
        b.favicon = favicon
    b.save()
    return b


# ─────────────────────────────────────────────────────────────────────────────
# The public home page: hero background (picture or video) and campus photos
# ─────────────────────────────────────────────────────────────────────────────

LANDING_PHOTO_DEFAULTS = [
    ('images/landing/gallery-lab.jpg', 'Academics', 'Science & innovation'),
    ('images/landing/gallery-makerspace.jpg', 'Hi-Tech Academy', 'The MakerSpace'),
    ('images/landing/gallery-sports.jpg', 'Sports', 'Pitches & courts'),
]
# Intercontinental Schools, Government House (Tarauni), Kano — the secondary school as mapped on OpenStreetMap
DEFAULT_MAP_LOCATION = '11.978936,8.553712'
IMAGE_LIMIT = 5 * 1024 * 1024
VIDEO_LIMIT = 20 * 1024 * 1024        # nginx accepts uploads up to 20 MB on this page


def _check_image(upload, what):
    import os
    from PIL import Image, UnidentifiedImageError
    if os.path.splitext(upload.name)[1].lower() not in ('.jpg', '.jpeg', '.png', '.webp'):
        raise PlatformError(f'{what}: use a JPG, PNG or WebP picture.')
    if upload.size > IMAGE_LIMIT:
        raise PlatformError(f'{what}: the picture must be smaller than 5 MB.')
    try:
        Image.open(upload).verify()
    except (UnidentifiedImageError, OSError, SyntaxError):
        raise PlatformError(f'{what}: that file is not a picture we can read.')
    upload.seek(0)


def _check_video(upload):
    import os
    ext = os.path.splitext(upload.name)[1].lower()
    if ext not in ('.mp4', '.webm'):
        raise PlatformError('Background video: use an MP4 or WebM file.')
    if upload.size > VIDEO_LIMIT:
        raise PlatformError('Background video: it must be smaller than 20 MB — a short, silent clip of 10–20 seconds is best.')
    head = upload.read(16)
    upload.seek(0)
    if not ((ext == '.mp4' and head[4:8] == b'ftyp') or (ext == '.webm' and head[:4] == b'\x1a\x45\xdf\xa3')):
        raise PlatformError('Background video: that file is not a real MP4 or WebM video.')


def _replace(obj, field, upload):
    old = getattr(obj, field)
    if old:
        old.delete(save=False)
    setattr(obj, field, upload)


def save_landing(tenant, data, files):
    """Save the home-page settings from the Branding page. Raises PlatformError with a plain message."""
    from .models import LandingPage
    page, _ = LandingPage.objects.get_or_create(tenant=tenant)
    uploads = {name: files.get(name) for name in ('hero_image', 'hero_video', 'photo1', 'photo2', 'photo3')}
    names = {'hero_image': 'Background picture', 'photo1': 'Campus photo 1', 'photo2': 'Campus photo 2',
             'photo3': 'Campus photo 3'}
    for name, upload in uploads.items():                   # check everything before changing anything
        if upload:
            _check_video(upload) if name == 'hero_video' else _check_image(upload, names[name])
    for name, upload in uploads.items():
        if upload:
            _replace(page, name, upload)
        elif data.get(f'remove_{name}') and getattr(page, name):
            _replace(page, name, None)
    page.use_video = bool(data.get('use_video')) and bool(page.hero_video)
    if 'map_location' in data:
        page.map_location = (data.get('map_location') or '').strip()[:200]
    for n in (1, 2, 3):
        setattr(page, f'photo{n}_label', (data.get(f'photo{n}_label') or '').strip()[:40])
        setattr(page, f'photo{n}_title', (data.get(f'photo{n}_title') or '').strip()[:60])
    page.save()
    return page


def landing_content(tenant):
    """What the home page shows, with the built-in pictures wherever nothing was uploaded."""
    from django.templatetags.static import static
    from .models import LandingPage
    page = LandingPage.objects.filter(tenant=tenant).first() if tenant else None
    hero_image = page.hero_image.url if page and page.hero_image else static('images/landing/campus.jpg')
    video = page.hero_video.url if page and page.use_video and page.hero_video else ''
    photos = []
    for n, (default, label, title) in enumerate(LANDING_PHOTO_DEFAULTS, start=1):
        img = getattr(page, f'photo{n}') if page else None
        photos.append({'url': img.url if img else static(default),
                       'label': (getattr(page, f'photo{n}_label') if page else '') or label,
                       'title': (getattr(page, f'photo{n}_title') if page else '') or title})
    from urllib.parse import quote
    where = (page.map_location if page and page.map_location else '') or DEFAULT_MAP_LOCATION
    q = quote(where)
    return {'hero_image': hero_image, 'hero_video': video, 'photos': photos, 'page': page,
            'map_location': where,
            'map_embed': f'https://www.google.com/maps?q={q}&z=17&output=embed',
            'map_link': f'https://www.google.com/maps/dir/?api=1&destination={q}'}


# ─────────────────────────────────────────────────────────────────────────────
# Help centre, tours, notices, export, backup/restore
# ─────────────────────────────────────────────────────────────────────────────

def render_body(text):
    """Very small, safe formatter for help articles: ## headings, - bullets, blank-line paragraphs."""
    out, bullets = [], []

    def flush():
        if bullets:
            out.append('<ul>' + ''.join(f'<li>{escape(b)}</li>' for b in bullets) + '</ul>')
            bullets.clear()
    for block in re.split(r'\n\s*\n', (text or '').strip()):
        for line in block.splitlines():
            s = line.strip()
            if s.startswith('- '):
                bullets.append(s[2:])
                continue
            flush()
            if s.startswith('## '):
                out.append(f'<h3>{escape(s[3:])}</h3>')
            elif s:
                out.append(f'<p>{escape(s)}</p>')
        flush()
    return ''.join(out)


def search_articles(q='', module=''):
    qs = Article.objects.filter(published=True)
    if q:
        for word in q.split()[:6]:
            qs = qs.filter(Q(title__icontains=word) | Q(body__icontains=word) | Q(summary__icontains=word))
    if module:
        qs = qs.filter(module=module)
    return qs


def tour_for(user, path):
    if not getattr(user, 'is_authenticated', False):
        return None
    seen = TourSeen.objects.filter(user=user).values_list('tour_id', flat=True)
    aud = ['ALL']
    if getattr(user, 'is_student', False):
        aud.append('STUDENT')
    elif getattr(user, 'is_staff_user', False) or user.is_superuser:
        aud.append('STAFF')
        if user.is_superuser:
            aud.append('ADMIN')
    for t in Tour.objects.filter(active=True, audience__in=aud).exclude(pk__in=seen).order_by('-path_prefix'):
        if path.startswith(t.path_prefix):
            return t
    return None


def notices_for(user, tenant):
    if not getattr(user, 'is_authenticated', False) or tenant is None:
        return []
    now = timezone.now()
    qs = Notice.objects.filter(starts__lte=now).filter(Q(ends__isnull=True) | Q(ends__gte=now)) \
        .exclude(dismissals__user=user)
    out = []
    for n in qs:
        if n.audience == 'ADMINS' and not user.is_superuser:
            continue
        if n.audience == 'STAFF' and not (user.is_superuser or getattr(user, 'is_staff_user', False)):
            continue
        out.append(n)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# F435 export, F436 backup/restore, offboarding
# ─────────────────────────────────────────────────────────────────────────────

def tenant_models():
    """Every installed model that carries a tenant FK (school data)."""
    from django.apps import apps
    out = []
    for m in apps.get_models():
        if m._meta.app_label in ('tenants', 'admin', 'sessions', 'contenttypes', 'auth'):
            continue
        if m._meta.app_label == 'saas_admin' and m._meta.label not in ('saas_admin.Campus', 'saas_admin.Branding'):
            continue
        if m._meta.label in ('security.UserSession',):      # live session keys never leave the server
            continue
        f = next((f for f in m._meta.fields if f.name == 'tenant'), None)
        if f is not None and getattr(f.related_model, '_meta', None) and f.related_model._meta.label == 'tenants.Tenant':
            out.append(m)
    return out


def _qs(model, tenant):
    return model._base_manager.filter(tenant=tenant)


def build_export(export):
    """A ZIP with one CSV per table of the school's data (files such as photos are not included)."""
    import csv
    from django.core.files.base import ContentFile
    tenant = export.tenant
    buf = io.BytesIO()
    counts = {}
    try:
        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
            for m in tenant_models():
                qs = _qs(m, tenant)
                n = qs.count()
                if not n:
                    continue
                fields = [f for f in m._meta.concrete_fields
                          if f.name not in ('password', 'term_password', 'parent_term_password')]
                s = io.StringIO()
                w = csv.writer(s)
                w.writerow([f.attname for f in fields])
                for row in qs.values_list(*[f.attname for f in fields]).iterator():
                    w.writerow(['' if v is None else v for v in row])
                z.writestr(f'{m._meta.app_label}/{m.__name__}.csv', s.getvalue())
                counts[m._meta.label] = n
            z.writestr('README.txt', f'Data export for {tenant.name}, {timezone.now():%d %B %Y %H:%M}.\n'
                                     f'One CSV file per table. Passwords are not included.\n')
        export.file.save(f'export-{tenant.subdomain}.zip', ContentFile(buf.getvalue()), save=False)
        export.size, export.tables, export.status = len(buf.getvalue()), counts, 'READY'
    except Exception as problem:
        log.exception('Export failed')
        export.status, export.error = 'FAILED', str(problem)[:255]
    export.finished_at = timezone.now()
    export.save()
    return export


def request_export(tenant, user):
    if DataExport.objects.filter(tenant=tenant, status='QUEUED').exists():
        raise PlatformError('An export is already being prepared.')
    ex = DataExport.objects.create(tenant=tenant, requested_by=user)
    from lms_project.tasks import run_in_background
    run_in_background(build_export, ex)
    return ex


def make_backup(tenant, kind='AUTO'):
    from django.core import serializers
    from django.core.files.base import ContentFile
    objs = []
    for m in tenant_models():
        objs.extend(_qs(m, tenant))
    from users.models import User
    users = list(User.objects.filter(tenant=tenant))
    data = serializers.serialize('json', users + objs)
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('backup.json', data)
    b = Backup(tenant=tenant, kind=kind, size=len(raw.getvalue()), rows=len(users) + len(objs))
    b.file.save(f'backup-{tenant.subdomain}-{timezone.now():%Y%m%d%H%M}.zip', ContentFile(raw.getvalue()), save=False)
    b.save()
    keep = Backup.objects.filter(tenant=tenant, kind='AUTO').order_by('-created_at')[14:]
    for old in keep:
        old.file.delete(save=False)
        old.delete()
    return b


def restore_backup(backup, dry_run=True):
    """
    Put back rows from a backup that no longer exist (e.g. records deleted by mistake). Rows still present are left
    untouched, so this never overwrites newer changes. Returns {'missing': n, 'restored': n, 'failed': n}.
    """
    from django.core import serializers
    with backup.file.open('rb') as fh, zipfile.ZipFile(fh) as z:
        data = z.read('backup.json').decode()
    missing, restored, failed = 0, 0, 0
    items = list(serializers.deserialize('json', data))
    for obj in items:
        model = type(obj.object)
        if model._base_manager.filter(pk=obj.object.pk).exists():
            continue
        missing += 1
        if dry_run:
            continue
        try:
            with transaction.atomic():
                obj.save()
            restored += 1
        except Exception:
            failed += 1
    if not dry_run:
        backup.restored_at = timezone.now()
        backup.restore_note = f'{restored} restored, {failed} failed'
        backup.save(update_fields=['restored_at', 'restore_note'])
    return {'missing': missing, 'restored': restored, 'failed': failed}


# ─────────────────────────────────────────────────────────────────────────────
# Sections (campus records): Nursery & Primary, Secondary, Darun
# ─────────────────────────────────────────────────────────────────────────────

def campus_summary(tenant):
    from users.models import ENROLLED_STATUSES, Student
    rows = []
    for c in Campus.objects.filter(tenant=tenant).prefetch_related('classes', 'staff'):
        ids = list(c.classes.values_list('pk', flat=True))
        rows.append({'campus': c, 'classes': len(ids), 'staff': c.staff.count(),
                     'students': Student._base_manager.filter(tenant=tenant, class_assigned_id__in=ids,
                                                              status__in=ENROLLED_STATUSES).count()})
    return rows


def campus_class_ids(request):
    """Class ids for the campus picked in the session (None = all campuses)."""
    cid = request.session.get('campus_id')
    if not cid:
        return None
    c = Campus.objects.filter(tenant=request.tenant, pk=cid).first()
    return list(c.classes.values_list('pk', flat=True)) if c else None


def assign_class(campus, cls):
    for other in cls.campuses.exclude(pk=campus.pk):
        other.classes.remove(cls)
    campus.classes.add(cls)


# ─────────────────────────────────────────────────────────────────────────────
# Nightly job
# ─────────────────────────────────────────────────────────────────────────────

def nightly():
    """A backup of the school's data (the last 14 nightly backups are kept)."""
    out = {'backups': 0}
    for t in Tenant.objects.all():
        try:
            make_backup(t)
            out['backups'] += 1
        except Exception:
            log.exception('Nightly backup failed for %s', t.name)
    return out


def date_or(raw, default=None):
    try:
        return date.fromisoformat(raw or '')
    except ValueError:
        return default


def to_json(v):
    return json.dumps(v, default=str)
