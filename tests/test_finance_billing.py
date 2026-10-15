"""
Fees: sibling rebates, staff discounts, scholarships, payment plans, debtor
follow-up and receipt verification.
"""
from datetime import date, timedelta
from decimal import Decimal

from django.test import override_settings
from django.utils import timezone

from academics.models import AcademicSession, Term
from finance.models import (ChildBilling, DebtorContact, FeeDiscount, FeeStructure,
                            Payment, PaymentPlan, Wallet)
from finance.services.discounts import apply_discounts, sibling_position
from finance.services.payment_service import ensure_child_billing
from finance.services.plans import create_plan, due_soon, record_instalment_payment
from tenants.middleware import set_current_tenant
from users.models import Staff, Student, User

from .factories import TenantTestCase, make_admin, make_class, make_school, make_student


def _term_and_session(school):
    session = AcademicSession.objects.create(tenant=school, name='2026/2027',
                                             start_date=date(2026, 9, 1),
                                             end_date=date(2027, 7, 31))
    term = Term.objects.create(tenant=school, name='1st Term', session=session,
                               start_date=date(2026, 9, 1), end_date=date(2026, 12, 20))
    return session, term


class DiscountTests(TenantTestCase):

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha')
        self.jss1 = make_class(self.school, 'JSS 1')
        set_current_tenant(self.school)
        self.session, self.term = _term_and_session(self.school)
        self.student = make_student(self.school, 'STU001', school_class=self.jss1,
                                    parent_email='family@example.com')
        self.fees = [
            FeeStructure.objects.create(tenant=self.school, name='Tuition', amount=100000,
                                        category='FEE', school_class=self.jss1),
            FeeStructure.objects.create(tenant=self.school, name='Bus', amount=20000,
                                        category='OTHER', school_class=self.jss1),
        ]

    def test_without_discounts_the_family_pays_the_full_fee(self):
        gross, off, net, lines = apply_discounts(self.student, self.fees)
        self.assertEqual(gross, Decimal('120000'))
        self.assertEqual(off, Decimal('0.00'))
        self.assertEqual(net, Decimal('120000'))
        self.assertEqual(lines, [])

    def test_a_percentage_scholarship_comes_off(self):
        FeeDiscount.objects.create(tenant=self.school, name='Merit scholarship',
                                   applies_to=FeeDiscount.STUDENT, student=self.student,
                                   value_type=FeeDiscount.PERCENT, value=25)
        gross, off, net, _ = apply_discounts(self.student, self.fees)
        self.assertEqual(off, Decimal('30000.00'))
        self.assertEqual(net, Decimal('90000.00'))

    def test_a_fixed_amount_bursary_comes_off(self):
        FeeDiscount.objects.create(tenant=self.school, name='Bursary',
                                   applies_to=FeeDiscount.STUDENT, student=self.student,
                                   value_type=FeeDiscount.AMOUNT, value=15000)
        _, off, net, _ = apply_discounts(self.student, self.fees)
        self.assertEqual(off, Decimal('15000.00'))
        self.assertEqual(net, Decimal('105000.00'))

    def test_a_discount_can_be_limited_to_one_fee_category(self):
        FeeDiscount.objects.create(tenant=self.school, name='Free bus',
                                   applies_to=FeeDiscount.STUDENT, student=self.student,
                                   value_type=FeeDiscount.PERCENT, value=100,
                                   category='OTHER')
        _, off, net, _ = apply_discounts(self.student, self.fees)
        self.assertEqual(off, Decimal('20000.00'))   # the bus fee only
        self.assertEqual(net, Decimal('100000.00'))

    def test_the_bill_never_goes_below_zero(self):
        FeeDiscount.objects.create(tenant=self.school, name='Full waiver',
                                   applies_to=FeeDiscount.STUDENT, student=self.student,
                                   value_type=FeeDiscount.AMOUNT, value=500000)
        _, off, net, _ = apply_discounts(self.student, self.fees)
        self.assertEqual(off, Decimal('120000'))
        self.assertEqual(net, Decimal('0'))

    def test_a_paused_discount_is_ignored(self):
        FeeDiscount.objects.create(tenant=self.school, name='Paused award',
                                   applies_to=FeeDiscount.STUDENT, student=self.student,
                                   value=50, is_active=False)
        _, off, _, _ = apply_discounts(self.student, self.fees)
        self.assertEqual(off, Decimal('0.00'))

    def test_a_class_discount_applies_to_that_class_only(self):
        other_class = make_class(self.school, 'JSS 2')
        FeeDiscount.objects.create(tenant=self.school, name='JSS 2 promo',
                                   applies_to=FeeDiscount.CLASS, school_class=other_class,
                                   value=10)
        _, off, _, _ = apply_discounts(self.student, self.fees)
        self.assertEqual(off, Decimal('0.00'))

    def test_sibling_position_counts_children_of_the_same_guardian(self):
        second = make_student(self.school, 'STU002', parent_email='family@example.com')
        third = make_student(self.school, 'STU003', parent_email='family@example.com')
        self.assertEqual(sibling_position(self.student), 1)
        self.assertEqual(sibling_position(second), 2)
        self.assertEqual(sibling_position(third), 3)

    def test_the_sibling_rebate_only_applies_from_the_nth_child(self):
        make_student(self.school, 'STU002', parent_email='family@example.com')
        third = make_student(self.school, 'STU003', school_class=self.jss1,
                             parent_email='family@example.com')
        FeeDiscount.objects.create(tenant=self.school, name='Third child rebate',
                                   applies_to=FeeDiscount.SIBLING, sibling_from=3, value=10)
        _, off_first, _, _ = apply_discounts(self.student, self.fees)
        _, off_third, _, _ = apply_discounts(third, self.fees)
        self.assertEqual(off_first, Decimal('0.00'))
        self.assertEqual(off_third, Decimal('12000.00'))

    def test_staff_children_get_their_discount(self):
        user = User.objects.create_user(username='teacher1', password='x', tenant=self.school,
                                        email='family@example.com', is_staff_user=True)
        Staff.objects.create(tenant=self.school, user=user, full_name='A Teacher')
        FeeDiscount.objects.create(tenant=self.school, name='Staff children',
                                   applies_to=FeeDiscount.STAFF_CHILD, value=50)
        _, off, _, _ = apply_discounts(self.student, self.fees)
        self.assertEqual(off, Decimal('60000.00'))

    def test_discounts_never_cross_schools(self):
        other = make_school('beta')
        set_current_tenant(other)
        other_student = make_student(other, 'B001')
        FeeDiscount.objects.create(tenant=self.school, name='Alpha only',
                                   applies_to=FeeDiscount.ALL, value=50)
        _, off, _, _ = apply_discounts(other_student, self.fees)
        self.assertEqual(off, Decimal('0.00'))

    def test_the_term_bill_records_the_discount(self):
        FeeDiscount.objects.create(tenant=self.school, name='Merit scholarship',
                                   applies_to=FeeDiscount.STUDENT, student=self.student,
                                   value=25)
        ensure_child_billing(self.student, self.term, self.session)
        billing = ChildBilling.objects.get(student=self.student)
        self.assertEqual(billing.gross_fee, Decimal('120000.00'))
        self.assertEqual(billing.discount, Decimal('30000.00'))
        self.assertEqual(billing.total_fee, Decimal('90000.00'))
        self.assertIn('Merit scholarship', billing.discount_note)


@override_settings(ROOT_DOMAIN='localhost', ALLOWED_HOSTS=['*'])
class DiscountPageTests(TenantTestCase):

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha')
        self.admin = make_admin(self.school)
        set_current_tenant(self.school)
        self.student = make_student(self.school, 'STU001')
        self.client.force_login(self.admin)

    def test_awarding_a_scholarship_through_the_page(self):
        self.client.post('/finance/admin/discounts/',
                         {'action': 'add', 'name': 'Merit award',
                          'applies_to': 'STUDENT', 'student_id': self.student.id,
                          'value_type': 'PERCENT', 'value': '30'},
                         HTTP_HOST=self.host(self.school))
        discount = FeeDiscount.objects.get()
        self.assertEqual(discount.student, self.student)
        self.assertEqual(discount.value, Decimal('30'))
        self.assertEqual(discount.approved_by, self.admin)

    def test_a_scholarship_without_a_student_is_refused(self):
        response = self.client.post('/finance/admin/discounts/',
                                    {'action': 'add', 'name': 'Oops', 'applies_to': 'STUDENT',
                                     'value': '30'},
                                    HTTP_HOST=self.host(self.school), follow=True)
        self.assertContains(response, 'Choose the student')
        self.assertFalse(FeeDiscount.objects.exists())

    def test_pausing_a_discount(self):
        discount = FeeDiscount.objects.create(tenant=self.school, name='Rule',
                                              applies_to=FeeDiscount.ALL, value=10)
        self.client.post('/finance/admin/discounts/',
                         {'action': 'toggle', 'discount_id': discount.id},
                         HTTP_HOST=self.host(self.school))
        discount.refresh_from_db()
        self.assertFalse(discount.is_active)

    def test_the_preview_page_explains_the_calculation(self):
        FeeDiscount.objects.create(tenant=self.school, name='Everyone', value=10,
                                   applies_to=FeeDiscount.ALL)
        response = self.client.get('/finance/admin/discounts/preview/',
                                   {'student_id': self.student.id},
                                   HTTP_HOST=self.host(self.school))
        self.assertEqual(response.status_code, 200)


class PaymentPlanTests(TenantTestCase):

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha')
        set_current_tenant(self.school)
        self.student = make_student(self.school, 'STU001')

    def test_a_plan_splits_the_amount_exactly(self):
        plan = create_plan(self.student, Decimal('100000'), 3, date(2026, 10, 1))
        amounts = [i.amount for i in plan.installments.all()]
        self.assertEqual(len(amounts), 3)
        self.assertEqual(sum(amounts), Decimal('100000'))

    def test_instalment_dates_step_forward(self):
        plan = create_plan(self.student, 90000, 3, date(2026, 10, 1), every_days=30)
        dates = [i.due_date for i in plan.installments.all()]
        self.assertEqual(dates, [date(2026, 10, 1), date(2026, 10, 31), date(2026, 11, 30)])

    def test_money_is_applied_to_the_oldest_instalment_first(self):
        plan = create_plan(self.student, 90000, 3, date(2026, 10, 1))
        record_instalment_payment(plan, Decimal('30000'))
        first, second, _ = plan.installments.all()
        self.assertTrue(first.is_paid)
        self.assertFalse(second.is_paid)
        self.assertEqual(plan.balance, Decimal('60000'))

    def test_a_part_payment_is_recorded_against_the_instalment(self):
        plan = create_plan(self.student, 90000, 3, date(2026, 10, 1))
        record_instalment_payment(plan, Decimal('10000'))
        first = plan.installments.first()
        self.assertEqual(first.amount_paid, Decimal('10000'))
        self.assertFalse(first.is_paid)

    def test_a_plan_completes_when_fully_paid(self):
        plan = create_plan(self.student, 60000, 2, date(2026, 10, 1))
        record_instalment_payment(plan, Decimal('60000'))
        plan.refresh_from_db()
        self.assertEqual(plan.status, PaymentPlan.COMPLETED)
        self.assertEqual(plan.balance, Decimal('0'))

    def test_overdue_instalments_are_flagged(self):
        plan = create_plan(self.student, 60000, 2, timezone.now().date() - timedelta(days=10))
        self.assertTrue(plan.installments.first().is_overdue)

    def test_reminders_cover_overdue_and_upcoming_instalments(self):
        create_plan(self.student, 60000, 2, timezone.now().date() - timedelta(days=5))
        create_plan(make_student(self.school, 'STU002'), 40000, 2,
                    timezone.now().date() + timedelta(days=2))
        self.assertEqual(len(due_soon(self.school, within_days=3)), 2)

    @override_settings(RUN_TASKS_EAGERLY=True,
                       EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
    def test_sending_reminders_emails_the_parents(self):
        from django.core import mail

        from finance.services.plans import send_instalment_reminders
        create_plan(self.student, 60000, 2, timezone.now().date())
        sent = send_instalment_reminders(self.school, within_days=3)
        self.assertEqual(sent, 1)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('instalment', mail.outbox[0].subject)


@override_settings(ROOT_DOMAIN='localhost', ALLOWED_HOSTS=['*'])
class DebtorFollowUpTests(TenantTestCase):

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha')
        self.admin = make_admin(self.school)
        set_current_tenant(self.school)
        self.student = make_student(self.school, 'STU001')
        self.client.force_login(self.admin)

    def test_logging_a_call(self):
        self.client.post('/finance/admin/debtors/',
                         {'student_id': self.student.id, 'method': 'CALL',
                          'outcome': 'PROMISED', 'promise_date': '2026-10-20',
                          'note': 'Father will pay on Friday'},
                         HTTP_HOST=self.host(self.school))
        contact = DebtorContact.objects.get()
        self.assertEqual(contact.student, self.student)
        self.assertEqual(contact.promise_date, date(2026, 10, 20))
        self.assertEqual(contact.contacted_by, self.admin)

    def test_the_history_page_lists_every_follow_up(self):
        DebtorContact.objects.create(tenant=self.school, student=self.student,
                                     note='Called, no answer', outcome=DebtorContact.NO_ANSWER)
        response = self.client.get(f'/finance/admin/debtors/{self.student.id}/',
                                   HTTP_HOST=self.host(self.school))
        self.assertContains(response, 'Called, no answer')

    def test_the_debtor_list_loads(self):
        response = self.client.get('/finance/admin/debtors/', HTTP_HOST=self.host(self.school))
        self.assertEqual(response.status_code, 200)

    def test_a_parent_cannot_open_the_debtor_list(self):
        parent = User.objects.create_user(username='par1', password='x',
                                          tenant=self.school, is_parent=True)
        self.client.force_login(parent)
        response = self.client.get('/finance/admin/debtors/', HTTP_HOST=self.host(self.school))
        self.assertNotEqual(response.status_code, 200)


@override_settings(ROOT_DOMAIN='localhost', ALLOWED_HOSTS=['*'])
class ReceiptVerificationTests(TenantTestCase):

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha')
        set_current_tenant(self.school)
        self.student = make_student(self.school, 'STU001')
        self.wallet = Wallet.objects.create(tenant=self.school, student=self.student)
        self.payment = Payment.objects.create(
            tenant=self.school, wallet=self.wallet, amount=Decimal('25000'),
            reference='TIT-TESTREF1', status=Payment.APPROVED, method='CASH')

    def test_a_genuine_receipt_is_confirmed(self):
        response = self.client.get('/finance/verify/TIT-TESTREF1/',
                                   HTTP_HOST=self.host(self.school))
        self.assertContains(response, 'genuine')
        self.assertContains(response, 'Student One')
        self.assertContains(response, '25,000')

    def test_an_unknown_reference_is_not_confirmed(self):
        response = self.client.get('/finance/verify/NOT-A-REAL-REF/',
                                   HTTP_HOST=self.host(self.school), )
        self.assertEqual(response.status_code, 404)
        self.assertContains(response, "can't verify", status_code=404)

    def test_verification_needs_no_login(self):
        response = self.client.get('/finance/verify/TIT-TESTREF1/',
                                   HTTP_HOST=self.host(self.school))
        self.assertEqual(response.status_code, 200)
