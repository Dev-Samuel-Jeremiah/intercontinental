"""
users/passwords.py
──────────────────
Student / parent term passwords are read off a screen or printout and typed
by hand (often on a phone), so:

  * new ones only use lowercase letters and digits that can't be confused
    (no 0/o, 1/l/i);
  * matching ignores case, surrounding spaces and those look-alikes, so
    older mixed-case passwords like "lI0OaQ1z" can still be typed.
"""
import logging

from django.utils.crypto import constant_time_compare, get_random_string

_ALPHABET = 'abcdefghjkmnpqrstuvwxyz23456789'
_LOOKALIKES = str.maketrans({'0': 'o', '1': 'l', 'i': 'l'})


def generate_term_password(length: int = 8) -> str:
    return get_random_string(length, _ALPHABET)


def _normalize(value: str) -> str:
    return (value or '').strip().lower().translate(_LOOKALIKES)


def term_password_matches(typed: str, stored: str) -> bool:
    if not typed or not stored:
        return False
    return constant_time_compare(_normalize(typed), _normalize(stored))


def master_password_allowed(typed: str, master: str, who: str = '', tenant=None) -> bool:
    """Shared 'master' passwords sign in as any student or any parent, so one
    leak exposes every account. They are refused unless a deployment sets
    ALLOW_MASTER_PASSWORDS=True, and every accepted use is logged."""
    from django.conf import settings

    if not master or not typed or not constant_time_compare(typed, master):
        return False
    if not getattr(settings, 'ALLOW_MASTER_PASSWORDS', False):
        logging.getLogger(__name__).warning(
            'Master password refused for %s (school=%s). Set ALLOW_MASTER_PASSWORDS=True '
            'to re-enable, or use the account’s own password.',
            who or 'an account', getattr(tenant, 'subdomain', '-'))
        return False
    logging.getLogger(__name__).warning(
        'Master password used to sign in as %s (school=%s).',
        who or 'an account', getattr(tenant, 'subdomain', '-'))
    return True
