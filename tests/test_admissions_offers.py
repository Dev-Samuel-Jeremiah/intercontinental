"""
Admissions, part two: application fees, offer letters, entrance assessment
scheduling, ranking and the waiting list.

Paystack is mocked — no test ever calls the real gateway.
"""
from datetime import date, timedelta
from unittest.mock import patch

from django.core import mail
from django.test import override_settings
from django.utils import timezone

from admissions.models import AdmissionQuota, Application
from admissions.pdf import build_offer_letter
from admissions.services import (rank_applicants, schedule_exam, start_fee_payment,
                                 verify_fee_payment)
from tenants.middleware import set_current_tenant

from .factories import TenantTestCase, make_admin, make_class, make_school


def _paystack(ok=True, url='https://checkout.paystack.com/abc123', message='Declined'):
    class FakeResponse:
        def json(self):
            if ok:
                return {'status': True, 'data': {'authorization_url': url,
                                                 'reference': 'PSK-REF-1', 'status': 'success'}}
            return {'status': False, 'message': message}
    return FakeResponse()


@override_settings(ROOT_DOMAIN='localhost', ALLOWED_HOSTS=['*'],
                   PAYSTACK_SECRET_KEY='sk_test_dummy')
class ApplicationFeeTests(TenantTestCase):

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha')
        set_current_tenant(self.school)
        self.application = Application.objects.create(
            tenant=self.school, reference='APP-2026-AAAAA', full_name='Amara Nwosu',
            parent_name='Ngozi', parent_phone='08031234567',
            parent_email='ngozi@example.com', fee_amount=2000)

    @patch('requests.post', return_value=_paystack())
    def test_paying_sends_the_parent_to_the_gateway(self, mocked):
        response = self.client.get(f'/apply/{self.application.reference}/fee/',
                                   HTTP_HOST=self.host(self.school))
        self.assertEqual(response.status_code, 302)
        self.assertIn('checkout.paystack.com', response['Location'])
        self.application.refresh_from_db()
        self.assertEqual(self.application.fee_reference, 'PSK-REF-1')

    @patch('requests.post', return_value=_paystack(ok=False, message='Card declined'))
    def test_a_gateway_refusal_is_explained_without_losing_the_application(self, mocked):
        response = self.client.get(f'/apply/{self.application.reference}/fee/',
                                   HTTP_HOST=self.host(self.school))
        self.assertContains(response, 'Card declined')
        self.assertContains(response, self.application.reference)

    @patch('requests.get', return_value=_paystack())
    def test_a_successful_payment_is_recorded(self, mocked):
        response = self.client.get(f'/apply/{self.application.reference}/fee/callback/',
                                   {'reference': 'PSK-REF-1'}, HTTP_HOST=self.host(self.school))
        self.assertEqual(response.status_code, 302)
        self.application.refresh_from_db()
        self.assertTrue(self.application.fee_paid)
        self.assertIsNotNone(self.application.fee_paid_at)
        self.assertTrue(self.application.events.filter(note__icontains='fee paid').exists())

    @override_settings(PAYSTACK_SECRET_KEY='')
    def test_without_paystack_the_parent_is_told_to_pay_at_the_school(self):
        url, error = start_fee_payment(self.application, 'http://x/callback/')
        self.assertIsNone(url)
        self.assertIn('not set up', error)

    def test_a_free_application_has_nothing_to_pay(self):
        self.application.fee_amount = 0
        self.application.save()
        url, error = start_fee_payment(self.application, 'http://x/callback/')
        self.assertIsNone(url)
        self.assertIn('No application fee', error)

    @patch('requests.get', side_effect=OSError('network down'))
    def test_a_gateway_outage_does_not_mark_the_fee_paid(self, mocked):
        paid, error = verify_fee_payment(self.application, 'PSK-REF-1')
        self.assertFalse(paid)
        self.application.refresh_from_db()
        self.assertFalse(self.application.fee_paid)

    def test_staff_can_record_a_cash_payment(self):
        admin = make_admin(self.school)
        self.client.force_login(admin)
        self.client.post(f'/admissions/{self.application.pk}/',
                         {'action': 'mark_fee_paid', 'fee_reference': 'Teller 8842'},
                         HTTP_HOST=self.host(self.school))
        self.application.refresh_from_db()
        self.assertTrue(self.application.fee_paid)
        self.assertEqual(self.application.fee_reference, 'Teller 8842')


@override_settings(ROOT_DOMAIN='localhost', ALLOWED_HOSTS=['*'])
class OfferLetterTests(TenantTestCase):

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha', name='Royal Kidies School')
        self.jss1 = make_class(self.school, 'JSS 1')
        self.admin = make_admin(self.school)
        set_current_tenant(self.school)
        self.application = Application.objects.create(
            tenant=self.school, reference='APP-2026-AAAAA', full_name='Amara Nwosu',
            applying_for=self.jss1, parent_name='Ngozi Nwosu', parent_phone='08031234567',
            parent_email='ngozi@example.com', fee_amount=2000,
            offer_expires_on=date.today() + timedelta(days=14))
        self.client.force_login(self.admin)

    def test_the_letter_is_a_pdf(self):
        pdf = build_offer_letter(self.application).read()
        self.assertTrue(pdf.startswith(b'%PDF'))
        self.assertGreater(len(pdf), 1000)

    def test_downloading_the_letter_works(self):
        response = self.client.get(f'/admissions/{self.application.pk}/offer-letter/',
                                   HTTP_HOST=self.host(self.school))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/pdf')
        self.assertIn('offer-APP-2026-AAAAA', response['Content-Disposition'])

    @override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
                       RUN_TASKS_EAGERLY=True)
    def test_emailing_the_letter_attaches_the_pdf_and_moves_the_stage(self):
        self.client.get(f'/admissions/{self.application.pk}/offer-letter/email/',
                        HTTP_HOST=self.host(self.school))
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertIn('Royal Kidies School', message.subject)
        self.assertEqual(message.to, ['ngozi@example.com'])
        self.assertTrue(message.attachments)
        self.assertTrue(message.attachments[0][1].startswith(b'%PDF'))
        self.application.refresh_from_db()
        self.assertEqual(self.application.status, Application.OFFERED)
        self.assertIsNotNone(self.application.offer_sent_at)

    def test_emailing_without_an_address_says_so(self):
        self.application.parent_email = ''
        self.application.save()
        response = self.client.get(f'/admissions/{self.application.pk}/offer-letter/email/',
                                   HTTP_HOST=self.host(self.school), follow=True)
        self.assertContains(response, 'no email address')

    def test_another_school_cannot_download_the_letter(self):
        other = make_school('beta')
        intruder = make_admin(other, username='betaadmin')
        self.client.force_login(intruder)
        response = self.client.get(f'/admissions/{self.application.pk}/offer-letter/',
                                   HTTP_HOST=self.host(other))
        self.assertEqual(response.status_code, 404)


@override_settings(ROOT_DOMAIN='localhost', ALLOWED_HOSTS=['*'],
                   EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
                   RUN_TASKS_EAGERLY=True)
class ExamSchedulingTests(TenantTestCase):

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha')
        self.admin = make_admin(self.school)
        set_current_tenant(self.school)
        self.application = Application.objects.create(
            tenant=self.school, reference='APP-2026-AAAAA', full_name='Amara Nwosu',
            parent_name='Ngozi', parent_phone='08031234567',
            parent_email='ngozi@example.com')

    def test_scheduling_sets_the_date_and_moves_the_stage(self):
        when = timezone.now() + timedelta(days=7)
        schedule_exam(self.application, when, venue='Main hall', actor=self.admin, notify=False)
        self.application.refresh_from_db()
        self.assertEqual(self.application.status, Application.EXAM)
        self.assertEqual(self.application.exam_venue, 'Main hall')
        self.assertTrue(self.application.events.filter(note__icontains='scheduled').exists())

    def test_the_parent_is_told_about_the_assessment(self):
        schedule_exam(self.application, timezone.now() + timedelta(days=7),
                      venue='Main hall', actor=self.admin, notify=True)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('Amara Nwosu', mail.outbox[0].body)
        self.application.refresh_from_db()
        self.assertIsNotNone(self.application.exam_notified_at)

    def test_scheduling_through_the_page(self):
        self.client.force_login(self.admin)
        when = (timezone.now() + timedelta(days=5)).strftime('%Y-%m-%dT10:00')
        self.client.post(f'/admissions/{self.application.pk}/',
                         {'action': 'schedule_exam', 'exam_date': when,
                          'exam_venue': 'Hall B'},
                         HTTP_HOST=self.host(self.school))
        self.application.refresh_from_db()
        self.assertIsNotNone(self.application.exam_date)
        self.assertEqual(self.application.exam_venue, 'Hall B')

    def test_a_missing_date_is_rejected_politely(self):
        self.client.force_login(self.admin)
        response = self.client.post(f'/admissions/{self.application.pk}/',
                                    {'action': 'schedule_exam', 'exam_date': ''},
                                    HTTP_HOST=self.host(self.school), follow=True)
        self.assertContains(response, 'Pick a date')
        self.application.refresh_from_db()
        self.assertIsNone(self.application.exam_date)


@override_settings(ROOT_DOMAIN='localhost', ALLOWED_HOSTS=['*'])
class RankingAndWaitlistTests(TenantTestCase):

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha')
        self.jss1 = make_class(self.school, 'JSS 1')
        self.admin = make_admin(self.school)
        set_current_tenant(self.school)
        self.weak = Application.objects.create(
            tenant=self.school, reference='APP-1', full_name='Weak Applicant',
            applying_for=self.jss1, parent_name='P', parent_phone='08030000001',
            status=Application.INTERVIEW, exam_score=40, interview_score=10)
        self.strong = Application.objects.create(
            tenant=self.school, reference='APP-2', full_name='Strong Applicant',
            applying_for=self.jss1, parent_name='P', parent_phone='08030000002',
            status=Application.INTERVIEW, exam_score=90, interview_score=20)
        self.unscored = Application.objects.create(
            tenant=self.school, reference='APP-3', full_name='Unscored Applicant',
            applying_for=self.jss1, parent_name='P', parent_phone='08030000003',
            status=Application.SCREENING)
        self.client.force_login(self.admin)

    def test_the_best_score_ranks_first_and_unscored_come_last(self):
        ranked = rank_applicants(self.school, school_class=self.jss1)
        self.assertEqual([a.full_name for a in ranked],
                         ['Strong Applicant', 'Weak Applicant', 'Unscored Applicant'])
        self.assertEqual(ranked[0].rank, 1)

    def test_the_ranking_page_lists_applicants_in_order(self):
        response = self.client.get('/admissions/ranking/', HTTP_HOST=self.host(self.school))
        content = response.content.decode()
        self.assertLess(content.index('Strong Applicant'), content.index('Weak Applicant'))

    def test_the_waitlist_only_shows_waiting_applicants(self):
        self.weak.status = Application.WAITLIST
        self.weak.save()
        response = self.client.get('/admissions/waitlist/', HTTP_HOST=self.host(self.school))
        self.assertContains(response, 'Weak Applicant')
        self.assertNotContains(response, 'Strong Applicant')

    def test_the_waitlist_shows_where_seats_have_opened(self):
        AdmissionQuota.objects.create(tenant=self.school, school_class=self.jss1, seats=5)
        response = self.client.get('/admissions/waitlist/', HTTP_HOST=self.host(self.school))
        self.assertContains(response, 'seat')

    def test_offering_a_place_from_the_waitlist(self):
        self.weak.status = Application.WAITLIST
        self.weak.save()
        self.client.post(f'/admissions/{self.weak.pk}/',
                         {'action': 'change_status', 'status': 'OFFERED',
                          'note': 'Offered a place from the waiting list'},
                         HTTP_HOST=self.host(self.school))
        self.weak.refresh_from_db()
        self.assertEqual(self.weak.status, Application.OFFERED)

    def test_ranking_never_mixes_schools(self):
        other = make_school('beta')
        set_current_tenant(other)
        Application.objects.create(tenant=other, reference='APP-B', full_name='Beta Applicant',
                                   parent_name='P', parent_phone='08030000009',
                                   status=Application.INTERVIEW, exam_score=99)
        set_current_tenant(self.school)
        names = [a.full_name for a in rank_applicants(self.school)]
        self.assertNotIn('Beta Applicant', names)
