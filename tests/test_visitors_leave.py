"""
Visitors (passes, check-in, badges), staff leave and leave-aware staff
attendance, and verifiable attendance certificates.
"""
import shutil
import tempfile
from datetime import date, datetime, time, timedelta
from unittest import mock

from django.test import override_settings
from django.utils import timezone

from academics.models import PublicHoliday
from idcards.models import StaffClockLog
from safety import staff_attendance as sa, visitors as vis
from safety.models import StaffLeave, Visit
from tenants.middleware import set_current_tenant
from users import attendance as att
from users.models import Attendance, Staff, User

from .factories import (TenantTestCase, make_admin, make_class, make_school, make_student,
                        make_teacher)


@override_settings(RUN_TASKS_EAGERLY=True)
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
        self.teacher_user = make_teacher(self.school)
        set_current_tenant(self.school)
        self.teacher = Staff.objects.get(user=self.teacher_user)
        self.teacher_user.email = 'teacher@example.com'
        self.teacher_user.save()
        self.today = timezone.localdate()


class VisitorTests(Base):

    def test_pre_registering_sends_the_pass(self):
        with mock.patch('communications.services.send_sms', return_value=(True, 'ok')) as sms:
            v = vis.pre_register(self.school, {'name': 'Mr Okon', 'phone': '08031234567', 'purpose': 'MEETING'},
                                 host=self.teacher)
        self.assertEqual(v.status, 'EXPECTED')
        self.assertIn(f'/safety/pass/{v.token}/', sms.call_args.args[1])
        self.assertIn('to see', sms.call_args.args[1])

    def test_check_in_gives_badge_numbers_and_tells_the_host(self):
        a = vis.pre_register(self.school, {'name': 'Mr Okon'}, host=self.teacher)
        b = vis.pre_register(self.school, {'name': 'Mrs Ade'})
        with mock.patch('communications.services.send_email', return_value=(True, 'ok')) as email:
            vis.check_in(a, self.admin, id_number='A123')
        vis.check_in(b, self.admin)
        self.assertEqual((a.badge_number, b.badge_number), (1, 2))
        self.assertEqual(email.call_args.args[0], 'teacher@example.com')
        self.assertEqual(a.status, 'ON_SITE')

    def test_a_visitor_expected_another_day_cannot_check_in(self):
        v = vis.pre_register(self.school, {'name': 'Mr Okon'}, expected_on=self.today + timedelta(days=2))
        with self.assertRaisesMessage(vis.VisitError, 'not today'):
            vis.check_in(v, self.admin)

    def test_walk_in_and_check_out(self):
        v = vis.walk_in(self.school, {'name': 'Delivery Man', 'purpose': 'DELIVERY', 'vehicle': 'abc 123 xy'},
                        self.admin)
        self.assertEqual((v.status, v.vehicle), ('ON_SITE', 'ABC 123 XY'))
        vis.check_out(v)
        self.assertEqual(v.status, 'LEFT')
        with self.assertRaises(vis.VisitError):
            vis.check_out(v)

    def test_checking_in_twice_is_refused(self):
        v = vis.walk_in(self.school, {'name': 'Mr Okon'}, self.admin)
        with self.assertRaisesMessage(vis.VisitError, 'already checked in'):
            vis.check_in(v, self.admin)

    def test_turning_away_needs_a_reason(self):
        v = vis.pre_register(self.school, {'name': 'Mr Okon'})
        with self.assertRaises(vis.VisitError):
            vis.turn_away(v, self.admin, '')
        vis.turn_away(v, self.admin, 'No appointment confirmed')
        self.assertEqual(v.status, 'DENIED')

    def test_bad_input(self):
        with self.assertRaisesMessage(vis.VisitError, 'full name'):
            vis.pre_register(self.school, {'name': 'Al'})
        with self.assertRaisesMessage(vis.VisitError, 'full phone'):
            vis.pre_register(self.school, {'name': 'Mr Okon', 'phone': '0803'})
        with self.assertRaises(vis.VisitError):
            vis.pre_register(self.school, {'name': 'Mr Okon'}, expected_on=self.today - timedelta(days=1))


class LeaveTests(Base):

    def test_requesting_and_approving(self):
        lv = sa.request_leave(self.teacher, 'SICK', self.today, self.today + timedelta(days=1), 'Malaria',
                              self.teacher_user)
        self.assertEqual(lv.status, 'PENDING')
        sa.review(lv, True, self.admin)
        self.assertEqual((lv.status, lv.reviewed_by), ('APPROVED', self.admin))

    def test_nobody_approves_their_own_leave(self):
        lv = sa.request_leave(self.teacher, 'ANNUAL', self.today, self.today, user=self.teacher_user)
        with self.assertRaisesMessage(sa.LeaveError, 'Someone else'):
            sa.review(lv, True, self.teacher_user)

    def test_overlapping_leave_is_refused(self):
        sa.request_leave(self.teacher, 'ANNUAL', self.today, self.today + timedelta(days=5))
        with self.assertRaisesMessage(sa.LeaveError, 'overlaps'):
            sa.request_leave(self.teacher, 'SICK', self.today + timedelta(days=3), self.today + timedelta(days=7))

    def test_dates_are_checked(self):
        with self.assertRaises(sa.LeaveError):
            sa.request_leave(self.teacher, 'ANNUAL', self.today, self.today - timedelta(days=1))
        with self.assertRaises(sa.LeaveError):
            sa.request_leave(self.teacher, 'HOLIDAY', self.today, self.today)

    def test_working_days_skip_weekends_and_holidays(self):
        monday = self.today - timedelta(days=self.today.weekday())
        PublicHoliday.objects.create(tenant=self.school, name='Independence Day', date=monday + timedelta(days=2))
        days = sa.working_days(monday, monday + timedelta(days=6), self.school)
        self.assertEqual(len(days), 4)

    def test_leave_shows_as_leave_not_absence(self):
        day = self.today - timedelta(days=self.today.weekday() + 7)          # a past Monday
        lv = StaffLeave.objects.create(tenant=self.school, staff=self.teacher, leave_type='SICK',
                                       start_date=day, end_date=day, status='APPROVED')
        rows, counts, closed = sa.day_report(self.school, day)
        row = next(r for r in rows if r['staff'] == self.teacher)
        self.assertEqual((row['state'], row['leave']), ('leave', lv))
        self.assertEqual(counts['leave'], 1)

    def test_a_clock_in_makes_them_present(self):
        day = self.today - timedelta(days=self.today.weekday() + 7)
        log = StaffClockLog.objects.create(tenant=self.school, staff=self.teacher, direction='IN')
        StaffClockLog.objects.filter(pk=log.pk).update(
            scanned_at=timezone.make_aware(datetime.combine(day, time(7, 40))))
        rows, _, _ = sa.day_report(self.school, day)
        row = next(r for r in rows if r['staff'] == self.teacher)
        self.assertEqual(row['state'], 'present')

    def _clock(self, day, at, direction):
        log = StaffClockLog.objects.create(tenant=self.school, staff=self.teacher, direction=direction)
        StaffClockLog.objects.filter(pk=log.pk).update(scanned_at=timezone.make_aware(datetime.combine(day, at)))

    def test_clock_out_and_time_on_site(self):
        day = self.today - timedelta(days=self.today.weekday() + 7)
        self._clock(day, time(7, 30), 'IN')
        self._clock(day, time(15, 0), 'OUT')
        rows, counts, _ = sa.day_report(self.school, day)
        row = next(r for r in rows if r['staff'] == self.teacher)
        self.assertEqual((row['arrived'].time(), row['left'].time(), row['hours']), (time(7, 30), time(15, 0), '7h 30m'))
        self.assertFalse(row['no_clock_out'])
        self.assertEqual(counts['clocked_out'], 1)

    def test_a_past_day_without_clock_out_is_flagged(self):
        day = self.today - timedelta(days=self.today.weekday() + 7)
        self._clock(day, time(7, 30), 'IN')
        rows, counts, _ = sa.day_report(self.school, day)
        row = next(r for r in rows if r['staff'] == self.teacher)
        self.assertTrue(row['no_clock_out'])
        self.assertIsNone(row['left'])
        self.assertEqual(counts['no_clock_out'], 1)

    def test_a_repeat_scan_does_not_clock_out(self):
        from idcards.models import ReceptionStation
        from idcards.services import record_staff_clock
        station = ReceptionStation.get_or_create_default(self.school)
        first = record_staff_clock(self.teacher, station=station)
        again = record_staff_clock(self.teacher, station=station)
        self.assertEqual((first.direction, again.pk, getattr(again, 'repeat', False)), ('IN', first.pk, True))
        StaffClockLog.objects.filter(pk=first.pk).update(scanned_at=timezone.now() - timedelta(minutes=5))
        self.assertEqual(record_staff_clock(self.teacher, station=station).direction, 'OUT')

    def test_month_summary_rate_ignores_leave_days(self):
        first = (self.today.replace(day=1) - timedelta(days=1)).replace(day=1)      # last month
        import calendar
        last = first.replace(day=calendar.monthrange(first.year, first.month)[1])
        days = sa.working_days(first, last, self.school)
        StaffLeave.objects.create(tenant=self.school, staff=self.teacher, leave_type='ANNUAL',
                                  start_date=days[0], end_date=days[-1], status='APPROVED')
        rows, n = sa.month_summary(self.school, first, last)
        row = next(r for r in rows if r['staff'] == self.teacher)
        self.assertEqual((row['leave'], row['absent'], row['rate']), (n, 0, None))

    def test_days_taken(self):
        monday = date(self.today.year, 3, 2) if date(self.today.year, 3, 2).weekday() == 0 else \
            date(self.today.year, 3, 2) - timedelta(days=date(self.today.year, 3, 2).weekday())
        StaffLeave.objects.create(tenant=self.school, staff=self.teacher, leave_type='ANNUAL',
                                  start_date=monday, end_date=monday + timedelta(days=6), status='APPROVED')
        self.assertEqual(sa.days_taken(self.teacher, self.today.year), {'Annual leave': 5})


class CertificateTests(Base):

    def setUp(self):
        super().setUp()
        self.jss1 = make_class(self.school, 'JSS 1A')
        set_current_tenant(self.school)
        self.ada = make_student(self.school, 'STU001', full_name='Ada Bello', school_class=self.jss1)
        for i in range(1, 11):
            Attendance.objects.create(tenant=self.school, student=self.ada, school_class=self.jss1,
                                      date=self.today - timedelta(days=i), status='A' if i == 1 else 'P')

    def test_the_figures(self):
        c = att.certificate(self.ada, self.today - timedelta(days=30), self.today)
        self.assertEqual((c['present'], c['absent'], c['rate']), (9, 1, 90))

    def test_a_reference_verifies(self):
        c = att.certificate(self.ada, self.today - timedelta(days=30), self.today)
        again = att.verify_certificate(c['reference'], self.school)
        self.assertEqual((again['student'], again['present']), (self.ada, 9))

    def test_a_tampered_reference_does_not_verify(self):
        c = att.certificate(self.ada, self.today - timedelta(days=30), self.today)
        self.assertIsNone(att.verify_certificate(c['reference'][:-3] + 'abc', self.school))

    def test_another_school_cannot_verify_ours(self):
        c = att.certificate(self.ada, self.today - timedelta(days=30), self.today)
        other = make_school('beta')
        self.assertIsNone(att.verify_certificate(c['reference'], other))

    def test_no_attendance_no_certificate(self):
        with self.assertRaisesMessage(att.AttendanceError, 'No attendance'):
            att.certificate(self.ada, self.today - timedelta(days=400), self.today - timedelta(days=300))


@override_settings(ROOT_DOMAIN='localhost', ALLOWED_HOSTS=['*'])
class PageTests(Base):

    def _get(self, path):
        return self.client.get(path, HTTP_HOST=self.host(self.school))

    def _post(self, path, data, **kw):
        return self.client.post(path, data, HTTP_HOST=self.host(self.school), **kw)

    def test_the_pages_load(self):
        self.client.force_login(self.admin)
        for path in ('/safety/visitors/', '/safety/staff/leave/', '/safety/staff/attendance/'):
            self.assertEqual(self._get(path).status_code, 200, path)

    def test_walk_in_from_the_desk_goes_to_the_badge(self):
        self.client.force_login(self.admin)
        response = self._post('/safety/visitors/', {'action': 'walk_in', 'name': 'Mr Okon', 'purpose': 'MEETING'})
        v = Visit.objects.get()
        self.assertRedirects(response, f'/safety/visitors/{v.id}/badge/', fetch_redirect_response=False)
        self.assertContains(self._get(f'/safety/visitors/{v.id}/badge/'), '#1')

    def test_the_public_pass_page(self):
        v = vis.pre_register(self.school, {'name': 'Mr Okon'})
        self.client.logout()
        page = self._get(f'/safety/pass/{v.token}/')
        self.assertContains(page, 'Mr Okon')
        self.assertNotContains(page, 'id_number')
        vis.turn_away(v, self.admin, 'No')
        self.assertEqual(self._get(f'/safety/pass/{v.token}/').status_code, 404)

    def test_scanning_a_pass_needs_staff(self):
        v = vis.pre_register(self.school, {'name': 'Mr Okon'})
        self.client.logout()
        self.assertNotEqual(self._get(f'/safety/visitors/scan/{v.token}/').status_code, 200)
        self.client.force_login(self.admin)
        self.assertContains(self._get(f'/safety/visitors/scan/{v.token}/'), 'Check in')

    def test_a_teacher_requests_leave_and_cannot_approve_it(self):
        self.client.force_login(self.teacher_user)
        self._post('/safety/staff/leave/', {'action': 'request', 'leave_type': 'SICK',
                                            'start_date': self.today.isoformat(), 'end_date': self.today.isoformat()})
        lv = StaffLeave.objects.get()
        self._post('/safety/staff/leave/', {'action': 'approve', 'leave_id': lv.id})
        lv.refresh_from_db()
        self.assertEqual(lv.status, 'PENDING')
        self.assertNotEqual(self._get('/safety/staff/attendance/').status_code, 200)

    def test_an_admin_recording_leave_for_someone_approves_it(self):
        self.client.force_login(self.admin)
        self._post('/safety/staff/leave/', {'action': 'request', 'staff_id': self.teacher.id,
                                            'leave_type': 'OFFICIAL', 'start_date': self.today.isoformat(),
                                            'end_date': self.today.isoformat()})
        self.assertEqual(StaffLeave.objects.get().status, 'APPROVED')

    def test_certificate_page_and_public_verification(self):
        jss1 = make_class(self.school, 'JSS 1A')
        set_current_tenant(self.school)
        ada = make_student(self.school, 'STU001', full_name='Ada Bello', school_class=jss1)
        Attendance.objects.create(tenant=self.school, student=ada, school_class=jss1,
                                  date=self.today - timedelta(days=1), status='P')
        self.client.force_login(self.admin)
        page = self._get(f'/students/{ada.id}/attendance-certificate/?purpose=Visa')
        self.assertContains(page, 'Certificate of Attendance')
        ref = page.context['cert']['reference']
        self.client.logout()
        verify = self._get(f'/attendance/verify/{ref}/')
        self.assertContains(verify, 'Certificate')
        self.assertContains(verify, 'Ada Bello')
        self.assertEqual(self._get('/attendance/verify/forged-reference/').status_code, 404)

    def test_teachers_cannot_issue_certificates(self):
        self.client.force_login(self.teacher_user)
        jss1 = make_class(self.school, 'JSS 1A')
        ada = make_student(self.school, 'STU001', school_class=jss1)
        self.assertNotEqual(self._get(f'/students/{ada.id}/attendance-certificate/').status_code, 200)

    def test_parents_cannot_reach_visitors_or_leave(self):
        parent = User.objects.create_user(username='par9', password='x', tenant=self.school, is_parent=True)
        self.client.force_login(parent)
        for path in ('/safety/visitors/', '/safety/staff/leave/', '/safety/staff/attendance/'):
            self.assertNotEqual(self._get(path).status_code, 200, path)


class FreshSettingsTests(Base):
    """A school that has never opened the settings pages must still work."""

    def test_lateness_and_early_pickup_work_on_brand_new_settings(self):
        from safety import services as svc
        moment = timezone.make_aware(datetime.combine(self.today, time(9, 0)))
        self.assertEqual(att.minutes_late(self.school, moment), 60)          # created on the fly
        self.assertIn(svc.is_early(self.school, moment), (True, False))

    def test_a_pass_link_can_be_built_outside_a_request(self):
        v = vis.pre_register(self.school, {'name': 'Mr Okon'})
        self.assertIn(f'/safety/pass/{v.token}/', vis.pass_url(v))
