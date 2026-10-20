"""
Production hardening: the uptime health check, redirects that never leave the
site, CSRF on every page that changes data, unsigned WhatsApp webhooks refused,
safe PDF rendering (no scripts, no local files), and test data refused on a
live site.
"""
from unittest import mock

from django.core.management import CommandError, call_command
from django.test import Client, RequestFactory, override_settings

from academics.models import AcademicSession, Term
from lms_project import pdf
from lms_project.redirects import is_safe

from .factories import TenantTestCase, make_admin, make_school


class HardeningTests(TenantTestCase):

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha')
        self.admin = make_admin(self.school, username='head', password='admin-pass-1')

    def test_health_check(self):
        page = self.client.get('/healthz/', HTTP_HOST=self.host(self.school))
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.json(), {'status': 'ok'})
        self.assertIn('no-cache', page['Cache-Control'])

    def test_login_never_redirects_off_site(self):
        for nxt, lands in (('https://evil.example/phish', '/dashboard/'), ('//evil.example/', '/dashboard/'),
                           ('/dashboard/admin/', '/dashboard/admin/')):
            client = Client()
            page = client.post(f'/staff/login/?next={nxt}', {'username': 'head', 'password': 'admin-pass-1'},
                               HTTP_HOST=self.host(self.school))
            self.assertEqual(page.status_code, 302, nxt)
            self.assertEqual(page['Location'], lands, nxt)

    def test_is_safe(self):
        request = RequestFactory().get('/', HTTP_HOST='portal.test')
        with override_settings(ALLOWED_HOSTS=['portal.test']):
            self.assertTrue(is_safe(request, '/results/'))
            for bad in ('https://evil.example/', '//evil.example', '///evil.example', 'javascript:alert(1)', ''):
                self.assertFalse(is_safe(request, bad), bad)

    def test_data_changing_pages_need_the_csrf_token(self):
        session = AcademicSession.objects.create(tenant=self.school, name='2026/2027', is_active=True)
        term = Term.objects.create(tenant=self.school, session=session, name='1st Term', is_active=True)
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.admin)
        for url, data in ((f'/academics/terms/{term.id}/update-school-days/', {'days': '60'}),
                          ('/results/save-teacher-remark/', {'student_id': '1', 'remark': 'x'})):
            page = client.post(url, data, HTTP_HOST=self.host(self.school))
            self.assertEqual(page.status_code, 403, url)
        term.refresh_from_db()
        self.assertNotEqual(term.manual_school_days, 60)

    @override_settings(WHATSAPP_APP_SECRET='')
    def test_unsigned_whatsapp_messages_are_refused(self):
        page = self.client.post('/hooks/whatsapp/', '{"entry": []}', content_type='application/json',
                                HTTP_HOST=self.host(self.school))
        self.assertEqual(page.status_code, 403)

    def test_seed_data_is_refused_on_a_live_site(self):
        with self.assertRaisesMessage(CommandError, 'refused on a live site'):
            call_command('seed_test_data')

    @override_settings(SITE_URL='https://portal.test')
    def test_pdfs_run_no_scripts_and_read_no_local_files(self):
        with mock.patch('pdfkit.from_string', return_value=b'%PDF') as make:
            pdf.html_to_pdf('<html><head><title>x</title></head><body>hi</body></html>', **pdf.A4)
        html, _out = make.call_args[0]
        options = make.call_args[1]['options']
        self.assertIn('disable-javascript', options)
        self.assertIn('disable-local-file-access', options)
        self.assertNotIn('enable-local-file-access', options)
        self.assertIn('<head><base href="https://portal.test/">', html)
