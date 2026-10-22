"""
What each staff role sees: administrators choose the sidebar sections, links and dashboard cards
for teachers, account officers, librarians and proprietors. Hidden sections are closed as well as
hidden, administrators always see everything, and only administrators can change it.
"""
from django.urls import reverse

from dashboard import menu
from security.models import MenuRule
from tenants.middleware import set_current_tenant
from users.models import Staff, User

from .factories import TenantTestCase, make_admin, make_school, make_teacher


class MenuAccessTests(TenantTestCase):

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha')
        self.admin = make_admin(self.school)
        self.teacher = make_teacher(self.school)
        set_current_tenant(self.school)
        self.clerk = User.objects.create_user(username='clerk', password='x', tenant=self.school, is_staff_user=True)
        Staff.objects.create(user=self.clerk, tenant=self.school, full_name='Clerk One', role='ACCOUNT')

    def get(self, user, path, **kw):
        self.client.force_login(user)
        return self.client.get(path, HTTP_HOST=self.host(self.school), **kw)

    def save(self, role, shown):
        self.client.force_login(self.admin)
        return self.client.post('/security/menu/', {'role': role, 'action': 'save', 'show': shown},
                                HTTP_HOST=self.host(self.school), follow=True)

    def every(self, role):
        dash = menu.DASHBOARD_OF[role]
        return [i['name'] for g in menu.sections_for(role) for i in g['items']] + \
               [f'{dash}:{k}' for k, _ in menu.CARDS[dash]]

    def test_the_sidebar_is_read_per_role(self):
        teacher = {g['id'] for g in menu.sections_for('TEACHER')}
        self.assertIn('grp-admissions', teacher)
        self.assertNotIn('grp-finance', teacher)                     # administrators only, so not offered
        self.assertIn('grp-analytics', {g['id'] for g in menu.sections_for('ACCOUNT')})
        self.assertNotIn('grp-analytics', teacher)

    def test_teachers_do_not_see_or_open_admissions_by_default(self):
        board = reverse('admissions:board')
        self.assertNotContains(self.get(self.teacher, '/dashboard/teacher/'), f'href="{board}"')
        self.assertRedirects(self.get(self.teacher, board), '/dashboard/', fetch_redirect_response=False)
        self.assertContains(self.get(self.teacher, board, follow=True), 'has not given your role access')
        self.assertContains(self.get(self.admin, '/dashboard/admin/'), f'href="{board}"')
        self.assertEqual(self.get(self.admin, board).status_code, 200)

    def test_hiding_a_section_and_a_dashboard_card(self):
        shown = [n for n in self.every('TEACHER') if not n.startswith('library:') and n != 'teacher:exams']
        page = self.save('TEACHER', shown)
        self.assertContains(page, 'Saved.')
        home = self.get(self.teacher, '/dashboard/teacher/')
        self.assertNotContains(home, 'id="grp-library"')
        self.assertNotContains(home, 'My exams')
        self.assertContains(home, 'Homework I set')
        self.assertContains(home, f'href="{reverse("admissions:board")}"')        # ticked back on
        self.assertEqual(self.get(self.teacher, reverse('library:home')).status_code, 302)
        self.assertEqual(self.get(self.teacher, reverse('admissions:board')).status_code, 200)

    def test_one_role_does_not_change_another(self):
        shown = [n for n in self.every('ACCOUNT') if n != 'admin:fees']
        self.save('ACCOUNT', shown)
        self.assertNotContains(self.get(self.clerk, '/dashboard/admin/'), 'Fees this term')
        office = Staff.objects.get(user=self.teacher)
        office.role = 'ADMIN'
        office.save()
        self.assertContains(self.get(self.teacher, '/dashboard/admin/'), 'Fees this term')   # administrators see all

    def test_recommended_and_always_open_pages(self):
        self.save('TEACHER', [])                                                  # untick everything
        self.assertEqual(self.get(self.teacher, '/dashboard/teacher/').status_code, 200)
        self.assertEqual(self.get(self.teacher, reverse('security:me')).status_code, 200)
        self.assertEqual(self.get(self.teacher, reverse('idcards:my_id_card')).status_code, 200)
        self.client.force_login(self.admin)
        self.client.post('/security/menu/', {'role': 'TEACHER', 'action': 'reset'}, HTTP_HOST=self.host(self.school))
        self.assertFalse(MenuRule.objects.filter(role='TEACHER').exists())
        self.assertEqual(menu.saved_hidden(self.school, 'TEACHER'), menu.default_hidden('TEACHER'))

    def test_only_administrators_can_change_it(self):
        self.assertEqual(self.get(self.admin, '/security/menu/?role=LIB').status_code, 200)
        self.assertEqual(self.get(self.teacher, '/security/menu/').status_code, 302)
        self.client.force_login(self.teacher)
        self.client.post('/security/menu/', {'role': 'TEACHER', 'action': 'show_all'}, HTTP_HOST=self.host(self.school))
        self.assertFalse(MenuRule.objects.exists())

    def test_unknown_names_are_not_stored(self):
        self.save('TEACHER', self.every('TEACHER'))
        self.save('LIB', ['not:a_link'])
        rule = MenuRule.objects.get(role='LIB')
        self.assertNotIn('not:a_link', rule.hidden)
        self.assertTrue(set(rule.hidden) <= set(self.every('LIB')))
