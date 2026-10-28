"""
HR: staff records and directory, onboarding, contracts and reminders,
appraisals, training, discipline, exits and final settlement, recruitment
and the public careers page — plus extra-lesson and cover pay in payroll.
"""
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.utils import timezone

from curriculum.models import CoverAssignment, Department
from hr import services as svc
from hr.models import (Appraisal, DisciplinaryCase, ExitCase, JobApplication, OnboardingStep, StaffRecord,
                       Vacancy)
from payroll import runs
from payroll.models import PayrollSettings, Payslip, StaffLoan, StaffPayroll
from tenants.middleware import set_current_tenant
from timetable.models import (ExtraLessonGroup, GeneratedTimetable, TimeSlot, TimetableConfiguration,
                              TimetableSlot)
from users.models import Staff, Subject, User

from .factories import TenantTestCase, make_admin, make_class, make_school, make_teacher

D = Decimal


def pdf(name='cv.pdf'):
    return SimpleUploadedFile(name, b'%PDF-1.4 test file', content_type='application/pdf')


@override_settings(RUN_TASKS_EAGERLY=True)
class Base(TenantTestCase):

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha')
        self.admin = make_admin(self.school)
        self.admin.email = 'head@example.com'
        self.admin.save()
        self.t_user = make_teacher(self.school, username='alphat', full_name='Mrs Ade')
        self.t_user.email = 'ade@example.com'
        self.t_user.save()
        self.h_user = make_teacher(self.school, username='alphah', full_name='Mr Hod')
        set_current_tenant(self.school)
        self.teacher = Staff.objects.get(user=self.t_user)
        self.hod = Staff.objects.get(user=self.h_user)
        self.dept = Department.objects.create(tenant=self.school, name='Sciences', hod=self.hod)

    def _get(self, path, **kw):
        return self.client.get(path, HTTP_HOST=self.host(self.school), **kw)

    def _post(self, path, data=None, **kw):
        return self.client.post(path, data or {}, HTTP_HOST=self.host(self.school), **kw)


class DirectoryAndOnboardingTests(Base):

    def test_directory_search_and_admin_only_file(self):
        rec = svc.record_for(self.teacher)
        rec.job_title, rec.extension, rec.department = 'Chemistry teacher', '214', self.dept
        rec.save()
        self.client.force_login(self.h_user)
        page = self._get('/hr/directory/?q=214')
        self.assertContains(page, 'Mrs Ade')
        self.assertNotIn('Mr Hod', page.content.decode().split('id="content"', 1)[-1])   # not in the results
        self.assertEqual(self._get(f'/hr/staff/{self.teacher.pk}/').status_code, 302)
        self.client.force_login(self.admin)
        self.assertContains(self._get(f'/hr/staff/{self.teacher.pk}/'), 'Chemistry teacher')

    def test_task_needing_document_cannot_be_ticked_without_it(self):
        steps = svc.checklist(self.teacher)
        self.assertTrue(steps)
        needs = next(s for s in steps if s.task.needs_file)
        with self.assertRaises(svc.HRError):
            svc.tick(needs, self.admin)
        svc.tick(needs, self.admin, pdf('guarantor.pdf'))
        needs.refresh_from_db()
        self.assertTrue(needs.done)
        plain = next(s for s in steps if not s.task.needs_file)
        self.client.force_login(self.admin)
        self._post(f'/hr/staff/{self.teacher.pk}/', {'action': 'tick', 'step': plain.pk})
        self.assertTrue(OnboardingStep.objects.get(pk=plain.pk).done)
        self.assertEqual(svc.onboarding_progress([self.teacher])[self.teacher.pk][0], 2)


class ContractTests(Base):

    def test_contract_rules_expiry_and_one_reminder(self):
        today = timezone.localdate()
        with self.assertRaises(svc.HRError):
            svc.add_contract(self.teacher, 'FIXED', today, None, self.admin)
        c = svc.add_contract(self.teacher, 'FIXED', today - timedelta(days=300), today + timedelta(days=20), self.admin)
        self.assertEqual(svc.current_contract(self.teacher), c)
        self.assertEqual(svc.expiring(self.school, 60), [c])
        mail.outbox.clear()
        self.assertEqual(len(svc.send_reminders(self.school)), 1)
        self.assertEqual(mail.outbox[0].to, ['head@example.com'])
        self.assertIn('Mrs Ade', mail.outbox[0].body)
        self.assertEqual(svc.send_reminders(self.school), [])
        svc.add_contract(self.teacher, 'PERMANENT', today + timedelta(days=21), None, self.admin)
        self.assertEqual(svc.expiring(self.school, 60), [])


class AppraisalTests(Base):

    def test_self_review_then_hod_review_gives_weighted_score(self):
        StaffRecord.objects.create(tenant=self.school, staff=self.teacher, department=self.dept)
        today = timezone.localdate()
        cycle = svc.start_cycle(self.school, 'First term', today, today + timedelta(days=14),
                                'Teaching: 3\nPunctuality: 1', self.admin)
        a = Appraisal.objects.get(cycle=cycle, staff=self.teacher)
        self.assertEqual(a.reviewer, self.hod)
        self.client.force_login(self.t_user)
        self._post(f'/hr/appraisals/{a.pk}/', {'action': 'self', 's0': '4', 's1': '5', 'self_comment': 'Good term'})
        a.refresh_from_db()
        self.assertEqual(a.status, Appraisal.REVIEW)
        # The staff member cannot review themselves.
        self._post(f'/hr/appraisals/{a.pk}/', {'action': 'review', 'r0': '5', 'r1': '5'})
        a.refresh_from_db()
        self.assertEqual(a.status, Appraisal.REVIEW)
        self.client.force_login(self.h_user)
        self.assertContains(self._get('/hr/me/'), 'Review Mrs Ade')
        self._post(f'/hr/appraisals/{a.pk}/', {'action': 'review', 'r0': '4', 'r1': '2'})
        a.refresh_from_db()
        self.assertEqual(a.status, Appraisal.DONE)
        self.assertEqual(a.overall, D('3.5'))  # (4×3 + 2×1) / 4

    def test_scores_must_be_one_to_five(self):
        today = timezone.localdate()
        cycle = svc.start_cycle(self.school, 'T', today, today, 'Teaching: 1', self.admin)
        a = Appraisal.objects.get(cycle=cycle, staff=self.teacher)
        with self.assertRaises(svc.HRError):
            svc.self_review(a, {'s0': '9'})


class TrainingAndDisciplineTests(Base):

    def test_staff_add_their_own_training(self):
        self.client.force_login(self.t_user)
        self._post('/hr/me/', {'action': 'training', 'title': 'Jolly Phonics', 'date': '2026-09-12', 'hours': '6',
                               'cost': '15,000', 'certificate': pdf('cert.pdf')})
        t = self.teacher.training.get()
        self.assertEqual((t.hours, t.cost), (D('6'), D('15000')))
        self.assertEqual(self._get(f'/hr/document/training/{t.pk}/').status_code, 200)
        self.client.force_login(self.h_user)
        self.assertEqual(self._get(f'/hr/document/training/{t.pk}/').status_code, 404)

    def test_query_issue_reply_close(self):
        mail.outbox.clear()
        case = svc.issue(self.teacher, 'QUERY', 'Late three days running', None, self.admin)
        self.assertEqual(mail.outbox[0].to, ['ade@example.com'])
        self.client.force_login(self.t_user)
        self.assertContains(self._get('/hr/me/'), 'waiting for your reply')
        self._post(f'/hr/case/{case.pk}/', {'action': 'respond', 'response': 'My car broke down.'})
        case.refresh_from_db()
        self.assertEqual(case.response, 'My car broke down.')
        self._post(f'/hr/case/{case.pk}/', {'action': 'close', 'outcome': 'NO_ACTION'})
        case.refresh_from_db()
        self.assertIsNone(case.closed_at)
        self.client.force_login(self.h_user)
        self.assertEqual(self._get(f'/hr/case/{case.pk}/').status_code, 404)
        svc.close_case(case, 'NO_ACTION', 'Explanation accepted')
        with self.assertRaises(svc.HRError):
            svc.respond(case, self.teacher, 'again')
        self.assertEqual(DisciplinaryCase.objects.filter(closed_at__isnull=False).count(), 1)


class ExitTests(Base):

    def test_settlement_and_completion(self):
        pr = StaffPayroll.objects.create(tenant=self.school, staff=self.teacher, basic_salary=D('200000'),
                                         housing=D('50000'), transport=D('30000'), other_allowances=D('20000'))
        StaffLoan.objects.create(tenant=self.school, staff_payroll=pr, amount=D('60000'), balance=D('40000'),
                                 monthly_repayment=D('10000'), start_month=6, start_year=2026)
        e = svc.open_exit(self.teacher, 'RESIGNATION', date(2026, 9, 15), date(2026, 10, 15), 'Relocating', self.admin)
        with self.assertRaises(svc.HRError):
            svc.open_exit(self.teacher, 'RESIGNATION', date(2026, 9, 15), date(2026, 10, 15), '', self.admin)
        e.other_amounts = D('10000')
        e.save()
        money = svc.settlement(e)
        self.assertEqual((money['worked_days'], money['month_days']), (11, 22))
        self.assertEqual(money['final_pay'], 150000.0)
        self.assertEqual(money['total'], 120000.0)
        with self.assertRaises(svc.HRError):
            svc.complete_exit(e, self.admin)
        self.client.force_login(self.admin)
        self._post(f'/hr/exit/{e.pk}/', {'action': 'handover', 'other_amounts': '10000',
                                         'done': [str(i) for i in range(len(e.handover))]})
        self._post(f'/hr/exit/{e.pk}/', {'action': 'complete', 'disable_login': '1'})
        e.refresh_from_db()
        self.assertIsNotNone(e.completed_at)
        self.assertEqual(e.settlement['total'], 120000.0)
        pr.refresh_from_db()
        self.assertFalse(pr.is_active)
        self.assertFalse(pr.loans.filter(active=True).exists())
        self.assertFalse(User.objects.get(pk=self.t_user.pk).is_active)
        self.assertEqual(StaffRecord.objects.get(staff=self.teacher).left_on, date(2026, 10, 15))
        self.assertEqual(ExitCase.objects.filter(completed_at__isnull=True).count(), 0)


class RecruitmentTests(Base):

    def setUp(self):
        super().setUp()
        self.job = Vacancy.objects.create(tenant=self.school, title='Physics Teacher', description='Teach SS physics',
                                          closing_date=timezone.localdate() + timedelta(days=10))

    def test_public_apply_duplicate_and_closed(self):
        self.assertContains(self._get('/hr/careers/'), 'Physics Teacher')
        page = self._post(f'/hr/careers/{self.job.pk}/', {'full_name': 'Tolu Ojo', 'email': 'tolu@example.com',
                                                           'phone': '08030000000', 'cv': pdf()})
        self.assertContains(page, 'application has been received')
        again = self._post(f'/hr/careers/{self.job.pk}/', {'full_name': 'Tolu Ojo', 'email': 'TOLU@example.com',
                                                            'phone': '08030000000', 'cv': pdf()})
        self.assertContains(again, 'already applied')
        word = SimpleUploadedFile('cv.png', b'\x89PNG\r\n\x1a\n0000', content_type='image/png')
        with self.assertRaises(svc.HRError):
            svc.apply(self.job, {'full_name': 'A', 'email': 'a@example.com', 'phone': '1'}, word)
        Vacancy.objects.filter(pk=self.job.pk).update(closing_date=timezone.localdate() - timedelta(days=1))
        self.assertNotContains(self._get('/hr/careers/'), 'Physics Teacher')
        self.assertEqual(JobApplication.objects.count(), 1)

    def test_shortlist_interview_email_and_cv_access(self):
        app = svc.apply(self.job, {'full_name': 'Tolu Ojo', 'email': 'tolu@example.com', 'phone': '080'}, pdf())
        self.client.force_login(self.t_user)
        self.assertEqual(self._get(f'/hr/document/cv/{app.pk}/').status_code, 404)
        self.client.force_login(self.admin)
        self.assertEqual(self._get(f'/hr/document/cv/{app.pk}/').status_code, 200)
        self._post(f'/hr/vacancies/{self.job.pk}/', {'id': app.pk, 'action': 'status', 'status': 'SHORTLISTED'})
        app.refresh_from_db()
        self.assertEqual(app.status, 'SHORTLISTED')
        mail.outbox.clear()
        self._post(f'/hr/vacancies/{self.job.pk}/', {'id': app.pk, 'action': 'interview',
                                                     'when': '2026-11-03T10:00', 'venue': 'Admin block'})
        app.refresh_from_db()
        self.assertEqual(app.status, 'INTERVIEW')
        self.assertEqual(mail.outbox[0].to, ['tolu@example.com'])
        self.assertIn('Admin block', mail.outbox[0].body)


class ExtraPayTests(Base):

    def test_extra_lessons_and_cover_go_on_payslip(self):
        pr = StaffPayroll.objects.create(tenant=self.school, staff=self.teacher, basic_salary=D('200000'),
                                         housing=D('50000'), transport=D('30000'), other_allowances=D('20000'))
        g = ExtraLessonGroup.objects.create(tenant=self.school, name='WAEC Physics', teacher=self.teacher, day='TUE',
                                            start_time=time(14, 30), end_time=time(16, 0), teacher_pay=D('5000'))
        ExtraLessonGroup.objects.filter(pk=g.pk).update(
            created_at=timezone.make_aware(datetime(2026, 9, 1, 9, 0)))
        settings_, _ = PayrollSettings.objects.get_or_create(tenant=self.school)
        settings_.cover_rate = D('1500')
        settings_.save()
        cls = make_class(self.school, 'JSS 1A')
        set_current_tenant(self.school)
        maths = Subject.objects.create(tenant=self.school, name='Mathematics')
        p1 = TimeSlot.objects.create(tenant=self.school, name='Period 1', start_time=time(8, 0), end_time=time(8, 40),
                                     order=1)
        cfg = TimetableConfiguration.objects.create(tenant=self.school, name='T', is_active=True,
                                                    active_days=['MON', 'TUE', 'WED', 'THU', 'FRI'])
        tt = GeneratedTimetable.objects.create(tenant=self.school, config=cfg, is_published=True)
        lesson = TimetableSlot.objects.create(tenant=self.school, timetable=tt, school_class=cls, day='MON',
                                              time_slot=p1, subject=maths, teacher=self.hod)
        for d in (date(2026, 10, 5), date(2026, 10, 12)):
            CoverAssignment.objects.create(tenant=self.school, slot=lesson, date=d, absent_teacher=self.hod,
                                           cover_teacher=self.teacher)
        run = runs.create(self.school, 'MONTHLY', 10, 2026, self.admin)
        slip = Payslip.objects.get(run=run, staff_payroll=pr)
        self.assertEqual(slip.extra_lessons, D('20000.00'))  # four Tuesdays in October 2026
        self.assertEqual(slip.cover_pay, D('3000.00'))
        self.assertEqual(slip.gross, D('323000.00'))
