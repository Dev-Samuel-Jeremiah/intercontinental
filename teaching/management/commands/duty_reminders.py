"""
Email staff the evening before a duty. Run daily, e.g. at 17:00:

    0 17 * * * cd /path/to/project && venv/bin/python manage.py duty_reminders
"""
from django.core.management.base import BaseCommand

from teaching.services import run_duty_reminders


class Command(BaseCommand):
    help = "Remind staff about tomorrow's duties."

    def handle(self, *args, **options):
        n = run_duty_reminders()
        self.stdout.write(self.style.SUCCESS(f'Sent {n} duty reminder{"s" if n != 1 else ""}.'))
