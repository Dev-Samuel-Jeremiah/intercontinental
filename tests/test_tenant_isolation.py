"""
Tenant isolation — the tests that must never go red.

A school must never see, change or collide with another school's data, whether
through the ORM, a URL with someone else's id, a login form or a dropdown.
"""
from django.core.cache import cache
from django.test import override_settings
from django.urls import set_urlconf

from results.models import Grading
from tenants.middleware import set_current_tenant
from users.models import Class, Student, User

from .factories import TenantTestCase, make_admin, make_class, make_school, make_student


@override_settings(ROOT_DOMAIN='localhost', ALLOWED_HOSTS=['*'])
class QueryScopingTests(TenantTestCase):
    """The tenant-scoped managers only return the current school's rows."""

    def setUp(self):
        super().setUp()
        self.a = make_school('alpha')
        self.b = make_school('beta')
        make_student(self.a, 'A/001', full_name='Alpha Child')
        make_student(self.b, 'B/001', full_name='Beta Child')

    def test_students_are_scoped_to_the_current_school(self):
        set_current_tenant(self.a)
        self.assertEqual([s.full_name for s in Student.objects.all()], ['Alpha Child'])
        set_current_tenant(self.b)
        self.assertEqual([s.full_name for s in Student.objects.all()], ['Beta Child'])

    def test_classes_are_scoped_to_the_current_school(self):
        make_class(self.a, 'JSS 1')
        make_class(self.b, 'JSS 1')
        set_current_tenant(self.a)
        self.assertEqual(Class.objects.count(), 1)

    def test_counting_without_a_tenant_sees_everything(self):
        """Management commands run unscoped — that is deliberate, so pin it."""
        set_current_tenant(None)
        self.assertEqual(Student.objects.count(), 2)


@override_settings(ROOT_DOMAIN='localhost', ALLOWED_HOSTS=['*'])
class CrossSchoolUrlTests(TenantTestCase):
    """Guessing another school's object id must not reveal anything."""

    def setUp(self):
        super().setUp()
        self.a = make_school('alpha')
        self.b = make_school('beta')
        self.admin_a = make_admin(self.a)
        self.other_student = make_student(self.b, 'B/001', full_name='Beta Child')

    def test_admin_cannot_open_another_schools_student(self):
        self.client.force_login(self.admin_a)
        response = self.client.get(f'/students/edit/{self.other_student.id}/',
                                   HTTP_HOST=self.host(self.a))
        self.assertNotEqual(response.status_code, 200)
        if response.status_code == 200:  # pragma: no cover - guarded above
            self.assertNotContains(response, 'Beta Child')

    def test_admin_student_list_shows_only_their_school(self):
        self.client.force_login(self.admin_a)
        response = self.client.get('/students/all/', HTTP_HOST=self.host(self.a))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'Beta Child')


@override_settings(ROOT_DOMAIN='localhost', ALLOWED_HOSTS=['*'])
class LoginIsolationTests(TenantTestCase):
    """Accounts belong to one school and can only sign in there."""

    def setUp(self):
        super().setUp()
        self.a = make_school('alpha')
        self.b = make_school('beta')

    def test_same_username_can_exist_at_two_schools(self):
        make_admin(self.a, username='john', password='alpha-pass-1')
        make_admin(self.b, username='john', password='beta-pass-1')
        self.assertEqual(User.objects.filter(username='john').count(), 2)

    def test_staff_cannot_sign_in_at_another_school(self):
        make_admin(self.a, username='john', password='alpha-pass-1')
        make_admin(self.b, username='john', password='beta-pass-1')
        # Right password, wrong school.
        self.client.post('/staff/login/', {'username': 'john', 'password': 'alpha-pass-1'},
                         HTTP_HOST=self.host(self.b))
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_staff_signs_in_at_their_own_school(self):
        user = make_admin(self.a, username='john', password='alpha-pass-1')
        self.client.post('/staff/login/', {'username': 'john', 'password': 'alpha-pass-1'},
                         HTTP_HOST=self.host(self.a))
        self.assertEqual(self.client.session.get('_auth_user_id'), str(user.pk))

    def test_student_login_is_scoped_by_school(self):
        """Two schools using the same admission number must not reach each other."""
        make_student(self.a, 'STU001', student_password='alpha-pass-1')
        make_student(self.b, 'STU001', student_password='beta-pass-1')
        self.client.post('/student/login/',
                         {'admission_number': 'STU001', 'password': 'beta-pass-1'},
                         HTTP_HOST=self.host(self.a))
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_parent_login_is_scoped_by_school(self):
        make_student(self.a, 'STU001', parent_password='alpha-par-1')
        make_student(self.b, 'STU001', parent_password='beta-par-1')
        self.client.post('/parent/login/',
                         {'admission_number': 'STU001', 'password': 'beta-par-1'},
                         HTTP_HOST=self.host(self.a))
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_parent_signs_in_at_their_own_school(self):
        make_student(self.a, 'STU001', parent_password='alpha-par-1')
        self.client.post('/parent/login/',
                         {'admission_number': 'STU001', 'password': 'alpha-par-1'},
                         HTTP_HOST=self.host(self.a))
        self.assertIn('_auth_user_id', self.client.session)


@override_settings(ROOT_DOMAIN='localhost', ALLOWED_HOSTS=['*'])
class PerSchoolUniquenessTests(TenantTestCase):
    """One school's names must never block another school from using them."""

    def setUp(self):
        super().setUp()
        self.a = make_school('alpha')
        self.b = make_school('beta')

    def test_both_schools_can_use_the_same_grade(self):
        for school in (self.a, self.b):
            set_current_tenant(school)
            Grading.objects.create(tenant=school, grade='A1', min_score=75)
        set_current_tenant(None)  # count across both schools
        self.assertEqual(Grading.objects.filter(grade='A1').count(), 2)

    def test_both_schools_can_use_the_same_hostel_name(self):
        from hostel.models import Hostel
        for school in (self.a, self.b):
            set_current_tenant(school)
            Hostel.objects.create(tenant=school, name='Boys Hostel')
        set_current_tenant(None)
        self.assertEqual(Hostel.objects.filter(name='Boys Hostel').count(), 2)

    def test_both_schools_can_use_the_same_subject(self):
        from users.models import Subject
        for school in (self.a, self.b):
            set_current_tenant(school)
            Subject.objects.create(tenant=school, name='Mathematics')
        set_current_tenant(None)
        self.assertEqual(Subject.objects.filter(name='Mathematics').count(), 2)


@override_settings(ROOT_DOMAIN='localhost', ALLOWED_HOSTS=['*'])
class FormChoiceScopingTests(TenantTestCase):
    """Dropdowns must only offer the current school's records."""

    def setUp(self):
        super().setUp()
        self.a = make_school('alpha')
        self.b = make_school('beta')
        make_class(self.a, 'Alpha Class')
        make_class(self.b, 'Beta Class')

    def test_announcement_class_choices_are_scoped(self):
        set_urlconf('lms_project.tenant_urls')
        from announcement.forms import AnnouncementForm
        set_current_tenant(self.a)
        names = [str(c) for c in AnnouncementForm().fields['school_class'].queryset]
        self.assertIn('Alpha Class', names)
        self.assertNotIn('Beta Class', names)

    def test_cbt_exam_class_choices_are_scoped(self):
        from cbt.forms import ExamForm
        set_current_tenant(self.b)
        names = [str(c) for c in ExamForm().fields['classes'].queryset]
        self.assertIn('Beta Class', names)
        self.assertNotIn('Alpha Class', names)


@override_settings(ROOT_DOMAIN='localhost', ALLOWED_HOSTS=['*'])
class SchoolStatusTests(TenantTestCase):
    """A dedicated install has no subscription: no status ever locks the school out."""

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha')
        self.admin = make_admin(self.school)

    def test_no_status_blocks_the_portal(self):
        self.client.force_login(self.admin)
        for status in ('PENDING', 'SUSPENDED', 'CANCELLED'):
            self.school.status = status
            self.school.save(update_fields=['status'])
            cache.clear()
            response = self.client.get('/dashboard/', HTTP_HOST=self.host(self.school), follow=True)
            self.assertEqual(response.status_code, 200, status)
