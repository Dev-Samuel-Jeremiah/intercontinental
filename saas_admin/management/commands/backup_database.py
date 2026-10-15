"""
A consistent snapshot of the SQLite database (and, with --media, the uploaded
files), compressed into BACKUP_DIR. Old snapshots are deleted after
BACKUP_KEEP_DAYS days.

Uses SQLite's online backup, so it is safe while the site is running —
never copy the live .sqlite3 file with cp.

    python manage.py backup_database            # database only
    python manage.py backup_database --media    # database + uploaded files

Run it from cron (see deploy/DEPLOY.md), and copy BACKUP_DIR off the server
regularly: a backup on the same disk does not survive the disk.
"""
import gzip
import shutil
import sqlite3
import tarfile
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connections


class Command(BaseCommand):
    help = 'Snapshot the SQLite database (and optionally uploaded files) into BACKUP_DIR.'

    def add_arguments(self, parser):
        parser.add_argument('--media', action='store_true', help='Also archive mediafiles and private_media.')

    def handle(self, *args, **options):
        db = connections['default'].settings_dict
        if db['ENGINE'] != 'django.db.backends.sqlite3':
            raise CommandError('backup_database is for SQLite. Use pg_dump for PostgreSQL.')
        out_dir = Path(settings.BACKUP_DIR)
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime('%Y%m%d-%H%M%S')

        target = out_dir / f'db-{stamp}.sqlite3.gz'
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = Path(tmp) / 'db.sqlite3'
            src = sqlite3.connect(str(db['NAME']))
            dst = sqlite3.connect(str(snapshot))
            try:
                src.backup(dst)
            finally:
                dst.close()
                src.close()
            check = sqlite3.connect(str(snapshot))
            try:
                ok = check.execute('PRAGMA integrity_check').fetchone()[0]
            finally:
                check.close()
            if ok != 'ok':
                raise CommandError(f'The snapshot failed its integrity check: {ok}')
            with open(snapshot, 'rb') as fh, gzip.open(target, 'wb', compresslevel=6) as gz:
                shutil.copyfileobj(fh, gz)
        self.stdout.write(self.style.SUCCESS(f'Database → {target} ({target.stat().st_size // 1024} KB)'))

        if options['media']:
            archive = out_dir / f'media-{stamp}.tar.gz'
            with tarfile.open(archive, 'w:gz') as tar:
                for folder in (Path(settings.MEDIA_ROOT), Path(settings.PRIVATE_MEDIA_ROOT)):
                    if folder.exists():
                        tar.add(folder, arcname=folder.name)
            self.stdout.write(self.style.SUCCESS(f'Files → {archive} ({archive.stat().st_size // 1024} KB)'))

        cutoff = datetime.now() - timedelta(days=settings.BACKUP_KEEP_DAYS)
        removed = 0
        for old in list(out_dir.glob('db-*.sqlite3.gz')) + list(out_dir.glob('media-*.tar.gz')):
            if datetime.fromtimestamp(old.stat().st_mtime) < cutoff:
                old.unlink()
                removed += 1
        if removed:
            self.stdout.write(f'Removed {removed} backup(s) older than {settings.BACKUP_KEEP_DAYS} days.')
