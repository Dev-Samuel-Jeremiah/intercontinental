"""
Late payment penalties and refunds: what a family owes when they pay late,
and what happens when the school has to give money back.
"""
from datetime import date, timedelta
from decimal import Decimal

from django.test import override_settings

from academics.models import AcademicSession, Term
from finance.models import (ChildBilling, LateFeeCharge, LateFeeRule, ParentBilling,
                            Payment, Refund, Wallet)
from finance.services import late_fees, refunds as refund_service
from finance.services.accounts import statement
from tenants.middleware import set_current_tenant
from users.models import User

from .factories import TenantTestCase, make_admin, make_class, make_school, make_student


def _term_and_session(school):
    """The school's current session and term — the pages read these."""
    session = AcademicSession.objects.create(tenant=school, name='2026/2027',
                                             start_date=date(2026, 9, 1),
                                             end_date=date(2027, 7, 31),
                                             is_active=True)
    term = Term.objects.create(tenant=school, name='1st Term', session=session,
                               start_date=date(2026, 9, 1), end_date=date(2026, 12, 20),
                               is_active=True)
    return session, term


class LateFeeBase(TenantTestCase):

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha')
        self.jss1 = make_class(self.school, 'JSS 1')
        set_current_tenant(self.school)
        self.session, self.term = _term_and_session(self.school)
        self.due = date(2026, 10, 1)

    def _billing(self, admission_number, fee, paid=Decimal('0')):
        student = make_student(self.school, admission_number, school_class=self.jss1)
        wallet = Wallet.objects.create(tenant=self.school, student=student)
        parent = ParentBilling.objects.create(tenant=self.school, wallet=wallet,
                                              session=self.session, term=self.term,
                                              total_expected=fee, total_paid=paid)
        return ChildBilling.objects.create(tenant=self.school, student=student,
                                           parent_billing=parent,
                                           total_fee=Decimal(str(fee)),
                                           gross_fee=Decimal(str(fee)),
                                           paid=Decimal(str(paid)))

    def _rule(self, **kwargs):
        defaults = dict(tenant=self.school, name='Late payment charge',
                        session=self.session, term=self.term, due_date=self.due,
                        charge_type=LateFeeRule.AMOUNT, value=Decimal('2500'))
        defaults.update(kwargs)
        return LateFeeRule.objects.create(**defaults)


class WhenACharageIsEarnedTests(LateFeeBase):

    def test_nothing_is_charged_before_the_due_date(self):
        rule = self._rule()
        self._billing('STU001', 100000)
        self.assertEqual(late_fees.apply_rule(rule, as_of=date(2026, 9, 30)), [])

    def test_nothing_is_charged_during_the_grace_period(self):
        rule = self._rule(grace_days=7)
        self._billing('STU001', 100000)
        self.assertEqual(late_fees.apply_rule(rule, as_of=date(2026, 10, 5)), [])
        self.assertEqual(len(late_fees.apply_rule(rule, as_of=date(2026, 10, 8))), 1)

    def test_a_family_that_has_paid_in_full_is_never_charged(self):
        rule = self._rule()
        self._billing('STU001', 100000, paid=100000)
        self.assertEqual(late_fees.apply_rule(rule, as_of=date(2026, 10, 15)), [])

    def test_a_family_owing_less_than_the_floor_is_skipped(self):
        rule = self._rule(minimum_balance=Decimal('5000'))
        self._billing('STU001', 100000, paid=98000)   # owes 2,000
        self.assertEqual(late_fees.apply_rule(rule, as_of=date(2026, 10, 15)), [])

    def test_a_paused_rule_charges_nobody(self):
        rule = self._rule(is_active=False)
        self._billing('STU001', 100000)
        self.assertEqual(late_fees.apply_rule(rule, as_of=date(2026, 10, 15)), [])

    def test_running_the_same_rule_twice_charges_once(self):
        rule = self._rule()
        self._billing('STU001', 100000)
        late_fees.apply_rule(rule, as_of=date(2026, 10, 15))
        self.assertEqual(late_fees.apply_rule(rule, as_of=date(2026, 10, 15)), [])
        self.assertEqual(LateFeeCharge.objects.count(), 1)


class HowMuchIsChargedTests(LateFeeBase):

    def test_a_flat_charge(self):
        rule = self._rule(value=Decimal('2500'))
        self._billing('STU001', 100000, paid=40000)
        charge = late_fees.apply_rule(rule, as_of=date(2026, 10, 15))[0]
        self.assertEqual(charge.amount, Decimal('2500.00'))
        self.assertEqual(charge.balance_then, Decimal('60000'))

    def test_a_percentage_of_what_is_still_owed(self):
        rule = self._rule(charge_type=LateFeeRule.PERCENT, value=Decimal('5'))
        self._billing('STU001', 100000, paid=40000)
        charge = late_fees.apply_rule(rule, as_of=date(2026, 10, 15))[0]
        self.assertEqual(charge.amount, Decimal('3000.00'))   # 5% of 60,000

    def test_a_weekly_charge_grows_each_week(self):
        rule = self._rule(repeat=LateFeeRule.WEEKLY, value=Decimal('1000'))
        self._billing('STU001', 100000)
        raised = late_fees.apply_rule(rule, as_of=date(2026, 10, 22))   # three weeks late
        self.assertEqual(len(raised), 4)
        self.assertEqual(sum(c.amount for c in raised), Decimal('4000.00'))

    def test_a_monthly_charge_grows_each_month(self):
        rule = self._rule(repeat=LateFeeRule.MONTHLY, value=Decimal('5000'))
        self._billing('STU001', 100000)
        raised = late_fees.apply_rule(rule, as_of=date(2026, 12, 1))
        self.assertEqual(len(raised), 3)   # 1 Oct, +30 days, +60 days

    def test_a_cap_stops_the_penalty_growing(self):
        rule = self._rule(repeat=LateFeeRule.WEEKLY, value=Decimal('1000'),
                          max_total=Decimal('2500'))
        self._billing('STU001', 100000)
        raised = late_fees.apply_rule(rule, as_of=date(2026, 11, 30))
        self.assertEqual(sum(c.amount for c in raised), Decimal('2500.00'))

    def test_only_the_late_term_is_charged(self):
        other_session = AcademicSession.objects.create(tenant=self.school, name='2025/2026')
        other_term = Term.objects.create(tenant=self.school, name='3rd Term',
                                         session=other_session)
        rule = self._rule(session=other_session, term=other_term, name='Old term')
        self._billing('STU001', 100000)
        self.assertEqual(late_fees.apply_rule(rule, as_of=date(2026, 10, 15)), [])


class WhatTheFamilyOwesTests(LateFeeBase):

    def test_a_penalty_is_added_to_the_bill(self):
        rule = self._rule(value=Decimal('2500'))
        billing = self._billing('STU001', 100000, paid=40000)
        late_fees.apply_rule(rule, as_of=date(2026, 10, 15))
        billing.refresh_from_db()
        self.assertEqual(billing.late_fee, Decimal('2500.00'))
        self.assertEqual(billing.amount_due, Decimal('102500.00'))
        self.assertEqual(billing.balance, Decimal('62500.00'))

    def test_the_parent_total_follows_the_penalty(self):
        rule = self._rule(value=Decimal('2500'))
        billing = self._billing('STU001', 100000)
        late_fees.apply_rule(rule, as_of=date(2026, 10, 15))
        billing.parent_billing.refresh_from_db()
        self.assertEqual(billing.parent_billing.total_expected, Decimal('102500.00'))

    def test_waiving_a_penalty_takes_it_off_the_bill(self):
        rule = self._rule(value=Decimal('2500'))
        billing = self._billing('STU001', 100000)
        charge = late_fees.apply_rule(rule, as_of=date(2026, 10, 15))[0]
        late_fees.waive(charge, reason='Father was in hospital')
        billing.refresh_from_db()
        self.assertEqual(billing.late_fee, Decimal('0.00'))
        self.assertEqual(billing.amount_due, Decimal('100000.00'))

    def test_a_waived_penalty_is_kept_as_a_record(self):
        rule = self._rule()
        self._billing('STU001', 100000)
        charge = late_fees.apply_rule(rule, as_of=date(2026, 10, 15))[0]
        late_fees.waive(charge, reason='Agreed with the head')
        charge.refresh_from_db()
        self.assertTrue(charge.waived)
        self.assertEqual(charge.waived_reason, 'Agreed with the head')
        self.assertIsNotNone(charge.waived_at)

    def test_a_waiver_can_be_undone(self):
        rule = self._rule(value=Decimal('2500'))
        billing = self._billing('STU001', 100000)
        charge = late_fees.apply_rule(rule, as_of=date(2026, 10, 15))[0]
        late_fees.waive(charge)
        late_fees.restore(charge)
        billing.refresh_from_db()
        self.assertEqual(billing.late_fee, Decimal('2500.00'))

    def test_a_waived_charge_is_not_raised_again(self):
        rule = self._rule()
        self._billing('STU001', 100000)
        charge = late_fees.apply_rule(rule, as_of=date(2026, 10, 15))[0]
        late_fees.waive(charge)
        self.assertEqual(late_fees.apply_rule(rule, as_of=date(2026, 10, 20)), [])

    def test_a_preview_charges_nobody(self):
        rule = self._rule()
        self._billing('STU001', 100000)
        rows = late_fees.preview_rule(rule, as_of=date(2026, 10, 15))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['amount'], Decimal('2500.00'))
        self.assertFalse(LateFeeCharge.objects.exists())

    def test_penalties_never_cross_schools(self):
        rule = self._rule()
        self._billing('STU001', 100000)
        other = make_school('beta')
        set_current_tenant(other)
        session, term = _term_and_session(other)
        student = make_student(other, 'STU001')
        wallet = Wallet.objects.create(tenant=other, student=student)
        parent = ParentBilling.objects.create(tenant=other, wallet=wallet,
                                              session=session, term=term)
        ChildBilling.objects.create(tenant=other, student=student, parent_billing=parent,
                                    total_fee=Decimal('500000'))

        set_current_tenant(self.school)
        raised = late_fees.apply_rule(rule, as_of=date(2026, 10, 15))
        self.assertEqual(len(raised), 1)
        self.assertEqual(raised[0].child_billing.student.admission_number, 'STU001')
        self.assertEqual(raised[0].tenant, self.school)
        self.assertFalse(LateFeeCharge.objects.filter(tenant=other).exists())


class RefundTests(TenantTestCase):

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha')
        self.admin = make_admin(self.school)
        set_current_tenant(self.school)
        self.session, self.term = _term_and_session(self.school)
        self.student = make_student(self.school, 'STU001')
        self.wallet = Wallet.objects.create(tenant=self.school, student=self.student)

    def _refund(self, amount=10000, **kwargs):
        return refund_service.request_refund(self.school, self.student, amount, **kwargs)

    def test_a_refund_starts_as_a_request(self):
        refund = self._refund(15000, detail='Paid twice in September')
        self.assertEqual(refund.status, Refund.REQUESTED)
        self.assertEqual(refund.amount, Decimal('15000'))

    def test_a_refund_of_nothing_is_refused(self):
        with self.assertRaises(ValueError):
            self._refund(0)

    def test_a_refund_must_be_approved_before_it_is_paid(self):
        refund = self._refund()
        with self.assertRaises(ValueError):
            refund_service.mark_paid(refund)

    def test_approving_then_paying(self):
        refund = self._refund()
        refund_service.approve(refund, self.admin)
        self.assertEqual(refund.status, Refund.APPROVED)
        self.assertEqual(refund.approved_by, self.admin)
        refund_service.mark_paid(refund, self.admin, reference='TRF-991')
        refund.refresh_from_db()
        self.assertEqual(refund.status, Refund.PAID)
        self.assertEqual(refund.reference, 'TRF-991')
        self.assertIsNotNone(refund.paid_on)

    def test_a_paid_refund_cannot_be_rejected(self):
        refund = self._refund()
        refund_service.approve(refund)
        refund_service.mark_paid(refund)
        with self.assertRaises(ValueError):
            refund_service.reject(refund)

    def test_a_wallet_refund_tops_the_wallet_up(self):
        refund = self._refund(7500, method=Refund.WALLET)
        refund_service.approve(refund)
        refund_service.mark_paid(refund)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('7500.00'))

    def test_a_transfer_refund_does_not_touch_the_wallet(self):
        refund = self._refund(7500, method=Refund.TRANSFER)
        refund_service.approve(refund)
        refund_service.mark_paid(refund)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('0.00'))

    def test_only_a_paid_refund_counts(self):
        self._refund(5000)                                     # still a request
        approved = self._refund(3000); refund_service.approve(approved)
        paid = self._refund(2000)
        refund_service.approve(paid); refund_service.mark_paid(paid)
        self.assertEqual(refund_service.refunds_total(self.school), Decimal('2000.00'))
        self.assertEqual(refund_service.awaiting_total(self.school), Decimal('8000.00'))

    def test_a_refund_comes_off_the_schools_income(self):
        Payment.objects.create(tenant=self.school, wallet=self.wallet,
                               amount=Decimal('100000'), reference='PAY-1',
                               status=Payment.APPROVED)
        refund = self._refund(25000)
        refund_service.approve(refund)
        refund_service.mark_paid(refund)
        report = statement(self.school)
        self.assertEqual(report['received'], Decimal('100000.00'))
        self.assertEqual(report['refunds'], Decimal('25000.00'))
        self.assertEqual(report['income'], Decimal('75000.00'))
        self.assertEqual(report['net'], Decimal('75000.00'))

    def test_a_request_alone_does_not_change_the_books(self):
        Payment.objects.create(tenant=self.school, wallet=self.wallet,
                               amount=Decimal('100000'), reference='PAY-1',
                               status=Payment.APPROVED)
        self._refund(25000)
        report = statement(self.school)
        self.assertEqual(report['income'], Decimal('100000.00'))
        self.assertEqual(report['refunds_awaiting'], Decimal('25000.00'))

    def test_families_who_overpaid_are_listed(self):
        parent = ParentBilling.objects.create(tenant=self.school, wallet=self.wallet,
                                              session=self.session, term=self.term)
        ChildBilling.objects.create(tenant=self.school, student=self.student,
                                    parent_billing=parent, total_fee=Decimal('50000'),
                                    paid=Decimal('62000'))
        rows = refund_service.overpaid_students(self.school)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['over'], Decimal('12000.00'))

    def test_refunds_never_cross_schools(self):
        refund = self._refund(5000)
        refund_service.approve(refund)
        refund_service.mark_paid(refund)
        other = make_school('beta')
        self.assertEqual(refund_service.refunds_total(other), Decimal('0.00'))
        self.assertEqual(refund_service.refunds_total(self.school), Decimal('5000.00'))


@override_settings(ROOT_DOMAIN='localhost', ALLOWED_HOSTS=['*'])
class RecoveryPageTests(LateFeeBase):

    def setUp(self):
        super().setUp()
        self.admin = make_admin(self.school)
        set_current_tenant(self.school)
        self.client.force_login(self.admin)

    def test_adding_a_rule_charges_nobody_yet(self):
        self._billing('STU001', 100000)
        self.client.post('/finance/admin/late-fees/',
                         {'action': 'add', 'name': 'October charge',
                          'due_date': '2026-10-01', 'grace_days': '0',
                          'charge_type': 'AMOUNT', 'value': '2500', 'repeat': 'ONCE'},
                         HTTP_HOST=self.host(self.school))
        self.assertEqual(LateFeeRule.objects.count(), 1)
        self.assertFalse(LateFeeCharge.objects.exists())

    def test_a_rule_without_an_amount_is_refused(self):
        response = self.client.post('/finance/admin/late-fees/',
                                    {'action': 'add', 'due_date': '2026-10-01',
                                     'value': '0'},
                                    HTTP_HOST=self.host(self.school), follow=True)
        self.assertContains(response, 'Enter what to charge')
        self.assertFalse(LateFeeRule.objects.exists())

    def test_two_rules_cannot_share_a_name_in_one_term(self):
        self._rule(name='October charge')
        response = self.client.post('/finance/admin/late-fees/',
                                    {'action': 'add', 'name': 'october charge',
                                     'due_date': '2026-10-01', 'value': '1000'},
                                    HTTP_HOST=self.host(self.school), follow=True)
        self.assertContains(response, 'already a rule called')
        self.assertEqual(LateFeeRule.objects.count(), 1)

    def test_running_a_rule_from_the_page(self):
        rule = self._rule(due_date=date(2020, 1, 1))   # long past, so everyone is late
        billing = self._billing('STU001', 100000)
        self.client.post('/finance/admin/late-fees/',
                         {'action': 'run', 'rule_id': rule.id},
                         HTTP_HOST=self.host(self.school))
        billing.refresh_from_db()
        self.assertEqual(billing.late_fee, Decimal('2500.00'))

    def test_waiving_from_the_page(self):
        rule = self._rule(due_date=date(2020, 1, 1))
        billing = self._billing('STU001', 100000)
        charge = late_fees.apply_rule(rule)[0]
        self.client.post('/finance/admin/late-fees/',
                         {'action': 'waive', 'charge_id': charge.id,
                          'reason': 'Hardship'},
                         HTTP_HOST=self.host(self.school))
        charge.refresh_from_db(); billing.refresh_from_db()
        self.assertTrue(charge.waived)
        self.assertEqual(charge.waived_by, self.admin)
        self.assertEqual(billing.late_fee, Decimal('0.00'))

    def test_removing_a_rule_clears_what_it_charged(self):
        rule = self._rule(due_date=date(2020, 1, 1))
        billing = self._billing('STU001', 100000)
        late_fees.apply_rule(rule)
        self.client.post('/finance/admin/late-fees/',
                         {'action': 'delete', 'rule_id': rule.id},
                         HTTP_HOST=self.host(self.school))
        billing.refresh_from_db()
        self.assertEqual(billing.late_fee, Decimal('0.00'))
        self.assertFalse(LateFeeCharge.objects.exists())

    def test_the_preview_shows_who_would_pay(self):
        rule = self._rule(due_date=date(2020, 1, 1))
        self._billing('STU001', 100000)
        response = self.client.get(f'/finance/admin/late-fees/?preview={rule.id}',
                                   HTTP_HOST=self.host(self.school))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['preview']), 1)
        self.assertFalse(LateFeeCharge.objects.exists())

    def test_requesting_and_paying_a_refund_from_the_page(self):
        student = make_student(self.school, 'STU009')
        Wallet.objects.create(tenant=self.school, student=student)
        self.client.post('/finance/admin/refunds/',
                         {'action': 'add', 'student_id': student.id, 'amount': '12000',
                          'reason': 'OVERPAYMENT', 'method': 'TRANSFER',
                          'detail': 'Paid twice'},
                         HTTP_HOST=self.host(self.school))
        refund = Refund.objects.get()
        self.assertEqual(refund.status, Refund.REQUESTED)
        self.assertEqual(refund.requested_by, self.admin)

        self.client.post('/finance/admin/refunds/',
                         {'action': 'approve', 'refund_id': refund.id},
                         HTTP_HOST=self.host(self.school))
        self.client.post('/finance/admin/refunds/',
                         {'action': 'mark_paid', 'refund_id': refund.id,
                          'reference': 'TRF-55'},
                         HTTP_HOST=self.host(self.school))
        refund.refresh_from_db()
        self.assertEqual(refund.status, Refund.PAID)
        self.assertEqual(refund.reference, 'TRF-55')

    def test_paying_a_refund_that_was_never_approved_is_refused(self):
        student = make_student(self.school, 'STU009')
        refund = refund_service.request_refund(self.school, student, 5000)
        response = self.client.post('/finance/admin/refunds/',
                                    {'action': 'mark_paid', 'refund_id': refund.id},
                                    HTTP_HOST=self.host(self.school), follow=True)
        self.assertContains(response, 'must be approved')
        refund.refresh_from_db()
        self.assertEqual(refund.status, Refund.REQUESTED)

    def test_a_parent_cannot_reach_either_page(self):
        parent = User.objects.create_user(username='par1', password='x',
                                          tenant=self.school, is_parent=True)
        self.client.force_login(parent)
        for path in ('/finance/admin/late-fees/', '/finance/admin/refunds/'):
            self.assertNotEqual(
                self.client.get(path, HTTP_HOST=self.host(self.school)).status_code, 200)
