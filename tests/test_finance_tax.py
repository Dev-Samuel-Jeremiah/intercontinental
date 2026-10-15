"""
Withholding tax and VAT on what the school buys: the deduction, what is owed
to each tax authority, remittances and the schedule — and that every other
part of finance sees the net amount the supplier was actually paid.
"""
import csv
import io
from datetime import date
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings

from finance.models import (BankAccount, BankLine, Expense, ExpenseCategory, PettyCashTopUp,
                            Vendor, WhtRate, WhtRemittance)
from finance.services import export, reconcile, tax
from finance.services.accounts import petty_cash_balance, statement
from tenants.middleware import set_current_tenant
from users.models import User

from .factories import TenantTestCase, make_admin, make_school

SEPT = (date(2026, 9, 1), date(2026, 9, 30))


class TaxBase(TenantTestCase):

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha')
        set_current_tenant(self.school)
        tax.ensure_rate_table(self.school)
        self.goods = WhtRate.objects.get(name='Supply of goods')            # 2 / 2
        self.contract = WhtRate.objects.get(name='Construction and maintenance work')  # 2 / 5
        self.category = ExpenseCategory.objects.create(tenant=self.school, name='Repairs')
        self.company = Vendor.objects.create(tenant=self.school, name='Hilltop Electricals Ltd',
                                             payee_type='COMPANY', tin='12345678-0001')
        self.person = Vendor.objects.create(tenant=self.school, name='Mallam Audu',
                                            payee_type='INDIVIDUAL')

    def _expense(self, amount, rate=None, vendor=None, payee_type=None, method=Expense.TRANSFER,
                 status=Expense.PAID, day=date(2026, 9, 10), reference=''):
        expense = Expense(tenant=self.school, category=self.category, vendor=vendor,
                          description='Rewire the science lab', amount=Decimal(str(amount)),
                          method=method, status=status, spent_on=day, reference=reference)
        tax.apply_wht(expense, rate, payee_type, vendor=vendor)
        expense.save()
        return expense


class DeductionTests(TaxBase):

    def test_every_school_gets_its_own_starting_table(self):
        other = make_school('beta')
        tax.ensure_rate_table(other)          # alpha is still the active school
        tax.ensure_rate_table(other)          # and asking twice creates nothing more
        self.assertEqual(WhtRate.objects.for_tenant(other).count(), len(tax.DEFAULT_RATES))
        WhtRate.objects.for_tenant(other).filter(name='Supply of goods').update(company_rate=7)
        self.goods.refresh_from_db()
        self.assertEqual(self.goods.company_rate, Decimal('2.00'))

    def test_the_table_is_not_recreated_once_a_school_has_one(self):
        self.goods.delete()
        tax.ensure_rate_table(self.school)
        self.assertFalse(WhtRate.objects.filter(name='Supply of goods').exists())

    def test_a_company_has_the_company_rate_deducted(self):
        expense = self._expense(500000, self.contract, vendor=self.company)
        self.assertEqual(expense.wht_percent, Decimal('2.00'))
        self.assertEqual(expense.wht_amount, Decimal('10000.00'))
        self.assertEqual(expense.net_paid, Decimal('490000.00'))
        self.assertEqual(expense.wht_authority, Expense.FEDERAL)

    def test_an_individual_has_the_individual_rate_and_goes_to_the_state(self):
        expense = self._expense(500000, self.contract, vendor=self.person)
        self.assertEqual(expense.wht_amount, Decimal('25000.00'))
        self.assertEqual(expense.wht_authority, Expense.STATE)

    def test_a_payee_who_is_not_a_saved_supplier(self):
        expense = self._expense(100000, self.contract, payee_type=Expense.INDIVIDUAL)
        self.assertEqual(expense.wht_amount, Decimal('5000.00'))
        self.assertEqual(expense.wht_authority, Expense.STATE)

    def test_an_exempt_supplier_is_never_deducted_from(self):
        self.company.wht_exempt = True
        self.company.save()
        expense = self._expense(500000, self.contract, vendor=self.company)
        self.assertEqual(expense.wht_amount, Decimal('0.00'))
        self.assertIsNone(expense.wht_rate)
        self.assertEqual(expense.wht_authority, '')

    def test_no_rate_means_nothing_withheld(self):
        expense = self._expense(500000, None, vendor=self.company)
        self.assertEqual((expense.wht_amount, expense.net_paid),
                         (Decimal('0.00'), Decimal('500000')))

    def test_kobo_round_half_up(self):
        _, withheld = tax.compute_wht(Decimal('1234.25'), self.goods, 'COMPANY')
        self.assertEqual(withheld, Decimal('24.69'))   # 24.685 → 24.69

    def test_changing_the_table_never_changes_a_recorded_expense(self):
        expense = self._expense(500000, self.contract, vendor=self.company)
        self.contract.company_rate = Decimal('10')
        self.contract.save()
        expense.refresh_from_db()
        self.assertEqual((expense.wht_percent, expense.wht_amount),
                         (Decimal('2.00'), Decimal('10000.00')))


class OwedAndRemittedTests(TaxBase):

    def test_only_paid_expenses_count_as_withheld(self):
        self._expense(500000, self.contract, vendor=self.company)
        self._expense(500000, self.contract, vendor=self.company, status=Expense.PENDING)
        federal = tax.position(self.school)[0]
        self.assertEqual(federal['owed'], Decimal('10000.00'))

    def test_each_authority_is_owed_separately(self):
        self._expense(500000, self.contract, vendor=self.company)    # 10,000 federal
        self._expense(500000, self.contract, vendor=self.person)     # 25,000 state
        owed = {p['key']: p['owed'] for p in tax.position(self.school)}
        self.assertEqual(owed, {Expense.FEDERAL: Decimal('10000.00'),
                                Expense.STATE: Decimal('25000.00')})

    def test_a_remittance_pays_over_the_months_deductions(self):
        self._expense(500000, self.contract, vendor=self.company, day=date(2026, 9, 5))
        self._expense(200000, self.goods, vendor=self.company, day=date(2026, 9, 25))
        later = self._expense(100000, self.goods, vendor=self.company, day=date(2026, 10, 2))
        remittance = tax.record_remittance(self.school, Expense.FEDERAL, *SEPT,
                                           reference='NRS-0091', paid_on=date(2026, 10, 15))
        self.assertEqual(remittance.amount, Decimal('14000.00'))
        self.assertEqual(remittance.expenses.count(), 2)
        later.refresh_from_db()
        self.assertIsNone(later.wht_remittance)
        self.assertEqual(tax.position(self.school)[0]['owed'], Decimal('2000.00'))

    def test_a_remittance_only_covers_its_own_authority(self):
        self._expense(500000, self.contract, vendor=self.company)
        self._expense(500000, self.contract, vendor=self.person)
        tax.record_remittance(self.school, Expense.FEDERAL, *SEPT)
        owed = {p['key']: p['owed'] for p in tax.position(self.school)}
        self.assertEqual(owed[Expense.FEDERAL], Decimal('0.00'))
        self.assertEqual(owed[Expense.STATE], Decimal('25000.00'))

    def test_nothing_is_paid_over_twice(self):
        self._expense(500000, self.contract, vendor=self.company)
        tax.record_remittance(self.school, Expense.FEDERAL, *SEPT)
        with self.assertRaises(ValueError):
            tax.record_remittance(self.school, Expense.FEDERAL, *SEPT)

    def test_undoing_a_remittance_makes_the_tax_owed_again(self):
        self._expense(500000, self.contract, vendor=self.company)
        remittance = tax.record_remittance(self.school, Expense.FEDERAL, *SEPT)
        tax.undo_remittance(remittance)
        self.assertFalse(WhtRemittance.objects.exists())
        self.assertEqual(tax.position(self.school)[0]['owed'], Decimal('10000.00'))

    def test_vat_paid_to_suppliers(self):
        expense = self._expense(107500, None, vendor=self.company)
        expense.vat_amount = Decimal('7500')
        expense.save()
        self.assertEqual(tax.vat_paid(self.school, *SEPT), Decimal('7500.00'))

    def test_the_schedule_lists_each_deduction(self):
        self._expense(500000, self.contract, vendor=self.company, reference='INV-204')
        self._expense(500000, self.contract, vendor=self.person)          # state, not here
        rows = list(csv.reader(io.StringIO(
            tax.schedule_csv(self.school, *SEPT, Expense.FEDERAL))))
        self.assertEqual(len(rows), 2)
        header = rows[0]
        row = dict(zip(header, rows[1]))
        self.assertEqual(row['Beneficiary name'], 'Hilltop Electricals Ltd')
        self.assertEqual(row['Beneficiary TIN'], '12345678-0001')
        self.assertEqual(row['Gross amount'], '500000.00')
        self.assertEqual(row['WHT rate (%)'], '2.00')
        self.assertEqual(row['WHT deducted'], '10000.00')
        self.assertEqual(row['Invoice / reference'], 'INV-204')

    def test_another_schools_deductions_never_appear(self):
        self._expense(500000, self.contract, vendor=self.company)
        other = make_school('beta')
        set_current_tenant(None)
        self.assertEqual(tax.position(other)[0]['owed'], Decimal('0.00'))
        with self.assertRaises(ValueError):
            tax.record_remittance(other, Expense.FEDERAL, *SEPT)


class NetAmountEverywhereTests(TaxBase):
    """The supplier gets the net. Everything that follows the money must agree."""

    def test_the_full_bill_is_still_the_schools_cost(self):
        self._expense(500000, self.contract, vendor=self.company)
        self.assertEqual(statement(self.school, *SEPT)['expenses'], Decimal('500000.00'))

    def test_petty_cash_only_loses_the_net(self):
        PettyCashTopUp.objects.create(tenant=self.school, amount=Decimal('50000'))
        self._expense(20000, self.goods, payee_type=Expense.INDIVIDUAL,
                      method=Expense.PETTY_CASH)                    # 400 withheld
        self.assertEqual(petty_cash_balance(self.school), Decimal('30400.00'))

    def test_the_bank_line_matches_the_net_payment(self):
        expense = self._expense(500000, self.contract, vendor=self.company)
        account = BankAccount.objects.create(tenant=self.school, name='Main', bank_name='GTB',
                                             account_number='1')
        reconcile.import_statement(self.school, account, SimpleUploadedFile(
            's.csv', b'Date,Description,Amount\n10/09/2026,TRF HILLTOP,-490000\n'))
        self.assertEqual(BankLine.objects.get().expense, expense)

    def test_the_bank_line_matches_the_remittance(self):
        self._expense(500000, self.contract, vendor=self.company)
        remittance = tax.record_remittance(self.school, Expense.FEDERAL, *SEPT,
                                           paid_on=date(2026, 10, 15))
        account = BankAccount.objects.create(tenant=self.school, name='Main', bank_name='GTB',
                                             account_number='1')
        reconcile.import_statement(self.school, account, SimpleUploadedFile(
            's.csv', b'Date,Description,Amount\n15/10/2026,NRS WHT SEP,-10000\n'))
        self.assertEqual(BankLine.objects.get().wht_remittance, remittance)

    def test_an_unbanked_remittance_is_listed(self):
        self._expense(500000, self.contract, vendor=self.company)
        remittance = tax.record_remittance(self.school, Expense.FEDERAL, *SEPT,
                                           paid_on=date(2026, 10, 15))
        left = reconcile.recorded_not_banked(self.school, date(2026, 10, 1), date(2026, 10, 31))
        self.assertEqual(left['remittances'], [remittance])

    def test_the_journal_splits_the_bill_three_ways(self):
        self._expense(500000, self.contract, vendor=self.company)
        entry = export.journal(self.school, *SEPT)[0]
        lines = {(l['code'], 'Dr' if l['debit'] else 'Cr'): l['debit'] or l['credit']
                 for l in entry['lines']}
        self.assertEqual(lines, {('6000', 'Dr'): Decimal('500000'),
                                 ('1000', 'Cr'): Decimal('490000.00'),
                                 ('2200', 'Cr'): Decimal('10000.00')})

    def test_the_journal_clears_the_liability_when_remitted(self):
        self._expense(500000, self.contract, vendor=self.company)
        tax.record_remittance(self.school, Expense.FEDERAL, *SEPT, paid_on=date(2026, 9, 30))
        entries = export.journal(self.school, *SEPT)
        debit, credit = export.totals(entries)
        self.assertEqual(debit, credit)
        balance = {r['code']: r['debit'] - r['credit'] for r in export.trial_balance(entries)}
        self.assertEqual(balance['2200'], Decimal('0.00'))
        self.assertEqual(balance['1000'], Decimal('-500000.00'))

    def test_an_expense_without_tax_still_posts_two_lines(self):
        self._expense(500000, None, vendor=self.company)
        self.assertEqual(len(export.journal(self.school, *SEPT)[0]['lines']), 2)


@override_settings(ROOT_DOMAIN='localhost', ALLOWED_HOSTS=['*'])
class TaxPageTests(TaxBase):

    def setUp(self):
        super().setUp()
        self.admin = make_admin(self.school)
        set_current_tenant(self.school)
        self.client.force_login(self.admin)

    def _post(self, path, data, **kwargs):
        return self.client.post(path, data, HTTP_HOST=self.host(self.school), **kwargs)

    def _get(self, path):
        return self.client.get(path, HTTP_HOST=self.host(self.school))

    def test_recording_an_expense_with_withholding_tax(self):
        response = self._post('/finance/admin/expenses/', {
            'action': 'add', 'description': 'Rewire the lab', 'category_id': self.category.id,
            'amount': '500000', 'vat_amount': '0', 'vendor_id': self.company.id,
            'wht_rate_id': self.contract.id, 'method': 'TRANSFER', 'already_paid': '1'},
            follow=True)
        expense = Expense.objects.get()
        self.assertEqual(expense.wht_amount, Decimal('10000.00'))
        self.assertContains(response, 'Pay the supplier NGN 490,000.00')

    def test_vat_bigger_than_the_bill_is_refused(self):
        response = self._post('/finance/admin/expenses/', {
            'action': 'add', 'description': 'x', 'category_id': self.category.id,
            'amount': '1000', 'vat_amount': '1000'}, follow=True)
        self.assertContains(response, 'VAT should be part of the amount')
        self.assertFalse(Expense.objects.exists())

    def test_another_schools_category_cannot_be_used(self):
        other = make_school('beta')
        set_current_tenant(other)
        theirs = ExpenseCategory.objects.create(tenant=other, name='Theirs')
        set_current_tenant(self.school)
        self._post('/finance/admin/expenses/', {
            'action': 'add', 'description': 'x', 'category_id': theirs.id, 'amount': '1000'})
        self.assertFalse(Expense.objects.filter(tenant=self.school).exists())

    def test_the_tax_page_defaults_to_last_month(self):
        response = self._get('/finance/admin/tax/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['first'].day, 1)

    def test_paying_over_from_the_page(self):
        self._expense(500000, self.contract, vendor=self.company)
        self._post('/finance/admin/tax/', {'action': 'remit', 'authority': 'FEDERAL',
                                           'month': '2026-09', 'paid_on': '2026-10-14',
                                           'reference': 'NRS-0091', 'method': 'TRANSFER'})
        remittance = WhtRemittance.objects.get()
        self.assertEqual((remittance.amount, remittance.reference, remittance.paid_on),
                         (Decimal('10000.00'), 'NRS-0091', date(2026, 10, 14)))
        self.assertEqual(remittance.recorded_by, self.admin)

    def test_editing_the_rate_table(self):
        data = {'action': 'save_rates', 'month': '2026-09',
                'new_name': 'Directors fees', 'new_company': '10', 'new_individual': '10'}
        for rate in WhtRate.objects.all():
            data[f'company_{rate.id}'] = str(rate.company_rate)
            data[f'individual_{rate.id}'] = str(rate.individual_rate)
            data[f'active_{rate.id}'] = '1'
        data[f'company_{self.goods.id}'] = '3'
        self._post('/finance/admin/tax/', data)
        self.goods.refresh_from_db()
        self.assertEqual(self.goods.company_rate, Decimal('3.00'))
        self.assertTrue(WhtRate.objects.filter(name='Directors fees').exists())

    def test_a_rate_over_100_is_refused(self):
        data = {'action': 'save_rates', f'company_{self.goods.id}': '150',
                f'individual_{self.goods.id}': '2', f'active_{self.goods.id}': '1'}
        response = self._post('/finance/admin/tax/', data, follow=True)
        self.assertContains(response, 'between 0 and 100')
        self.goods.refresh_from_db()
        self.assertEqual(self.goods.company_rate, Decimal('2.00'))

    def test_downloading_the_schedule(self):
        self._expense(500000, self.contract, vendor=self.company)
        response = self._get('/finance/admin/tax/schedule.csv?month=2026-09&authority=FEDERAL')
        self.assertIn('wht-schedule-nrs-2026-09.csv', response['Content-Disposition'])
        self.assertIn(b'Hilltop Electricals Ltd', response.content)

    def test_saving_supplier_tax_details(self):
        self._post('/finance/admin/vendors/', {'action': 'tax', 'vendor_id': self.person.id,
                                               'payee_type': 'INDIVIDUAL', 'tin': '9988',
                                               'wht_exempt': '1'})
        self.person.refresh_from_db()
        self.assertEqual((self.person.tin, self.person.wht_exempt), ('9988', True))

    def test_a_parent_cannot_reach_the_tax_pages(self):
        parent = User.objects.create_user(username='par1', password='x',
                                          tenant=self.school, is_parent=True)
        self.client.force_login(parent)
        for path in ('/finance/admin/tax/', '/finance/admin/tax/schedule.csv'):
            self.assertNotEqual(self._get(path).status_code, 200, path)
