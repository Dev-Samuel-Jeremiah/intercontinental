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


class PersonalMenuTests(TenantTestCase):
    """One person can have their own modules and permission switches, apart from their role."""

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha')
        self.admin = make_admin(self.school)
        self.ada_user = make_teacher(self.school, username='ada', full_name='Ada Obi')
        self.ben_user = make_teacher(self.school, username='ben', full_name='Ben Eze')
        set_current_tenant(self.school)
        self.ada = Staff.objects.get(user=self.ada_user)

    def page(self, user, path, **kw):
        self.client.force_login(user)
        return self.client.get(path, HTTP_HOST=self.host(self.school), **kw)

    def post(self, data):
        self.client.force_login(self.admin)
        return self.client.post('/security/menu/', data, HTTP_HOST=self.host(self.school), follow=True)

    def teacher_links(self):
        return [i['name'] for g in menu.sections_for('TEACHER') for i in g['items']] + \
               [f'teacher:{k}' for k, _ in menu.CARDS['teacher']]

    def test_teachers_start_without_id_cards_and_admissions(self):
        home = self.page(self.ben_user, '/dashboard/teacher/')
        self.assertNotContains(home, 'id="grp-idcards-safety"')
        self.assertNotContains(home, 'id="grp-admissions"')

    def test_one_teacher_gets_admissions(self):
        page = self.post({'staff': self.ada.pk, 'action': 'save', 'show': self.teacher_links(),
                          'switch': ['can_manage_students']})
        self.assertContains(page, 'Saved. Ada Obi now sees')
        self.assertContains(page, 'Has their own settings')
        board = reverse('admissions:board')
        self.assertContains(self.page(self.ada_user, '/dashboard/teacher/'), f'href="{board}"')
        self.assertEqual(self.page(self.ada_user, board).status_code, 200)
        self.assertNotContains(self.page(self.ben_user, '/dashboard/teacher/'), f'href="{board}"')   # Ben follows the role
        self.assertEqual(self.page(self.ben_user, board).status_code, 302)

    def test_switches_are_saved_and_shape_the_menu(self):
        shown = self.teacher_links()
        self.post({'staff': self.ada.pk, 'action': 'save', 'show': shown, 'switch': ['can_mark_attendance']})
        self.ada.refresh_from_db()
        self.assertTrue(self.ada.can_mark_attendance)
        self.assertFalse(self.ada.can_manage_students)               # unticked
        home = self.page(self.ada_user, '/dashboard/teacher/')
        self.assertContains(home, f'href="{reverse("idcards:gate_scan_log")}"')
        self.assertNotContains(home, f'href="{reverse("idcards:id_card_directory")}"')
        page = self.page(self.admin, f'/security/menu/?staff={self.ada.pk}')
        self.assertContains(page, 'Ticked, but not shown')            # ID cards ticked, switch off

    def test_back_to_the_role_and_role_reset_keeps_people(self):
        self.post({'staff': self.ada.pk, 'action': 'save', 'show': self.teacher_links()})
        self.post({'role': 'TEACHER', 'action': 'reset'})
        self.assertTrue(MenuRule.objects.filter(staff=self.ada).exists())          # her own settings survive
        self.post({'staff': self.ada.pk, 'action': 'reset'})
        self.assertFalse(MenuRule.objects.filter(staff=self.ada).exists())
        self.assertEqual(menu.saved_hidden(self.school, 'TEACHER', self.ada), menu.default_hidden('TEACHER'))

    def test_only_this_schools_staff(self):
        other = make_school('beta')
        outsider = Staff.objects.get(user=make_teacher(other, username='out'))
        set_current_tenant(self.school)
        self.assertEqual(self.page(self.admin, f'/security/menu/?staff={outsider.pk}').status_code, 404)


class OnePermissionsPageTests(TenantTestCase):
    """Staff Permissions moved into Module Permissions: the staff list, roles and account restriction."""

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha')
        self.admin = make_admin(self.school)
        self.t_user = make_teacher(self.school, username='ada', full_name='Ada Obi')
        set_current_tenant(self.school)
        self.ada = Staff.objects.get(user=self.t_user)

    def go(self, method, path, data=None):
        self.client.force_login(self.admin)
        return getattr(self.client, method)(path, data or {}, HTTP_HOST=self.host(self.school), follow=True)

    def test_the_old_page_forwards_and_lists_everyone(self):
        page = self.go('get', '/staff/permissions/')
        self.assertContains(page, 'Ada Obi')
        self.assertContains(page, 'All staff')
        self.assertNotContains(self.go('get', '/security/menu/?view=staff&q=zzz'), 'Ada Obi')

    def test_change_role_and_restrict(self):
        self.go('post', '/security/menu/', {'staff': self.ada.pk, 'action': 'account', 'role': 'LIB', 'restricted': '1'})
        self.ada.refresh_from_db()
        self.t_user.refresh_from_db()
        self.assertEqual(self.ada.role, 'LIB')
        self.assertTrue(self.t_user.restricted)
        self.go('post', '/security/menu/', {'staff': self.ada.pk, 'action': 'account', 'role': 'ADMIN'})
        self.ada.refresh_from_db()
        self.assertEqual(self.ada.role, 'ADMIN')                                  # main administrator may do this
        page = self.go('get', f'/security/menu/?staff={self.ada.pk}')
        self.assertContains(page, 'An administrator sees and can open everything')

    def test_an_admin_role_cannot_make_admins_or_change_themselves(self):
        office = make_teacher(self.school, username='office', full_name='Office')
        o = Staff.objects.get(user=office)
        o.role = 'ADMIN'
        o.save()
        self.client.force_login(office)
        self.client.post('/security/menu/', {'staff': self.ada.pk, 'action': 'account', 'role': 'ADMIN'},
                         HTTP_HOST=self.host(self.school))
        self.ada.refresh_from_db()
        self.assertEqual(self.ada.role, 'TEACHER')
        self.client.post('/security/menu/', {'staff': o.pk, 'action': 'account', 'role': 'TEACHER'},
                         HTTP_HOST=self.host(self.school))
        o.refresh_from_db()
        self.assertEqual(o.role, 'ADMIN')
