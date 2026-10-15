"""
security/media.py
─────────────────
Signed links for personal files (F459).

Student passports, guardian photos, payment proofs and receipts, admission
documents, lesson files and assignment submissions, and the head teacher's
signature are kept outside the public media folder. Their `.url` is a signed
link, /m/<token>/<name>, that:

  * cannot be guessed — the token is an HMAC of the file path made with
    SECRET_KEY;
  * expires — links are good for SIGNED_MEDIA_HOURS (default 6) and change
    every hour, so browsers can still cache them within the hour;
  * only works on the school's own address — a link made inside one school
    is refused on another school's address.

Templates need no change: `{{ student.passport.url }}` now returns a signed
link.

Files uploaded before this was switched on are still in the public media
folder; they keep working (the view falls back to that folder) and
`python manage.py move_signed_media` moves them into the private folder.

Production: point SIGNED_MEDIA_ACCEL at an nginx `internal` location for the
signed folder and nginx sends the file instead of Django, e.g.
    location /protected-media/ { internal; alias /srv/school/private_media/signed/; }
    SIGNED_MEDIA_ACCEL=/protected-media/
"""
import math
import mimetypes
import os
import time
from urllib.parse import quote

from django.conf import settings
from django.core import signing
from django.core.files.storage import FileSystemStorage
from django.http import FileResponse, Http404, HttpResponse

SALT = 'school.signed-media'


def signed_root():
    root = getattr(settings, 'SIGNED_MEDIA_ROOT', '') or os.path.join(str(settings.PRIVATE_MEDIA_ROOT), 'signed')
    return str(root)


def _hours():
    return max(int(getattr(settings, 'SIGNED_MEDIA_HOURS', 6) or 6), 1)


def make_token(name, tenant_id=None, now=None):
    now = now or time.time()
    expires = int(math.ceil(now / 3600.0) * 3600) + _hours() * 3600   # stable within the hour
    return signing.dumps({'n': name, 't': str(tenant_id) if tenant_id else '', 'e': expires}, salt=SALT,
                         compress=True)


def read_token(token):
    try:
        data = signing.loads(token, salt=SALT)
    except signing.BadSignature:
        return None
    if not isinstance(data, dict) or data.get('e', 0) < time.time():
        return None
    return data


def signed_url(name):
    from tenants.middleware import get_current_tenant
    tenant = get_current_tenant()
    token = make_token(name, getattr(tenant, 'pk', None))
    return f'/m/{token}/{quote(os.path.basename(name))}'


class SignedMediaStorage(FileSystemStorage):
    """Files outside the public media folder whose URLs are signed, expiring links."""

    @property
    def base_location(self):
        return signed_root()

    @property
    def location(self):
        return os.path.abspath(self.base_location)

    @property
    def base_url(self):
        return None

    def url(self, name):
        return signed_url(name)

    def _legacy(self, name):
        return os.path.join(str(settings.MEDIA_ROOT), name)

    def exists(self, name):
        return super().exists(name) or os.path.exists(self._legacy(name))

    def path(self, name):
        p = super().path(name)
        if not os.path.exists(p) and os.path.exists(self._legacy(name)):
            return self._legacy(name)
        return p

    def open(self, name, mode='rb'):
        p = super().path(name)
        if not os.path.exists(p) and os.path.exists(self._legacy(name)) and 'r' in mode:
            return open(self._legacy(name), mode)
        return super().open(name, mode)

    def delete(self, name):
        super().delete(name)
        legacy = self._legacy(name)
        if os.path.exists(legacy):
            os.remove(legacy)

    def size(self, name):
        return os.path.getsize(self.path(name))


def signed_storage():
    return SignedMediaStorage()


def serve(request, token, name=''):
    data = read_token(token)
    if not data:
        raise Http404('This link has expired. Go back and open the page again.')
    tenant = getattr(request, 'tenant', None)
    if data['t'] and str(getattr(tenant, 'pk', '')) != data['t']:
        raise Http404
    rel = os.path.normpath(data['n']).lstrip('/\\')
    if rel.startswith('..'):
        raise Http404
    storage = SignedMediaStorage()
    path = storage.path(rel)
    roots = [os.path.abspath(signed_root()), os.path.abspath(str(settings.MEDIA_ROOT))]
    if not any(os.path.abspath(path).startswith(r + os.sep) for r in roots) or not os.path.isfile(path):
        raise Http404
    ctype = mimetypes.guess_type(path)[0] or 'application/octet-stream'
    accel = getattr(settings, 'SIGNED_MEDIA_ACCEL', '')
    if accel and os.path.abspath(path).startswith(os.path.abspath(signed_root()) + os.sep):
        resp = HttpResponse(content_type=ctype)
        resp['X-Accel-Redirect'] = accel.rstrip('/') + '/' + quote(rel.replace(os.sep, '/'))
    else:
        resp = FileResponse(open(path, 'rb'), content_type=ctype)
    resp['Cache-Control'] = 'private, max-age=3600'
    resp['X-Content-Type-Options'] = 'nosniff'
    if not ctype.startswith(('image/', 'video/', 'audio/')) and ctype != 'application/pdf':
        resp['Content-Disposition'] = f'attachment; filename="{os.path.basename(path)}"'
    return resp


def move_legacy_files(fields, dry_run=False):
    """Move files of the given (model, field_name) pairs from MEDIA_ROOT into the signed folder."""
    import shutil
    moved = 0
    for model, field_name in fields:
        for name in model._base_manager.exclude(**{field_name: ''}).exclude(**{f'{field_name}__isnull': True}) \
                .values_list(field_name, flat=True).iterator():
            src = os.path.join(str(settings.MEDIA_ROOT), name)
            dst = os.path.join(signed_root(), name)
            if os.path.isfile(src) and not os.path.exists(dst):
                moved += 1
                if not dry_run:
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    shutil.move(src, dst)
    return moved


def protected_fields():
    """Every (model, field name) that uses signed storage."""
    from django.apps import apps
    out = []
    for m in apps.get_models():
        for f in m._meta.fields:
            if isinstance(getattr(f, 'storage', None), SignedMediaStorage):
                out.append((m, f.name))
    return out
