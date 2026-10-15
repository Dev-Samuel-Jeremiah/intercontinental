"""
Parent experience: family hub and satisfaction score (F280), daily digest
(F263), fee statement (F265), teachers (F276) and appointments (F266), PTA
(F267), volunteering (F268), feedback and complaints (F269) and
password-free sign-in (F277).
"""
import re
from datetime import date, time, timedelta
from decimal import Decimal
from unittest import mock

from django.core import mail
from django.test import override_settings
from django.utils import timezone

from communications.models import FamilyPreference
from curriculum.models import Homework
from family import services as svc
from family.models import Appointment, LoginToken, Opportunity, PtaLevy, PtaPayment, SatisfactionResponse, Ticket
from finance.models import FeePayment, FeeStructure
from finance.views import _get_family_wallet_for_student
from tenants.middleware import set_current_tenant
from users.models import Attendance, Staff, StaffSubjectClass, Subject, User

from .factories import TenantTestCase, make_admin, make_class, make_school, make_student, make_teacher

D = Decimal


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
        self.jss1 = make_class(self.school, 'JSS 1A')
        self.jss2 = make_class(self.school, 'JSS 2A')
        self.ada = make_student(self.school, 'A/001', full_name='Ada Obi', school_class=self.jss1,
                                parent_email='mum@example.com')
        self.ben = make_student(self.school, 'A/002', full_name='Ben Obi', school_class=self.jss2,
                                parent_email='mum@example.com')
        self.chi = make_student(self.school, 'A/003', full_name='Chi Eze', school_class=self.jss1,
                                parent_email='eze@example.com')
        set_current_tenant(self.school)
        self.teacher = Staff.objects.get(user=self.t_user)
        self.maths = Subject.objects.create(tenant=self.school, name='Mathematics')
        StaffSubjectClass.objects.create(tenant=self.school, staff=self.teacher, subject=self.maths,
                                         school_class=self.jss1)
        self.parent = User.objects.create_user(username=f'parent_{self.ada.id}', password='x', tenant=self.school,
                                               is_parent=True, email='mum@example.com')
        self.parent2 = User.objects.create_user(username=f'parent_{self.chi.id}', password='x', tenant=self.school,
                                                is_parent=True, email='eze@example.com')

    def _get(self, path, **kw):
        return self.client.get(path, HTTP_HOST=self.host(self.school), **kw)

    def _post(self, path, data=None, **kw):
        return self.client.post(path, data or {}, HTTP_HOST=self.host(self.school), **kw)

    def again(self):
        set_current_tenant(self.school)


class HubAndScoreTests(Base):

    def test_hub_and_one_score_per_family_per_term(self):
        self.client.force_login(self.parent)
        page = self._get('/family/')
        self.assertContains(page, 'Family')
        self.assertContains(page, 'recommend the school')
        self._post('/family/', {'score': '9', 'comment': 'Great teachers'})
        self.again()
        self.assertEqual(SatisfactionResponse.objects.get().score, 9)
        with self.assertRaises(svc.FamilyError):
            svc.record_score(self.school, self.parent, self.ben, 3)     # sibling = same family
        self.assertNotContains(self._get('/family/'), 'recommend the school')
        self.client.force_login(self.t_user)
        self.assertEqual(self._get('/family/').status_code, 302)

    def test_nps_maths_and_periods(self):
        rows = [mock.Mock(score=s) for s in (10, 9, 8, 6, 3)]
        self.assertEqual(svc.nps(rows)['nps'], 0)
        self.assertEqual(svc.nps([mock.Mock(score=10)])['nps'], 100)
        self.assertEqual(svc.period_for(date(2026, 10, 1)), '2026/27 T1')
        self.assertEqual(svc.period_for(date(2027, 2, 1)), '2026/27 T2')
        self.assertEqual(svc.period_for(date(2027, 6, 1)), '2026/27 T3')


class DigestTests(Base):

    def setUp(self):
        super().setUp()
        self.today = timezone.localdate()
        Attendance.objects.create(tenant=self.school, student=self.ada, school_class=self.jss1, date=self.today,
                                  status='A')
        Homework.objects.create(tenant=self.school, school_class=self.jss1, subject=self.maths, title='Fractions ex. 4',
                                set_on=self.today, due_on=self.today + timedelta(days=3))

    def test_digest_content(self):
        d = svc.digest([self.ada, self.ben], self.today)
        self.assertTrue(d['has_news'])
        self.assertEqual(d['items'][0]['attendance'], 'Absent')
        html = svc.digest_html(self.school, d)
        self.assertIn('Fractions ex. 4', html)
        self.client.force_login(self.parent)
        self.assertContains(self._get('/family/today/'), 'Fractions ex. 4')

    def test_sent_once_when_on_and_wanted(self):
        self.assertEqual(svc.send_digests(self.school, self.today), 0)       # switched off
        cfg = svc.settings_for(self.school)
        cfg.digest_enabled = True
        cfg.save()
        FamilyPreference.objects.create(tenant=self.school, key='eze@example.com', topics_off=['DIGEST'])
        mail.outbox.clear()
        self.assertEqual(svc.send_digests(self.school, self.today), 1)
        self.assertEqual([m.to[0] for m in mail.outbox], ['mum@example.com'])
        self.assertEqual(svc.send_digests(self.school, self.today), 0)       # not twice


class StatementTests(Base):

    def test_parent_downloads_their_childs_statement(self):
        fee = FeeStructure.objects.create(tenant=self.school, name='Tuition', amount=D('150000'))
        FeePayment.objects.create(tenant=self.school, student=self.ada, fee_structure=fee, amount_paid=D('100000'),
                                  balance_due=D('50000'), status='PARTIAL')
        st = svc.statement(self.ada)
        self.assertEqual((st['billed'], st['paid'], st['balance']), (D('150000'), D('100000'), D('50000')))
        self.client.force_login(self.parent)
        self.assertContains(self._get(f'/family/statement/{self.ada.pk}/'), 'Tuition')
        pdf = self._get(f'/family/statement/{self.ada.pk}/?pdf=1')
        self.assertTrue(pdf.content.startswith(b'%PDF'))
        self.assertEqual(self._get(f'/family/statement/{self.chi.pk}/').status_code, 404)
        self.client.force_login(self.admin)
        self.assertEqual(self._get(f'/family/statement/{self.chi.pk}/').status_code, 200)


class AppointmentTests(Base):

    def setUp(self):
        super().setUp()
        self.day = timezone.localdate() + timedelta(days=1)
        while self.day.weekday() != 0:
            self.day += timedelta(days=1)
        svc.set_availability(self.teacher, 'MON', time(14, 0), time(15, 0), 15, 'Staff room')

    def test_teachers_page_and_booking(self):
        self.client.force_login(self.parent)
        self.assertContains(self._get('/family/teachers/'), 'Mrs Ade')
        self.assertEqual(len(svc.free_slots(self.teacher, self.day)), 4)
        mail.outbox.clear()
        self._post('/family/book/', {'student': self.ada.pk, 'staff': self.teacher.pk,
                                     'slot': f'{self.day:%Y-%m-%d}|14:15', 'mode': 'IN_PERSON',
                                     'reason': 'Maths progress'})
        self.again()
        appt = Appointment.objects.get()
        self.assertEqual((appt.start, appt.end, appt.note), (time(14, 15), time(14, 30), 'Staff room'))
        self.assertEqual(sorted(m.to[0] for m in mail.outbox), ['ade@example.com', 'mum@example.com'])
        self.assertEqual(len(svc.free_slots(self.teacher, self.day)), 3)
        with self.assertRaises(svc.FamilyError):
            svc.book(self.parent2, self.chi, self.teacher, self.day, time(14, 15), 'PHONE', 'Hi')
        with self.assertRaises(svc.FamilyError):      # Ben is in JSS 2A — not Mrs Ade's class
            svc.book(self.parent, self.ben, self.teacher, self.day, time(14, 30), 'PHONE', 'Hi')
        mail.outbox.clear()
        self._post('/family/book/', {'action': 'cancel', 'id': appt.pk})
        self.again()
        appt.refresh_from_db()
        self.assertEqual(appt.status, 'CANCELLED')
        self.assertEqual(mail.outbox[0].to, ['ade@example.com'])
        self.assertEqual(len(svc.free_slots(self.teacher, self.day)), 4)

    def test_staff_set_their_times(self):
        self.client.force_login(self.t_user)
        self._post('/family/appointments/', {'action': 'add', 'day': 'WED', 'start': '10:00', 'end': '09:00',
                                             'slot_minutes': '15'})
        self.again()
        self.assertEqual(self.teacher.availability.count(), 1)       # refused: ends before it starts
        self._post('/family/appointments/', {'action': 'add', 'day': 'WED', 'start': '10:00', 'end': '11:00',
                                             'slot_minutes': '30'})
        self.again()
        self.assertEqual(self.teacher.availability.count(), 2)


class PtaAndVolunteerTests(Base):

    def test_levy_paid_once_per_family(self):
        levy = PtaLevy.objects.create(tenant=self.school, title='PTA levy', amount=D('5000'))
        wallet = _get_family_wallet_for_student(self.ada)
        wallet.credit(D('8000'), 'Top up')
        self.client.force_login(self.parent)
        self._post('/family/pta/', {'levy': levy.pk})
        self.again()
        wallet.refresh_from_db()
        self.assertEqual(wallet.balance, D('3000'))
        with self.assertRaises(svc.FamilyError):
            svc.pay_levy_from_wallet(levy, self.ben, self.parent)    # same family
        with self.assertRaises(svc.FamilyError):
            svc.pay_levy_from_wallet(levy, self.chi, self.parent2)   # empty wallet
        svc.record_levy(levy, self.chi, 'CASH', self.admin)
        rep = svc.levy_report(levy)
        self.assertEqual((rep['paid'], rep['total'], rep['collected']), (2, 2, D('10000')))
        self.assertEqual(PtaPayment.objects.count(), 2)

    def test_volunteer_places(self):
        opp = Opportunity.objects.create(tenant=self.school, title='Sports marshals',
                                         date=timezone.localdate() + timedelta(days=5), places=1)
        self.client.force_login(self.parent)
        self._post('/family/volunteer/', {'id': opp.pk, 'name': 'Ngozi Obi', 'phone': '0803'})
        self.again()
        self.assertEqual(opp.signups.count(), 1)
        with self.assertRaises(svc.FamilyError):
            svc.volunteer(opp, self.parent2, 'Mr Eze', '0805')
        with self.assertRaises(svc.FamilyError):
            svc.volunteer(opp, self.parent, 'Ngozi Obi', '0803')


class TicketTests(Base):

    def test_complaint_lifecycle(self):
        mail.outbox.clear()
        self.client.force_login(self.parent)
        self._post('/family/feedback/', {'kind': 'COMPLAINT', 'category': 'FOOD', 'student': self.ada.pk,
                                         'subject': 'Cold lunch', 'body': 'Lunch was cold all week.'})
        self.again()
        t = Ticket.objects.get()
        self.assertEqual(t.reference, 'FB-00001')
        self.assertEqual(mail.outbox[0].to, ['head@example.com'])
        mail.outbox.clear()
        self.client.force_login(self.admin)
        self._post(f'/family/tickets/{t.pk}/', {'body': 'We are speaking to the kitchen.'})
        self.again()
        t.refresh_from_db()
        self.assertEqual(t.status, 'PROGRESS')
        self.assertEqual(mail.outbox[0].to, ['mum@example.com'])
        self._post(f'/family/tickets/{t.pk}/', {'body': 'Kitchen staff warned', 'internal': '1'})
        self.client.force_login(self.parent)
        self.assertNotContains(self._get(f'/family/feedback/{t.pk}/'), 'Kitchen staff warned')
        self.again()
        svc.ticket_reply(t, self.admin, 'Fixed — hot food from Monday.', status='RESOLVED')
        t.refresh_from_db()
        self.assertIsNotNone(t.resolved_at)
        svc.ticket_reply(t, self.parent, 'Still cold today.')
        t.refresh_from_db()
        self.assertEqual(t.status, 'OPEN')                      # reopened by the parent
        svc.ticket_reply(t, self.admin, '', status='RESOLVED')
        svc.rate(t, self.parent, 4, 'Better now')
        t.refresh_from_db()
        self.assertEqual((t.status, t.rating), ('CLOSED', 4))
        self.client.force_login(self.parent2)
        self.assertEqual(self._get(f'/family/feedback/{t.pk}/').status_code, 404)

    def test_overdue(self):
        t = svc.open_ticket(self.school, self.parent, None, 'COMPLAINT', 'OTHER', 'Gate', 'Gate was locked')
        Ticket.objects.filter(pk=t.pk).update(created_at=timezone.now() - timedelta(days=6))
        t.refresh_from_db()
        self.assertTrue(svc.overdue(t, 5))
        self.assertFalse(svc.overdue(t, 7))


class MagicLoginTests(Base):

    def test_email_link_signs_in_once(self):
        mail.outbox.clear()
        self._post('/family/login/', {'contact': 'MUM@example.com'})
        self.again()
        html = mail.outbox[0].alternatives[0][0]
        link = re.search(r'href="([^"]+)"', html).group(1)
        path = link[link.index('/family/login/'):]
        self.assertEqual(self._get(path).status_code, 302)
        self.assertEqual(int(self.client.session['_auth_user_id']), self.parent.pk)
        self.client.logout()
        self._get(path)
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_sms_code(self):
        self.chi.parent_phone = '08035550000'
        self.chi.save()
        sent = {}
        with mock.patch('communications.services.send_sms',
                        side_effect=lambda to, text: sent.update(text=text) or (True, 'r')):
            page = self._post('/family/login/', {'contact': '0803 555 0000'})
        self.assertContains(page, 'Enter the code')
        code = sent['text'].split('code: ')[1][:6]
        self._post('/family/login/code/', {'contact': '0803 555 0000', 'code': '000000' if code != '000000' else '111111'})
        self.assertNotIn('_auth_user_id', self.client.session)
        self._post('/family/login/code/', {'contact': '0803 555 0000', 'code': code})
        self.assertEqual(int(self.client.session['_auth_user_id']), self.parent2.pk)

    def test_unknown_contact_and_rate_limit(self):
        page = self._post('/family/login/', {'contact': 'nobody@example.com'})
        self.assertContains(page, 'If <b>nobody@example.com</b> is on file')
        self.again()
        self.assertEqual(LoginToken.objects.count(), 0)
        for _ in range(5):
            svc.request_login(self.school, 'eze@example.com')
        self.assertEqual(LoginToken.objects.count(), svc.MAX_REQUESTS)

    def test_new_parent_account_has_no_password(self):
        User.objects.filter(pk=self.parent2.pk).delete()
        user = svc.parent_user(self.chi)
        self.assertTrue(user.is_parent)
        self.assertFalse(user.has_usable_password())
