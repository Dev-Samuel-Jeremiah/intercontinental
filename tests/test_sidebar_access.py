"""
A member of staff is only shown sidebar links they can open: every link in a staff sidebar is
opened here and none may turn them away. Links that depend on a permission switch or a job (form
teacher, nurse, librarian…) appear once the person has it. Administrators still see everything.
"""
import re

from django.db.models import BooleanField
from django.urls import reverse

from dashboard import menu
from tenants.middleware import set_current_tenant
from users.models import Class, Staff, User

from .factories import TenantTestCase, make_admin, make_school

SWITCHES = [f.name for f in Staff._meta.get_fields() if isinstance(f, BooleanField) and f.name.startswith('can_')]
LINK = re.compile(r'<a href="(/[^"]*)" class="nav-item"')


class SidebarAccessTests(TenantTestCase):

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha')
        self.admin = make_admin(self.school)
        set_current_tenant(self.school)
        self.host_name = self.host(self.school)

    def person(self, role, **switches):
        user = User.objects.create_user(username=f'u_{role}_{len(switches)}', password='x', tenant=self.school,
                                        is_staff_user=True)
        Staff.objects.create(user=user, tenant=self.school, full_name=f'{role} person', role=role,
                             **{f: switches.get(f, False) for f in SWITCHES})
        return user

    def sidebar(self, user):
        self.client.force_login(user)
        page = self.client.get('/dashboard/', HTTP_HOST=self.host_name, follow=True)
        nav = page.content.decode().split('class="nav-scroll"', 1)[-1]
        return sorted(set(LINK.findall(nav)))

    def refused(self, user, links):
        out = []
        for link in links:
            r = self.client.get(link, HTTP_HOST=self.host_name)
            if r.status_code in (301, 302) and (r['Location'].rstrip('/').endswith('/dashboard') or 'login' in r['Location']):
                out.append(link)
            elif r.status_code in (403, 404):
                out.append(link)
        return out

    def test_no_staff_sidebar_link_turns_them_away(self):
        for role, _ in menu.ROLES:
            menu.save(self.school, role, [])                          # the school hides nothing
            user = self.person(role)
            links = self.sidebar(user)
            self.assertGreater(len(links), 20, role)
            self.assertEqual(self.refused(user, links), [], f'{role} is shown links that refuse them')

    def test_admin_only_pages_are_not_listed(self):
        links = self.sidebar(self.person('TEACHER'))
        for hidden in ('/safety/staff/attendance/', '/idcards/reception/qr/', '/safety/settings/'):
            self.assertFalse(any(link.startswith(hidden) for link in links), hidden)
        admin_links = self.sidebar(self.admin)
        self.assertIn('/safety/staff/attendance/', admin_links)

    def test_a_switch_or_a_job_brings_the_link_back(self):
        examiner = self.person('TEACHER', can_manage_exams=True)
        links = self.sidebar(examiner)
        self.assertTrue(any('/cbt/' in link and 'create' in link for link in links))
        teacher = self.person('TEACHER')
        form_class = reverse('teaching:class_dashboard')
        self.assertNotIn(form_class, self.sidebar(teacher))
        Class.objects.create(tenant=self.school, name='JSS 1A').form_teachers.add(Staff.objects.get(user=teacher))
        from django.core.cache import cache
        cache.clear()
        after = self.sidebar(teacher)
        self.assertIn(form_class, after)                               # now a form teacher
        self.assertIn(reverse('results:broadsheet'), after)
        self.assertEqual(self.refused(teacher, after), [])
