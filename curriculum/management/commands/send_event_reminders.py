"""
Email reminders for school events that ask for one (set "Remind people" on the
event). Run once a day, e.g. at 07:00:

    0 7 * * * cd /path/to/project && venv/bin/python manage.py send_event_reminders
"""
from django.core.management.base import BaseCommand

from curriculum.events import run_reminders


class Command(BaseCommand):
    help = 'Send due reminder emails for school events.'

    def handle(self, *args, **options):
        result = run_reminders()
        total = sum(result.values())
        self.stdout.write(self.style.SUCCESS(
            f'Sent {total} reminder email{"s" if total != 1 else ""}'
            + (': ' + ', '.join(f'{k} {v}' for k, v in result.items()) if result else '.')))
