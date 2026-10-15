"""
Nightly backup of the school's data (the last 14 nightly backups are kept).
Run once a night, e.g. at 01:30:

    30 1 * * * cd /path/to/project && venv/bin/python manage.py nightly_backup
"""
from django.core.management.base import BaseCommand

from saas_admin.services import nightly


class Command(BaseCommand):
    help = "Back up the school's data."

    def handle(self, *args, **options):
        r = nightly()
        self.stdout.write(self.style.SUCCESS(', '.join(f'{k}: {v}' for k, v in r.items())))
