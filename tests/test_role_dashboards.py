"""
Role dashboards: each role lands on its own dashboard, the proprietor role
sees the owner's view and the money, parents see every child in the family,
and one broken section never takes a dashboard down.
"""
from datetime import date, timedelta
from unittest import mock

from django.utils import timezone

from academics.models import AcademicSession, Term
from dashboard import insights
from security.access import allowed
from tenants.middleware import set_current_tenant
from users.models import Staff, User

from .factories import TenantTestCase, make_admin, make_class, make_school, make_student, make_teacher


class Base(TenantTestCase):

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha')
        self.superadmin = make_admin(self.school)
        self.teacher = make_teacher(self.school, username='tolu', full_name='Tolu Ade')
        set_current_tenant(self.school)
        self.head = self._staff('head', 'ADMIN', 'Mrs Head')
        self.owner = self._staff('owner', 'PROPRIETOR', 'Chief Owner')
        self.cls = make_class(self.school, 'JSS 1A')
        today = timezone.localdate()
        session = AcademicSession.objects.create(tenant=self.school, name='2025/2026', start_date=today - timedelta(days=200))
        Term.objects.create(tenant=self.school, session=session, name='3rd Term',
                            start_date=today - timedelta(days=20), end_date=today + timedelta(days=50))
        self.ada = make_student(self.school, 'A1', 'Ada Obi', school_class=self.cls, parent_email='mum@example.com')
        self.ben = make_student(self.school, 'A2', 'Ben Obi', school_class=self.cls, parent_email='mum@example.com')
        self.parent = User.objects.create_user(username=f'parent_{self.ada.id}', password='x', tenant=self.school,
                                               is_parent=True, email='mum@example.com')

    def _staff(self, username, role, name):
        user = User.objects.create_user(username=username, password='x', tenant=self.school, is_staff_user=True)
        Staff.objects.create(user=user, tenant=self.school, full_name=name, role=role)
        return user

    def get(self, user, path):
        self.client.force_login(user)
        return self.client.get(path, HTTP_HOST=self.host(self.school))


class RoutingTests(Base):

    def test_each_role_lands_on_its_own_dashboard(self):
        for user, target in ((self.superadmin, '/dashboard/super-admin/'), (self.head, '/dashboard/admin/'),
                             (self.owner, '/dashboard/proprietor/'), (self.teacher, '/dashboard/teacher/'),
                             (self.ada.user, '/dashboard/student/'), (self.parent, '/parent/dashboard/')):
            page = self.get(user, '/dashboard/')
            self.assertEqual(page.status_code, 302, user.username)
            self.assertTrue(page['Location'].endswith(target), (user.username, page['Location']))

    def test_each_dashboard_is_different(self):
        for user, path, marker in ((self.superadmin, '/dashboard/super-admin/', 'Command centre'),
                                   (self.head, '/dashboard/admin/', 'The school today'),
                                   (self.owner, '/dashboard/proprietor/', "The owner's view"),
                                   (self.teacher, '/dashboard/teacher/', 'My teaching day'),
                                   (self.ada.user, '/dashboard/student/', 'My learning hub'),
                                   (self.parent, '/parent/dashboard/', 'My children')):
            page = self.get(user, path)
            self.assertEqual(page.status_code, 200, path)
            self.assertContains(page, marker, msg_prefix=path)
            self.assertContains(page, 'db-hero', msg_prefix=path)


class ProprietorTests(Base):

    def test_proprietor_sees_the_money(self):
        set_current_tenant(self.school)
        self.assertTrue(allowed(self.owner, 'can_access_finance'))
        self.assertFalse(allowed(self.teacher, 'can_access_finance'))

    def test_teachers_and_students_are_kept_out(self):
        for user in (self.teacher, self.ada.user):
            page = self.get(user, '/dashboard/proprietor/')
            self.assertEqual(page.status_code, 302)
            self.assertNotIn('/proprietor/', page['Location'])

    def test_head_of_school_may_look_too(self):
        self.assertEqual(self.get(self.head, '/dashboard/proprietor/').status_code, 200)


class ParentTests(Base):

    def test_parent_sees_every_child_and_can_switch(self):
        page = self.get(self.parent, '/parent/dashboard/')
        self.assertContains(page, 'Ada Obi')
        self.assertContains(page, 'Ben Obi')
        self.assertContains(page, f'?student={self.ben.id}')
        page = self.get(self.parent, f'/parent/dashboard/?student={self.ben.id}')
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.context['student'], self.ben)

    def test_other_families_are_not_reachable(self):
        other = make_student(self.school, 'B1', 'Chi Eze', school_class=self.cls, parent_email='eze@example.com')
        page = self.get(self.parent, f'/parent/dashboard/?student={other.id}')
        self.assertNotContains(page, 'Chi Eze', status_code=page.status_code)


class InsightTests(Base):

    def test_term_progress_never_runs_past_the_last_week(self):
        term = Term(start_date=date(2026, 1, 5), end_date=date(2026, 3, 9))
        with mock.patch('django.utils.timezone.localdate', return_value=date(2026, 5, 1)):
            p = insights.term_progress(term)
        self.assertEqual((p['week'], p['percent'], p['days_left']), (p['weeks'], 100, 0))
        self.assertIsNone(insights.term_progress(Term(start_date=date(2026, 1, 5))))

    def test_a_broken_section_does_not_break_the_page(self):
        def boom():
            raise RuntimeError('db down')
        with self.assertLogs('dashboard.insights', 'ERROR'):
            self.assertEqual(insights.safe(boom, []), [])
        with mock.patch.object(insights, 'attention_for', side_effect=RuntimeError('db down')), \
                self.assertLogs('dashboard.insights', 'ERROR'):
            page = self.get(self.head, '/dashboard/admin/')
        self.assertEqual(page.status_code, 200)


class AttentionCardTests(TenantTestCase):
    """The admin's 'Needs your attention' card: everything waiting, urgent first, each with a working link."""

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha')
        self.admin = make_admin(self.school)
        set_current_tenant(self.school)

    def test_every_item_counts_and_links(self):
        from django.urls import reverse
        from dashboard.insights import _attention_items
        for tone, label, how, page, icon in _attention_items():
            self.assertIsInstance(how(self.school), int, label)          # no wrong field names hiding an item
            reverse(page)

    def test_waiting_things_show_urgent_first(self):
        from safety.models import StaffLeave
        from security.models import Breach, PasswordHelpRequest
        from users.models import Staff
        teacher = Staff.objects.get(user=make_teacher(self.school))
        StaffLeave.objects.create(tenant=self.school, staff=teacher, leave_type='SICK', start_date=date(2026, 11, 2),
                                  end_date=date(2026, 11, 3))
        PasswordHelpRequest.objects.create(tenant=self.school, kind='STUDENT', identifier='A1', name='X')
        Breach.objects.create(tenant=self.school, reference='BR-1', title='Laptop lost', description='Lost on a bus',
                              discovered_at=timezone.now())
        self.client.force_login(self.admin)
        page = self.client.get('/dashboard/super-admin/', HTTP_HOST=self.host(self.school)).content.decode()
        self.assertIn('Needs your attention', page)
        breach, leave = page.index('Data breaches still open'), page.index('Leave requests')
        self.assertLess(breach, leave)                                       # urgent first
        self.assertIn('href="/safety/staff/leave/"', page)
        self.assertIn('People asking for a new password', page)

    def test_all_clear(self):
        self.client.force_login(self.admin)
        page = self.client.get('/dashboard/super-admin/', HTTP_HOST=self.host(self.school))
        self.assertContains(page, 'All clear: nothing is waiting for you.')
