"""
Email the transport office about vehicle documents and driving licences that
expire soon. Run daily, e.g. at 07:00:

    0 7 * * * cd /path/to/project && venv/bin/python manage.py transport_reminders
"""
from django.core.management.base import BaseCommand

from transport.services import run_reminders


class Command(BaseCommand):
    help = 'Remind the transport office about expiring vehicle documents and licences.'

    def handle(self, *args, **options):
        n = run_reminders()
        self.stdout.write(self.style.SUCCESS(f'Reminded about {n} expiring item{"s" if n != 1 else ""}.'))
