"""
Starter help-centre articles and product tours. Safe to run again: existing slugs/keys are left alone.

    python manage.py seed_help
"""
from django.core.management.base import BaseCommand

from saas_admin.help_content import ARTICLES, TOURS
from saas_admin.models import Article, Tour


class Command(BaseCommand):
    help = 'Add the starter help articles and product tours.'

    def handle(self, *args, **options):
        a = t = 0
        for item in ARTICLES:
            _, created = Article.objects.get_or_create(slug=item['slug'], defaults=item)
            a += created
        for item in TOURS:
            _, created = Tour.objects.get_or_create(key=item['key'], defaults=item)
            t += created
        self.stdout.write(self.style.SUCCESS(f'{a} article(s) and {t} tour(s) added.'))
