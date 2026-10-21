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


class LandingPageTests(TenantTestCase):
    """The public landing page: the portal's own look, sign-in for all three kinds of user, real events only."""

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha')

    def test_landing_page(self):
        from datetime import timedelta
        from django.utils import timezone
        from curriculum.models import CalendarEvent
        from tenants.middleware import set_current_tenant
        set_current_tenant(self.school)
        today = timezone.localdate()
        CalendarEvent.objects.create(tenant=self.school, title='Inter-house sports', kind='SPORTS',
                                     start_date=today + timedelta(days=5), audience='ALL')
        CalendarEvent.objects.create(tenant=self.school, title='Staff training day', kind='MEETING',
                                     start_date=today + timedelta(days=6), audience='STAFF')
        CalendarEvent.objects.create(tenant=self.school, title='Last year prize giving', kind='OTHER',
                                     start_date=today - timedelta(days=30), audience='ALL')
        page = self.client.get('/', HTTP_HOST=self.host(self.school))
        self.assertEqual(page.status_code, 200)
        for url in ('/staff/login/', '/student/login/', '/parent/login/', '/apply/'):
            self.assertContains(page, f'href="{url}', msg_prefix=url)
        CalendarEvent.objects.create(tenant=self.school, title='PTA general meeting', kind='PTA',
                                     start_date=today + timedelta(days=7), audience='PARENTS')
        page = self.client.get('/', HTTP_HOST=self.host(self.school))
        self.assertContains(page, 'Inter-house sports')          # upcoming, for everyone
        self.assertContains(page, 'PTA general meeting')         # for parents and students
        self.assertNotContains(page, '1998')
        self.assertNotContains(page, 'Staff training day')       # staff-only events stay private
        self.assertNotContains(page, 'Last year prize giving')   # past events are not shown
        self.assertNotContains(page, 'cdn.tailwindcss.com')      # no prototyping CDN on the live site
        self.assertContains(page, f'© {today.year}')


class LandingSettingsTests(TenantTestCase):
    """Administrators change the home page's background (picture or video) and campus photos under Branding."""

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha')
        self.admin = make_admin(self.school)
        self.teacher = make_teacher(self.school)
        self.addCleanup(self._clean_files)

    def _clean_files(self):
        from saas_admin.models import LandingPage
        for page in LandingPage.objects.all():
            for f in ('hero_image', 'hero_video', 'photo1', 'photo2', 'photo3'):
                if getattr(page, f):
                    getattr(page, f).delete(save=False)

    @staticmethod
    def picture(name='p.png'):
        from io import BytesIO
        from django.core.files.uploadedfile import SimpleUploadedFile
        from PIL import Image
        buf = BytesIO()
        Image.new('RGB', (40, 30), (20, 60, 120)).save(buf, 'PNG')
        return SimpleUploadedFile(name, buf.getvalue(), content_type='image/png')

    def save(self, user, data):
        self.client.force_login(user)
        return self.client.post('/account/branding/', dict(action='landing', **data), HTTP_HOST=self.host(self.school))

    def home(self):
        self.client.logout()
        return self.client.get('/', HTTP_HOST=self.host(self.school)).content.decode()

    def test_background_picture_photos_and_captions(self):
        self.save(self.admin, {'hero_image': self.picture('banner.png'), 'photo2': self.picture('robots.png'),
                               'photo2_label': 'Clubs', 'photo2_title': 'Robotics club'})
        html = self.home()
        self.assertIn('/media/landing/banner', html)
        self.assertIn('/media/landing/robots', html)
        self.assertIn('Robotics club', html)
        self.assertIn('gallery-lab.jpg', html)                      # untouched slots keep the built-in photo
        self.save(self.admin, {'remove_hero_image': '1', 'remove_photo2': '1'})
        html = self.home()
        self.assertIn('images/landing/campus.jpg', html)
        self.assertIn('gallery-makerspace.jpg', html)

    def test_background_video(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        mp4 = SimpleUploadedFile('clip.mp4', b'\x00\x00\x00\x18ftypmp42' + b'\x00' * 64, content_type='video/mp4')
        self.save(self.admin, {'hero_video': mp4, 'use_video': '1'})
        html = self.home()
        self.assertIn('<video class="bg"', html)
        self.assertIn('/media/landing/clip', html)
        self.save(self.admin, {})                                     # switched off: back to the picture
        self.assertNotIn('<video class="bg"', self.home())

    def test_bad_files_are_refused(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from saas_admin.models import LandingPage
        fake_png = SimpleUploadedFile('x.png', b'not really a picture', content_type='image/png')
        fake_mp4 = SimpleUploadedFile('x.mp4', b'<html>not a video</html>', content_type='video/mp4')
        script = SimpleUploadedFile('x.svg', b'<svg onload=alert(1)>', content_type='image/svg+xml')
        for data in ({'hero_image': fake_png}, {'hero_video': fake_mp4}, {'photo1': script}):
            page = self.save(self.admin, data)
            self.assertEqual(page.status_code, 302)
        page = LandingPage.objects.filter(tenant=self.school).first()
        self.assertFalse(page and (page.hero_image or page.hero_video or page.photo1))

    def test_only_administrators(self):
        from saas_admin.models import LandingPage
        self.save(self.teacher, {'hero_image': self.picture()})
        self.assertFalse(LandingPage.objects.filter(tenant=self.school).exclude(hero_image='').exists())


class SignInPageTests(TenantTestCase):
    """The sign-in pages share the portal's look and the school's own banner picture."""

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha')

    def test_sign_in_pages(self):
        host = self.host(self.school)
        staff = self.client.get('/staff/login/', HTTP_HOST=host)
        self.assertContains(staff, 'action="/staff/login/"')
        for target in ("'/parent/login/'", "'/student/login/'", '/family/login/'):
            self.assertContains(staff, target)                      # each role signs in at its own page
        self.assertContains(staff, 'images/landing/campus.jpg')       # the built-in picture until one is uploaded
        self.assertNotContains(staff, 'cdn.tailwindcss.com')
        parent = self.client.get('/parent/login/', HTTP_HOST=host)
        self.assertContains(parent, 'name="admission_number"')
        self.assertContains(parent, '/family/login/')

    def test_the_uploaded_banner_is_used(self):
        from saas_admin.models import LandingPage
        page = LandingPage.objects.create(tenant=self.school, hero_image='landing/our-campus.jpg')
        html = self.client.get('/staff/login/', HTTP_HOST=self.host(self.school)).content.decode()
        self.assertIn('/media/landing/our-campus.jpg', html)
        page.delete()

    def test_a_failed_student_sign_in_says_why(self):
        page = self.client.post('/student/login/', {'admission_number': 'NOPE1', 'password': 'x'},
                                HTTP_HOST=self.host(self.school), follow=True)
        self.assertContains(page, 'Invalid admission number')
