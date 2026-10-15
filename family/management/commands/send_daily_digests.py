"""
Email each family a summary of their children's school day, for schools that
switched digests on (Parent experience → Settings). Run on school days in the
late afternoon:

    30 16 * * 1-5 cd /path/to/project && venv/bin/python manage.py send_daily_digests
"""
from django.core.management.base import BaseCommand

from family.services import run_digests


class Command(BaseCommand):
    help = "Send today's family digests."

    def handle(self, *args, **options):
        n = run_digests()
        self.stdout.write(self.style.SUCCESS(f'Sent {n} family digest{"s" if n != 1 else ""}.'))
