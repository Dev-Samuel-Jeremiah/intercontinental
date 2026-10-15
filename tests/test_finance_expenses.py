"""
Money going out: expenses and their approval, suppliers, petty cash, budgets
and the income-and-expenditure statement.
"""
from datetime import date, timedelta
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.utils import timezone

from finance.models import (Budget, Expense, ExpenseCategory, Payment, PettyCashTopUp,
                            Vendor, Wallet)
from finance.services.accounts import (expenses_by_category, expenses_total, income_total,
                                       petty_cash_balance, statement)
from tenants.middleware import set_current_tenant
from users.models import User

from .factories import TenantTestCase, make_admin, make_school, make_student


class ExpenseRecordingTests(TenantTestCase):

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha')
        set_current_tenant(self.school)
        self.diesel = ExpenseCategory.objects.create(tenant=self.school, name='Diesel')
        self.repairs = ExpenseCategory.objects.create(tenant=self.school, name='Repairs')

    def _expense(self, amount, category=None, status=Expense.PAID, when=None, **kwargs):
        return Expense.objects.create(
            tenant=self.school, category=category or self.diesel,
            description=kwargs.pop('description', 'Generator diesel'),
            amount=Decimal(str(amount)), status=status,
            spent_on=when or timezone.now().date(), **kwargs)

    def test_only_money_actually_paid_counts_as_spent(self):
        self._expense(50000, status=Expense.PAID)
        self._expense(20000, status=Expense.PENDING)
        self._expense(10000, status=Expense.REJECTED)
        self.assertEqual(expenses_total(self.school), Decimal('50000.00'))

    def test_spending_is_grouped_by_category(self):
        self._expense(50000, self.diesel)
        self._expense(30000, self.repairs, description='Fix the gate')
        rows = expenses_by_category(self.school)
        self.assertEqual(rows[0]['category'], 'Diesel')
        self.assertEqual(rows[0]['total'], Decimal('50000.00'))
        self.assertEqual(rows[1]['total'], Decimal('30000.00'))

    def test_spending_outside_the_period_is_left_out(self):
        self._expense(50000, when=date(2026, 1, 15))
        self._expense(20000, when=date(2026, 6, 15))
        total = expenses_total(self.school, start=date(2026, 6, 1), end=date(2026, 6, 30))
        self.assertEqual(total, Decimal('20000.00'))

    def test_an_expense_names_who_was_paid(self):
        vendor = Vendor.objects.create(tenant=self.school, name='Jos Diesel Supplies')
        with_vendor = self._expense(10000, vendor=vendor)
        without = self._expense(5000, payee='Mallam Audu')
        self.assertEqual(with_vendor.paid_to, 'Jos Diesel Supplies')
        self.assertEqual(without.paid_to, 'Mallam Audu')

    def test_a_supplier_shows_what_they_have_been_paid(self):
        vendor = Vendor.objects.create(tenant=self.school, name='Jos Diesel Supplies')
        self._expense(40000, vendor=vendor)
        self._expense(15000, vendor=vendor, status=Expense.PENDING)   # not paid yet
        self.assertEqual(vendor.total_paid, Decimal('40000.00'))

    def test_expenses_never_cross_schools(self):
        other = make_school('beta')
        set_current_tenant(other)
        category = ExpenseCategory.objects.create(tenant=other, name='Diesel')
        Expense.objects.create(tenant=other, category=category, description='Theirs',
                               amount=Decimal('99999'), status=Expense.PAID)
        self.assertEqual(expenses_total(self.school), Decimal('0.00'))
        self.assertEqual(expenses_total(other), Decimal('99999.00'))


class PettyCashTests(TenantTestCase):

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha')
        set_current_tenant(self.school)
        self.category = ExpenseCategory.objects.create(tenant=self.school, name='Sundry')

    def test_the_tin_starts_empty(self):
        self.assertEqual(petty_cash_balance(self.school), Decimal('0.00'))

    def test_top_ups_add_to_the_tin(self):
        PettyCashTopUp.objects.create(tenant=self.school, amount=Decimal('20000'))
        PettyCashTopUp.objects.create(tenant=self.school, amount=Decimal('5000'))
        self.assertEqual(petty_cash_balance(self.school), Decimal('25000.00'))

    def test_petty_cash_spending_comes_off_the_tin(self):
        PettyCashTopUp.objects.create(tenant=self.school, amount=Decimal('20000'))
        Expense.objects.create(tenant=self.school, category=self.category, description='Brooms',
                               amount=Decimal('3500'), method=Expense.PETTY_CASH,
                               status=Expense.PAID)
        self.assertEqual(petty_cash_balance(self.school), Decimal('16500.00'))

    def test_spending_paid_by_transfer_does_not_touch_the_tin(self):
        PettyCashTopUp.objects.create(tenant=self.school, amount=Decimal('20000'))
        Expense.objects.create(tenant=self.school, category=self.category, description='Generator',
                               amount=Decimal('150000'), method=Expense.TRANSFER,
                               status=Expense.PAID)
        self.assertEqual(petty_cash_balance(self.school), Decimal('20000.00'))


class StatementTests(TenantTestCase):

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha')
        set_current_tenant(self.school)
        self.student = make_student(self.school, 'STU001')
        self.wallet = Wallet.objects.create(tenant=self.school, student=self.student)
        self.category = ExpenseCategory.objects.create(tenant=self.school, name='Diesel')

    def _payment(self, amount, status=Payment.APPROVED, method='CASH'):
        return Payment.objects.create(
            tenant=self.school, wallet=self.wallet, amount=Decimal(str(amount)),
            reference=f'REF-{Payment.objects.count() + 1}', status=status, method=method)

    def test_income_counts_approved_payments_only(self):
        self._payment(50000, status=Payment.APPROVED)
        self._payment(30000, status=Payment.PENDING)
        self.assertEqual(income_total(self.school), Decimal('50000.00'))

    def test_the_statement_shows_income_minus_expenditure(self):
        self._payment(100000)
        Expense.objects.create(tenant=self.school, category=self.category, description='Diesel',
                               amount=Decimal('40000'), status=Expense.PAID)
        report = statement(self.school)
        self.assertEqual(report['income'], Decimal('100000.00'))
        self.assertEqual(report['expenses'], Decimal('40000.00'))
        self.assertEqual(report['net'], Decimal('60000.00'))

    def test_the_net_can_be_negative(self):
        self._payment(10000)
        Expense.objects.create(tenant=self.school, category=self.category, description='Repairs',
                               amount=Decimal('45000'), status=Expense.PAID)
        self.assertEqual(statement(self.school)['net'], Decimal('-35000.00'))

    def test_budgets_appear_beside_what_was_spent(self):
        Budget.objects.create(tenant=self.school, category=self.category, amount=Decimal('50000'))
        Expense.objects.create(tenant=self.school, category=self.category, description='Diesel',
                               amount=Decimal('20000'), status=Expense.PAID)
        row = statement(self.school)['by_category'][0]
        self.assertEqual(row['budget'], Decimal('50000'))
        self.assertEqual(row['left'], Decimal('30000'))
        self.assertFalse(row['over'])

    def test_going_over_budget_is_flagged(self):
        Budget.objects.create(tenant=self.school, category=self.category, amount=Decimal('10000'))
        Expense.objects.create(tenant=self.school, category=self.category, description='Diesel',
                               amount=Decimal('25000'), status=Expense.PAID)
        row = statement(self.school)['by_category'][0]
        self.assertTrue(row['over'])
        self.assertEqual(row['left'], Decimal('-15000'))

    def test_a_budget_with_no_spending_still_shows(self):
        Budget.objects.create(tenant=self.school, category=self.category, amount=Decimal('50000'))
        row = statement(self.school)['by_category'][0]
        self.assertEqual(row['total'], Decimal('0.00'))
        self.assertEqual(row['left'], Decimal('50000'))

    def test_pending_requests_are_reported_separately(self):
        Expense.objects.create(tenant=self.school, category=self.category, description='Waiting',
                               amount=Decimal('12000'), status=Expense.PENDING)
        report = statement(self.school)
        self.assertEqual(report['awaiting_approval'], Decimal('12000.00'))
        self.assertEqual(report['expenses'], Decimal('0.00'))


@override_settings(ROOT_DOMAIN='localhost', ALLOWED_HOSTS=['*'])
class ExpensePageTests(TenantTestCase):

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha')
        self.admin = make_admin(self.school)
        set_current_tenant(self.school)
        self.category = ExpenseCategory.objects.create(tenant=self.school, name='Diesel')
        self.client.force_login(self.admin)

    def test_recording_an_expense_with_a_receipt(self):
        receipt = SimpleUploadedFile('receipt.pdf', b'%PDF-1.4 x', content_type='application/pdf')
        self.client.post('/finance/admin/expenses/',
                         {'action': 'add', 'description': 'Generator diesel',
                          'category_id': self.category.id, 'amount': '45000',
                          'method': 'CASH', 'payee': 'Mallam Audu', 'receipt': receipt,
                          'already_paid': '1'},
                         HTTP_HOST=self.host(self.school))
        expense = Expense.objects.get()
        self.assertEqual(expense.amount, Decimal('45000'))
        self.assertEqual(expense.status, Expense.PAID)
        self.assertTrue(expense.receipt)
        self.assertEqual(expense.requested_by, self.admin)

    def test_an_expense_without_an_amount_is_refused(self):
        response = self.client.post('/finance/admin/expenses/',
                                    {'action': 'add', 'description': 'Something',
                                     'category_id': self.category.id, 'amount': '0'},
                                    HTTP_HOST=self.host(self.school), follow=True)
        self.assertContains(response, 'Enter what the money was for')
        self.assertFalse(Expense.objects.exists())

    def test_approving_and_paying_a_request(self):
        expense = Expense.objects.create(tenant=self.school, category=self.category,
                                         description='New kettle', amount=Decimal('9000'))
        self.client.post('/finance/admin/expenses/',
                         {'action': 'approve', 'expense_id': expense.id},
                         HTTP_HOST=self.host(self.school))
        expense.refresh_from_db()
        self.assertEqual(expense.status, Expense.APPROVED)
        self.assertEqual(expense.approved_by, self.admin)

        self.client.post('/finance/admin/expenses/',
                         {'action': 'mark_paid', 'expense_id': expense.id},
                         HTTP_HOST=self.host(self.school))
        expense.refresh_from_db()
        self.assertEqual(expense.status, Expense.PAID)

    def test_rejecting_a_request_keeps_it_out_of_the_totals(self):
        expense = Expense.objects.create(tenant=self.school, category=self.category,
                                         description='Personal item', amount=Decimal('5000'))
        self.client.post('/finance/admin/expenses/',
                         {'action': 'reject', 'expense_id': expense.id},
                         HTTP_HOST=self.host(self.school))
        expense.refresh_from_db()
        self.assertEqual(expense.status, Expense.REJECTED)
        self.assertEqual(expenses_total(self.school), Decimal('0.00'))

    def test_adding_a_supplier(self):
        self.client.post('/finance/admin/vendors/',
                         {'action': 'add', 'name': 'Jos Diesel Supplies',
                          'service': 'Diesel', 'phone': '08031234567',
                          'bank_name': 'GTBank', 'account_number': '0123456789'},
                         HTTP_HOST=self.host(self.school))
        vendor = Vendor.objects.get()
        self.assertEqual(vendor.name, 'Jos Diesel Supplies')
        self.assertEqual(vendor.bank_name, 'GTBank')

    def test_the_same_supplier_cannot_be_added_twice(self):
        Vendor.objects.create(tenant=self.school, name='Jos Diesel Supplies')
        response = self.client.post('/finance/admin/vendors/',
                                    {'action': 'add', 'name': 'jos diesel supplies'},
                                    HTTP_HOST=self.host(self.school), follow=True)
        self.assertContains(response, 'already on the list')
        self.assertEqual(Vendor.objects.count(), 1)

    def test_topping_up_petty_cash_through_the_page(self):
        self.client.post('/finance/admin/petty-cash/',
                         {'amount': '20000', 'note': 'From the bank'},
                         HTTP_HOST=self.host(self.school))
        self.assertEqual(petty_cash_balance(self.school), Decimal('20000.00'))

    def test_setting_a_budget_through_the_page(self):
        self.client.post('/finance/admin/budgets/',
                         {'action': 'set', 'category_id': self.category.id, 'amount': '300000'},
                         HTTP_HOST=self.host(self.school))
        self.assertEqual(Budget.objects.get().amount, Decimal('300000'))

    def test_the_statement_page_loads(self):
        response = self.client.get('/finance/admin/statement/', HTTP_HOST=self.host(self.school))
        self.assertEqual(response.status_code, 200)

    def test_a_parent_cannot_see_the_schools_expenses(self):
        parent = User.objects.create_user(username='par1', password='x',
                                          tenant=self.school, is_parent=True)
        self.client.force_login(parent)
        response = self.client.get('/finance/admin/expenses/', HTTP_HOST=self.host(self.school))
        self.assertNotEqual(response.status_code, 200)
