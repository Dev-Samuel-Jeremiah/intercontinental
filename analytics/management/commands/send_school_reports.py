"""
Email the weekly (Mondays) and monthly (1st) school summaries. Run daily:

    15 7 * * * cd /path/to/project && venv/bin/python manage.py send_school_reports
"""
from django.core.management.base import BaseCommand

from analytics.reports import run


class Command(BaseCommand):
    help = 'Send due school summary emails to subscribers.'

    def handle(self, *args, **options):
        n = run()
        self.stdout.write(self.style.SUCCESS(f'Sent {n} summary email{"s" if n != 1 else ""}.'))
