"""
results/locks.py
────────────────
Score-entry deadlines: after a term's lock date nobody can change its scores
until an administrator reopens entry, for a set time and with a reason.
"""
from datetime import timedelta

from django.utils import timezone

from .models import ScoreLock


class ScoresLocked(ValueError):
    pass


def lock_for(term):
    if not term:
        return None
    return ScoreLock.objects.filter(tenant_id=term.tenant_id, term=term).first()


def check_open(term):
    lock = lock_for(term)
    if lock and lock.is_locked:
        raise ScoresLocked(f'Score entry for {term.name} closed on {lock.locks_on:%d %b %Y}. '
                           'Ask an administrator to reopen it.')


def set_deadline(term, locks_on):
    lock, _ = ScoreLock.objects.update_or_create(tenant_id=term.tenant_id, term=term,
                                                 defaults={'locks_on': locks_on})
    return lock


def reopen(lock, user, reason, hours=48):
    if not (reason or '').strip():
        raise ScoresLocked('Say why entry is being reopened.')
    if not 1 <= int(hours) <= 24 * 14:
        raise ScoresLocked('Reopen for between 1 hour and 14 days.')
    lock.reopened_until = timezone.now() + timedelta(hours=int(hours))
    lock.reopen_reason = reason.strip()[:255]
    lock.reopened_by = user
    lock.save(update_fields=['reopened_until', 'reopen_reason', 'reopened_by', 'updated_at'])
    return lock


def close_now(lock):
    lock.reopened_until = None
    lock.save(update_fields=['reopened_until', 'updated_at'])
    return lock
