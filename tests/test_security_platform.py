"""
Security & platform upgrade: the staff permission switches really decide who
may use finance, payment approval, boarding and the store (and refusals are
audited); field-level audit of scores, payments, fees, exams and settings;
integrity-checked daily backups with a run log, the automatic catch-up
backup, and the platform health page.
"""
import gzip
import shutil
import sqlite3
import tempfile
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock

from django.test import override_settings
from django.utils import timezone

from academics.models import AcademicSession, Term
from cbt.models import Exam
from finance.models import FeeStructure
from results.models import TermResult
from security import platform
from security.access import allowed
from security.models import AuditEvent, SystemBackup
from tenants.middleware import set_current_tenant
from users.models import Staff, Subject

from .factories import TenantTestCase, make_admin, make_class, make_school, make_student, make_teacher


class Base(TenantTestCase):

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha')
        self.admin = make_admin(self.school)
        self.teacher = make_teacher(self.school, username='tolu', full_name='Tolu Ade')
        self.bursar = make_teacher(self.school, username='bursar', full_name='Bisi Bursar')
        set_current_tenant(self.school)
        Staff.objects.filter(user=self.bursar).update(role='ACCOUNT')

    def get(self, user, path):
        self.client.force_login(user)
        return self.client.get(path, HTTP_HOST=self.host(self.school))


class AccessTests(Base):

    def test_teachers_no_longer_get_finance_hostel_or_store(self):
        for path in ('/finance/admin/', '/hostel/', '/inventory/dashboard/'):
            self.assertEqual(self.get(self.teacher, path).status_code, 403, path)
        self.assertEqual(AuditEvent.objects.filter(action='DENIED', user=self.teacher).count(), 3)
        self.assertEqual(self.get(self.bursar, '/finance/admin/').status_code, 200)
        self.assertEqual(self.get(self.admin, '/inventory/dashboard/').status_code, 200)

    def test_switches_grant_and_payment_approval_is_separate(self):
        staff = Staff.objects.get(user=self.teacher)
        staff.can_access_finance = True
        staff.save()
        set_current_tenant(self.school)
        self.assertTrue(allowed(self.teacher, 'can_access_finance'))
        self.assertFalse(allowed(self.teacher, 'can_approve_payments'))
        self.assertEqual(self.get(self.teacher, '/finance/admin/').status_code, 200)
        self.client.force_login(self.teacher)
        r = self.client.post('/finance/admin/wallet/adjust/', {}, HTTP_HOST=self.host(self.school))
        self.assertEqual(r.status_code, 403)
        self.assertTrue(allowed(self.bursar, 'can_approve_payments'))
        staff.can_access_inventory = True
        staff.save()
        self.assertEqual(self.get(self.teacher, '/inventory/dashboard/').status_code, 200)


class AuditTests(Base):

    def setUp(self):
        super().setUp()
        session = AcademicSession.objects.create(tenant=self.school, name='2026/2027', is_active=True)
        self.term = Term.objects.create(tenant=self.school, session=session, name='1st Term', is_active=True)
        self.jss1 = make_class(self.school, 'JSS 1')
        self.maths = Subject.objects.create(tenant=self.school, name='Mathematics')
        self.ada = make_student(self.school, 'A1', 'Ada Obi', school_class=self.jss1)
        set_current_tenant(self.school)

    def test_score_changes_are_audited(self):
        tr = TermResult.objects.create(tenant=self.school, student=self.ada, class_assigned=self.jss1,
                                       subject=self.maths, session=self.term.session, term=self.term, ca1_score=6)
        tr.ca1_score, tr.exam_score = 9, 55
        tr.save()
        e = AuditEvent.objects.get(action='SCORES')
        self.assertIn('Ada Obi · Mathematics · 1st Term', e.summary)
        self.assertIn('6 → 9', e.summary)
        self.assertEqual(e.category, 'RESULTS')
        tr.save()                                                  # nothing changed — nothing recorded
        self.assertEqual(AuditEvent.objects.filter(action='SCORES').count(), 1)
        tr.delete()
        self.assertTrue(AuditEvent.objects.filter(action='DELETED', summary__contains='Term Result').exists())

    def test_fees_exams_and_settings_are_audited(self):
        fee = FeeStructure.objects.create(tenant=self.school, name='Tuition', amount=Decimal('150000'))
        fee.amount = Decimal('175000')
        fee.save()
        summaries = list(AuditEvent.objects.filter(action='FEES').values_list('summary', flat=True))
        self.assertTrue(any('created' in s for s in summaries))
        self.assertTrue(any('150000' in s and '175000' in s for s in summaries))
        exam = Exam.objects.create(tenant=self.school, title='Maths test', subject=self.maths, start_time=timezone.now(),
                                   end_time=timezone.now() + timedelta(hours=1), duration_minutes=30)
        exam.published = True
        exam.save()
        self.assertTrue(AuditEvent.objects.filter(action='EXAM', summary__contains='published: False → True').exists())
        self.school.name = 'Alpha International'
        self.school.save()
        self.assertTrue(AuditEvent.objects.filter(action='SETTINGS', tenant=self.school,
                                                  summary__contains='Alpha International').exists())
        page = self.get(self.admin, '/security/audit/?category=MONEY')
        self.assertContains(page, 'Tuition')


class BackupTests(Base):

    def setUp(self):
        super().setUp()
        self.dir = tempfile.mkdtemp(prefix='backups-test-')
        self.offsite = tempfile.mkdtemp(prefix='offsite-test-')
        self.override = override_settings(BACKUP_DIR=self.dir, BACKUP_OFFSITE_DIR=self.offsite,
                                          RUN_TASKS_EAGERLY=True, AUTO_BACKUP=True)
        self.override.enable()
        # The test database lives in memory; snapshot a small database file instead
        self.dbfile = Path(self.dir).parent / f'{Path(self.dir).name}-school.sqlite3'
        conn = sqlite3.connect(self.dbfile)
        conn.execute('CREATE TABLE pupils (id INTEGER PRIMARY KEY, name TEXT)')
        conn.executemany('INSERT INTO pupils (name) VALUES (?)', [('Ada',), ('Bola',)])
        conn.commit()
        conn.close()
        self.patch = mock.patch('security.platform.database_path', return_value=self.dbfile)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.override.disable()
        self.dbfile.unlink(missing_ok=True)
        shutil.rmtree(self.dir, ignore_errors=True)
        shutil.rmtree(self.offsite, ignore_errors=True)
        super().tearDown()

    def test_backup_is_checked_recorded_and_copied(self):
        rec = platform.run_backup('MANUAL', user=self.admin)
        self.assertTrue(rec.ok, rec.error)
        self.assertEqual(rec.integrity, 'ok')
        path = Path(self.dir) / rec.file_name
        self.assertTrue(path.exists())
        self.assertTrue((Path(self.offsite) / rec.file_name).exists())
        with tempfile.TemporaryDirectory() as tmp:                 # the snapshot really is a database
            restored = Path(tmp) / 'db.sqlite3'
            with gzip.open(path) as src, open(restored, 'wb') as out:
                shutil.copyfileobj(src, out)
            conn = sqlite3.connect(restored)
            self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual(conn.execute('SELECT count(*) FROM pupils').fetchone()[0], 2)
            conn.close()
        self.assertTrue(AuditEvent.objects.filter(action='BACKUP', summary__contains='made').exists())
        self.assertFalse(platform.backup_due())

    def test_failure_is_recorded_and_admins_told(self):
        from notifications.models import Notification
        with override_settings(BACKUP_OFFSITE_DIR='', BACKUP_OFFSITE_COMMAND='false'):
            rec = platform.run_backup('SCHEDULE')
        self.assertTrue(rec.ok)                                    # the backup itself is fine…
        self.assertTrue(rec.offsite.startswith('FAILED'))          # …but the off-site copy is flagged
        self.assertTrue(Notification.objects.filter(title='Backup failed').exists())

    def test_automatic_catch_up_backup(self):
        from django.core.cache import cache
        cache.clear()
        self.assertTrue(platform.backup_due())
        self.assertTrue(platform.maybe_auto_backup())
        self.assertEqual(SystemBackup.objects.filter(trigger='AUTO', ok=True).count(), 1)
        cache.clear()
        self.assertFalse(platform.maybe_auto_backup())             # not due again for a day

    def test_platform_page_and_downloads(self):
        rec = platform.run_backup('MANUAL', user=self.admin)
        page = self.get(self.admin, '/security/platform/')
        self.assertContains(page, 'Last good backup')
        self.assertContains(page, 'System health')
        r = self.get(self.admin, f'/security/platform/backups/{rec.file_name}/')
        self.assertEqual(r.status_code, 200)
        self.assertTrue(AuditEvent.objects.filter(action='EXPORT', summary__contains=rec.file_name).exists())
        self.assertIn(self.get(self.admin, '/security/platform/backups/..%2Fdb.sqlite3/').status_code, (302, 404))  # refused
        bursar = self.get(self.bursar, '/security/platform/')
        self.assertNotEqual(bursar.status_code, 200)
        self.assertEqual(self.get(self.teacher, f'/security/platform/backups/{rec.file_name}/').status_code != 200, True)
