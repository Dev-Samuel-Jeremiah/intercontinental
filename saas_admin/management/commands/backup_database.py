"""
A consistent, integrity-checked snapshot of the database (and, with
--media, the uploaded files), compressed into BACKUP_DIR, copied off the
server if BACKUP_OFFSITE_DIR / BACKUP_OFFSITE_COMMAND are set, and recorded
under Security → Platform & Backups. Old snapshots are deleted after
BACKUP_KEEP_DAYS days; administrators are told if a backup fails.

Safe while the site is running: SQLite uses its online backup (never copy the
live .sqlite3 file with cp); PostgreSQL uses pg_dump (custom format, restore
with pg_restore — see deploy/DEPLOY.md).

    python manage.py backup_database            # database only
    python manage.py backup_database --media    # database + uploaded files

Run it daily from the systemd timer (deploy/intercontinental-backup.timer) or
cron — see deploy/DEPLOY.md.
"""
from django.core.management.base import BaseCommand, CommandError

from security import platform


class Command(BaseCommand):
    help = 'Snapshot the database (SQLite or PostgreSQL, and optionally uploaded files) into BACKUP_DIR.'

    def add_arguments(self, parser):
        parser.add_argument('--media', action='store_true', help='Also archive mediafiles and private_media.')
        parser.add_argument('--trigger', default='SCHEDULE', choices=[k for k, _ in platform.SystemBackup.TRIGGERS])

    def handle(self, *args, **options):
        try:
            rec = platform.run_backup(options['trigger'], media=options['media'])
        except platform.PlatformError as problem:
            raise CommandError(str(problem))
        if not rec.ok:
            raise CommandError(f'Backup failed: {rec.error}')
        self.stdout.write(self.style.SUCCESS(f'Database → {platform.backup_dir() / rec.file_name} ({rec.size // 1024} KB)'
                                             + (f' · {rec.offsite}' if rec.offsite else '')))
