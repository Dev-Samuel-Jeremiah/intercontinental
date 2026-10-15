"""
security/crypto.py
──────────────────
Field-level encryption for the most sensitive free text (F453): medical
conditions and nurse notes, counselling notes, safeguarding concerns and the
private notes behind a student's support needs.

Values are encrypted with Fernet (AES-128-CBC + HMAC-SHA256) before they reach
the database and decrypted when read, so a stolen database dump or backup does
not reveal them. The column stays a plain text column.

Keys
  FIELD_ENCRYPTION_KEY   one Fernet key, or several separated by commas (the
                         first encrypts; all are tried when decrypting, so keys
                         can be rotated). Make one with:
                           python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
  If unset, a key is derived from SECRET_KEY. Set FIELD_ENCRYPTION_KEY in
  production and keep a copy somewhere safe: without the key the encrypted
  text cannot be read.

Rows written before encryption was switched on are read as they are and
encrypted the next time they are saved (or all at once with
`python manage.py encrypt_fields`).

Encrypted fields cannot be searched or filtered in the database.
"""
import base64
import hashlib
import logging

from django.conf import settings
from django.db import models

PREFIX = 'enc1:'
log = logging.getLogger(__name__)
_cache = {}


def _keys():
    raw = getattr(settings, 'FIELD_ENCRYPTION_KEY', '') or ''
    keys = [k.strip() for k in str(raw).split(',') if k.strip()]
    if not keys:
        digest = hashlib.sha256(('field-encryption:' + settings.SECRET_KEY).encode()).digest()
        keys = [base64.urlsafe_b64encode(digest).decode()]
    return tuple(keys)


def _fernet():
    keys = _keys()
    if keys not in _cache:
        from cryptography.fernet import Fernet, MultiFernet
        _cache.clear()
        _cache[keys] = MultiFernet([Fernet(k.encode()) for k in keys])
    return _cache[keys]


def is_encrypted(value):
    return isinstance(value, str) and value.startswith(PREFIX)


def encrypt(value):
    if value is None or value == '' or is_encrypted(value):
        return value
    return PREFIX + _fernet().encrypt(str(value).encode()).decode()


def decrypt(value):
    if not is_encrypted(value):
        return value
    try:
        return _fernet().decrypt(value[len(PREFIX):].encode()).decode()
    except Exception:
        log.error('Could not decrypt a protected field — check FIELD_ENCRYPTION_KEY.')
        return '[protected — the encryption key has changed]'


class EncryptedTextField(models.TextField):
    """A TextField whose contents are encrypted at rest."""

    description = 'Text encrypted at rest'

    def from_db_value(self, value, expression, connection):
        return decrypt(value)

    def to_python(self, value):
        return decrypt(super().to_python(value))

    def get_prep_value(self, value):
        value = super().get_prep_value(value)
        return encrypt(value)
