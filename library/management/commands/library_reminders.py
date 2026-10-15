"""
Library reminders: books due soon, overdue notices (with the fine so far) and
reservations not collected in time. Run daily, e.g. at 07:30:

    30 7 * * * cd /path/to/project && venv/bin/python manage.py library_reminders
"""
from django.core.management.base import BaseCommand

from library.services import run_reminders


class Command(BaseCommand):
    help = 'Send library due-soon and overdue reminders and release uncollected reservations.'

    def handle(self, *args, **options):
        n = run_reminders()
        self.stdout.write(self.style.SUCCESS(
            f'{n["due"]} due-soon reminder(s), {n["overdue"]} overdue notice(s), {n["expired"]} hold(s) released.'))
