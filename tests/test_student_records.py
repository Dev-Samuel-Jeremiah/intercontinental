"""
Student records: the status lifecycle and alumni, the change history, the
360° profile and its timeline — and that students who have left drop out of
the places only current students belong.
"""
from datetime import date, timedelta
from decimal import Decimal

from django.test import override_settings
from django.utils import timezone

from academics.models import AcademicSession, Term
from finance.models import ChildBilling, ParentBilling, Payment, Wallet
from tenants.middleware import set_current_tenant
from users.audit import audit_source
from users.lifecycle import StatusError, change_status
from users.models import (ENROLLED_STATUSES, Attendance, Student, StudentChange,
                          StudentStatusChange, User)
from users import profile

from .factories import (TenantTestCase, make_admin, make_class, make_school, make_student,
                        make_teacher)


class RecordsBase(TenantTestCase):

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha')
        self.admin = make_admin(self.school)
        self.jss1 = make_class(self.school, 'JSS 1')
        self.jss2 = make_class(self.school, 'JSS 2')
        set_current_tenant(self.school)
        self.ada = make_student(self.school, 'STU001', full_name='Ada Bello',
                                school_class=self.jss1, parent_email='bello@example.com')

    def _refresh(self, *objs):
        for o in objs:
            o.refresh_from_db()


class StatusLifecycleTests(RecordsBase):

    def test_suspending_keeps_the_student_on_the_roll(self):
        change_status(self.ada, 'Suspended', reason='Fighting', until=timezone.localdate()
                      + timedelta(days=7), user=self.admin)
        self._refresh(self.ada, self.ada.user)
        self.assertEqual(self.ada.status, 'Suspended')
        self.assertIn(self.ada.status, ENROLLED_STATUSES)
        self.assertTrue(self.ada.user.is_active)
        self.assertIsNone(self.ada.left_on)
        change = StudentStatusChange.objects.get()
        self.assertEqual((change.from_status, change.to_status, change.reason),
                         ('Active', 'Suspended', 'Fighting'))
        self.assertIsNotNone(change.until)

    def test_withdrawing_takes_them_off_the_roll_and_closes_their_login(self):
        change_status(self.ada, 'Withdrawn', reason='Family moved to Abuja',
                      destination='Greenfield School, Abuja', user=self.admin)
        self._refresh(self.ada, self.ada.user)
        self.assertEqual(self.ada.left_on, timezone.localdate())
        self.assertEqual(self.ada.leaving_class, 'JSS 1')
        self.assertEqual(self.ada.destination, 'Greenfield School, Abuja')
        self.assertFalse(self.ada.user.is_active)

    def test_graduating_moves_them_to_the_alumni_register(self):
        change_status(self.ada, 'Graduated', effective_on=date(2026, 7, 20),
                      destination='University of Lagos', user=self.admin)
        self._refresh(self.ada)
        self.assertTrue(self.ada.is_alumnus)
        self.assertEqual(self.ada.left_on, date(2026, 7, 20))

    def test_re_admitting_restores_their_login(self):
        change_status(self.ada, 'Withdrawn', reason='Moved', user=self.admin)
        change_status(self.ada, 'Active', reason='Came back', user=self.admin)
        self._refresh(self.ada, self.ada.user)
        self.assertTrue(self.ada.user.is_active)
        self.assertIsNone(self.ada.left_on)
        self.assertEqual(self.ada.status_changes.count(), 2)

    def test_some_changes_need_a_reason(self):
        for status in ('Suspended', 'Withdrawn', 'Deceased'):
            with self.assertRaisesMessage(StatusError, 'Give a reason'):
                change_status(self.ada, status, user=self.admin)
        change_status(self.ada, 'Graduated', user=self.admin)     # no reason needed

    def test_the_same_status_twice_is_refused(self):
        with self.assertRaisesMessage(StatusError, 'already active'):
            change_status(self.ada, 'Active')

    def test_a_suspension_must_end_after_it_starts(self):
        with self.assertRaisesMessage(StatusError, 'return date'):
            change_status(self.ada, 'Suspended', reason='x', until=timezone.localdate())

    def test_an_invalid_status_is_refused(self):
        with self.assertRaises(StatusError):
            change_status(self.ada, 'Expelled', reason='x')


class ChangeHistoryTests(RecordsBase):

    def test_an_edit_records_old_and_new_values(self):
        self.ada.parent_phone = '08031234567'
        self.ada.full_name = 'Adaeze Bello'
        with audit_source('Edit', self.admin):
            self.ada.save()
        rows = {c.field: c for c in StudentChange.objects.all()}
        self.assertEqual(set(rows), {'parent_phone', 'full_name'})
        self.assertEqual((rows['full_name'].old_value, rows['full_name'].new_value),
                         ('Ada Bello', 'Adaeze Bello'))
        self.assertEqual(rows['full_name'].changed_by, self.admin)

    def test_saving_without_changes_records_nothing(self):
        self.ada.save()
        self.assertFalse(StudentChange.objects.exists())

    def test_passwords_are_never_recorded(self):
        self.ada.parent_term_password = 'NEW-SECRET-9'
        self.ada.save()
        self.assertFalse(StudentChange.objects.exists())
        self.assertFalse(StudentChange.objects.filter(new_value__contains='SECRET').exists())

    def test_a_class_change_shows_class_names(self):
        self.ada.class_assigned = self.jss2
        self.ada.save()
        change = StudentChange.objects.get()
        self.assertEqual((change.label, change.old_value, change.new_value),
                         ('Class', 'JSS 1', 'JSS 2'))

    def test_a_status_change_is_in_the_history_too(self):
        change_status(self.ada, 'Suspended', reason='Late', user=self.admin)
        change = StudentChange.objects.get(field='status')
        self.assertEqual((change.source, change.changed_by), ('Status change', self.admin))

    def test_creating_a_student_is_not_a_change(self):
        make_student(self.school, 'STU002')
        self.assertFalse(StudentChange.objects.exists())


class ProfileDataTests(RecordsBase):

    def test_siblings_are_found_by_parent_email(self):
        tobi = make_student(self.school, 'STU002', full_name='Tobi Bello',
                            parent_email='bello@example.com')
        make_student(self.school, 'STU003', full_name='Stranger')
        self.assertEqual(profile.siblings(self.ada), [tobi])

    def test_attendance_counts_this_term_and_overall(self):
        session = AcademicSession.objects.create(tenant=self.school, name='2026/2027',
                                                 start_date=date(2026, 9, 1))
        term = Term.objects.create(tenant=self.school, name='1st Term', session=session,
                                   start_date=date(2026, 9, 1), end_date=date(2026, 12, 20))
        for day, mark in ((date(2026, 7, 1), 'A'), (date(2026, 9, 10), 'P'),
                          (date(2026, 9, 11), 'P'), (date(2026, 9, 12), 'A')):
            Attendance.objects.create(tenant=self.school, student=self.ada,
                                      school_class=self.jss1, date=day, status=mark)
        summary = profile.attendance_summary(self.ada, term)
        self.assertEqual((summary['term']['present'], summary['term']['days'],
                          summary['term']['rate']), (2, 3, 67))
        self.assertEqual(summary['overall']['days'], 4)
        self.assertEqual(summary['recent_absences'][0], date(2026, 9, 12))

    def test_no_attendance_is_handled(self):
        summary = profile.attendance_summary(self.ada)
        self.assertIsNone(summary['overall']['rate'])

    def test_opening_a_profile_creates_nothing(self):
        before = Wallet.objects.count()
        profile.fees_summary(self.ada)
        profile.timeline(self.ada)
        self.assertEqual(Wallet.objects.count(), before)

    def test_the_timeline(self):
        self.ada.class_assigned = self.jss2
        with audit_source('Promotion', self.admin):
            self.ada.save()
        change_status(self.ada, 'Suspended', reason='Fighting', user=self.admin)
        wallet = Wallet.objects.create(tenant=self.school, student=self.ada)
        Payment.objects.create(tenant=self.school, wallet=wallet, amount=Decimal('50000'),
                               reference='PAY-1', status=Payment.APPROVED, method='CASH')
        titles = [e['title'] for e in profile.timeline(self.ada)]
        self.assertIn('Enrolled', titles)
        self.assertIn('Promoted to JSS 2', titles)
        self.assertIn('Suspended', titles)
        self.assertIn('Payment of ₦50,000 received', titles)
        self.assertEqual(titles[-1], 'Enrolled')           # oldest last

    def test_fees_can_be_left_out_of_the_timeline(self):
        wallet = Wallet.objects.create(tenant=self.school, student=self.ada)
        Payment.objects.create(tenant=self.school, wallet=wallet, amount=Decimal('50000'),
                               reference='PAY-1', status=Payment.APPROVED, method='CASH')
        titles = [e['title'] for e in profile.timeline(self.ada, include_fees=False)]
        self.assertFalse(any('Payment' in t for t in titles))


class OnTheRollTests(RecordsBase):
    """Students who have left must not turn up where only current students belong."""

    def setUp(self):
        super().setUp()
        self.gone = make_student(self.school, 'STU009', full_name='Gone Student',
                                 school_class=self.jss1)
        change_status(self.gone, 'Withdrawn', reason='Moved', user=self.admin)
        self.away = make_student(self.school, 'STU010', full_name='Away Student',
                                 school_class=self.jss1)
        change_status(self.away, 'Suspended', reason='Fighting', user=self.admin)

    @override_settings(ROOT_DOMAIN='localhost', ALLOWED_HOSTS=['*'])
    def test_the_attendance_register_lists_only_enrolled_students(self):
        self.client.force_login(self.admin)
        response = self.client.get(f'/mark/{self.jss1.id}/', HTTP_HOST=self.host(self.school))
        self.assertEqual(response.status_code, 200)
        names = [s.full_name for s in response.context['students']]
        self.assertIn('Ada Bello', names)
        self.assertIn('Away Student', names)            # suspended: still on the register
        self.assertNotIn('Gone Student', names)

    def test_a_suspended_childs_parents_still_get_fee_messages(self):
        enrolled = Student.objects.filter(status__in=ENROLLED_STATUSES)
        self.assertIn(self.away, enrolled)
        self.assertNotIn(self.gone, enrolled)

    def test_a_withdrawn_seat_is_free_again(self):
        from admissions.models import AdmissionQuota
        from admissions.services import seats_left
        AdmissionQuota.objects.create(tenant=self.school, school_class=self.jss1, seats=5)
        # 5 seats, taken by Ada and the suspended student; the withdrawn one frees theirs.
        self.assertEqual(seats_left(self.school, self.jss1), 3)


@override_settings(ROOT_DOMAIN='localhost', ALLOWED_HOSTS=['*'])
class ProfilePageTests(RecordsBase):

    def _get(self, path):
        return self.client.get(path, HTTP_HOST=self.host(self.school))

    def _post(self, path, data, **kwargs):
        return self.client.post(path, data, HTTP_HOST=self.host(self.school), **kwargs)

    def test_an_admin_sees_everything(self):
        self.client.force_login(self.admin)
        response = self._get(f'/students/{self.ada.id}/')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['can_manage'])
        self.assertIsNotNone(response.context['fees'])
        self.assertContains(response, 'Change history')

    def test_a_teacher_sees_the_profile_but_not_fees_or_history(self):
        teacher = make_teacher(self.school)
        set_current_tenant(self.school)
        self.client.force_login(teacher)
        response = self._get(f'/students/{self.ada.id}/')
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context['fees'])
        self.assertEqual(response.context['changes'], [])
        self.assertNotContains(response, 'Change history')

    def test_a_teacher_cannot_change_status(self):
        teacher = make_teacher(self.school)
        set_current_tenant(self.school)
        self.client.force_login(teacher)
        self._post(f'/students/{self.ada.id}/status/', {'status': 'Withdrawn', 'reason': 'x'})
        self._refresh(self.ada)
        self.assertEqual(self.ada.status, 'Active')

    def test_a_parent_cannot_open_profiles(self):
        parent = User.objects.create_user(username='par1', password='x', tenant=self.school,
                                          is_parent=True)
        self.client.force_login(parent)
        self.assertNotEqual(self._get(f'/students/{self.ada.id}/').status_code, 200)
        self.assertNotEqual(self._get('/students/alumni/').status_code, 200)

    def test_changing_status_from_the_profile(self):
        self.client.force_login(self.admin)
        response = self._post(f'/students/{self.ada.id}/status/',
                              {'status': 'Graduated', 'effective_on': '2026-07-20',
                               'destination': 'Covenant University'}, follow=True)
        self.assertContains(response, 'moved to the alumni register')
        self._refresh(self.ada)
        self.assertEqual((self.ada.status, self.ada.destination),
                         ('Graduated', 'Covenant University'))

    def test_a_refused_status_change_shows_why(self):
        self.client.force_login(self.admin)
        response = self._post(f'/students/{self.ada.id}/status/', {'status': 'Withdrawn'},
                              follow=True)
        self.assertContains(response, 'Give a reason')

    def test_another_schools_student_is_not_found(self):
        other = make_school('beta')
        theirs = make_student(other, 'STU001')
        set_current_tenant(self.school)
        self.client.force_login(self.admin)
        self.assertEqual(self._get(f'/students/{theirs.id}/').status_code, 404)

    def test_the_alumni_register_and_export(self):
        change_status(self.ada, 'Graduated', effective_on=date(2026, 7, 20),
                      destination='UNILAG', user=self.admin)
        make_student(self.school, 'STU002', full_name='Still Here', school_class=self.jss1)
        self.client.force_login(self.admin)
        page = self._get('/students/alumni/?year=2026')
        self.assertEqual([s.full_name for s in page.context['alumni']], ['Ada Bello'])
        csv_text = self._get('/students/alumni/export/?year=2026').content.decode()
        self.assertIn('Ada Bello,STU001', csv_text)
        self.assertIn('UNILAG', csv_text)
        self.assertNotIn('Still Here', csv_text)

    def test_promotion_is_recorded_in_the_history(self):
        self.client.force_login(self.admin)
        self._post('/students/promote/', {'student_ids': [self.ada.id],
                                          'new_class': self.jss2.id, 'new_term': '1st Term'})
        self._refresh(self.ada)
        self.assertEqual(self.ada.class_assigned, self.jss2)
        change = StudentChange.objects.get(field='class_assigned')
        self.assertEqual((change.old_value, change.new_value, change.source),
                         ('JSS 1', 'JSS 2', 'Promotion'))

    def test_deleting_needs_a_form_not_a_link(self):
        self.client.force_login(self.admin)
        response = self._get(f'/students/delete/{self.ada.id}/')
        self.assertRedirects(response, f'/students/{self.ada.id}/', fetch_redirect_response=False)
        self.assertTrue(Student.objects.filter(pk=self.ada.pk).exists())

    def test_a_student_with_payments_cannot_be_deleted_but_does_not_crash(self):
        session = AcademicSession.objects.create(tenant=self.school, name='2026/2027')
        term = Term.objects.create(tenant=self.school, name='1st Term', session=session)
        wallet = Wallet.objects.create(tenant=self.school, student=self.ada)
        parent = ParentBilling.objects.create(tenant=self.school, wallet=wallet,
                                              session=session, term=term)
        ChildBilling.objects.create(tenant=self.school, student=self.ada, parent_billing=parent)
        self.client.force_login(self.admin)
        response = self._post(f'/students/delete/{self.ada.id}/', {}, follow=True)
        self.assertContains(response, 'Mark them Withdrawn or Graduated instead')
        self.assertTrue(Student.objects.filter(pk=self.ada.pk).exists())

    def test_a_student_who_has_left_cannot_sign_in(self):
        change_status(self.ada, 'Withdrawn', reason='Moved', user=self.admin)
        response = self._post('/student/login/', {'admission_number': 'STU001',
                                                  'password': 'stud-pass-1'}, follow=True)
        self.assertContains(response, 'This student account is closed')
        self.assertFalse(response.wsgi_request.user.is_authenticated)

    def test_a_suspended_student_can_still_sign_in(self):
        change_status(self.ada, 'Suspended', reason='Late', user=self.admin)
        response = self._post('/student/login/', {'admission_number': 'STU001',
                                                  'password': 'stud-pass-1'})
        self.assertEqual(response.status_code, 302)
