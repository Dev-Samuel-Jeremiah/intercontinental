"""Encrypt protected notes that were saved before field encryption was switched on (F453)."""
from django.apps import apps
from django.core.management.base import BaseCommand

from security.crypto import EncryptedTextField, encrypt, is_encrypted


class Command(BaseCommand):
    help = 'Encrypt existing rows of every EncryptedTextField. Safe to run more than once.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **opts):
        from django.db import connection
        total = 0
        for model in apps.get_models():
            fields = [f for f in model._meta.fields if isinstance(f, EncryptedTextField)]
            if not fields:
                continue
            table = connection.ops.quote_name(model._meta.db_table)
            pk = connection.ops.quote_name(model._meta.pk.column)
            for f in fields:
                col = connection.ops.quote_name(f.column)
                with connection.cursor() as cur:
                    cur.execute(f'SELECT {pk}, {col} FROM {table}')
                    rows = [(i, v) for i, v in cur.fetchall() if v and not is_encrypted(v)]
                    if not opts['dry_run']:
                        for i, v in rows:
                            cur.execute(f'UPDATE {table} SET {col} = %s WHERE {pk} = %s', [encrypt(v), i])
                total += len(rows)
                self.stdout.write(f'{model._meta.label}.{f.name}: {len(rows)} rows')
        verb = 'would be encrypted' if opts['dry_run'] else 'encrypted'
        self.stdout.write(self.style.SUCCESS(f'{total} values {verb}.'))
