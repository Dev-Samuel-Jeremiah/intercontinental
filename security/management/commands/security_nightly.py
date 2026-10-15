"""Nightly: retention rules, breach and data-request reminders, old sign-in records.  Cron ~02:00."""
from django.core.management.base import BaseCommand

from security import services as svc


class Command(BaseCommand):
    help = 'Run retention rules, send breach and data-request reminders and clear old sign-in records.'

    def handle(self, *args, **opts):
        out = svc.nightly()
        self.stdout.write(self.style.SUCCESS(
            f"Retention removed {out['retention']}, reminders {out['reminders']}, cleaned {out['cleaned']}."))
