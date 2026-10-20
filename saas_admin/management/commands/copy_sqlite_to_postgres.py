"""
Move a school from SQLite to PostgreSQL: copy every row of every table, keeping
all ids, so links, uploaded files, sign-ins and history stay exactly as they
were.

    # 1. .env points at the new PostgreSQL database (DB_ENGINE=postgresql, DB_NAME, ...)
    # 2. create its tables:
    python manage.py migrate
    # 3. copy (the portal should be stopped so nothing changes half-way):
    python manage.py copy_sqlite_to_postgres --sqlite /srv/intercontinental/data/db.sqlite3

Before anything is written it checks that both databases have the same
migrations applied and that every text value fits PostgreSQL's length limits
(SQLite never enforces them). The copy runs in one transaction: it either all
arrives or nothing changes. Encrypted notes stay readable as long as SECRET_KEY
and FIELD_ENCRYPTION_KEY are unchanged.
"""
import time

from django.apps import apps
from django.core.management.base import BaseCommand, CommandError
from django.core.management.color import no_style
from django.db import DEFAULT_DB_ALIAS, connections, models, transaction
from django.db.models.functions import Length

SOURCE = 'sqlite_source'
BATCH = 2000


class Command(BaseCommand):
    help = 'Copy every row from a SQLite database into this PostgreSQL database (ids kept).'

    def add_arguments(self, parser):
        parser.add_argument('--sqlite', required=True, help='Path to the SQLite database file to copy from.')
        parser.add_argument('--replace', action='store_true',
                            help='Empty the PostgreSQL database first if it already holds data.')
        parser.add_argument('--trim-long-text', action='store_true',
                            help='Cut text that is longer than its field allows instead of stopping.')

    def handle(self, *args, **opts):
        target = connections[DEFAULT_DB_ALIAS]
        if target.vendor != 'postgresql':
            raise CommandError('The portal is not set to PostgreSQL. Set DB_ENGINE=postgresql (and DB_NAME, '
                               'DB_USER, DB_PASSWORD, DB_HOST) in .env, run migrate, then run this again.')
        self._open_source(opts['sqlite'])
        source = connections[SOURCE]
        self._same_migrations(source, target)
        tables = [m for m in apps.get_models(include_auto_created=True)
                  if m._meta.managed and not m._meta.proxy and not m._meta.swapped]
        tables = self._existing(source, tables)
        self._empty_or_replace(target, tables, opts['replace'])
        long_text = self._long_text(tables)
        if long_text and not opts['trim_long_text']:
            lines = '\n'.join(f'  {label}.{field}: {n} value(s) longer than {limit}' for label, field, limit, n in long_text)
            raise CommandError('Some text is longer than PostgreSQL will accept (SQLite never checked):\n'
                               f'{lines}\nShorten it in the portal, or run again with --trim-long-text.')

        started, total = time.monotonic(), 0
        with transaction.atomic(using=DEFAULT_DB_ALIAS):
            with target.cursor() as cur:
                # Foreign keys are checked at the end of the transaction, so the order rows arrive in
                # does not matter; the tables made by migrate (content types, permissions) are replaced
                # by the source's own so every id lines up.
                cur.execute('SET CONSTRAINTS ALL DEFERRED')
                names = ', '.join(target.ops.quote_name(m._meta.db_table) for m in tables)
                cur.execute(f'TRUNCATE {names} RESTART IDENTITY CASCADE')
            for model in tables:
                copied = self._copy(model, opts['trim_long_text'])
                total += copied
                if copied:
                    self.stdout.write(f'  {model._meta.label:45} {copied:>8,}')
            with target.cursor() as cur:
                for sql in target.ops.sequence_reset_sql(no_style(), tables):
                    cur.execute(sql)
        self._verify(tables)
        self.stdout.write(self.style.SUCCESS(
            f'Copied {total:,} rows from {len(tables)} tables in {time.monotonic() - started:.0f}s. '
            f'Row counts match. Start the portal and sign in to check.'))

    # ── checks ───────────────────────────────────────────────────────────
    def _open_source(self, path):
        from pathlib import Path
        if not Path(path).is_file():
            raise CommandError(f'No SQLite file at {path}.')
        connections.settings[SOURCE] = connections.configure_settings({
            DEFAULT_DB_ALIAS: connections.settings[DEFAULT_DB_ALIAS],
            SOURCE: {'ENGINE': 'django.db.backends.sqlite3', 'NAME': path, 'OPTIONS': {'timeout': 30}},
        })[SOURCE]

    def _applied(self, connection):
        with connection.cursor() as cur:
            cur.execute('SELECT app, name FROM django_migrations')
            return set(cur.fetchall())

    def _same_migrations(self, source, target):
        src, dst = self._applied(source), self._applied(target)
        if src != dst:
            only_src, only_dst = sorted(src - dst)[:5], sorted(dst - src)[:5]
            raise CommandError('The two databases are at different versions.\n'
                               f'  Only in SQLite: {only_src or "-"}\n  Only in PostgreSQL: {only_dst or "-"}\n'
                               'Run "python manage.py migrate" against both (same code), then try again.')

    def _existing(self, source, tables):
        names = set(source.introspection.table_names())
        return [m for m in tables if m._meta.db_table in names]

    def _empty_or_replace(self, target, tables, replace):
        from django.contrib.auth import get_user_model
        user_table = get_user_model()._meta.db_table
        with target.cursor() as cur:
            cur.execute(f'SELECT EXISTS (SELECT 1 FROM {target.ops.quote_name(user_table)})')
            has_data = cur.fetchone()[0]
        if has_data and not replace:
            raise CommandError('The PostgreSQL database already has users in it. Run again with --replace to '
                               'empty it first (everything in it will be replaced by the SQLite copy).')

    def _long_text(self, tables):
        found = []
        for model in tables:
            for f in model._meta.concrete_fields:
                if isinstance(f, (models.CharField, models.SlugField)) and f.max_length and not f.primary_key:
                    n = (model._base_manager.using(SOURCE).annotate(_len=Length(f.attname))
                         .filter(_len__gt=f.max_length).count())
                    if n:
                        found.append((model._meta.label, f.name, f.max_length, n))
        return found

    # ── copying ──────────────────────────────────────────────────────────
    def _clean(self, obj, model, trim):
        for f in model._meta.concrete_fields:
            value = getattr(obj, f.attname, None)
            if isinstance(value, str):
                if '\x00' in value:                                   # PostgreSQL text cannot hold NUL
                    value = value.replace('\x00', '')
                if trim and isinstance(f, models.CharField) and f.max_length and len(value) > f.max_length:
                    value = value[:f.max_length]
                setattr(obj, f.attname, value)
        return obj

    def _copy(self, model, trim):
        # "Created at" / "updated at" fields would otherwise be stamped with the time of the copy.
        stamped = [(f, f.auto_now, f.auto_now_add) for f in model._meta.concrete_fields
                   if getattr(f, 'auto_now', False) or getattr(f, 'auto_now_add', False)]
        for f, _now, _add in stamped:
            f.auto_now = f.auto_now_add = False
        try:
            rows = model._base_manager.using(SOURCE).order_by('pk').iterator(chunk_size=BATCH)
            batch, copied = [], 0
            for obj in rows:
                obj._state.db = DEFAULT_DB_ALIAS
                batch.append(self._clean(obj, model, trim))
                if len(batch) >= BATCH:
                    model._base_manager.using(DEFAULT_DB_ALIAS).bulk_create(batch)
                    copied += len(batch)
                    batch = []
            if batch:
                model._base_manager.using(DEFAULT_DB_ALIAS).bulk_create(batch)
                copied += len(batch)
            return copied
        finally:
            for f, now, add in stamped:
                f.auto_now, f.auto_now_add = now, add

    def _verify(self, tables):
        wrong = []
        for model in tables:
            a = model._base_manager.using(SOURCE).count()
            b = model._base_manager.using(DEFAULT_DB_ALIAS).count()
            if a != b:
                wrong.append(f'{model._meta.label}: SQLite {a}, PostgreSQL {b}')
        if wrong:
            raise CommandError('Row counts differ after the copy:\n  ' + '\n  '.join(wrong))
