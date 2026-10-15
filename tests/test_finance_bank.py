"""
Bank reconciliation: reading the bank's statement, matching each line to what
the school recorded, and showing what is left over.
"""
import io
from datetime import date, datetime
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.utils import timezone

from finance.models import (BankAccount, BankLine, Expense, ExpenseCategory, Payment,
                            Refund, Wallet)
from finance.services import reconcile, refunds as refund_service
from tenants.middleware import set_current_tenant
from users.models import User

from .factories import TenantTestCase, make_admin, make_school, make_student


def _csv(text, name='statement.csv'):
    return SimpleUploadedFile(name, text.encode('utf-8'), content_type='text/csv')


def _aware(day):
    return timezone.make_aware(datetime.combine(day, datetime.min.time().replace(hour=10)))


class ReadingStatementsTests(TenantTestCase):

    def test_credit_and_debit_columns(self):
        lines = reconcile.read_statement(_csv(
            'Trans Date,Narration,Reference,Debit,Credit,Balance\n'
            '14/09/2026,TRF FROM ADE BELLO,FT2609141,,"50,000.00","1,250,000.00"\n'
            '15/09/2026,DIESEL SUPPLY,FT2609152,"185,000.00",,"1,065,000.00"\n'))
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]['date'], date(2026, 9, 14))
        self.assertEqual(lines[0]['amount'], Decimal('50000.00'))
        self.assertEqual(lines[0]['balance'], Decimal('1250000.00'))
        self.assertEqual(lines[1]['amount'], Decimal('-185000.00'))
        self.assertEqual(lines[1]['description'], 'DIESEL SUPPLY')

    def test_a_single_signed_amount_column(self):
        lines = reconcile.read_statement(_csv(
            'Date,Description,Amount\n'
            '2026-09-14,Fees,50000\n'
            '2026-09-15,Bank charge,-52.50\n'))
        self.assertEqual([l['amount'] for l in lines],
                         [Decimal('50000.00'), Decimal('-52.50')])

    def test_letterhead_above_the_headings_is_skipped(self):
        lines = reconcile.read_statement(_csv(
            'GUARANTY TRUST BANK PLC\n'
            'Account Statement\n'
            'Account Number:,0123456789\n'
            '\n'
            'Value Date,Remarks,Withdrawals,Lodgements,Balance\n'
            '14-Sep-2026,Opening Balance,,,"1,200,000.00"\n'
            '14-Sep-2026,NIP TRF FROM CHIOMA,,"75,000.00","1,275,000.00"\n'
            ',Total,,"75,000.00",\n'))
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]['amount'], Decimal('75000.00'))
        self.assertEqual(lines[0]['date'], date(2026, 9, 14))

    def test_bracketed_and_dr_amounts_are_money_out(self):
        self.assertEqual(reconcile.parse_amount('(1,500.00)'), Decimal('-1500.00'))
        self.assertEqual(reconcile.parse_amount('1,500.00 DR'), Decimal('-1500.00'))
        self.assertEqual(reconcile.parse_amount('₦2,000'), Decimal('2000.00'))
        self.assertIsNone(reconcile.parse_amount(''))

    def test_common_date_formats(self):
        for raw in ('14/09/2026', '2026-09-14', '14-Sep-2026', '14-Sep-26', '14 Sep 2026'):
            self.assertEqual(reconcile.parse_date(raw), date(2026, 9, 14), raw)

    def test_an_excel_statement(self):
        from openpyxl import Workbook
        book = Workbook()
        sheet = book.active
        sheet.append(['Transaction Date', 'Details', 'Money In', 'Money Out', 'Balance'])
        sheet.append([datetime(2026, 9, 14), 'POS SETTLEMENT', 30000, None, 900000])
        buffer = io.BytesIO(); book.save(buffer)
        upload = SimpleUploadedFile('statement.xlsx', buffer.getvalue())
        lines = reconcile.read_statement(upload)
        self.assertEqual(lines[0]['date'], date(2026, 9, 14))
        self.assertEqual(lines[0]['amount'], Decimal('30000.00'))

    def test_a_file_that_is_not_a_statement_is_refused(self):
        with self.assertRaises(reconcile.StatementError):
            reconcile.read_statement(_csv('Name,Class\nAda,JSS1\n'))

    def test_headings_with_no_transactions_are_refused(self):
        with self.assertRaises(reconcile.StatementError):
            reconcile.read_statement(_csv('Date,Description,Amount\n'))


class MatchingTests(TenantTestCase):

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha')
        set_current_tenant(self.school)
        self.account = BankAccount.objects.create(tenant=self.school, name='Fees account',
                                                  bank_name='GTBank', account_number='0123456789')
        self.student = make_student(self.school, 'STU001', full_name='Ade Bello')
        self.wallet = Wallet.objects.create(tenant=self.school, student=self.student)
        self.category = ExpenseCategory.objects.create(tenant=self.school, name='Diesel')

    def _payment(self, amount, day, reference, method='BANK_TRANSFER', status=Payment.APPROVED):
        return Payment.objects.create(tenant=self.school, wallet=self.wallet,
                                      amount=Decimal(str(amount)), reference=reference,
                                      method=method, status=status, created_at=_aware(day))

    def _import(self, text):
        return reconcile.import_statement(self.school, self.account, _csv(text))

    def test_a_payment_matches_on_amount_and_date(self):
        payment = self._payment(50000, date(2026, 9, 13), 'PAY-AAA111')
        _, added, _, matched = self._import('Date,Description,Amount\n14/09/2026,TRF ADE,50000\n')
        self.assertEqual((added, matched), (1, 1))
        line = BankLine.objects.get()
        self.assertEqual(line.payment, payment)
        self.assertTrue(line.matched_automatically)

    def test_a_reference_on_the_line_beats_the_date(self):
        far = self._payment(50000, date(2026, 8, 1), 'PAY-REF778899')   # a month earlier
        self._payment(50000, date(2026, 9, 14), 'PAY-OTHER11')
        self._import('Date,Description,Amount\n14/09/2026,TRF PAY-REF778899 ADE,50000\n')
        self.assertEqual(BankLine.objects.get().payment, far)

    def test_two_equal_payments_are_never_guessed_between(self):
        self._payment(50000, date(2026, 9, 14), 'PAY-ONE111')
        self._payment(50000, date(2026, 9, 14), 'PAY-TWO222')
        _, _, _, matched = self._import('Date,Description,Amount\n14/09/2026,TRF,50000\n')
        self.assertEqual(matched, 0)
        self.assertEqual(BankLine.objects.get().status, BankLine.UNMATCHED)

    def test_a_payment_too_far_away_in_time_is_not_matched(self):
        self._payment(50000, date(2026, 9, 1), 'PAY-OLD111')
        _, _, _, matched = self._import('Date,Description,Amount\n14/09/2026,TRF,50000\n')
        self.assertEqual(matched, 0)

    def test_cash_payments_are_never_matched_to_the_bank(self):
        self._payment(50000, date(2026, 9, 14), 'PAY-CASH11', method='CASH')
        _, _, _, matched = self._import('Date,Description,Amount\n14/09/2026,TRF,50000\n')
        self.assertEqual(matched, 0)

    def test_a_rejected_payment_is_never_matched(self):
        self._payment(50000, date(2026, 9, 14), 'PAY-REJ111', status=Payment.REJECTED)
        _, _, _, matched = self._import('Date,Description,Amount\n14/09/2026,TRF,50000\n')
        self.assertEqual(matched, 0)

    def test_a_payment_matches_only_one_line(self):
        self._payment(50000, date(2026, 9, 14), 'PAY-ONLY11')
        _, _, _, matched = self._import('Date,Description,Amount\n'
                                        '14/09/2026,TRF A,50000\n15/09/2026,TRF B,50000\n')
        self.assertEqual(matched, 1)

    def test_money_out_matches_a_paid_expense(self):
        expense = Expense.objects.create(tenant=self.school, category=self.category,
                                         description='Diesel', amount=Decimal('185000'),
                                         method=Expense.TRANSFER, status=Expense.PAID,
                                         spent_on=date(2026, 9, 15))
        self._import('Date,Description,Amount\n15/09/2026,DIESEL,-185000\n')
        self.assertEqual(BankLine.objects.get().expense, expense)

    def test_an_unpaid_or_petty_cash_expense_is_not_matched(self):
        for status, method in ((Expense.PENDING, Expense.TRANSFER),
                               (Expense.PAID, Expense.PETTY_CASH)):
            Expense.objects.create(tenant=self.school, category=self.category,
                                   description='x', amount=Decimal('9000'), method=method,
                                   status=status, spent_on=date(2026, 9, 15))
        _, _, _, matched = self._import('Date,Description,Amount\n15/09/2026,X,-9000\n')
        self.assertEqual(matched, 0)

    def test_money_out_matches_a_paid_refund(self):
        refund = refund_service.request_refund(self.school, self.student, 12000)
        refund_service.approve(refund)
        refund_service.mark_paid(refund, paid_on=date(2026, 9, 16))
        self._import('Date,Description,Amount\n16/09/2026,REFUND ADE,-12000\n')
        self.assertEqual(BankLine.objects.get().refund, refund)

    def test_importing_the_same_statement_twice_adds_nothing(self):
        text = 'Date,Description,Amount\n14/09/2026,TRF,50000\n15/09/2026,CHG,-50\n'
        self._import(text)
        _, added, skipped, _ = self._import(text)
        self.assertEqual((added, skipped), (0, 2))
        self.assertEqual(BankLine.objects.count(), 2)

    def test_matching_by_hand_and_undoing_it(self):
        self._payment(50000, date(2026, 9, 14), 'PAY-ONE111')
        second = self._payment(50000, date(2026, 9, 14), 'PAY-TWO222')
        self._import('Date,Description,Amount\n14/09/2026,TRF,50000\n')
        line = BankLine.objects.get()
        self.assertEqual(len(reconcile.options_for(line)), 2)
        reconcile.match(line, 'payment', second)
        self.assertEqual(line.status, BankLine.MATCHED)
        self.assertFalse(line.matched_automatically)
        reconcile.unmatch(line)
        line.refresh_from_db()
        self.assertEqual(line.status, BankLine.UNMATCHED)
        self.assertIsNone(line.payment)

    def test_a_record_cannot_be_matched_to_two_lines(self):
        payment = self._payment(50000, date(2026, 9, 14), 'PAY-ONLY11')
        self._import('Date,Description,Amount\n14/09/2026,A,50000\n15/09/2026,B,50000\n')
        spare = BankLine.objects.get(status=BankLine.UNMATCHED)
        with self.assertRaises(ValueError):
            reconcile.match(spare, 'payment', payment)

    def test_setting_a_line_aside(self):
        self._import('Date,Description,Amount\n14/09/2026,SMS ALERT CHARGE,-4\n')
        line = BankLine.objects.get()
        reconcile.ignore(line, 'Bank charge')
        self.assertEqual(line.status, BankLine.IGNORED)
        self.assertEqual(line.note, 'Bank charge')

    def test_recorded_payments_missing_from_the_bank_are_listed(self):
        self._payment(50000, date(2026, 9, 14), 'PAY-INBANK1')
        missing = self._payment(70000, date(2026, 9, 15), 'PAY-MISSING')
        self._payment(20000, date(2026, 9, 15), 'PAY-CASHONLY', method='CASH')
        self._import('Date,Description,Amount\n14/09/2026,TRF,50000\n16/09/2026,CHG,-10\n')
        left = reconcile.recorded_not_banked(self.school, date(2026, 9, 14), date(2026, 9, 16))
        self.assertEqual(left['payments'], [missing])

    def test_the_summary(self):
        self._payment(50000, date(2026, 9, 14), 'PAY-AAA111')
        self._import('Date,Description,Amount,Balance\n'
                     '14/09/2026,TRF,50000,1050000\n'
                     '15/09/2026,UNKNOWN,30000,1080000\n'
                     '16/09/2026,CHG,-50,1079950\n')
        result = reconcile.summary(self.account)
        self.assertEqual(result['lines'], 3)
        self.assertEqual(result['matched'], 1)
        self.assertEqual(result['unmatched'], 2)
        self.assertEqual(result['money_in'], Decimal('80000.00'))
        self.assertEqual(result['money_out'], Decimal('50.00'))
        self.assertEqual(result['unmatched_in'], Decimal('30000.00'))
        self.assertEqual(result['bank_balance'], Decimal('1079950.00'))

    def test_matching_never_reaches_another_school(self):
        other = make_school('beta')
        set_current_tenant(other)
        theirs = make_student(other, 'STU001')
        their_wallet = Wallet.objects.create(tenant=other, student=theirs)
        Payment.objects.create(tenant=other, wallet=their_wallet, amount=Decimal('50000'),
                               reference='PAY-BETA111', method='BANK_TRANSFER',
                               status=Payment.APPROVED, created_at=_aware(date(2026, 9, 14)))
        set_current_tenant(None)   # e.g. a background job with no request
        _, _, _, matched = self._import(
            'Date,Description,Amount\n14/09/2026,TRF PAY-BETA111,50000\n')
        self.assertEqual(matched, 0)
        line = BankLine.objects.get(tenant=self.school)
        with self.assertRaises(ValueError):
            reconcile.match(line, 'payment', Payment.objects.get(tenant=other))


@override_settings(ROOT_DOMAIN='localhost', ALLOWED_HOSTS=['*'])
class BankPageTests(TenantTestCase):

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha')
        self.admin = make_admin(self.school)
        set_current_tenant(self.school)
        self.client.force_login(self.admin)

    def _post(self, data, **kwargs):
        return self.client.post('/finance/admin/bank/', data,
                                HTTP_HOST=self.host(self.school), **kwargs)

    def test_with_no_account_the_page_asks_for_one(self):
        response = self.client.get('/finance/admin/bank/', HTTP_HOST=self.host(self.school))
        self.assertContains(response, "Add the school's bank account")

    def test_adding_an_account_then_importing(self):
        self._post({'action': 'add_account', 'name': 'Fees account',
                    'bank_name': 'GTBank', 'account_number': '0123456789'})
        account = BankAccount.objects.get()
        response = self._post({'action': 'import', 'account_id': account.id,
                               'statement': _csv('Date,Description,Amount\n'
                                                 '14/09/2026,TRF,50000\n')}, follow=True)
        self.assertContains(response, '1 line(s) imported')
        self.assertEqual(BankLine.objects.count(), 1)

    def test_the_same_account_cannot_be_added_twice(self):
        BankAccount.objects.create(tenant=self.school, name='Fees', bank_name='GTBank',
                                   account_number='0123456789')
        response = self._post({'action': 'add_account', 'name': 'Again',
                               'bank_name': 'gtbank', 'account_number': '0123456789'},
                              follow=True)
        self.assertContains(response, 'already set up')
        self.assertEqual(BankAccount.objects.count(), 1)

    def test_a_pdf_statement_is_explained_not_crashed_on(self):
        account = BankAccount.objects.create(tenant=self.school, name='Fees',
                                             bank_name='GTBank', account_number='1')
        response = self._post({'action': 'import', 'account_id': account.id,
                               'statement': SimpleUploadedFile('s.pdf', b'%PDF-1.4')},
                              follow=True)
        self.assertContains(response, 'cannot be read')
        self.assertFalse(BankLine.objects.exists())

    def test_a_bad_file_shows_why(self):
        account = BankAccount.objects.create(tenant=self.school, name='Fees',
                                             bank_name='GTBank', account_number='1')
        response = self._post({'action': 'import', 'account_id': account.id,
                               'statement': _csv('Name,Class\nAda,JSS1\n')}, follow=True)
        self.assertContains(response, 'column headings')

    def test_matching_and_setting_aside_from_the_page(self):
        account = BankAccount.objects.create(tenant=self.school, name='Fees',
                                             bank_name='GTBank', account_number='1')
        student = make_student(self.school, 'STU001')
        wallet = Wallet.objects.create(tenant=self.school, student=student)
        payment = Payment.objects.create(tenant=self.school, wallet=wallet,
                                         amount=Decimal('50000'), reference='PAY-A1',
                                         method='BANK_TRANSFER', status=Payment.APPROVED)
        Payment.objects.create(tenant=self.school, wallet=wallet, amount=Decimal('50000'),
                               reference='PAY-A2', method='BANK_TRANSFER',
                               status=Payment.APPROVED)
        today = timezone.now().date().strftime('%d/%m/%Y')
        reconcile.import_statement(self.school, account, _csv(
            f'Date,Description,Amount\n{today},TRF,50000\n{today},CHG,-25\n'))
        credit = BankLine.objects.get(amount__gt=0)
        charge = BankLine.objects.get(amount__lt=0)

        page = self.client.get(f'/finance/admin/bank/?account={account.id}',
                               HTTP_HOST=self.host(self.school))
        self.assertContains(page, 'PAY-A1')

        self._post({'action': 'match', 'account_id': account.id, 'line_id': credit.id,
                    'record': f'payment:{payment.id}'})
        self._post({'action': 'ignore', 'account_id': account.id, 'line_id': charge.id,
                    'note': 'Bank charge'})
        credit.refresh_from_db(); charge.refresh_from_db()
        self.assertEqual(credit.payment, payment)
        self.assertEqual(credit.matched_by, self.admin)
        self.assertEqual(charge.status, BankLine.IGNORED)

    def test_a_parent_cannot_reach_the_page(self):
        parent = User.objects.create_user(username='par1', password='x',
                                          tenant=self.school, is_parent=True)
        self.client.force_login(parent)
        response = self.client.get('/finance/admin/bank/', HTTP_HOST=self.host(self.school))
        self.assertNotEqual(response.status_code, 200)
