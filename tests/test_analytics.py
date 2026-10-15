"""
Analytics: the command dashboard, fee collection and debt ageing, enrolment
trend and forecast, leavers, demographics, cohort comparison, click-through
student lists with Excel/CSV export, targets, activity, board pack and the
summary email.
"""
from datetime import date, timedelta
from decimal import Decimal

from django.core import mail
from django.test import override_settings
from django.utils import timezone

from academics.models import AcademicSession, Term
from analytics import metrics, reports
from analytics.models import KPITarget, ReportSubscription
from finance.models import ChildBilling, ParentBilling, Payment, Wallet
from results.models import TermResult
from tenants.middleware import set_current_tenant
from users.models import Attendance, Staff, Student, Subject

from .factories import TenantTestCase, make_admin, make_class, make_school, make_student, make_teacher


@override_settings(RUN_TASKS_EAGERLY=True)
class Base(TenantTestCase):

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha')
        self.admin = make_admin(self.school)
        self.teacher = make_teacher(self.school, username='alphat')
        self.bursar = make_teacher(self.school, username='alphab', full_name='Bursar')
        self.j1a, self.j1b, self.s1 = (make_class(self.school, n) for n in ('JSS 1A', 'JSS 1B', 'SS 1A'))
        set_current_tenant(self.school)
        Staff.objects.filter(user=self.bursar).update(role='ACCOUNT')
        self.today = timezone.localdate()
        self.session = AcademicSession.objects.create(tenant=self.school, name='2026/2027', is_active=True)
        self.term = Term.objects.create(tenant=self.school, session=self.session, name='1st Term', is_active=True,
                                        start_date=self.today - timedelta(days=40))
        self.ada = make_student(self.school, 'A1', full_name='Ada Obi', school_class=self.j1a, parent_email='a@x.com')
        self.ben = make_student(self.school, 'A2', full_name='Ben Eze', school_class=self.j1a, parent_email='b@x.com')
        self.chi = make_student(self.school, 'A3', full_name='Chi Musa', school_class=self.j1b, parent_email='c@x.com')
        self.dan = make_student(self.school, 'A4', full_name='Dan Ali', school_class=self.s1, parent_email='d@x.com')
        set_current_tenant(self.school)
        Student.objects.filter(pk=self.ada.pk).update(gender='Female', date_of_birth=date(self.today.year - 11, 1, 1))
        Student.objects.filter(pk=self.ben.pk).update(gender='Male', date_of_birth=date(self.today.year - 12, 1, 1))
        Student.objects.filter(pk=self.chi.pk).update(gender='Female', date_of_birth=date(self.today.year - 14, 1, 1))
        self.maths = Subject.objects.create(tenant=self.school, name='Mathematics')

    def bill(self, student, fee, paid=0):
        wallet, _ = Wallet.objects.get_or_create(student=student, defaults={'tenant': self.school})
        pb = ParentBilling.objects.create(tenant=self.school, wallet=wallet, session=self.session, term=self.term)
        ChildBilling.objects.create(tenant=self.school, student=student, parent_billing=pb, total_fee=Decimal(fee),
                                    paid=Decimal(paid))
        return wallet

    def pay(self, wallet, amount, status='APPROVED', days_ago=0, method='CASH'):
        return Payment.objects.create(tenant=self.school, wallet=wallet, amount=Decimal(amount), status=status,
                                      method=method, reference=f'R{Payment.objects.count()}-{amount}-{days_ago}',
                                      created_at=timezone.now() - timedelta(days=days_ago))

    def mark(self, student, status, day=None):
        return Attendance.objects.create(tenant=self.school, student=student, school_class=student.class_assigned,
                                         date=day or self.today, status=status)

    def result(self, student, score, session=None, cls=None, subject=None, term=None):
        return TermResult.objects.create(tenant=self.school, student=student, class_assigned=cls or student.class_assigned,
                                         subject=subject or self.maths, session=session or self.session,
                                         term=term or self.term, exam_score=score)

    def _get(self, path, **kw):
        return self.client.get(path, HTTP_HOST=self.host(self.school), **kw)

    def _post(self, path, data=None, **kw):
        return self.client.post(path, data or {}, HTTP_HOST=self.host(self.school), **kw)


class MetricTests(Base):

    def test_attendance_today(self):
        self.mark(self.ada, 'P')
        self.mark(self.ben, 'A')
        self.mark(self.chi, 'P')
        t = metrics.attendance_today(self.school)
        self.assertEqual((t['present'], t['absent'], t['rate']), (2, 1, 66.7))
        self.assertEqual([c.name for c in t['unmarked']], ['SS 1A'])

    def test_fees_by_class_and_ageing(self):
        wa = self.bill(self.ada, 100000, 100000)
        wb = self.bill(self.ben, 100000, 40000)
        self.bill(self.chi, 50000, 0)
        self.pay(wb, 40000, days_ago=5)
        self.pay(wa, 100000, days_ago=70)
        self.pay(wb, 999, status='PENDING')
        f = metrics.fee_position(self.school)
        self.assertEqual((f['expected'], f['paid'], f['outstanding'], f['debtors']),
                         (Decimal('250000'), Decimal('140000'), Decimal('110000'), 2))
        self.assertEqual(f['rate'], 56.0)
        rows = {r['name']: r for r in metrics.collection_by_class(self.school)}
        self.assertEqual(rows['JSS 1A']['rate'], 70.0)
        self.assertEqual(rows['JSS 1B']['debtors'], 1)
        ageing = {b['label']: b for b in metrics.debt_ageing(self.school)}
        self.assertEqual(ageing['0–30 days']['amount'], Decimal('60000'))         # Ben paid 5 days ago
        self.assertEqual(ageing['31–60 days']['never'], 1)                        # Chi never paid; term began 40 days ago
        cash = metrics.collections(self.school)
        self.assertEqual(cash['pending'], 1)
        self.assertEqual(sum(w['amount'] for w in cash['weeks']), Decimal('40000'))   # the 70-day-old one is outside

    def test_results_and_targets(self):
        self.result(self.ada, 80)
        self.result(self.ben, 40)
        r = metrics.results_summary(self.school)
        self.assertEqual((r['average'], r['pass_rate'], r['students']), (60.0, 50.0, 2))
        KPITarget.objects.create(tenant=self.school, metric='pass_rate', target=Decimal('80'))
        KPITarget.objects.create(tenant=self.school, metric='enrolment', target=Decimal('4'))
        k = {x['metric']: x for x in metrics.kpis(self.school)}
        self.assertEqual(k['pass_rate']['progress'], 62)
        self.assertFalse(k['pass_rate']['met'])
        self.assertTrue(k['enrolment']['met'])

    def test_enrolment_trend_leavers_and_forecast(self):
        Student.objects.filter(pk__in=[self.ada.pk, self.ben.pk]).update(
            created_at=timezone.now() - timedelta(days=200))
        Student.objects.filter(pk=self.dan.pk).update(created_at=timezone.now() - timedelta(days=200),
                                                       status='Withdrawn', left_on=self.today - timedelta(days=40),
                                                       status_reason='moved to Abuja', destination='Unity School')
        series, ahead = metrics.enrolment_series(self.school, 12)
        self.assertEqual(series[-1]['count'], 3)          # Ada, Ben, Chi (Dan left)
        self.assertEqual(series[-4]['count'], 3)          # Ada, Ben, Dan three months ago — Chi joined today
        r = metrics.retention(self.school, 12)
        self.assertEqual((r['withdrawn'], r['withdrawal_rate']), (1, 25.0))
        self.assertEqual(r['reasons'][0], ('Moved to abuja', 1))
        self.assertEqual(r['destinations'][0], ('Unity School', 1))
        rising = [{'month': date(2026, m, 1), 'count': 100 + 10 * m} for m in range(1, 7)]
        self.assertEqual([p['count'] for p in metrics.forecast(rising)], [170, 180, 190])

    def test_demographics(self):
        d = metrics.demographics(self.school)
        self.assertEqual((d['totals']['male'], d['totals']['female'], d['totals']['unspecified']), (1, 2, 1))
        bands = {b['band']: b['count'] for b in d['summary']}
        self.assertEqual((bands['10–12'], bands['13–15'], bands['Unknown']), (2, 1, 1))

    def test_cohort_comparison(self):
        old = AcademicSession.objects.create(tenant=self.school, name='2025/2026')
        old_term = Term.objects.create(tenant=self.school, session=old, name='1st Term')
        self.result(self.ada, 70)
        self.result(self.chi, 50)
        self.result(self.dan, 40, session=old, cls=self.j1a, term=old_term)    # Dan was in JSS 1 last year
        c = metrics.cohort(self.school, 'JSS 1', '1st Term')
        self.assertEqual([s['session'] for s in c['sessions']], ['2025/2026', '2026/2027'])
        self.assertEqual([s['average'] for s in c['sessions']], [40.0, 60.0])
        self.assertEqual(c['subjects'], ['Mathematics'])


class PageTests(Base):

    def test_who_can_see_what(self):
        self.client.force_login(self.teacher)
        self.assertEqual(self._get('/analytics/').status_code, 302)
        self.client.force_login(self.bursar)
        self.assertEqual(self._get('/analytics/fees/').status_code, 200)
        self.assertEqual(self._get('/analytics/board-pack/').status_code, 302)
        self.client.force_login(self.admin)
        for path in ('/analytics/', '/analytics/fees/', '/analytics/enrolment/', '/analytics/demographics/',
                     '/analytics/cohorts/', '/analytics/targets/', '/analytics/activity/', '/analytics/board-pack/',
                     '/analytics/emails/'):
            self.assertEqual(self._get(path).status_code, 200, path)

    def test_drill_down_and_export(self):
        self.bill(self.ada, 1000, 0)
        self.bill(self.ben, 1000, 1000)
        self.mark(self.chi, 'A')
        self.client.force_login(self.admin)
        r = self._get('/analytics/students/?owing=1')
        self.assertContains(r, 'Ada Obi')
        self.assertNotContains(r, 'Ben Eze')
        r = self._get(f'/analytics/students/?absent={self.today.isoformat()}')
        self.assertContains(r, 'Chi Musa')
        self.assertNotContains(r, 'Ada Obi')
        r = self._get(f'/analytics/students/?gender=Female&class={self.j1a.pk}')
        self.assertContains(r, 'Ada Obi')
        self.assertNotContains(r, 'Chi Musa')
        csv = self._get('/analytics/students/?owing=1&export=csv')
        self.assertEqual(csv['Content-Type'], 'text/csv; charset=utf-8')
        self.assertIn('Ada Obi', csv.content.decode('utf-8-sig'))
        xlsx = self._get('/analytics/demographics/?export=xlsx')
        self.assertTrue(xlsx['Content-Type'].startswith('application/vnd.openxmlformats'))
        self.assertTrue(xlsx.content.startswith(b'PK'))

    def test_targets_page(self):
        self.client.force_login(self.admin)
        self._post('/analytics/targets/', {'attendance': '95', 'fees': '150', 'enrolment': '500'})
        self.assertEqual(KPITarget.objects.get(metric='attendance').target, Decimal('95'))
        self.assertFalse(KPITarget.objects.filter(metric='fees').exists())      # over 100% refused
        self.assertEqual(KPITarget.objects.get(metric='enrolment').target, Decimal('500'))
        self._post('/analytics/targets/', {'attendance': '', 'enrolment': '500'})
        self.assertFalse(KPITarget.objects.filter(metric='attendance').exists())

    def test_activity_feed(self):
        self.pay(self.bill(self.ada, 1000), 5000)
        feed = [f['text'] for f in metrics.activity(self.school)]
        self.assertTrue(any('₦5,000 payment received' in t for t in feed))
        self.assertTrue(any('Ada Obi joined JSS 1A' in t for t in feed))


class EmailTests(Base):

    def test_send_now_and_schedule(self):
        self.client.force_login(self.admin)
        self._post('/analytics/emails/', {'action': 'add', 'email': 'Owner@Example.com', 'frequency': 'WEEKLY'})
        sub = ReportSubscription.objects.get()
        self.assertEqual(sub.email, 'owner@example.com')
        self._post('/analytics/emails/', {'action': 'send', 'id': sub.pk})
        self.assertEqual(mail.outbox[-1].to, ['owner@example.com'])
        self.assertIn('Students on roll', mail.outbox[-1].alternatives[0][0])
        monday = self.today - timedelta(days=self.today.weekday())
        sub.refresh_from_db()
        sub.last_sent_at = None
        sub.save()
        self.assertTrue(reports.due(sub, monday))
        self.assertFalse(reports.due(sub, monday + timedelta(days=1)))
        monthly = ReportSubscription.objects.create(tenant=self.school, email='gov@example.com', frequency='MONTHLY')
        self.assertTrue(reports.due(monthly, monday.replace(day=1)))
        mail.outbox.clear()
        sent = reports.run(monday)
        self.assertGreaterEqual(sent, 1)
        self.assertIn('owner@example.com', [m.to[0] for m in mail.outbox])
        self.assertEqual(reports.run(monday), 0)                                    # never twice
