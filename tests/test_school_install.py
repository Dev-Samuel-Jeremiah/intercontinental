"""
Dedicated single-school installation: every host serves the one school, the
SaaS pages are gone, nothing can lock the school out, and the school-level
tools that remain (branding, sections, help, notices, export, backups) work.
"""
import io
import zipfile
from datetime import timedelta

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings
from django.utils import timezone

from saas_admin import services as svc
from saas_admin.models import Article, Backup, Branding, Campus, DataExport, Notice, Tour, TourSeen
from tenants.middleware import clear_current_tenant, set_current_tenant
from tenants.models import Tenant
from users.models import Staff, Student, User

from .factories import TenantTestCase, make_admin, make_class, make_school, make_student, make_teacher


@override_settings(ROOT_DOMAIN='localhost', ALLOWED_HOSTS=['*'], RUN_TASKS_EAGERLY=True, SCHOOL_SLUG='alpha')
class Base(TenantTestCase):

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha', name='Alpha International School')
        self.admin = make_admin(self.school)
        self.teacher = make_teacher(self.school, username='alphatch', full_name='Mr Teach')
        clear_current_tenant()

    def _get(self, path, host='portal.alpha.example', **kw):
        return self.client.get(path, HTTP_HOST=host, **kw)

    def _post(self, path, data=None, host='portal.alpha.example', **kw):
        return self.client.post(path, data or {}, HTTP_HOST=host, **kw)


class SingleSchoolRoutingTests(Base):

    def test_any_host_serves_the_school(self):
        for host in ('portal.alpha.example', 'localhost', '127.0.0.1', 'www.anything.ng'):
            r = self._get('/staff/login/', host=host)
            self.assertContains(r, 'Alpha International School', msg_prefix=host)

    def test_home_is_the_school_sign_in(self):
        r = self._get('/')
        self.assertIn(r.status_code, (200, 302))
        self.assertNotContains(self._get('/staff/login/'), 'Techmiary')

    def test_saas_pages_are_gone(self):
        self.client.force_login(self.admin)
        for path in ('/subscription/', '/subscription/upgrade/', '/console/', '/console/security/', '/start/',
                     '/pricing/', '/register/', '/free/', '/agent/', '/account/', '/support/', '/__debug__/',
                     '/tls-ask/'):
            self.assertEqual(self._get(path).status_code, 404, path)

    def test_no_status_or_trial_locks_the_school_out(self):
        self.client.force_login(self.admin)
        for status in ('PENDING', 'TRIAL', 'SUSPENDED', 'CANCELLED'):
            Tenant.objects.filter(pk=self.school.pk).update(status=status, trial_ends=timezone.localdate()
                                                            - timedelta(days=30))
            r = self._get('/dashboard/', follow=True)
            self.assertEqual(r.status_code, 200, status)

    def test_every_module_is_on(self):
        for code in ('finance', 'payroll', 'hostel', 'cbt', 'timetable', 'communications', 'anything'):
            self.assertTrue(self.school.has_feature(code))

    def test_portal_url_comes_from_settings(self):
        with override_settings(SITE_URL='https://portal.alpha.example'):
            self.assertEqual(self.school.portal_url, 'https://portal.alpha.example')

    def test_dashboard_has_no_vendor_or_billing(self):
        self.client.force_login(self.admin)
        html = self._get('/dashboard/', follow=True).content.decode()
        for needle in ('Techmiary', 'Subscription', 'Upgrade Plan', 'Free trial', 'fbq(', 'WhatsApp Community'):
            self.assertNotIn(needle, html)
        self.assertIn('Sections', html)

    def test_before_setup_explains_what_to_run(self):
        Tenant.objects.all().delete()
        cache_clear = __import__('django.core.cache', fromlist=['cache']).cache.clear
        cache_clear()
        r = self._get('/')
        self.assertContains(r, 'setup_school', status_code=503)


class SetupSchoolTests(TenantTestCase):

    def _run(self, *args):
        out = io.StringIO()
        call_command('setup_school', *args, stdout=out)
        return out.getvalue()

    def test_creates_school_admin_session_and_sections(self):
        out = self._run('--name', 'Intercontinental School Kano', '--slug', 'intercontinental',
                        '--admin-username', 'ict', '--admin-password', 'Temp-Pass-123', '--email', 'info@isk.ng')
        school = Tenant.objects.get(subdomain='intercontinental')
        self.assertEqual((school.name, school.status), ('Intercontinental School Kano', 'ACTIVE'))
        admin = User.objects.get(username='ict', tenant=school)
        self.assertTrue(admin.is_superuser and admin.must_change_password and not admin.is_staff)
        self.assertTrue(admin.check_password('Temp-Pass-123'))
        staff = Staff.objects.get(user=admin)
        self.assertEqual(staff.role, 'ADMIN')
        self.assertTrue(staff.can_access_finance and staff.can_publish_results)
        self.assertEqual(sorted(Campus.objects.filter(tenant=school).values_list('name', flat=True)),
                         ['Darun', 'Nursery & Primary', 'Secondary'])
        from academics.models import AcademicSession, Term
        self.assertTrue(AcademicSession.objects.filter(tenant=school, is_active=True).exists())
        self.assertTrue(Term.objects.filter(tenant=school, name='1st Term', is_active=True).exists())
        self.assertGreater(Article.objects.count(), 0)
        self.assertIn('Temp-Pass-123', out)

    def test_running_again_changes_nothing(self):
        self._run('--slug', 'intercontinental', '--admin-password', 'Temp-Pass-123')
        self._run('--slug', 'intercontinental')
        self.assertEqual(Tenant.objects.count(), 1)
        self.assertEqual(User.objects.filter(is_superuser=True).count(), 1)
        self.assertEqual(Campus.objects.count(), 3)

    def test_refuses_a_second_school(self):
        make_school('existing')
        with self.assertRaises(CommandError):
            self._run('--slug', 'intercontinental')


class SchoolToolsTests(Base):

    def students(self, n):
        cls = make_class(self.school, 'JSS 1A')
        for i in range(n):
            make_student(self.school, f'S/{i:03d}', full_name=f'Kid {i}', school_class=cls)
        clear_current_tenant()

    def test_branding(self):
        with self.assertRaises(svc.PlatformError):
            svc.save_branding(self.school, {'primary': 'red'})
        self.client.force_login(self.admin)
        r = self._post('/account/branding/', {'primary': '#1f6feb', 'accent': '#ecd09a',
                                              'brand_name': 'ISK Portal'})
        self.assertEqual(r.status_code, 302)
        b = Branding.objects.get(tenant=self.school)
        self.assertEqual(b.brand_name, 'ISK Portal')
        html = self._get('/dashboard/', follow=True).content.decode()
        self.assertIn('#1f6feb', html)
        self.assertIn('ISK Portal', html)

    def test_sections_page_and_assignment(self):
        set_current_tenant(self.school)
        darun = Campus.objects.create(tenant=self.school, name='Darun', code='DAR')
        sec = Campus.objects.create(tenant=self.school, name='Secondary', code='SEC')
        cls = make_class(self.school, 'Darun 1')
        make_student(self.school, 'D/1', school_class=cls)
        clear_current_tenant()
        self.client.force_login(self.admin)
        self.assertContains(self._get('/account/sections/'), 'Darun')
        self._post('/account/sections/', {'action': 'assign', 'campus': sec.pk, 'classes': [cls.pk]})
        self._post('/account/sections/', {'action': 'assign', 'campus': darun.pk, 'classes': [cls.pk]})
        rows = {r['campus'].name: r for r in svc.campus_summary(self.school)}
        self.assertEqual((rows['Darun']['students'], rows['Secondary']['classes']), (1, 0))
        self.client.force_login(self.teacher)
        self.assertEqual(self._get('/account/sections/').status_code, 302)   # admins only

    def test_help_centre_and_tours(self):
        call_command('seed_help', stdout=io.StringIO())
        self.client.force_login(self.teacher)
        self.assertContains(self._get('/help/?q=wallet'), 'family wallet')
        self.assertNotContains(self._get('/help/'), 'support ticket')
        tour = Tour.objects.get(key='welfare-overview')
        self.assertEqual(svc.tour_for(self.teacher, '/welfare/incidents/'), tour)
        self._post(f'/support/tours/{tour.pk}/seen/')
        self.assertTrue(TourSeen.objects.filter(user=self.teacher, tour=tour).exists())

    def test_notices(self):
        n = Notice.objects.create(message='Inter-house sports on Friday', starts=timezone.now() - timedelta(hours=1),
                                  audience='ADMINS')
        self.assertEqual(svc.notices_for(self.admin, self.school), [n])
        self.assertEqual(svc.notices_for(self.teacher, self.school), [])
        self.client.force_login(self.admin)
        self._post(f'/support/notices/{n.pk}/dismiss/')
        self.assertEqual(svc.notices_for(self.admin, self.school), [])

    def test_export_backup_restore(self):
        self.students(2)
        set_current_tenant(self.school)
        Campus.objects.create(tenant=self.school, name='Secondary')
        clear_current_tenant()
        ex = DataExport.objects.create(tenant=self.school, requested_by=self.admin)
        svc.build_export(ex)
        self.assertEqual(ex.status, 'READY')
        with ex.file.open('rb') as fh, zipfile.ZipFile(fh) as z:
            self.assertIn('users/Student.csv', z.namelist())
            self.assertIn('saas_admin/Campus.csv', z.namelist())
            self.assertNotIn('parent_term_password', z.read('users/Student.csv').decode().splitlines()[0])
        b = svc.make_backup(self.school, 'MANUAL')
        st = Student._base_manager.filter(tenant=self.school).first()
        name = st.full_name
        st.delete()
        self.assertGreaterEqual(svc.restore_backup(b, dry_run=False)['restored'], 1)
        self.assertTrue(Student._base_manager.filter(full_name=name).exists())

    def test_data_page_backup_and_download(self):
        self.client.force_login(self.admin)
        self.assertContains(self._get('/account/data/'), 'Back up now')
        self._post('/account/data/', {'action': 'backup'})
        b = Backup.objects.get(tenant=self.school)
        r = self._get(f'/account/backups/{b.pk}/download/')
        self.assertEqual(r.status_code, 200)
        self.client.force_login(self.teacher)
        self.assertEqual(self._get(f'/account/backups/{b.pk}/download/').status_code, 302)

    def test_nightly_backup(self):
        out = io.StringIO()
        call_command('nightly_backup', stdout=out)
        self.assertIn('backups: 1', out.getvalue())
        self.assertEqual(Backup.objects.filter(tenant=self.school, kind='AUTO').count(), 1)
