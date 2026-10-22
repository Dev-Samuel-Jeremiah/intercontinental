"""
Password help for people with no email: anyone can ask the school office (the answer never says
whether the account exists), an administrator checks who they are and gives a temporary password
that is shown once, and only school administrators can do it.
"""
from django.urls import reverse

from security.models import PasswordHelpRequest
from tenants.middleware import set_current_tenant
from users.models import Staff, Student

from .factories import TenantTestCase, make_admin, make_school, make_student, make_teacher

ASK = '/security/forgot-password/no-email/'
DESK = '/security/password-help/'


class PasswordHelpTests(TenantTestCase):

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha')
        self.admin = make_admin(self.school)
        self.teacher = make_teacher(self.school)
        set_current_tenant(self.school)
        self.ada = make_student(self.school, 'A1', 'Ada Obi')

    def ask(self, kind, identifier, ip='10.0.0.1', **extra):
        data = {'kind': kind, 'identifier': identifier, 'name': 'Someone', 'phone': '08031234567', **extra}
        return self.client.post(ASK, data, HTTP_HOST=self.host(self.school), REMOTE_ADDR=ip)

    def as_admin(self, method='get', data=None, user=None):
        self.client.force_login(user or self.admin)
        call = getattr(self.client, method)
        return call(DESK, data or {}, HTTP_HOST=self.host(self.school), follow=method == 'post')

    def test_the_sign_in_pages_link_to_it(self):
        host = self.host(self.school)
        self.assertContains(self.client.get('/', HTTP_HOST=host), ASK)                      # landing page footer
        self.assertContains(self.client.get(reverse('users:staff_login'), HTTP_HOST=host), ASK)
        self.assertContains(self.client.get(reverse('cbt:password_reset'), HTTP_HOST=host), ASK)
        self.assertContains(self.client.get(reverse('users:parent_login'), HTTP_HOST=host), ASK)
        page = self.client.get(ASK + '?who=parent', HTTP_HOST=host)
        self.assertContains(page, "Your child's admission number")

    def test_asking_never_says_whether_the_account_exists(self):
        known = self.ask('STAFF', self.teacher.username)
        unknown = self.ask('STAFF', 'nobody-here', ip='10.0.0.2')
        for page in (known, unknown):
            self.assertContains(page, 'Your request has reached the school office.')
        rows = {r.identifier: r for r in PasswordHelpRequest.objects.all()}
        self.assertEqual(rows[self.teacher.username].user, self.teacher)
        self.assertFalse(rows['nobody-here'].matched)
        self.ask('STAFF', self.teacher.username.upper())              # asking again while it waits
        self.assertEqual(PasswordHelpRequest.objects.count(), 2)

    def test_too_many_requests_from_one_connection(self):
        for n in range(5):
            self.ask('STUDENT', f'X{n}')
        page = self.ask('STUDENT', 'X9')
        self.assertContains(page, 'Too many requests')
        self.assertEqual(PasswordHelpRequest.objects.count(), 5)

    def test_staff_reset_shows_the_password_once_and_forces_a_change(self):
        self.ask('STAFF', self.teacher.username)
        req = PasswordHelpRequest.objects.get()
        refused = self.as_admin('post', {'action': 'reset', 'pk': req.pk})          # identity box not ticked
        self.assertContains(refused, 'checked who they are')
        page = self.as_admin('post', {'action': 'reset', 'pk': req.pk, 'identity_checked': '1'})
        self.assertContains(page, 'it will not be shown again')
        self.teacher.refresh_from_db()
        password = page.context['shown']['password']
        self.assertTrue(self.teacher.check_password(password))
        self.assertTrue(self.teacher.must_change_password)
        self.assertNotContains(self.as_admin(), password)                            # gone on the next visit
        req.refresh_from_db()
        self.assertEqual((req.status, req.handled_by), ('DONE', self.admin))
        again = self.as_admin('post', {'action': 'reset', 'pk': req.pk, 'identity_checked': '1'})
        self.assertContains(again, 'already been dealt with')

    def test_student_and_parent_resets(self):
        self.ask('STUDENT', 'a1')
        self.ask('PARENT', 'A1')
        student_req, parent_req = (PasswordHelpRequest.objects.get(kind=k) for k in ('STUDENT', 'PARENT'))
        page = self.as_admin('post', {'action': 'reset', 'pk': student_req.pk, 'identity_checked': '1'})
        new = page.context['shown']['password']
        self.ada.user.refresh_from_db()
        self.assertEqual(self.ada.user.term_password, new)
        self.assertTrue(self.ada.user.check_password(new))
        page = self.as_admin('post', {'action': 'reset', 'pk': parent_req.pk, 'identity_checked': '1'})
        self.ada.refresh_from_db()
        self.assertEqual(self.ada.parent_term_password, page.context['shown']['password'])

    def test_reset_at_the_office_and_decline(self):
        page = self.as_admin('post', {'action': 'desk', 'kind': 'PARENT', 'identifier': 'A1', 'identity_checked': '1'})
        self.ada.refresh_from_db()
        self.assertEqual(self.ada.parent_term_password, page.context['shown']['password'])
        self.assertTrue(PasswordHelpRequest.objects.get().from_desk)
        missing = self.as_admin('post', {'action': 'desk', 'kind': 'STAFF', 'identifier': 'ghost', 'identity_checked': '1'})
        self.assertContains(missing, 'No staff username or admission number matches')
        self.assertEqual(PasswordHelpRequest.objects.count(), 1)
        self.ask('STAFF', self.teacher.username)
        req = PasswordHelpRequest.objects.get(from_desk=False)
        self.as_admin('post', {'action': 'decline', 'pk': req.pk})
        req.refresh_from_db()
        self.assertEqual(req.status, 'DECLINED')

    def test_only_administrators_and_only_their_own_school(self):
        self.client.force_login(self.teacher)
        self.assertEqual(self.client.get(DESK, HTTP_HOST=self.host(self.school)).status_code, 302)
        other = make_school('beta')
        make_teacher(other, username='betateacher')
        set_current_tenant(other)
        foreign = PasswordHelpRequest.objects.create(tenant=other, kind='STAFF', identifier='betateacher', name='X')
        self.client.force_login(self.admin)
        resp = self.client.post(DESK, {'action': 'reset', 'pk': foreign.pk, 'identity_checked': '1'},
                                HTTP_HOST=self.host(self.school))
        self.assertEqual(resp.status_code, 404)
        self.ask('STAFF', 'betateacher')                                         # another school's username
        self.assertFalse(PasswordHelpRequest.objects.get(tenant=self.school).matched)

    def test_an_admin_role_cannot_reset_the_main_administrator(self):
        office = make_teacher(self.school, username='office', full_name='Office Admin')
        Staff.objects.filter(user=office).update(role='ADMIN')
        self.ask('STAFF', self.admin.username)
        req = PasswordHelpRequest.objects.get()
        page = self.as_admin('post', {'action': 'reset', 'pk': req.pk, 'identity_checked': '1'}, user=office)
        self.assertContains(page, "Only the school&#x27;s main administrator")
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.check_password('admin-pass-1'))
        self.assertTrue(Student.objects.filter(pk=self.ada.pk).exists())
