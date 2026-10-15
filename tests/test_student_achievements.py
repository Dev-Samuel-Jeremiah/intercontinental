"""
Awards and leadership roles, support needs that CBT honours, parents'
correction requests, and the per-child data export.
"""
import io
import json
import shutil
import tempfile
import zipfile
from datetime import date, timedelta

from django.test import override_settings
from django.utils import timezone

from academics.models import AcademicSession, Term
from cbt.models import Exam, ExamAttempt, ExamPart
from tenants.middleware import set_current_tenant
from users import change_requests, support
from users.models import (Award, DataExport, Guardian, LeadershipRole, ProfileChangeRequest,
                          StudentChange, Subject, SupportNeeds, User)

from .factories import (TenantTestCase, make_admin, make_class, make_school, make_student,
                        make_teacher)


class Base(TenantTestCase):

    def setUp(self):
        super().setUp()
        for setting in ('MEDIA_ROOT', 'PRIVATE_MEDIA_ROOT'):
            folder = tempfile.mkdtemp()
            self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
            o = override_settings(**{setting: folder})
            o.enable()
            self.addCleanup(o.disable)
        self.school = make_school('alpha')
        self.admin = make_admin(self.school)
        self.jss1 = make_class(self.school, 'JSS 1A')
        set_current_tenant(self.school)
        self.session = AcademicSession.objects.create(tenant=self.school, name='2026/2027',
                                                      start_date=date(2026, 9, 1), is_active=True)
        self.term = Term.objects.create(tenant=self.school, name='1st Term', session=self.session,
                                        start_date=date(2026, 9, 1), end_date=date(2026, 12, 18),
                                        is_active=True)
        self.ada = make_student(self.school, 'STU001', full_name='Ada Bello', school_class=self.jss1,
                                parent_email='mum@example.com')


@override_settings(ROOT_DOMAIN='localhost', ALLOWED_HOSTS=['*'])
class AwardRoleTests(Base):

    def _post(self, path, data, **kw):
        return self.client.post(path, data, HTTP_HOST=self.host(self.school), **kw)

    def test_recording_an_award_puts_it_on_the_report_card(self):
        self.client.force_login(self.admin)
        self._post(f'/students/{self.ada.id}/awards/', {
            'action': 'add', 'title': 'Best in Mathematics', 'category': 'ACADEMIC',
            'level': 'STATE', 'position': '1st', 'awarded_on': '2026-10-01', 'show_on_report': '1'})
        award = Award.objects.get()
        self.assertEqual((award.level, award.term, award.recorded_by), ('STATE', self.term, self.admin))
        self.assertEqual(support.report_awards(self.ada, self.term, self.session), [award])

    def test_an_award_not_for_the_report_stays_off_it(self):
        Award.objects.create(tenant=self.school, student=self.ada, title='Neatest desk',
                             awarded_on=date(2026, 10, 1), show_on_report=False)
        self.assertEqual(support.report_awards(self.ada, self.term, self.session), [])

    def test_an_award_from_another_term_stays_off_it(self):
        Award.objects.create(tenant=self.school, student=self.ada, title='Old prize',
                             awarded_on=date(2026, 3, 1))
        self.assertEqual(support.report_awards(self.ada, self.term, self.session), [])

    def test_future_dated_awards_are_refused(self):
        self.client.force_login(self.admin)
        future = (timezone.localdate() + timedelta(days=5)).isoformat()
        response = self._post(f'/students/{self.ada.id}/awards/', {'action': 'add', 'title': 'X',
                                                                   'awarded_on': future}, follow=True)
        self.assertContains(response, 'cannot be dated in the future')
        self.assertFalse(Award.objects.exists())

    def test_a_teacher_can_record_but_not_remove_others_awards(self):
        award = Award.objects.create(tenant=self.school, student=self.ada, title='Admin award',
                                     recorded_by=self.admin)
        teacher = make_teacher(self.school)
        set_current_tenant(self.school)
        self.client.force_login(teacher)
        self._post(f'/students/{self.ada.id}/awards/', {'action': 'add', 'title': 'Quiz winner'})
        self._post(f'/students/{self.ada.id}/awards/', {'action': 'delete', 'award_id': award.id})
        self.assertEqual(set(Award.objects.values_list('title', flat=True)), {'Admin award', 'Quiz winner'})

    def test_appointing_and_ending_a_role(self):
        self.client.force_login(self.admin)
        self._post(f'/students/{self.ada.id}/roles/', {'action': 'add', 'title': 'Head Girl',
                                                       'kind': 'SCHOOL', 'start_date': '2026-09-08'})
        role = LeadershipRole.objects.get()
        self.assertTrue(role.is_current)
        self._post(f'/students/{self.ada.id}/roles/', {'action': 'end', 'role_id': role.id})
        role.refresh_from_db()
        self.assertEqual(role.end_date, timezone.localdate())

    def test_a_role_cannot_end_before_it_starts(self):
        self.client.force_login(self.admin)
        response = self._post(f'/students/{self.ada.id}/roles/', {
            'action': 'add', 'title': 'Prefect', 'start_date': '2026-09-08', 'end_date': '2026-09-01'},
            follow=True)
        self.assertContains(response, 'cannot end before it starts')

    def test_the_awards_register_page(self):
        Award.objects.create(tenant=self.school, student=self.ada, title='Spelling bee')
        LeadershipRole.objects.create(tenant=self.school, student=self.ada, title='Class Captain')
        self.client.force_login(self.admin)
        page = self.client.get('/students/achievements/', HTTP_HOST=self.host(self.school))
        self.assertContains(page, 'Spelling bee')
        self.assertContains(page, 'Class Captain')


@override_settings(ROOT_DOMAIN='localhost', ALLOWED_HOSTS=['*'])
class SupportAndCbtTests(Base):

    def setUp(self):
        super().setUp()
        self.subject = Subject.objects.create(tenant=self.school, name='Mathematics')
        now = timezone.now()
        self.exam = Exam.objects.create(tenant=self.school, title='Maths test', subject=self.subject,
                                        session=self.session, term=self.term, published=True,
                                        start_time=now - timedelta(minutes=5),
                                        end_time=now + timedelta(minutes=55), duration_minutes=60)
        self.exam.classes.add(self.jss1)
        self.part = ExamPart.objects.create(exam=self.exam, part_type='CBT', duration_minutes=60)

    def _support(self, **kw):
        return SupportNeeds.objects.create(tenant=self.school, student=self.ada, **kw)

    def test_exam_time_includes_extra_time(self):
        self.assertEqual(support.exam_seconds(60, self.ada), 3600)
        self._support(extra_time_percent=25)
        self.assertEqual(support.exam_seconds(60, self.ada), 4500)

    def test_the_exam_page_gives_the_extra_time(self):
        self._support(extra_time_percent=50, large_print=True)
        self.client.force_login(self.ada.user)
        page = self.client.get(f'/cbt/student/exams/{self.exam.id}/take/CBT/',
                               HTTP_HOST=self.host(self.school))
        self.assertEqual(page.status_code, 200)
        self.assertGreater(page.context['remaining_seconds'], 3600 + 1700)   # ~90 minutes
        self.assertTrue(page.context['large_print'])
        self.assertContains(page, 'Extra time included (+50%)')
        self.assertContains(page, 'font-size: 20px')

    def test_extra_time_can_run_past_the_end_for_a_started_exam(self):
        self._support(extra_time_percent=50)
        Exam.objects.filter(pk=self.exam.pk).update(end_time=timezone.now() - timedelta(minutes=10))
        self.exam.refresh_from_db()
        self.assertFalse(self.exam.is_active)
        self.assertTrue(support.exam_open_for(self.exam, self.ada, attempt_in_progress=True,
                                              duration_minutes=60))     # 30 minutes' grace
        self.assertFalse(support.exam_open_for(self.exam, self.ada, attempt_in_progress=False,
                                               duration_minutes=60))    # but not to start

    def test_without_extra_time_the_window_is_strict(self):
        Exam.objects.filter(pk=self.exam.pk).update(end_time=timezone.now() - timedelta(minutes=1))
        self.exam.refresh_from_db()
        self.assertFalse(support.exam_open_for(self.exam, self.ada, True, 60))

    def test_the_grace_ends_when_the_extra_time_does(self):
        self._support(extra_time_percent=10)                      # 6 minutes on 60
        Exam.objects.filter(pk=self.exam.pk).update(end_time=timezone.now() - timedelta(minutes=8))
        self.exam.refresh_from_db()
        self.assertFalse(support.exam_open_for(self.exam, self.ada, True, 60))

    def test_a_started_exam_can_be_resumed_after_the_end_with_extra_time(self):
        self._support(extra_time_percent=50)
        ExamAttempt.objects.create(student=self.ada, exam_part=self.part,
                                   started_at=timezone.now() - timedelta(minutes=70))
        Exam.objects.filter(pk=self.exam.pk).update(end_time=timezone.now() - timedelta(minutes=5))
        self.client.force_login(self.ada.user)
        page = self.client.get(f'/cbt/student/exams/{self.exam.id}/take/CBT/',
                               HTTP_HOST=self.host(self.school))
        self.assertEqual(page.status_code, 200)
        self.assertGreater(page.context['remaining_seconds'], 1000)   # 90 − 70 = ~20 minutes

    def test_saving_support_needs(self):
        self.client.force_login(self.admin)
        self.client.post(f'/students/{self.ada.id}/support/', {
            'extra_time_percent': '25', 'reader': '1', 'classroom_notes': 'Written instructions too',
            'private_notes': 'Dyslexia assessment, May 2026'}, HTTP_HOST=self.host(self.school))
        s = SupportNeeds.objects.get()
        self.assertEqual(s.adjustments, ['25% extra', 'Reader'])

    def test_teachers_see_adjustments_but_not_private_notes(self):
        self._support(extra_time_percent=25, classroom_notes='Seat by the window',
                      private_notes='SECRET-DIAGNOSIS')
        teacher = make_teacher(self.school)
        set_current_tenant(self.school)
        self.client.force_login(teacher)
        page = self.client.get(f'/students/{self.ada.id}/', HTTP_HOST=self.host(self.school))
        self.assertContains(page, 'Seat by the window')
        self.assertNotContains(page, 'SECRET-DIAGNOSIS')
        register = self.client.get('/students/support/', HTTP_HOST=self.host(self.school))
        self.assertContains(register, '25% extra')
        self.assertNotContains(register, 'SECRET-DIAGNOSIS')

    def test_a_teacher_cannot_change_support_needs(self):
        teacher = make_teacher(self.school)
        set_current_tenant(self.school)
        self.client.force_login(teacher)
        self.client.post(f'/students/{self.ada.id}/support/', {'extra_time_percent': '100'},
                         HTTP_HOST=self.host(self.school))
        self.assertFalse(SupportNeeds.objects.exists())


@override_settings(ROOT_DOMAIN='localhost', ALLOWED_HOSTS=['*'])
class CorrectionTests(Base):

    def setUp(self):
        super().setUp()
        self.parent = User.objects.create_user(username=f'parent_{self.ada.id}', password='x',
                                               tenant=self.school, is_parent=True,
                                               email='mum@example.com')

    def test_a_request_changes_nothing_until_approved(self):
        req = change_requests.submit(self.ada, 'parent_phone', '0803 123 4567', self.parent)
        self.ada.refresh_from_db()
        self.assertIsNone(self.ada.parent_phone)
        change_requests.review(req, True, self.admin, 'Confirmed by phone')
        self.ada.refresh_from_db()
        self.assertEqual(self.ada.parent_phone, '0803 123 4567')
        change = StudentChange.objects.get(field='parent_phone')
        self.assertEqual((change.source, change.changed_by), ('Parent request', self.admin))

    def test_a_declined_request_changes_nothing(self):
        req = change_requests.submit(self.ada, 'full_name', 'Adaeze Bello', self.parent)
        change_requests.review(req, False, self.admin)
        self.ada.refresh_from_db()
        self.assertEqual(self.ada.full_name, 'Ada Bello')

    def test_values_are_checked(self):
        for field, bad, why in (('parent_email', 'not-an-email', 'email'),
                                ('parent_phone', '0803', 'full phone'),
                                ('date_of_birth', '2099-01-01', 'not possible'),
                                ('full_name', 'Ada Bello', 'already')):
            with self.assertRaisesMessage(change_requests.RequestError, why):
                change_requests.submit(self.ada, field, bad, self.parent)

    def test_one_open_request_per_detail(self):
        change_requests.submit(self.ada, 'address', '12 Allen Avenue, Ikeja', self.parent)
        with self.assertRaisesMessage(change_requests.RequestError, 'already asked'):
            change_requests.submit(self.ada, 'address', '14 Allen Avenue', self.parent)

    def test_a_request_is_reviewed_once(self):
        req = change_requests.submit(self.ada, 'address', '12 Allen Avenue', self.parent)
        change_requests.review(req, True, self.admin)
        with self.assertRaises(change_requests.RequestError):
            change_requests.review(req, False, self.admin)

    def test_the_parent_page(self):
        self.client.force_login(self.parent)
        page = self.client.get('/parent/corrections/', HTTP_HOST=self.host(self.school))
        self.assertContains(page, 'Ada Bello')
        self.client.post('/parent/corrections/', {'child_id': self.ada.id, 'field': 'address',
                                                  'new_value': '12 Allen Avenue'},
                         HTTP_HOST=self.host(self.school))
        self.assertEqual(ProfileChangeRequest.objects.get().requested_by, self.parent)

    def test_a_parent_cannot_ask_about_another_child(self):
        stranger = make_student(self.school, 'STU050', parent_email='other@example.com')
        self.client.force_login(self.parent)
        self.client.post('/parent/corrections/', {'child_id': stranger.id, 'field': 'address',
                                                  'new_value': 'x street'},
                         HTTP_HOST=self.host(self.school))
        self.assertFalse(ProfileChangeRequest.objects.exists())

    def test_the_admin_queue_applies_a_request(self):
        req = change_requests.submit(self.ada, 'address', '12 Allen Avenue', self.parent)
        self.client.force_login(self.admin)
        self.client.post('/students/corrections/', {'request_id': req.id, 'decision': 'approve'},
                         HTTP_HOST=self.host(self.school))
        self.ada.refresh_from_db()
        self.assertEqual(self.ada.address, '12 Allen Avenue')

    def test_a_teacher_cannot_approve(self):
        req = change_requests.submit(self.ada, 'address', '12 Allen Avenue', self.parent)
        teacher = make_teacher(self.school)
        set_current_tenant(self.school)
        self.client.force_login(teacher)
        self.client.post('/students/corrections/', {'request_id': req.id, 'decision': 'approve'},
                         HTTP_HOST=self.host(self.school))
        req.refresh_from_db()
        self.assertEqual(req.status, 'PENDING')


@override_settings(ROOT_DOMAIN='localhost', ALLOWED_HOSTS=['*'])
class ExportTests(Base):

    def _export(self, reason='Parent request of 2 Oct'):
        return self.client.post(f'/students/{self.ada.id}/export/', {'reason': reason},
                                HTTP_HOST=self.host(self.school))

    def test_the_archive_holds_the_whole_record(self):
        Guardian.objects.create(tenant=self.school, student=self.ada, full_name='Musa Bello',
                                phone='0805 555 1234')
        Award.objects.create(tenant=self.school, student=self.ada, title='Spelling bee')
        SupportNeeds.objects.create(tenant=self.school, student=self.ada, extra_time_percent=25)
        self.client.force_login(self.admin)
        response = self._export()
        self.assertEqual(response['Content-Type'], 'application/zip')
        self.assertEqual(response['Cache-Control'], 'private, no-store')
        z = zipfile.ZipFile(io.BytesIO(response.content))
        names = set(z.namelist())
        self.assertTrue({'record.json', 'results.csv', 'attendance.csv', 'README.txt'} <= names)
        record = json.loads(z.read('record.json'))
        self.assertEqual(record['student']['admission_number'], 'STU001')
        self.assertEqual(record['guardians'][0]['name'], 'Musa Bello')
        self.assertEqual(record['awards'][0]['title'], 'Spelling bee')
        self.assertEqual(record['support_needs']['adjustments'], ['25% extra'])

    def test_every_export_is_logged_with_a_reason(self):
        self.client.force_login(self.admin)
        self._export('Transfer to Greenfield School')
        log = DataExport.objects.get()
        self.assertEqual((log.exported_by, log.reason), (self.admin, 'Transfer to Greenfield School'))

    def test_a_reason_is_required(self):
        self.client.force_login(self.admin)
        response = self._export('')
        self.assertNotEqual(response.get('Content-Type'), 'application/zip')
        self.assertFalse(DataExport.objects.exists())

    def test_only_administrators_can_export(self):
        teacher = make_teacher(self.school)
        set_current_tenant(self.school)
        self.client.force_login(teacher)
        response = self._export()
        self.assertNotEqual(response.get('Content-Type'), 'application/zip')

    def test_another_schools_child_cannot_be_exported(self):
        other = make_school('beta')
        theirs = make_student(other, 'STU001')
        set_current_tenant(self.school)
        self.client.force_login(self.admin)
        response = self.client.post(f'/students/{theirs.id}/export/', {'reason': 'Snooping here'},
                                    HTTP_HOST=self.host(self.school))
        self.assertEqual(response.status_code, 404)
