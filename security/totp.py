"""
security/totp.py
────────────────
Time-based one-time passwords (RFC 6238) for authenticator apps such as
Google Authenticator, Microsoft Authenticator or Authy. Standard library only.
"""
import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

STEP = 30
DIGITS = 6


def new_secret():
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip('=')


def _key(secret):
    s = secret.strip().replace(' ', '').upper()
    return base64.b32decode(s + '=' * (-len(s) % 8))


def code_at(secret, step):
    digest = hmac.new(_key(secret), struct.pack('>Q', step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    number = struct.unpack('>I', digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(number % (10 ** DIGITS)).zfill(DIGITS)


def current_step(now=None):
    return int((now if now is not None else time.time()) // STEP)


def now_code(secret, now=None):
    return code_at(secret, current_step(now))


def verify(secret, code, last_step=0, window=1, now=None):
    """Return the matching time step (to store, so the code cannot be used again) or None."""
    code = ''.join(ch for ch in str(code or '') if ch.isdigit())
    if len(code) != DIGITS or not secret:
        return None
    step = current_step(now)
    for s in range(step - window, step + window + 1):
        if s > last_step and hmac.compare_digest(code_at(secret, s), code):
            return s
    return None


def uri(secret, account, issuer='School Portal'):
    label = quote(f'{issuer}:{account}')
    return f'otpauth://totp/{label}?secret={secret}&issuer={quote(issuer)}&algorithm=SHA1&digits={DIGITS}&period={STEP}'
