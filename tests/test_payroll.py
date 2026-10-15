"""
Payroll: PAYE under the Nigeria Tax Act 2025, pension, NHF, unpaid leave,
loans, bonus runs, the prepare → approve → release chain, bank file and
schedules, payslip emails and staff self-service.
"""
from datetime import date
from decimal import Decimal

from django.core import mail
from django.test import override_settings

from payroll import engine, runs
from payroll.models import PayDetailsRequest, PayrollRun, PayrollSettings, Payslip, StaffLoan, StaffPayroll
from safety.models import StaffLeave
from tenants.middleware import set_current_tenant
from users.models import Staff

from .factories import TenantTestCase, make_admin, make_school, make_teacher

D = Decimal


@override_settings(RUN_TASKS_EAGERLY=True)
class Base(TenantTestCase):

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha')
        self.admin = make_admin(self.school)
        self.head = make_admin(self.school, username='alphahead')
        self.bursar = make_teacher(self.school, username='alphab', full_name='Bursar')
        self.t_user = make_teacher(self.school, username='alphat', full_name='Mrs Ade')
        self.t_user.email = 'ade@example.com'
        self.t_user.save()
        self.low_user = make_teacher(self.school, username='alphal', full_name='Mr Low')
        set_current_tenant(self.school)
        Staff.objects.filter(user=self.bursar).update(role='ACCOUNT')
        self.teacher = Staff.objects.get(user=self.t_user)
        self.pr = StaffPayroll.objects.create(
            tenant=self.school, staff=self.teacher, basic_salary=D('200000'), housing=D('50000'), transport=D('30000'),
            other_allowances=D('20000'), annual_rent=D('600000'), bank_name='GTBank', account_no='0123456789',
            account_name='ADE BOLA', tax_id='12345678-0001', pension_pin='PEN100200300', pfa_name='Stanbic IBTC')
        self.low = StaffPayroll.objects.create(tenant=self.school, staff=Staff.objects.get(user=self.low_user),
                                               basic_salary=D('70000'), bank_name='Opay', account_no='9000000001')

    def make_run(self, kind='MONTHLY', month=10, year=2026, user=None, **kw):
        return runs.create(self.school, kind, month, year, user or self.bursar, **kw)

    def slip(self, run, pr=None):
        return Payslip.objects.get(run=run, staff_payroll=pr or self.pr)

    def _get(self, path, **kw):
        return self.client.get(path, HTTP_HOST=self.host(self.school), **kw)

    def _post(self, path, data=None, **kw):
        return self.client.post(path, data or {}, HTTP_HOST=self.host(self.school), **kw)


class TaxTests(Base):

    def test_bands(self):
        self.assertEqual(engine.annual_tax(D('800000'))[0], D('0.00'))
        self.assertEqual(engine.annual_tax(D('1000000'))[0], D('30000.00'))
        self.assertEqual(engine.annual_tax(D('12000000'))[0], D('1950000.00'))
        self.assertEqual(engine.annual_tax(D('60000000'))[0], D('12930000.00'))
        self.assertEqual(engine.rent_relief(D('600000')), D('120000.00'))
        self.assertEqual(engine.rent_relief(D('5000000')), D('500000'))

    def test_monthly_payslip(self):
        s = self.slip(self.make_run())
        self.assertEqual(s.gross, D('300000.00'))
        self.assertEqual((s.pension_employee, s.pension_employer), (D('22400.00'), D('28000.00')))   # 8% / 10% of 280k
        self.assertEqual(s.nhf, D('0.00'))                                                            # not agreed
        # Yearly chargeable: 3,600,000 − 268,800 pension − 120,000 rent relief = 3,211,200
        self.assertEqual(s.workings['chargeable'], 3211200.0)
        # 0% on 800k + 15% on 2.2m + 18% on 211,200 = 368,016 a year
        self.assertEqual(s.paye, D('30668.00'))
        self.assertEqual(s.net, D('246932.00'))

    def test_minimum_wage_is_tax_free_and_nhf_needs_consent(self):
        run = self.make_run()
        low = self.slip(run, self.low)
        self.assertEqual(low.paye, D('0.00'))
        self.assertIn('minimum wage', low.workings['exempt'])
        self.pr.pays_nhf = True
        self.pr.save()
        runs.build(run)
        self.assertEqual(self.slip(run).nhf, D('5000.00'))                                            # 2.5% of basic

    def test_unpaid_leave_comes_off_pay(self):
        StaffLeave.objects.create(tenant=self.school, staff=self.teacher, leave_type='UNPAID', status='APPROVED',
                                  start_date=date(2026, 10, 5), end_date=date(2026, 10, 6))
        s = self.slip(self.make_run())
        self.assertEqual(s.unpaid_leave, engine.money(D('300000') * 2 / 22))                          # 22 working days
        self.assertEqual(s.gross, D('300000.00') - s.unpaid_leave)

    def test_bonus_is_taxed_at_the_margin(self):
        s = self.slip(self.make_run('BONUS', bonus_percent=50, title='Christmas bonus'))
        self.assertEqual((s.bonus, s.pension_employee), (D('100000.00'), D('0.00')))
        self.assertEqual(s.paye, D('18000.00'))                                                       # 18% band
        self.assertEqual(s.net, D('82000.00'))
        thirteenth = self.make_run('13TH', month=12)
        self.assertEqual(thirteenth.bonus_percent, D('100'))

    def test_extra_pay_and_deductions(self):
        run = self.make_run()
        s = runs.adjust(self.slip(run), extra_pay='10,000', other_deductions='2000', note='Saturday lessons')
        self.assertEqual(s.gross, D('310000.00'))
        self.assertEqual(s.other_deductions, D('2000.00'))
        self.assertEqual(s.paye, D('32468.00'))           # 18% of 10,000 more, monthly
        runs.build(run)
        self.assertEqual(self.slip(run).extra_pay, D('10000.00'))       # kept when recalculated


class LoanTests(Base):

    def test_loan_is_repaid_over_two_months(self):
        StaffLoan.objects.create(tenant=self.school, staff_payroll=self.pr, amount=D('50000'), balance=D('50000'),
                                 monthly_repayment=D('30000'), start_month=10, start_year=2026)
        oct_run = self.make_run()
        self.assertEqual(self.slip(oct_run).loan_repayment, D('30000.00'))
        for step in (runs.submit, runs.approve, runs.release):
            step(oct_run, self.bursar if step is runs.submit else self.admin)
        loan = StaffLoan.objects.get()
        self.assertEqual((loan.balance, loan.active), (D('20000.00'), True))
        nov = self.make_run(month=11)
        self.assertEqual(self.slip(nov).loan_repayment, D('20000.00'))
        runs.submit(nov, self.bursar)
        runs.approve(nov, self.admin)
        runs.release(nov, self.admin)
        loan.refresh_from_db()
        self.assertEqual((loan.balance, loan.active), (D('0.00'), False))
        self.assertEqual(loan.repayments.count(), 2)

    def test_loan_not_started_yet(self):
        StaffLoan.objects.create(tenant=self.school, staff_payroll=self.pr, amount=D('50000'), balance=D('50000'),
                                 monthly_repayment=D('30000'), start_month=12, start_year=2026)
        self.assertEqual(self.slip(self.make_run()).loan_repayment, D('0.00'))


class ApprovalTests(Base):

    def test_three_hands(self):
        run = self.make_run()
        with self.assertRaises(runs.RunError):
            runs.approve(run, self.admin)                       # not submitted yet
        runs.submit(run, self.bursar)
        with self.assertRaises(runs.RunError):
            runs.approve(run, self.bursar)                      # bursar can't approve
        with self.assertRaises(runs.RunError):
            runs.release(run, self.admin)                       # not approved yet
        runs.approve(run, self.admin)
        with self.assertRaises(runs.RunError):
            runs.build(run)                                     # frozen once submitted
        runs.release(run, self.admin)
        run.refresh_from_db()
        self.assertEqual(run.status, PayrollRun.PAID)
        self.assertEqual(mail.outbox[-1].to, ['ade@example.com'])
        self.assertIn('246,932.00', mail.outbox[-1].alternatives[0][0])
        with self.assertRaises(runs.RunError):
            runs.cancel(run, self.admin)

    def test_preparer_cannot_approve_own_run(self):
        run = self.make_run(user=self.admin)
        runs.submit(run, self.admin)
        with self.assertRaises(runs.RunError):
            runs.approve(run, self.admin)
        runs.approve(run, self.head)
        PayrollSettings.objects.filter(tenant=self.school).update(separate_approver=False)
        runs.release(run, self.admin)

    def test_send_back_and_duplicates(self):
        run = self.make_run()
        runs.submit(run, self.bursar)
        runs.send_back(run, self.admin, 'Check Mrs Ade\'s allowance')
        run.refresh_from_db()
        self.assertEqual((run.status, run.note), ('DRAFT', "Check Mrs Ade's allowance"))
        with self.assertRaises(runs.RunError):
            self.make_run()                                          # one salary run a month
        self.make_run('BONUS', bonus_percent=10)                     # but bonus runs are fine

    def test_pages_bank_file_and_schedules(self):
        self.client.force_login(self.bursar)
        r = self._post('/payroll/runs/', {'kind': 'MONTHLY', 'month': '10', 'year': '2026'})
        run = PayrollRun.objects.get()
        self.assertRedirects(r, f'/payroll/runs/{run.pk}/', fetch_redirect_response=False)
        self.assertContains(self._get(f'/payroll/runs/{run.pk}/'), 'Mrs Ade')
        self.assertEqual(self._get(f'/payroll/runs/{run.pk}/download/bank/').status_code, 302)   # not approved
        self._post(f'/payroll/runs/{run.pk}/', {'action': 'submit'})
        self.client.force_login(self.admin)
        self._post(f'/payroll/runs/{run.pk}/', {'action': 'approve'})
        bank = self._get(f'/payroll/runs/{run.pk}/download/bank/?export=csv').content.decode('utf-8-sig')
        self.assertIn('0123456789', bank)
        self.assertIn('246932.00', bank)
        self.assertIn('Salary Oct 2026', bank)
        pension = self._get(f'/payroll/runs/{run.pk}/download/pension/?export=csv').content.decode('utf-8-sig')
        self.assertIn('PEN100200300', pension)
        self.assertIn('Stanbic IBTC', pension)
        paye = self._get(f'/payroll/runs/{run.pk}/download/paye/?export=csv').content.decode('utf-8-sig')
        self.assertIn('12345678-0001', paye)
        page = self._get(f'/payroll/runs/{run.pk}/')
        self.assertContains(page, 'Mr Low: pension is deducted but there is no pension PIN')
        self.assertNotContains(page, 'no Tax ID')                                          # Mr Low pays no tax
        self.client.force_login(self.t_user)
        self.assertEqual(self._get('/payroll/runs/').status_code, 302)


class SelfServiceTests(Base):

    def test_my_payslips_and_details_change(self):
        run = self.make_run()
        self.client.force_login(self.t_user)
        self.assertNotContains(self._get('/payroll/me/'), str(run))                      # not paid yet
        slip = self.slip(run)
        self.assertEqual(self._get(f'/payroll/payslips/{slip.pk}/').status_code, 302)
        runs.submit(run, self.bursar)
        runs.approve(run, self.admin)
        runs.release(run, self.admin)
        self.assertContains(self._get('/payroll/me/'), '246,932.00')
        self.assertContains(self._get(f'/payroll/payslips/{slip.pk}/'), 'Net pay')
        other = self.slip(run, self.low)
        self.assertEqual(self._get(f'/payroll/payslips/{other.pk}/').status_code, 302)
        self._post('/payroll/me/', {'account_no': '1112223334', 'bank_name': 'GTBank'})
        req = PayDetailsRequest.objects.get()
        self.assertEqual(req.changes, {'account_no': '1112223334'})
        self.pr.refresh_from_db()
        self.assertEqual(self.pr.account_no, '0123456789')                               # not until approved
        self.client.force_login(self.bursar)
        self._post('/payroll/requests/', {'id': req.pk, 'action': 'approve'})
        self.pr.refresh_from_db()
        self.assertEqual(self.pr.account_no, '1112223334')

    def test_settings_are_admin_only(self):
        self.client.force_login(self.bursar)
        self.assertEqual(self._get('/payroll/settings/').status_code, 302)
        self.client.force_login(self.admin)
        self._post('/payroll/settings/', {'pension_employee_rate': '8', 'pension_employer_rate': '12', 'nhf_rate': '2.5',
                                          'minimum_wage': '70000', 'separate_approver': '1'})
        self.assertEqual(PayrollSettings.objects.get(tenant=self.school).pension_employer_rate, D('12.00'))
        self.assertEqual(self.slip(self.make_run()).pension_employer, D('33600.00'))
