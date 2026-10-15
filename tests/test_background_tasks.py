"""
Slow work (bulk email/SMS) runs outside the request, and a failing task must
never break the page the user is on.
"""
import time

from django.core import mail
from django.test import TransactionTestCase, override_settings

from lms_project.tasks import run_in_background
from tenants.middleware import get_current_tenant

from .factories import TenantTestCase, make_admin, make_school, make_student


class TaskRunnerTests(TransactionTestCase):
    """TransactionTestCase: tasks start on commit, so they need real commits."""

    @override_settings(RUN_TASKS_EAGERLY=True)
    def test_eager_mode_runs_inline_and_returns_the_result(self):
        self.assertEqual(run_in_background(lambda a, b: a + b, 2, 3), 5)

    def test_background_mode_actually_runs_the_work(self):
        seen = []
        run_in_background(lambda: seen.append('ran'))
        for _ in range(100):           # give the worker thread a moment
            if seen:
                break
            time.sleep(0.02)
        self.assertEqual(seen, ['ran'])

    def test_a_failing_task_is_swallowed_and_logged(self):
        def explode():
            raise RuntimeError('boom')
        with self.assertLogs('lms_project.tasks', level='ERROR') as logs:
            run_in_background(explode)
            for _ in range(100):
                if logs.records:
                    break
                time.sleep(0.02)
        self.assertTrue(any('failed' in r.getMessage() for r in logs.records))

    def test_the_task_knows_which_school_it_is_working_for(self):
        school = make_school('alpha')
        seen = {}
        with override_settings(RUN_TASKS_EAGERLY=True):
            run_in_background(lambda: seen.setdefault('tenant', get_current_tenant()),
                              tenant=school)
        self.assertEqual(seen['tenant'], school)


@override_settings(ROOT_DOMAIN='localhost', ALLOWED_HOSTS=['*'],
                   EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class BulkSendTests(TenantTestCase):
    """The pages return straight away; the sending still happens."""

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha')
        self.admin = make_admin(self.school)
        make_student(self.school, 'STU001', parent_email='p1@example.com')
        make_student(self.school, 'STU002', parent_email='p2@example.com')
        self.client.force_login(self.admin)

    @override_settings(RUN_TASKS_EAGERLY=True)
    def test_result_notifications_reach_every_parent(self):
        response = self.client.post('/communications/result-notifications/',
                                    {'channel': 'EMAIL'}, HTTP_HOST=self.host(self.school),
                                    follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 2)

    def test_the_page_responds_without_waiting_for_the_sending(self):
        response = self.client.post('/communications/result-notifications/',
                                    {'channel': 'EMAIL'}, HTTP_HOST=self.host(self.school))
        self.assertEqual(response.status_code, 302)
