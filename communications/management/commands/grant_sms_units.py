"""
Give a school SMS units without payment (platform staff only), e.g. a free
starter bundle or a manual bank-transfer purchase:

    python manage.py grant_sms_units greenfield 1000 --note "Bank transfer 12 Oct"
"""
from django.core.management.base import BaseCommand, CommandError

from communications.connect import ConnectError, grant_units, wallet
from tenants.middleware import clear_current_tenant, set_current_tenant
from tenants.models import Tenant


class Command(BaseCommand):
    help = 'Add SMS units to a school.'

    def add_arguments(self, parser):
        parser.add_argument('subdomain')
        parser.add_argument('units', type=int)
        parser.add_argument('--note', default='Free units')

    def handle(self, subdomain, units, note, **options):
        tenant = Tenant.objects.filter(subdomain=subdomain).first()
        if not tenant:
            raise CommandError(f'No school with subdomain "{subdomain}".')
        set_current_tenant(tenant)
        try:
            grant_units(tenant, units, note=note)
            balance = wallet(tenant).balance
        except ConnectError as problem:
            raise CommandError(str(problem))
        finally:
            clear_current_tenant()
        self.stdout.write(self.style.SUCCESS(f'{tenant.name} now has {balance:,} SMS units.'))
