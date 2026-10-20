"""
PostgreSQL in production: nightly backups use pg_dump (checked by reading the
dump back with pg_restore, credentials passed in the environment, never on the
command line), failures are recorded, old dumps are pruned, the health page
reports the database, and the SQLite → PostgreSQL copy refuses to run unless
the portal really is on PostgreSQL.
"""
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest import mock

from django.core.management import CommandError, call_command
from django.db import connections
from django.test import override_settings

from security import platform

from .factories import TenantTestCase, make_school


def completed(code=0, out='', err=''):
    return subprocess.CompletedProcess(args=[], returncode=code, stdout=out, stderr=err)


class PostgresBackupTests(TenantTestCase):

    def setUp(self):
        super().setUp()
        make_school('alpha')
        self.dir = tempfile.mkdtemp(prefix='pg-backups-')
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.override = override_settings(BACKUP_DIR=self.dir, BACKUP_OFFSITE_DIR='', BACKUP_OFFSITE_COMMAND='')
        self.override.enable()
        self.addCleanup(self.override.disable)
        # Only connection details SQLite ignores, so the test database keeps working.
        db = {'USER': 'school', 'PASSWORD': 's3cret', 'HOST': 'db.internal', 'PORT': '5432',
              'OPTIONS': dict(connections['default'].settings_dict.get('OPTIONS') or {}, sslmode='require')}
        patches = [mock.patch('security.platform.is_postgres', return_value=True),
                   mock.patch('security.platform.is_sqlite', return_value=False),
                   mock.patch('security.platform.shutil.which', return_value='/usr/bin/pg_dump'),
                   mock.patch.dict(connections['default'].settings_dict, db)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.calls = []

    def fake_run(self, args, env=None, **kw):
        self.calls.append((args, env))
        if args[0] == 'pg_dump':
            Path(args[args.index('--file') + 1]).write_bytes(b'PGDMP' + b'\0' * 2048)
            return completed()
        return completed(out='; Archive\n1234; 0 16390 TABLE DATA public users_user school\n'
                             '1235; 0 16391 TABLE DATA public results_termresult school\n')

    def test_nightly_dump(self):
        with mock.patch('security.platform.subprocess.run', side_effect=self.fake_run):
            rec = platform.run_backup('MANUAL')
        self.assertTrue(rec.ok, rec.error)
        self.assertTrue(rec.file_name.startswith('db-') and rec.file_name.endswith('.pgdump'))
        self.assertEqual(rec.integrity, 'ok (2 tables)')
        (dump_args, env), (list_args, _env) = self.calls
        self.assertEqual(dump_args[:3], ['pg_dump', '--format=custom', '--compress=6'])
        self.assertEqual(list_args[:2], ['pg_restore', '--list'])
        self.assertNotIn('s3cret', ' '.join(dump_args))                        # never on the command line
        self.assertEqual((env['PGPASSWORD'], env['PGHOST'], env['PGUSER'], env['PGSSLMODE']),
                         ('s3cret', 'db.internal', 'school', 'require'))
        self.assertEqual(platform.backup_path(rec.file_name).name, rec.file_name)  # downloadable

    def test_failed_dump_is_recorded(self):
        with mock.patch('security.platform.subprocess.run',
                        return_value=completed(1, err='pg_dump: error: connection to server failed')):
            rec = platform.run_backup('MANUAL')
        self.assertFalse(rec.ok)
        self.assertIn('connection to server failed', rec.error)
        self.assertEqual(list(Path(self.dir).glob('*.pgdump')), [])

    def test_unreadable_dump_is_not_a_good_backup(self):
        def run(args, **kw):
            if args[0] == 'pg_dump':
                Path(args[args.index('--file') + 1]).write_bytes(b'x')
                return completed()
            return completed(1, err='pg_restore: error: input file does not appear to be a valid archive')
        with mock.patch('security.platform.subprocess.run', side_effect=run):
            rec = platform.run_backup('MANUAL')
        self.assertFalse(rec.ok)
        self.assertIn('could not be read back', rec.error)

    def test_old_dumps_are_pruned(self):
        import os
        import time
        old = Path(self.dir) / 'db-20200101-000000.pgdump'
        old.write_bytes(b'x')
        os.utime(old, (time.time() - 40 * 86400,) * 2)
        self.assertEqual(platform._prune(), 1)
        self.assertFalse(old.exists())

    def test_auto_backup_runs_on_postgres(self):
        self.assertTrue(platform.can_back_up())


class CopyCommandTests(TenantTestCase):

    def test_refuses_unless_the_portal_is_on_postgres(self):
        with mock.patch.object(connections['default'], 'vendor', 'sqlite'):
            with self.assertRaisesMessage(CommandError, 'not set to PostgreSQL'):
                call_command('copy_sqlite_to_postgres', sqlite='/nonexistent.sqlite3')

    def test_needs_the_sqlite_file(self):
        with mock.patch.object(connections['default'], 'vendor', 'postgresql'):
            with self.assertRaisesMessage(CommandError, 'No SQLite file at /nonexistent.sqlite3'):
                call_command('copy_sqlite_to_postgres', sqlite='/nonexistent.sqlite3')
