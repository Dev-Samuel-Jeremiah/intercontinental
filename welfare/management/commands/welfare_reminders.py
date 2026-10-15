"""
Welfare reminders: vaccinations coming due (to families), support plans due
for review (to the plan owner) and restorative follow-ups due today. Run
daily, e.g. at 07:15:

    15 7 * * * cd /path/to/project && venv/bin/python manage.py welfare_reminders
"""
from django.core.management.base import BaseCommand

from welfare.services import run_reminders


class Command(BaseCommand):
    help = 'Send vaccination, support-plan review and restorative follow-up reminders.'

    def handle(self, *args, **options):
        n = run_reminders()
        self.stdout.write(self.style.SUCCESS(
            f'{n["immunisations"]} vaccination reminder(s), {n["plans"]} plan review(s), '
            f'{n["follow_ups"]} follow-up(s).'))
