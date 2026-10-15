"""
Staff work reports: writing daily/weekly reports, drafts, submitting,
lateness, the administrators' register, reviewing, and who can see what.
"""
from datetime import datetime, time, timedelta

from django.utils import timezone

from hr import reports as rpt
from hr.models import StaffReport
from notifications import services as notify
from safety.models import StaffLeave
from users.models import Staff

from .factories import TenantTestCase, make_admin, make_school, make_teacher

WORK = 'Taught JSS 1A fractions for two periods and marked the exercise books.'


class Base(TenantTestCase):

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha')
        self.admin = make_admin(self.school)
        self.teacher_user = make_teacher(self.school, 'chidi', full_name='Chidi Okafor')
        self.teacher = Staff.objects.get(user=self.teacher_user)
        self.other_user = make_teacher(self.school, 'aisha', full_name='Aisha Danjuma')
        self.today = timezone.localdate()

    def submit(self, staff=None, period='DAILY', day=None, text=WORK):
        r = rpt.report_for(staff or self.teacher, period, day or self.today)
        return rpt.save(r, {'work_done': text}, submit=True)

    def _post(self, path, data=None, user=None):
        self.client.force_login(user or self.teacher_user)
        return self.client.post(path, data or {}, HTTP_HOST=self.host(self.school))

    def _get(self, path, user=None):
        self.client.force_login(user or self.teacher_user)
        return self.client.get(path, HTTP_HOST=self.host(self.school))


class WritingTests(Base):

    def test_week_runs_monday_to_friday(self):
        wednesday = self.today - timedelta(days=self.today.weekday()) + timedelta(days=2)
        start, end = rpt.bounds(StaffReport.WEEKLY, wednesday)
        self.assertEqual((start.weekday(), (end - start).days), (0, 4))

    def test_a_draft_is_private_until_submitted(self):
        r = rpt.save(rpt.report_for(self.teacher, 'DAILY', self.today), {'work_done': 'Half written'})
        self.assertEqual(r.status, 'DRAFT')
        self.assertFalse(notify.visible_to(self.admin, self.school).filter(kind='report').exists())
        self.assertEqual(self._get(f'/hr/reports/{r.pk}/', user=self.admin).status_code, 302)
        r = rpt.save(r, {'work_done': WORK}, submit=True)
        self.assertEqual(r.status, 'SUBMITTED')
        self.assertEqual([n.title for n in notify.visible_to(self.admin, self.school).filter(kind='report')],
                         ['Daily report from Chidi Okafor'])

    def test_one_report_per_period_and_edits_update_it(self):
        self.submit()
        self.submit(text=WORK + ' Also covered SS 2B.')
        self.assertEqual(StaffReport.objects.filter(staff=self.teacher, period='DAILY').count(), 1)
        self.assertEqual(notify.visible_to(self.admin, self.school).filter(kind='report').count(), 1)

    def test_too_short_empty_and_future_reports_are_refused(self):
        with self.assertRaisesMessage(rpt.ReportError, 'more detail'):
            self.submit(text='Taught')
        with self.assertRaisesMessage(rpt.ReportError, 'empty'):
            rpt.save(rpt.report_for(self.teacher, 'DAILY', self.today), {})
        with self.assertRaisesMessage(rpt.ReportError, 'not started yet'):
            self.submit(day=self.today + timedelta(days=8))

    def test_lateness(self):
        on_time = self.submit(day=self.today - timedelta(days=1))
        self.assertFalse(on_time.late)
        old = self.submit(day=self.today - timedelta(days=10))
        self.assertTrue(old.late)
        weekly = self.submit(period='WEEKLY', day=self.today - timedelta(days=self.today.weekday() + 7))
        self.assertEqual(weekly.late, self.today.weekday() > 0)        # due the Monday after

    def test_writing_through_the_page(self):
        r = self._post('/hr/reports/write/', {'period': 'weekly', 'action': 'submit', 'work_done': WORK,
                                              'support_needed': 'A working projector in Lab 2.'})
        report = StaffReport.objects.get(staff=self.teacher, period='WEEKLY')
        self.assertRedirects(r, f'/hr/reports/{report.pk}/', fetch_redirect_response=False)
        self.assertEqual(report.support_needed, 'A working projector in Lab 2.')
        self.assertContains(self._get('/hr/reports/mine/'), 'Week of')


class ReviewTests(Base):

    def test_review_notifies_and_locks_the_report(self):
        r = self.submit()
        resp = self._post(f'/hr/reports/{r.pk}/', {'feedback': 'Well done.'}, user=self.admin)
        self.assertEqual(resp.status_code, 302)
        r.refresh_from_db()
        self.assertEqual((r.status, r.feedback, r.reviewed_by), ('REVIEWED', 'Well done.', self.admin))
        self.assertEqual([n.title for n in notify.visible_to(self.teacher_user, self.school).filter(kind='report')],
                         ['Your daily report was reviewed'])
        with self.assertRaisesMessage(rpt.ReportError, 'reviewed'):
            self.submit()
        self.assertEqual(self._get('/hr/reports/write/?period=daily')['Location'], f'/hr/reports/{r.pk}/')

    def test_you_cannot_review_your_own_report(self):
        admin_staff = Staff.objects.create(tenant=self.school, user=self.admin, full_name='Head Teacher', role='ADMIN')
        r = self.submit(staff=admin_staff)
        with self.assertRaisesMessage(rpt.ReportError, 'Someone else'):
            rpt.review(r, self.admin)

    def test_staff_only_see_their_own_reports(self):
        r = self.submit()
        self.assertEqual(self._get(f'/hr/reports/{r.pk}/', user=self.other_user).status_code, 302)
        self.assertEqual(self._get('/hr/reports/', user=self.other_user).status_code, 302)
        self.assertEqual(self._get(f'/hr/reports/{r.pk}/').status_code, 200)


class RegisterTests(Base):

    def test_register_states(self):
        monday = self.today - timedelta(days=self.today.weekday() + 7)      # a full past week
        sent = self.submit(day=monday)
        StaffReport.objects.filter(pk=sent.pk).update(                              # sent that afternoon: on time
            submitted_at=timezone.make_aware(datetime.combine(monday, time(16, 0))))
        other = Staff.objects.get(user=self.other_user)
        rpt.save(rpt.report_for(other, 'DAILY', monday), {'work_done': 'Started…'})   # draft only
        rows, counts, closed, _ = rpt.register(self.school, 'DAILY', monday)
        states = {r['staff'].full_name: r['state'] for r in rows}
        self.assertEqual((states['Chidi Okafor'], states['Aisha Danjuma']), ('submitted', 'draft'))
        self.assertEqual((counts['in'], counts['to_review'], counts['outstanding']), (1, 1, 1))
        self.assertFalse(closed)

    def test_overdue_leave_and_weekends(self):
        monday = self.today - timedelta(days=self.today.weekday() + 14)
        StaffLeave.objects.create(tenant=self.school, staff=Staff.objects.get(user=self.other_user), leave_type='SICK',
                                  start_date=monday, end_date=monday, status='APPROVED')
        rows, _, _, _ = rpt.register(self.school, 'DAILY', monday)
        states = {r['staff'].full_name: r['state'] for r in rows}
        self.assertEqual((states['Chidi Okafor'], states['Aisha Danjuma']), ('overdue', 'leave'))
        _, _, closed, _ = rpt.register(self.school, 'DAILY', monday + timedelta(days=5))     # Saturday
        self.assertTrue(closed)

    def test_the_register_page(self):
        self.submit()
        resp = self._get('/hr/reports/', user=self.admin)
        self.assertContains(resp, 'Chidi Okafor')
        self.assertContains(resp, 'Review')
        self.assertContains(self._get('/hr/reports/?period=weekly', user=self.admin), 'Weekly register')
