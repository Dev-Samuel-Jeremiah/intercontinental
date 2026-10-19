"""
The school AI's nightly learning: remember today, update the term-by-term
memory and the knowledge base, retrain the models (a new version only replaces
the active one if it does better on the latest term), and predict.

    python manage.py school_ai_learn

Run daily by deploy/intercontinental-ai.timer (see deploy/DEPLOY.md).
"""
from django.core.management.base import BaseCommand

from schoolai import training
from tenants.middleware import clear_current_tenant, set_current_tenant
from tenants.models import Tenant


class Command(BaseCommand):
    help = "Run the school AI's learning cycle for every school."

    def add_arguments(self, parser):
        parser.add_argument('--trigger', default='SCHEDULE', choices=['SCHEDULE', 'AUTO', 'MANUAL'])

    def handle(self, *args, **options):
        for tenant in Tenant.objects.filter(status='ACTIVE'):
            set_current_tenant(tenant)
            try:
                run = training.learn(tenant, options['trigger'])
            finally:
                clear_current_tenant()
            self.stdout.write(f'{tenant.name}: {"OK" if run.ok else "with problems"}')
            for s in run.steps:
                mark = {True: '✓', None: '·', False: '✗'}[s['ok']]
                self.stdout.write(f'  {mark} {s["step"]}: {s["result"]}')
