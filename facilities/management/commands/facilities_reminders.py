"""
Facilities reminders: open jobs for preventive maintenance that is due,
low-stock alerts (WhatsApp first, then SMS, plus email), contracts ending
and utility bills falling due. Run daily, e.g. at 06:45:

    45 6 * * * cd /path/to/project && venv/bin/python manage.py facilities_reminders
"""
from django.core.management.base import BaseCommand

from facilities.services import run_reminders


class Command(BaseCommand):
    help = 'Raise preventive maintenance jobs and send stock, contract and bill reminders.'

    def handle(self, *args, **options):
        n = run_reminders()
        self.stdout.write(self.style.SUCCESS(
            f'{n["jobs"]} job(s) raised, {n["alerts"]} stock alert(s), {n["contracts"]} contract(s), '
            f'{n["bills"]} bill(s).'))
