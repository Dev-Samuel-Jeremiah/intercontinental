"""
Review and approval before publishing, the fees check, result PINs, telling
families, and verifying a printed report card.
"""
from datetime import timedelta
from decimal import Decimal

from django.core import mail
from django.test import override_settings
from django.utils import timezone

from academics.models import AcademicSession, Term
from curriculum.models import Department
from finance.models import ChildBilling, ParentBilling
from results import release
from results.models import ClassApproval, ResultPin, ResultSheetSettings, SubjectSheet, TermResult
from tenants.middleware import set_current_tenant
from users.models import ClassSubject, Staff, StaffSubjectClass, Subject, User

from .factories import TenantTestCase, make_admin, make_class, make_school, make_student, make_teacher


class Base(TenantTestCase):

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha')
        self.admin = make_admin(self.school)
        self.teacher_user = make_teacher(self.school)
        self.hod_user = make_teacher(self.school, username='alphahod', full_name='Head')
        self.jss1 = make_class(self.school, 'JSS 1A')
        set_current_tenant(self.school)
        self.teacher = Staff.objects.get(user=self.teacher_user)
        self.teacher.can_enter_scores = True
        self.teacher.save()
        self.hod = Staff.objects.get(user=self.hod_user)
        today = timezone.localdate()
        self.session = AcademicSession.objects.create(tenant=self.school, name='2026/2027',
                                                      start_date=today - timedelta(days=60), is_active=True)
        self.term = Term.objects.create(tenant=self.school, name='1st Term', session=self.session,
                                        start_date=today - timedelta(days=30), end_date=today + timedelta(days=60),
                                        is_active=True)
        self.maths = Subject.objects.create(tenant=self.school, name='Mathematics')
        self.english = Subject.objects.create(tenant=self.school, name='English')
        for s in (self.maths, self.english):
            ClassSubject.objects.create(tenant=self.school, school_class=self.jss1, subject=s)
        StaffSubjectClass.objects.create(tenant=self.school, staff=self.teacher, subject=self.maths, school_class=self.jss1)
        dept = Department.objects.create(tenant=self.school, name='Sciences', hod=self.hod)
        dept.subjects.add(self.maths)
        self.ada = make_student(self.school, 'S1', full_name='Ada Bello', school_class=self.jss1,
                                parent_email='mum@example.com')
        set_current_tenant(self.school)
        self.ada.parent_phone = '08031234567'
        self.ada.save()
        self.cfg, _ = ResultSheetSettings.objects.get_or_create(tenant=self.school)

    def _get(self, path, **kw):
        return self.client.get(path, HTTP_HOST=self.host(self.school), **kw)

    def _post(self, path, data=None, **kw):
        return self.client.post(path, data or {}, HTTP_HOST=self.host(self.school), **kw)

    def _score(self, subject, total=70, **kw):
        return TermResult.objects.create(tenant=self.school, student=self.ada, class_assigned=self.jss1,
                                         subject=subject, session=self.session, term=self.term,
                                         ca1_score=min(total, 30), exam_score=max(0, total - 30), **kw)

    def _parent(self):
        return User.objects.create_user(username=f'parent_{self.ada.id}', password='x', tenant=self.school,
                                        is_parent=True, email='mum@example.com')

    def _report_url(self):
        return f'/results/results/{self.ada.id}/{self.session.id}/{self.term.id}/'


class ApprovalTests(Base):

    def setUp(self):
        super().setUp()
        self.cfg.require_approval = True
        self.cfg.save()

    def test_sheet_flow(self):
        sheet = release.sheet_for(self.jss1, self.maths, self.term)
        with self.assertRaises(release.ReleaseError):
            release.submit_sheet(sheet, self.teacher_user)              # nothing entered
        self._score(self.maths, 0)
        with self.assertRaises(release.ReleaseError):
            release.submit_sheet(sheet, self.teacher_user)              # a blank score
        TermResult.objects.update(ca1_score=20, exam_score=40)
        for tr in TermResult.objects.all():
            tr.save()
        release.submit_sheet(sheet, self.teacher_user)
        self.assertEqual(release.may_edit_scores(self.teacher_user, self.jss1, self.maths, self.term)[0], False)
        self.assertEqual(release.may_edit_scores(self.hod_user, self.jss1, self.maths, self.term)[0], True)
        with self.assertRaises(release.ReleaseError):
            release.review_sheet(sheet, self.teacher_user, True)          # not the HOD
        with self.assertRaises(release.ReleaseError):
            release.review_sheet(sheet, self.hod_user, False, '')         # sending back needs a reason
        release.review_sheet(sheet, self.hod_user, False, 'Check Ada')
        self.assertEqual(release.may_edit_scores(self.teacher_user, self.jss1, self.maths, self.term)[0], True)
        release.submit_sheet(sheet, self.teacher_user)
        release.review_sheet(sheet, self.hod_user, True)
        self.assertEqual(sheet.status, SubjectSheet.APPROVED)
        self.assertFalse(release.may_edit_scores(self.hod_user, self.jss1, self.maths, self.term)[0])

    def test_score_entry_is_blocked_while_with_the_reviewer(self):
        tr = self._score(self.maths, 60)
        sheet = release.submit_sheet(release.sheet_for(self.jss1, self.maths, self.term), self.teacher_user)
        self.client.force_login(self.teacher_user)
        url = f'/results/manual-scores/?class={self.jss1.id}&subject={self.maths.id}&term={self.term.id}'
        self.assertContains(self._get(url), "Waiting for the head of department")
        self._post('/results/manual-scores/', {'class': self.jss1.id, 'subject': self.maths.id, 'term': self.term.id,
                                               f'ca1_{tr.id}': 1, f'exam_{tr.id}': 1})
        tr.refresh_from_db()
        self.assertEqual(tr.total_score, 60)

    def test_class_approval_gates_publishing(self):
        self._score(self.maths, 70)
        self._score(self.english, 60)
        self.client.force_login(self.admin)
        publish = {'action': 'publish', 'student_ids': [self.ada.id], 'post_session': self.session.id,
                   'post_term': self.term.id}
        self._post('/results/publish-results/', publish)
        self.assertFalse(TermResult.objects.filter(published=True).exists())
        with self.assertRaises(release.ReleaseError):
            release.approve_class(self.jss1, self.term, self.admin)          # sheets not reviewed
        with self.assertRaises(release.ReleaseError):
            release.approve_class(self.jss1, self.term, self.hod_user, 'x')  # not the principal
        release.approve_class(self.jss1, self.term, self.admin, 'Exam board met early')
        self._post('/results/publish-results/', publish)
        self.assertEqual(TermResult.objects.filter(published=True).count(), 2)
        with self.assertRaises(release.ReleaseError):
            release.review_sheet(release.sheet_for(self.jss1, self.maths, self.term), self.hod_user, False, 'late')

    def test_pages(self):
        self._score(self.maths, 70)
        release.submit_sheet(release.sheet_for(self.jss1, self.maths, self.term), self.teacher_user)
        self.client.force_login(self.hod_user)
        r = self._get(f'/results/approvals/?term={self.term.id}')
        self.assertContains(r, 'Mathematics')
        self.assertNotContains(r, 'English')          # not their department
        self._post('/results/sheet/', {'class': self.jss1.id, 'subject': self.maths.id, 'term': self.term.id,
                                       'action': 'approve'})
        self.assertEqual(SubjectSheet.objects.get().status, 'APPROVED')
        self.client.force_login(self.teacher_user)
        self.assertEqual(self._get('/results/approvals/').status_code, 302)


class FeeGateTests(Base):

    def _bill(self, due, paid):
        from finance.models import Wallet
        wallet, _ = Wallet.objects.get_or_create(tenant=self.school, student=self.ada)
        pb = ParentBilling.objects.create(tenant=self.school, wallet=wallet, session=self.session,
                                          term=self.term, total_expected=due, total_paid=paid)
        return ChildBilling.objects.create(tenant=self.school, student=self.ada, parent_billing=pb,
                                           total_fee=due, paid=paid)

    def test_threshold(self):
        self.cfg.fee_gate, self.cfg.fee_gate_percent = True, 70
        self.cfg.save()
        self.assertTrue(release.fees_cleared(self.ada, self.term))         # no bill → never held back
        bill = self._bill(Decimal('100000'), Decimal('50000'))
        self.assertEqual(release.fee_status(self.ada, self.term)[2], 50)
        self.assertFalse(release.fees_cleared(self.ada, self.term))
        bill.paid = Decimal('70000')
        bill.save()
        self.assertTrue(release.fees_cleared(self.ada, self.term))

    def test_parent_sees_what_is_owed_instead_of_the_result(self):
        self.cfg.fee_gate = True
        self.cfg.save()
        self._bill(Decimal('100000'), Decimal('40000'))
        self._score(self.maths, 70, published=True)
        self.client.force_login(self._parent())
        r = self._get(self._report_url())
        self.assertContains(r, 'Fees to clear first')
        self.assertContains(r, '60000.00')
        self.assertNotContains(r, 'Mathematics')
        self.client.force_login(self.admin)                                # staff are never held back
        self.assertContains(self._get(self._report_url()), 'Mathematics')


class PinTests(Base):

    def setUp(self):
        super().setUp()
        self.cfg.require_pin = True
        self.cfg.save()
        self._score(self.maths, 70, published=True)
        self.batch = release.generate_pins(self.school, 'First term', 3, 500, max_uses=2, user=self.admin)

    def test_generation(self):
        pins = list(self.batch.pins.all())
        self.assertEqual(len(pins), 3)
        self.assertEqual(len({p.pin for p in pins}), 3)
        self.assertTrue(all(len(p.pin) == 12 and p.pin.isdigit() for p in pins))
        with self.assertRaises(release.ReleaseError):
            release.generate_pins(self.school, 'x', 0)

    def test_pin_opens_the_report_and_is_tied_to_one_child(self):
        pin = self.batch.pins.first()
        self.client.force_login(self._parent())
        self.assertContains(self._get(self._report_url()), 'Enter your result PIN')
        self._post(self._report_url(), {'pin': '0000 0000 0000'})
        self.assertContains(self._get(self._report_url()), 'Enter your result PIN')
        self._post(self._report_url(), {'pin': f'{pin.pin[:4]} {pin.pin[4:8]} {pin.pin[8:]}'})
        self.assertContains(self._get(self._report_url()), 'Mathematics')
        self.assertContains(self._get(self._report_url()), 'Mathematics')     # same visit: no extra use
        pin.refresh_from_db()
        self.assertEqual((pin.student, pin.term, pin.uses), (self.ada, self.term, 1))
        other = make_student(self.school, 'S2', full_name='Bola')
        set_current_tenant(self.school)
        from django.test import RequestFactory
        req = RequestFactory().post('/')
        req.session = {}
        with self.assertRaises(release.ReleaseError):
            release.redeem(req, pin.pin, other, self.term)

    def test_uses_run_out(self):
        pin = self.batch.pins.first()
        from django.test import RequestFactory
        for _ in range(2):
            req = RequestFactory().post('/')
            req.session = {}
            release.redeem(req, pin.pin, self.ada, self.term)
        req.session = {}
        with self.assertRaises(release.ReleaseError):
            release.redeem(req, pin.pin, self.ada, self.term)

    def test_admin_pages(self):
        self.client.force_login(self.admin)
        self.assertContains(self._get('/results/release/'), 'First term')
        r = self._get(f'/results/pins/{self.batch.id}/?format=csv')
        self.assertEqual(r.content.decode().count('\n'), 4)
        self.assertContains(self._get(f'/results/pins/{self.batch.id}/?unused=1'), 'Result checker card')
        self._post('/results/release/', {'action': 'generate', 'name': 'Second', 'count': 5, 'max_uses': 3})
        self.assertEqual(ResultPin.objects.count(), 8)
        self.client.force_login(self.teacher_user)
        self.assertEqual(self._get('/results/release/').status_code, 302)


class NotifyAndVerifyTests(Base):

    @override_settings(RUN_TASKS_EAGERLY=True)
    def test_families_are_told_once_when_published(self):
        self._score(self.maths, 70)
        self.client.force_login(self.admin)
        publish = {'action': 'publish', 'student_ids': [self.ada.id], 'post_session': self.session.id,
                   'post_term': self.term.id}
        self._post('/results/publish-results/', publish)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('result is ready', mail.outbox[0].subject)
        self._post('/results/publish-results/', publish)                       # already published
        self.assertEqual(len(mail.outbox), 1)

    def test_verification(self):
        self._score(self.maths, 72, published=True)
        ref = release.reference(self.ada, self.term)
        data = release.verify(ref, self.school)
        self.assertEqual((data['student'], data['average']), (self.ada, 72))
        self.assertIsNone(release.verify(ref + 'x', self.school))
        self.assertIsNone(release.verify(ref, make_school('beta')))
        set_current_tenant(self.school)
        r = self._get(f'/results/verify/{ref}/')                               # public, no login
        self.assertContains(r, 'Report card')
        self.assertContains(r, 'verified')
        self.assertEqual(self._get(f'/results/verify/{ref}/qr.png')['Content-Type'], 'image/png')
        TermResult.objects.update(published=False)
        self.assertEqual(self._get(f'/results/verify/{ref}/').status_code, 404)

    def test_report_card_carries_the_code(self):
        self._score(self.maths, 72, published=True)
        self.client.force_login(self.ada.user)
        self.assertContains(self._get(f'/results/results/{self.session.id}/{self.term.id}/'),
                            'data:image/png;base64,')
