"""
Email administrators about staff contracts that end within 30 days (each
contract is mentioned once). Run once a day, e.g. at 07:30:

    30 7 * * * cd /path/to/project && venv/bin/python manage.py hr_reminders
"""
from django.core.management.base import BaseCommand

from hr.services import send_reminders
from tenants.middleware import clear_current_tenant, set_current_tenant
from tenants.models import Tenant


class Command(BaseCommand):
    help = 'Remind school administrators about staff contracts ending soon.'

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=30)

    def handle(self, *args, **options):
        total = 0
        for tenant in Tenant.objects.filter(status__in=('ACTIVE', 'TRIAL')):
            set_current_tenant(tenant)
            try:
                total += len(send_reminders(tenant, options['days']))
            finally:
                clear_current_tenant()
        self.stdout.write(self.style.SUCCESS(f'Reminded about {total} contract{"s" if total != 1 else ""}.'))
