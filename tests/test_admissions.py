"""
Admissions: a parent applies on the public form, staff move the application
through the pipeline, and an accepted applicant becomes a student.
"""
from datetime import date

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings

from admissions.models import AdmissionQuota, Application, ApplicationDocument
from admissions.services import (build_admission_number, enrol_applicant, find_sibling,
                                 get_settings, seats_left)
from tenants.middleware import set_current_tenant
from users.models import Student, User

from .factories import (TenantTestCase, make_admin, make_class, make_school,
                        make_student, make_teacher)


@override_settings(ROOT_DOMAIN='localhost', ALLOWED_HOSTS=['*'])
class PublicApplicationTests(TenantTestCase):

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha')
        self.jss1 = make_class(self.school, 'JSS 1')
        set_current_tenant(self.school)
        self.settings_obj = get_settings(self.school)
        self.settings_obj.applications_open = True
        self.settings_obj.save()

    def _payload(self, **overrides):
        data = {
            'full_name': 'Amara Nwosu', 'gender': 'Female', 'date_of_birth': '2015-04-12',
            'applying_for': self.jss1.id, 'previous_school': 'Little Steps',
            'parent_name': 'Ngozi Nwosu', 'relationship': 'Mother',
            'parent_phone': '08031234567', 'parent_email': 'ngozi@example.com',
            'address': '12 Awolowo Road', 'source': 'SOCIAL', 'source_detail': 'Facebook',
        }
        data.update(overrides)
        return data

    def test_a_parent_can_apply_and_gets_a_reference(self):
        response = self.client.post('/apply/', self._payload(),
                                    HTTP_HOST=self.host(self.school), follow=True)
        self.assertEqual(response.status_code, 200)
        application = Application.objects.get(full_name='Amara Nwosu')
        self.assertEqual(application.status, Application.NEW)
        self.assertEqual(application.tenant, self.school)
        self.assertContains(response, application.reference)

    def test_the_form_is_hidden_when_admissions_are_closed(self):
        self.settings_obj.applications_open = False
        self.settings_obj.save()
        response = self.client.get('/apply/', HTTP_HOST=self.host(self.school))
        self.assertContains(response, 'closed', status_code=200)
        self.assertNotContains(response, 'Submit application')

    def test_an_incomplete_phone_number_is_rejected(self):
        self.client.post('/apply/', self._payload(parent_phone='0803'),
                         HTTP_HOST=self.host(self.school))
        self.assertFalse(Application.objects.exists())

    def test_documents_are_attached_to_the_application(self):
        upload = SimpleUploadedFile('result.pdf', b'%PDF-1.4 test', content_type='application/pdf')
        self.client.post('/apply/', dict(self._payload(), documents=upload),
                         HTTP_HOST=self.host(self.school))
        application = Application.objects.get()
        self.assertEqual(application.documents.count(), 1)

    def test_documents_can_be_required(self):
        self.settings_obj.require_documents = True
        self.settings_obj.save()
        self.client.post('/apply/', self._payload(), HTTP_HOST=self.host(self.school))
        self.assertFalse(Application.objects.exists())

    def test_a_sibling_is_detected_from_the_guardian_contact(self):
        make_student(self.school, 'STU001', full_name='Chidi Nwosu',
                     parent_email='ngozi@example.com')
        self.client.post('/apply/', self._payload(), HTTP_HOST=self.host(self.school))
        application = Application.objects.get()
        self.assertIsNotNone(application.sibling_of)
        self.assertEqual(application.sibling_of.full_name, 'Chidi Nwosu')

    def test_a_parent_can_check_their_status_with_the_reference(self):
        self.client.post('/apply/', self._payload(), HTTP_HOST=self.host(self.school))
        reference = Application.objects.get().reference
        response = self.client.get('/apply/status/', {'reference': reference},
                                   HTTP_HOST=self.host(self.school))
        self.assertContains(response, 'Amara Nwosu')

    def test_an_unknown_reference_says_so_politely(self):
        response = self.client.get('/apply/status/', {'reference': 'APP-2026-ZZZZZ'},
                                   HTTP_HOST=self.host(self.school))
        self.assertContains(response, "couldn't find an application")

    def test_one_school_cannot_see_another_schools_application(self):
        other = make_school('beta')
        set_current_tenant(other)
        get_settings(other)
        self.client.post('/apply/', self._payload(), HTTP_HOST=self.host(self.school))
        reference = Application.objects.filter(tenant=self.school).get().reference
        response = self.client.get('/apply/status/', {'reference': reference},
                                   HTTP_HOST=self.host(other))
        self.assertContains(response, "couldn't find an application")


@override_settings(ROOT_DOMAIN='localhost', ALLOWED_HOSTS=['*'])
class PipelineTests(TenantTestCase):

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha')
        self.jss1 = make_class(self.school, 'JSS 1')
        self.admin = make_admin(self.school)
        set_current_tenant(self.school)
        self.application = Application.objects.create(
            tenant=self.school, reference='APP-2026-AAAAA', full_name='Amara Nwosu',
            applying_for=self.jss1, parent_name='Ngozi Nwosu', parent_phone='08031234567',
            parent_email='ngozi@example.com')
        self.client.force_login(self.admin)

    def test_the_board_shows_the_application(self):
        response = self.client.get('/admissions/', HTTP_HOST=self.host(self.school))
        self.assertContains(response, 'Amara Nwosu')

    def test_staff_can_move_an_application_along_and_it_is_recorded(self):
        self.client.post(f'/admissions/{self.application.pk}/',
                         {'action': 'change_status', 'status': Application.OFFERED,
                          'note': 'Offer letter sent'},
                         HTTP_HOST=self.host(self.school))
        self.application.refresh_from_db()
        self.assertEqual(self.application.status, Application.OFFERED)
        event = self.application.events.first()
        self.assertEqual(event.to_status, Application.OFFERED)
        self.assertEqual(event.actor, self.admin)

    def test_scores_can_be_saved(self):
        self.client.post(f'/admissions/{self.application.pk}/',
                         {'action': 'save_decision', 'exam_score': '72.5',
                          'interview_score': '18', 'applying_for': self.jss1.id,
                          'decision_note': 'Strong in maths'},
                         HTTP_HOST=self.host(self.school))
        self.application.refresh_from_db()
        self.assertEqual(float(self.application.exam_score), 72.5)
        self.assertEqual(float(self.application.total_score), 90.5)

    def test_a_teacher_without_admin_rights_can_still_screen(self):
        teacher = make_teacher(self.school)
        self.client.force_login(teacher)
        response = self.client.get('/admissions/', HTTP_HOST=self.host(self.school))
        self.assertEqual(response.status_code, 200)

    def test_a_parent_cannot_open_the_pipeline(self):
        parent = User.objects.create_user(username='par1', password='x',
                                          tenant=self.school, is_parent=True)
        self.client.force_login(parent)
        response = self.client.get('/admissions/', HTTP_HOST=self.host(self.school))
        self.assertNotEqual(response.status_code, 200)

    def test_staff_cannot_open_another_schools_application(self):
        other = make_school('beta')
        set_current_tenant(other)
        theirs = Application.objects.create(
            tenant=other, reference='APP-2026-BBBBB', full_name='Beta Child',
            parent_name='P', parent_phone='08031111111')
        set_current_tenant(self.school)
        response = self.client.get(f'/admissions/{theirs.pk}/', HTTP_HOST=self.host(self.school))
        self.assertEqual(response.status_code, 404)


@override_settings(ROOT_DOMAIN='localhost', ALLOWED_HOSTS=['*'])
class EnrolmentTests(TenantTestCase):

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha', name='Royal Kidies School')
        self.jss1 = make_class(self.school, 'JSS 1')
        self.admin = make_admin(self.school)
        set_current_tenant(self.school)
        self.application = Application.objects.create(
            tenant=self.school, reference='APP-2026-AAAAA', full_name='Amara Nwosu',
            gender='Female', date_of_birth=date(2015, 4, 12), applying_for=self.jss1,
            parent_name='Ngozi Nwosu', parent_phone='08031234567',
            parent_email='ngozi@example.com', status=Application.ACCEPTED)

    def test_enrolling_creates_a_student_with_logins(self):
        student, credentials = enrol_applicant(self.application, actor=self.admin)
        self.assertEqual(student.full_name, 'Amara Nwosu')
        self.assertEqual(student.class_assigned, self.jss1)
        self.assertEqual(student.tenant, self.school)
        self.assertTrue(credentials['student_password'])
        self.assertTrue(credentials['parent_password'])
        self.assertEqual(student.parent_term_password, credentials['parent_password'])
        self.application.refresh_from_db()
        self.assertEqual(self.application.status, Application.ENROLLED)
        self.assertEqual(self.application.student, student)

    def test_the_new_student_can_sign_in(self):
        student, credentials = enrol_applicant(self.application, actor=self.admin)
        self.client.post('/student/login/',
                         {'admission_number': credentials['admission_number'],
                          'password': credentials['student_password']},
                         HTTP_HOST=self.host(self.school))
        self.assertEqual(self.client.session.get('_auth_user_id'), str(student.user_id))

    def test_the_parent_can_sign_in(self):
        _, credentials = enrol_applicant(self.application, actor=self.admin)
        self.client.post('/parent/login/',
                         {'admission_number': credentials['admission_number'],
                          'password': credentials['parent_password']},
                         HTTP_HOST=self.host(self.school))
        self.assertIn('_auth_user_id', self.client.session)

    def test_enrolling_twice_does_not_create_two_students(self):
        student, _ = enrol_applicant(self.application, actor=self.admin)
        again, _ = enrol_applicant(self.application, actor=self.admin)
        self.assertEqual(student.pk, again.pk)
        self.assertEqual(Student.objects.count(), 1)

    def test_enrolling_through_the_page_shows_the_credentials_once(self):
        self.client.force_login(self.admin)
        response = self.client.post(f'/admissions/{self.application.pk}/',
                                    {'action': 'enrol'}, HTTP_HOST=self.host(self.school))
        self.assertContains(response, 'is now a')
        self.assertContains(response, Student.objects.get().admission_number)


class AdmissionNumberTests(TenantTestCase):

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha', name='Royal Kidies School')
        self.jss1 = make_class(self.school, 'JSS 1')
        set_current_tenant(self.school)

    def test_the_default_format_uses_the_school_initials(self):
        number = build_admission_number(self.school, self.jss1)
        self.assertEqual(number, f'RKS/{date.today().year}/0001')

    def test_numbers_increment(self):
        first = build_admission_number(self.school, self.jss1)
        second = build_admission_number(self.school, self.jss1)
        self.assertNotEqual(first, second)
        self.assertTrue(second.endswith('0002'))

    def test_a_school_can_set_its_own_format(self):
        settings_obj = get_settings(self.school)
        settings_obj.number_format = '{prefix}-{short_year}-{class}-{seq:03d}'
        settings_obj.number_prefix = 'TIT'
        settings_obj.save()
        number = build_admission_number(self.school, self.jss1)
        self.assertEqual(number, f'TIT-{str(date.today().year)[-2:]}-JSS1-001')

    def test_a_broken_format_still_produces_a_number(self):
        settings_obj = get_settings(self.school)
        settings_obj.number_format = '{nonsense}/{seq}'
        settings_obj.save()
        number = build_admission_number(self.school, self.jss1)
        self.assertTrue(number)

    def test_each_school_numbers_its_own_students(self):
        other = make_school('beta', name='Beta Academy')
        set_current_tenant(other)
        theirs = build_admission_number(other, None)
        self.assertTrue(theirs.startswith('BA/'), theirs)


class QuotaAndSiblingTests(TenantTestCase):

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha')
        self.jss1 = make_class(self.school, 'JSS 1')
        set_current_tenant(self.school)

    def test_no_quota_means_no_limit(self):
        self.assertIsNone(seats_left(self.school, self.jss1))

    def test_seats_left_counts_students_and_live_offers(self):
        AdmissionQuota.objects.create(tenant=self.school, school_class=self.jss1, seats=3)
        make_student(self.school, 'STU001', school_class=self.jss1)
        Application.objects.create(tenant=self.school, reference='APP-1', full_name='A',
                                   applying_for=self.jss1, parent_name='P',
                                   parent_phone='08030000001', status=Application.OFFERED)
        self.assertEqual(seats_left(self.school, self.jss1), 1)

    def test_seats_never_go_negative(self):
        AdmissionQuota.objects.create(tenant=self.school, school_class=self.jss1, seats=1)
        make_student(self.school, 'STU001', school_class=self.jss1)
        make_student(self.school, 'STU002', school_class=self.jss1)
        self.assertEqual(seats_left(self.school, self.jss1), 0)

    def test_siblings_match_on_phone_even_when_formatted_differently(self):
        make_student(self.school, 'STU001', full_name='Chidi N.')
        student = Student.objects.get()
        student.parent_phone = '+234 803 123 4567'
        student.save()
        self.assertIsNotNone(find_sibling(self.school, '', '08031234567'))

    def test_siblings_do_not_match_across_schools(self):
        make_student(self.school, 'STU001', parent_email='shared@example.com')
        other = make_school('beta')
        set_current_tenant(other)
        self.assertIsNone(find_sibling(other, 'shared@example.com', ''))
