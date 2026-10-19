"""
security/platform.py
────────────────────
Keeping the system reliable: daily backups and a health check.

Backups
  `run_backup()` takes a consistent snapshot of the SQLite database (SQLite's
  online backup, safe while the site is running), checks it with
  PRAGMA integrity_check, compresses it into BACKUP_DIR, optionally copies it
  off the server (BACKUP_OFFSITE_DIR — e.g. a mounted drive — and/or
  BACKUP_OFFSITE_COMMAND — e.g. an rclone upload), records the run, deletes
  snapshots older than BACKUP_KEEP_DAYS and tells the administrators if
  anything failed.

  It runs from the daily timer/cron set up on the server, and — so a backup
  is never silently missed — the site itself starts one in the background if
  the last good backup is more than a day old (AUTO_BACKUP, on by default).
  Only one backup runs at a time (a file lock in BACKUP_DIR).

Health
  `health()` checks the things that make a school system fall over or leak:
  backup age, database integrity, disk space, pending migrations, debug mode,
  HTTPS cookies, the secret key, email, two-step sign-in for administrators,
  and the tools PDFs and videos need.
"""
import gzip
import logging
import os
import shlex
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

from django.conf import settings
from django.core.cache import cache
from django.db import connections
from django.utils import timezone

from .models import SystemBackup

log = logging.getLogger(__name__)
DUE_AFTER = timedelta(hours=24)
STALE_RUN = timedelta(hours=2)


class PlatformError(RuntimeError):
    pass


def backup_dir():
    path = Path(settings.BACKUP_DIR)
    path.mkdir(parents=True, exist_ok=True)
    return path


def is_sqlite():
    return connections['default'].settings_dict['ENGINE'] == 'django.db.backends.sqlite3'


def database_path():
    """The SQLite database file, or None if the database is not a file (e.g. in memory)."""
    name = str(connections['default'].settings_dict['NAME'])
    if not is_sqlite() or name.startswith('file:') or ':memory:' in name or 'mode=memory' in name:
        return None
    path = Path(name)
    return path if path.exists() else None


class _Lock:
    """An exclusive, non-blocking lock file, so two workers never back up at once."""

    def __init__(self):
        self.path = backup_dir() / '.backup.lock'
        self.fh = None

    def __enter__(self):
        import fcntl
        self.fh = open(self.path, 'w')
        try:
            fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.fh.close()
            raise PlatformError('A backup is already running.')
        return self

    def __exit__(self, *exc):
        import fcntl
        fcntl.flock(self.fh, fcntl.LOCK_UN)
        self.fh.close()


def _offsite(path):
    """Copy a finished backup off the server, if configured. Returns a short description."""
    done = []
    target = getattr(settings, 'BACKUP_OFFSITE_DIR', '')
    if target:
        Path(target).mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, Path(target) / path.name)
        done.append(f'copied to {target}')
    command = getattr(settings, 'BACKUP_OFFSITE_COMMAND', '')
    if command:
        args = [a.replace('{file}', str(path)) for a in shlex.split(command)]
        result = subprocess.run(args, capture_output=True, text=True, timeout=1800)
        if result.returncode != 0:
            raise PlatformError(f'Off-site upload failed: {(result.stderr or result.stdout).strip()[:150]}')
        done.append(f'uploaded with {args[0]}')
    return '; '.join(done)


def _notify_failure(message):
    try:
        from notifications import services as notes
        from tenants.models import Tenant
        for tenant in Tenant.objects.all():
            notes.to_admins(tenant, 'report', 'Backup failed', body=message[:300], url='/security/platform/',
                            level='danger', dedupe=f'backup-failed:{timezone.localdate()}:{tenant.pk}')
    except Exception:
        log.exception('Could not tell administrators about a failed backup')


def _prune():
    cutoff = datetime.now() - timedelta(days=settings.BACKUP_KEEP_DAYS)
    removed = 0
    for old in list(backup_dir().glob('db-*.sqlite3.gz')) + list(backup_dir().glob('media-*.tar.gz')):
        if datetime.fromtimestamp(old.stat().st_mtime) < cutoff:
            old.unlink()
            removed += 1
    return removed


def run_backup(trigger='SCHEDULE', media=False, user=None):
    """Back up the database (and, with media=True, the uploaded files). Returns the database SystemBackup."""
    from . import services as svc
    with _Lock():
        rec = SystemBackup.objects.create(kind='DB', trigger=trigger, started_by=user if getattr(user, 'pk', None) else None)
        try:
            if not is_sqlite():
                raise PlatformError('Automatic backups here are for SQLite. Use pg_dump for PostgreSQL.')
            source = database_path()
            if source is None:
                raise PlatformError('The database is not a file on this server, so it cannot be snapshotted.')
            stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
            target = backup_dir() / f'db-{stamp}.sqlite3.gz'
            with tempfile.TemporaryDirectory() as tmp:
                snapshot = Path(tmp) / 'db.sqlite3'
                src = sqlite3.connect(str(source), timeout=30)
                dst = sqlite3.connect(str(snapshot))
                try:
                    src.backup(dst)
                finally:
                    dst.close()
                    src.close()
                check = sqlite3.connect(str(snapshot))
                try:
                    rec.integrity = str(check.execute('PRAGMA integrity_check').fetchone()[0])[:120]
                finally:
                    check.close()
                if rec.integrity != 'ok':
                    raise PlatformError(f'The snapshot failed its integrity check: {rec.integrity}')
                with open(snapshot, 'rb') as fh, gzip.open(target, 'wb', compresslevel=6) as gz:
                    shutil.copyfileobj(fh, gz)
            rec.file_name, rec.size = target.name, target.stat().st_size
            try:
                rec.offsite = _offsite(target)[:200]
            except Exception as problem:            # the backup itself is fine; say the copy failed
                rec.offsite = f'FAILED: {problem}'[:200]
                _notify_failure(f'The backup was made, but the off-site copy failed: {problem}')
            rec.ok = True
            if media:
                files = SystemBackup.objects.create(kind='FILES', trigger=trigger, started_by=rec.started_by)
                archive = backup_dir() / f'media-{stamp}.tar.gz'
                with tarfile.open(archive, 'w:gz') as tar:
                    for folder in (Path(settings.MEDIA_ROOT), Path(settings.PRIVATE_MEDIA_ROOT)):
                        if folder.exists():
                            tar.add(folder, arcname=folder.name)
                files.file_name, files.size, files.ok, files.integrity = archive.name, archive.stat().st_size, True, 'ok'
                try:
                    files.offsite = _offsite(archive)[:200]
                except Exception as problem:
                    files.offsite = f'FAILED: {problem}'[:200]
                files.finished_at = timezone.now()
                files.save()
            _prune()
        except Exception as problem:
            rec.ok, rec.error = False, str(problem)[:2000]
            log.exception('Backup failed')
            _notify_failure(f'The {timezone.localdate():%d %b} database backup failed: {problem}')
        finally:
            rec.finished_at = timezone.now()
            rec.save()
        svc.log_event('BACKUP', f'Database backup {"made" if rec.ok else "FAILED"} ({rec.get_trigger_display().lower()})'
                                + (f' — {rec.size // 1024:,} KB' if rec.ok else f': {rec.error[:200]}'),
                      tenant=None, user=rec.started_by)
        return rec


def last_good():
    return SystemBackup.objects.filter(kind='DB', ok=True).first()


def backup_due(now=None):
    good = last_good()
    return good is None or (now or timezone.now()) - good.started_at > DUE_AFTER


def running():
    return SystemBackup.objects.filter(finished_at__isnull=True, started_at__gte=timezone.now() - STALE_RUN).exists()


def maybe_auto_backup():
    """Called now and then by the site: start a backup in the background if the last good one is over a day old."""
    if not getattr(settings, 'AUTO_BACKUP', True) or database_path() is None:
        return False
    if not cache.add('platform:auto-backup-check', 1, 600):      # at most every 10 minutes per worker
        return False
    if not backup_due() or running():
        return False
    from lms_project.tasks import run_in_background

    def job():
        try:
            run_backup('AUTO')
        except PlatformError:
            pass                                                 # another worker got there first
    run_in_background(job)
    return True


def start_manual(user):
    if running():
        raise PlatformError('A backup is already running.')
    from lms_project.tasks import run_in_background
    run_in_background(lambda: run_backup('MANUAL', user=user))


# ─────────────────────────────────────────────────────────────────────────────
# Health
# ─────────────────────────────────────────────────────────────────────────────

def _check(name, level, detail, fix=''):
    return {'name': name, 'level': level, 'detail': detail, 'fix': fix}


def _size(n):
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024 or unit == 'TB':
            return f'{n:,.0f} {unit}' if unit in ('B', 'KB') else f'{n:,.1f} {unit}'
        n /= 1024


def database_integrity():
    """PRAGMA quick_check, cached for an hour (it reads the whole file)."""
    cached = cache.get('platform:quick-check')
    if cached:
        return cached
    if not is_sqlite():
        return 'not SQLite'
    with connections['default'].cursor() as cur:
        cur.execute('PRAGMA quick_check')
        result = str(cur.fetchone()[0])
    cache.set('platform:quick-check', result, 3600)
    return result


def health(tenant=None):
    out = []
    now = timezone.now()
    good = last_good()
    if good is None:
        out.append(_check('Daily backup', 'bad', 'No successful backup yet.', 'Press "Back up now", and set up the daily timer.'))
    else:
        age = now - good.started_at
        hours = age.total_seconds() / 3600
        level = 'ok' if age <= DUE_AFTER + timedelta(hours=2) else ('warn' if hours < 72 else 'bad')
        out.append(_check('Daily backup', level, f'Last good backup {timezone.localtime(good.started_at):%d %b %Y, %H:%M} '
                                                 f'({hours:.0f} hours ago, {_size(good.size)}).',
                          '' if level == 'ok' else 'Check the backup timer on the server.'))
        failed = SystemBackup.objects.filter(kind='DB', ok=False, started_at__gt=good.started_at).first()
        if failed:
            out.append(_check('Latest backup run', 'bad', f'Failed at {timezone.localtime(failed.started_at):%d %b %H:%M}: '
                                                          f'{failed.error[:160]}'))
    offsite = getattr(settings, 'BACKUP_OFFSITE_DIR', '') or getattr(settings, 'BACKUP_OFFSITE_COMMAND', '')
    out.append(_check('Off-site copies', 'ok' if offsite else 'warn',
                      'Each backup is copied off the server.' if offsite else
                      'Backups are only on this server — a disk failure would take them too.',
                      '' if offsite else 'Set BACKUP_OFFSITE_DIR or BACKUP_OFFSITE_COMMAND in .env, or download backups regularly.'))
    if is_sqlite():
        try:
            result = database_integrity()
            out.append(_check('Database integrity', 'ok' if result == 'ok' else 'bad',
                              'The database passed its integrity check.' if result == 'ok' else result[:200]))
        except Exception as problem:
            out.append(_check('Database integrity', 'bad', str(problem)[:200]))
        db_path = database_path()
        if db_path is not None:
            wal = db_path.with_name(db_path.name + '-wal')
            wal_size = wal.stat().st_size if wal.exists() else 0
            out.append(_check('Database size', 'ok' if wal_size < 200 * 1024 * 1024 else 'warn',
                              f'{_size(db_path.stat().st_size)} (write-ahead log {_size(wal_size)}).'))
    try:
        usage = shutil.disk_usage(backup_dir())
        free_pct = 100 * usage.free / usage.total
        level = 'ok' if free_pct >= 15 and usage.free > 2 * 1024 ** 3 else ('warn' if free_pct >= 5 else 'bad')
        out.append(_check('Disk space', level, f'{_size(usage.free)} free of {_size(usage.total)} ({free_pct:.0f}%).',
                          '' if level == 'ok' else 'Free up space or enlarge the disk before backups start failing.'))
    except OSError as problem:
        out.append(_check('Disk space', 'warn', str(problem)[:200]))
    try:
        from django.db.migrations.executor import MigrationExecutor
        plan = MigrationExecutor(connections['default']).migration_plan(
            MigrationExecutor(connections['default']).loader.graph.leaf_nodes())
        out.append(_check('Database up to date', 'ok' if not plan else 'bad',
                          'All updates applied.' if not plan else f'{len(plan)} update(s) not applied.',
                          '' if not plan else 'Run python manage.py migrate.'))
    except Exception as problem:
        out.append(_check('Database up to date', 'warn', str(problem)[:200]))
    out.append(_check('Debug mode', 'bad' if settings.DEBUG else 'ok',
                      'Debug mode is ON — error pages can show internal details.' if settings.DEBUG else 'Off.',
                      'Set DEBUG=False in .env on the live server.' if settings.DEBUG else ''))
    secure = getattr(settings, 'SESSION_COOKIE_SECURE', False) and getattr(settings, 'CSRF_COOKIE_SECURE', False)
    out.append(_check('HTTPS', 'ok' if secure else ('warn' if settings.DEBUG else 'bad'),
                      'Sign-in cookies are only sent over HTTPS.' if secure else 'Sign-in cookies can travel over plain HTTP.'))
    key = str(getattr(settings, 'SECRET_KEY', ''))
    weak = key.startswith('django-insecure') or len(key) < 40 or len(set(key)) < 10
    out.append(_check('Secret key', 'bad' if weak else 'ok', 'Weak or default secret key.' if weak else 'Strong.',
                      'Generate a new SECRET_KEY for .env.' if weak else ''))
    backend = getattr(settings, 'EMAIL_BACKEND', '')
    email_ok = 'smtp' in backend and bool(getattr(settings, 'EMAIL_HOST_USER', ''))
    out.append(_check('Email', 'ok' if email_ok else 'warn',
                      'Email is set up.' if email_ok else 'Email is not set up — password resets and alerts will not be sent.'))
    if tenant is not None:
        try:
            from users.models import Staff, User
            from .models import SecuritySettings, TwoFactor
            s = SecuritySettings.objects.filter(tenant=tenant).first()
            policy = s.require_2fa if s else 'ADMINS'
            admins = set(User.objects.filter(tenant=tenant, is_superuser=True, is_active=True).values_list('pk', flat=True)) | \
                set(Staff.objects.filter(tenant=tenant, role='ADMIN').values_list('user_id', flat=True))
            with_2fa = set(TwoFactor.objects.filter(user_id__in=admins, confirmed_at__isnull=False)
                           .values_list('user_id', flat=True))
            missing = len(admins - with_2fa)
            level = 'ok' if not missing else ('warn' if policy != 'NONE' else 'bad')
            out.append(_check('Two-step sign-in', level,
                              f'{len(admins) - missing} of {len(admins)} administrators use two-step sign-in'
                              f' (policy: {dict(SecuritySettings.TWO_FACTOR).get(policy, policy).lower()}).',
                              '' if not missing else 'Ask every administrator to switch it on under My Sign-in & Security.'))
        except Exception as problem:
            log.warning('2FA health check failed: %s', problem)
    tools = [(name, shutil.which(binary)) for name, binary in (('PDF reports (wkhtmltopdf)', 'wkhtmltopdf'),
                                                               ('Video processing (ffmpeg)', 'ffmpeg'))]
    for name, found in tools:
        out.append(_check(name, 'ok' if found else 'warn', 'Installed.' if found else 'Not installed on this server.'))
    return out


def summary(checks):
    return {level: sum(1 for c in checks if c['level'] == level) for level in ('ok', 'warn', 'bad')}


def backups(limit=30):
    return list(SystemBackup.objects.select_related('started_by')[:limit])


def backup_path(name):
    """The path of a backup file by name (no directory tricks)."""
    if not name or '/' in name or '\\' in name or not (name.startswith('db-') or name.startswith('media-')):
        raise PlatformError('Unknown backup.')
    path = backup_dir() / name
    if not path.exists():
        raise PlatformError('That backup file is no longer on the server.')
    return path
