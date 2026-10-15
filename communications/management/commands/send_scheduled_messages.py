"""
Send campaigns whose scheduled time has come. Run every five minutes:

    */5 * * * * cd /path/to/project && venv/bin/python manage.py send_scheduled_messages
"""
from django.core.management.base import BaseCommand

from communications.services import run_scheduled_campaigns
from tenants.middleware import clear_current_tenant, set_current_tenant
from tenants.models import Tenant


class Command(BaseCommand):
    help = 'Send scheduled campaigns that are due.'

    def handle(self, *args, **options):
        total = 0
        for tenant in Tenant.objects.filter(status__in=('ACTIVE', 'TRIAL')):
            set_current_tenant(tenant)
            try:
                total += run_scheduled_campaigns()['processed']
            finally:
                clear_current_tenant()
        self.stdout.write(self.style.SUCCESS(f'Sent {total} scheduled campaign{"s" if total != 1 else ""}.'))
