"""
Procurement and facilities: requisitions (F401), purchase orders (F402),
vendors (F403), depreciation (F404), asset tags (F405), maintenance (F406),
preventive tasks (F407), generators (F408), utility bills (F409), stock alerts
(F410), lab chemicals (F411), ICT register (F412), stock counts (F413), goods
received (F414), disposals (F415), spaces (F416), cleaning (F417), patrols
(F418), kitchen stock (F419) and hall hire (F420).
"""
from datetime import date, timedelta
from decimal import Decimal as D
from unittest import mock

from django.core import mail
from django.test import override_settings
from django.utils import timezone

from facilities import services as svc
from facilities.models import (AssetTag, Chemical, CleaningTask, Generator, HireBooking, KitchenItem,
                               MaintenanceRequest, PatrolPoint, PreventiveTask, PurchaseOrder, Requisition, Space,
                               StockAlert, Vendor)
from inventory.models import Asset, Category, StockMovement
from tenants.middleware import set_current_tenant
from users.models import Staff

from .factories import TenantTestCase, make_admin, make_class, make_school, make_student, make_teacher


@override_settings(RUN_TASKS_EAGERLY=True)
class Base(TenantTestCase):

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha')
        self.admin = make_admin(self.school)
        self.admin.email = 'head@example.com'
        self.admin.save()
        self.keeper_user = make_teacher(self.school, username='alphastore', full_name='Mr Store')
        self.keeper_user.email = 'store@example.com'
        self.keeper_user.save()
        self.teacher = make_teacher(self.school, username='alphatch', full_name='Mrs Teach')
        self.teacher.email = 'teach@example.com'
        self.teacher.save()
        self.fixer_user = make_teacher(self.school, username='alphafix', full_name='Mr Fix')
        self.fixer_user.email = 'fix@example.com'
        self.fixer_user.save()
        set_current_tenant(self.school)
        self.keeper = Staff.objects.get(user=self.keeper_user)
        self.fixer = Staff.objects.get(user=self.fixer_user)
        cfg = svc.settings_for(self.school)
        cfg.store_keepers.add(self.keeper)
        cfg.maintenance_team.add(self.fixer)
        cfg.approval_limit = D('50000')
        cfg.save()
        cat = Category.objects.create(tenant=self.school, name='Stationery')
        self.chalk = Asset.objects.create(tenant=self.school, name='Chalk (box)', asset_code='CHK', category=cat,
                                          asset_type='CONSUMABLE', unit='box', reorder_level=5)
        self.vendor = Vendor.objects.create(tenant=self.school, name='Jos Supplies', email='sales@jos.example.com')

    def _get(self, path, **kw):
        return self.client.get(path, HTTP_HOST=self.host(self.school), **kw)

    def _post(self, path, data=None, **kw):
        return self.client.post(path, data or {}, HTTP_HOST=self.host(self.school), **kw)

    def again(self):
        set_current_tenant(self.school)

    def requisition(self, qty='10', cost='1000', user=None):
        return svc.create_requisition(self.school, user or self.teacher, 'Chalk', 'Running out',
                                      [('Chalk boxes', self.chalk, D(qty), D(cost))])


class BuyingTests(Base):

    def test_requisition_approval_limits(self):
        small = self.requisition()                       # ₦10,000
        big = self.requisition(qty='100')                # ₦100,000
        with self.assertRaises(svc.FacilityError):
            svc.decide_requisition(small, self.teacher, True)
        svc.decide_requisition(small, self.keeper_user, True)
        self.assertEqual(small.status, 'APPROVED')
        with self.assertRaises(svc.FacilityError):
            svc.decide_requisition(big, self.keeper_user, True)
        mail.outbox.clear()
        svc.decide_requisition(big, self.admin, False, 'Too many')
        self.assertEqual(mail.outbox[0].to, ['teach@example.com'])
        own = self.requisition(user=self.keeper_user)
        with self.assertRaises(svc.FacilityError):
            svc.decide_requisition(own, self.keeper_user, True)

    def test_requisition_form(self):
        self.client.force_login(self.teacher)
        self._post('/facilities/requisitions/', {'title': 'Markers', 'reason': 'Whiteboards', 'desc_0': 'Markers',
                                                 'qty_0': '20', 'cost_0': '300'})
        self.again()
        self.assertEqual(Requisition.objects.get(title='Markers').total, D('6000'))
        self.assertEqual(self._get('/facilities/orders/').status_code, 302)          # store only

    def test_order_receive_into_stock(self):
        req = self.requisition()
        svc.decide_requisition(req, self.keeper_user, True)
        po = svc.create_po(self.school, self.keeper_user, self.vendor, [], requisition=req)
        req.refresh_from_db()
        self.assertEqual(req.status, 'ORDERED')
        self.assertTrue(po.number.startswith('PO-'))
        mail.outbox.clear()
        svc.send_po(po)
        self.assertEqual(mail.outbox[0].to, ['sales@jos.example.com'])
        line = po.lines.get()
        svc.receive(po, self.keeper_user, {line.pk: (D('4'), D('0'))}, 'WB-1')
        po.refresh_from_db()
        self.assertEqual(po.status, 'PART')
        self.assertEqual(self.chalk.current_stock(), 4)
        with self.assertRaises(svc.FacilityError):
            svc.receive(po, self.keeper_user, {line.pk: (D('7'), D('0'))})
        self.client.force_login(self.keeper_user)
        self._post(f'/facilities/orders/{po.pk}/', {'action': 'receive', f'got_{line.pk}': '6'})
        self.again()
        po.refresh_from_db()
        req.refresh_from_db()
        self.assertEqual((po.status, req.status), ('RECEIVED', 'DONE'))
        self.assertEqual(self.chalk.current_stock(), 10)
        self.assertEqual(po.receipts.count(), 2)

    def test_vendor_rating(self):
        svc.rate_vendor(self.vendor, self.keeper_user, '5', '3', '4')
        svc.rate_vendor(self.vendor, self.keeper_user, '3', '3', '3')
        self.assertEqual(svc.vendor_score(self.vendor), 3.5)
        with self.assertRaises(svc.FacilityError):
            svc.rate_vendor(self.vendor, self.keeper_user, '9', '3', '3')


class AssetTests(Base):

    def test_tags_and_depreciation(self):
        bought = timezone.localdate() - timedelta(days=round(365.25 * 2))
        t = svc.add_tag(self.school, self.keeper_user, {'name': 'Projector', 'cost': '100000', 'salvage_value': '10000',
                                                        'life_years': '5', 'purchase_date': bought.isoformat()})
        self.assertEqual(t.code, 'TAG-00001')
        self.assertAlmostEqual(float(svc.book_value(t)), 64000, delta=50)
        self.assertEqual(len(svc.schedule(t)), 5)
        self.assertEqual(svc.schedule(t)[-1][3], D('10000.00'))
        t.method, t.reducing_rate = 'REDUCING', D('50')
        self.assertAlmostEqual(float(svc.book_value(t)), 25000, delta=50)
        self.assertEqual(svc.add_tag(self.school, self.keeper_user, {'name': 'Desk'}).code, 'TAG-00002')
        with self.assertRaises(svc.FacilityError):
            svc.add_tag(self.school, self.keeper_user, {'name': 'X', 'code': 'TAG-00001'})

    def test_ict_register_and_move(self):
        lab = Space.objects.create(tenant=self.school, name='ICT Lab', kind='ICT')
        self.client.force_login(self.keeper_user)
        self._post('/facilities/assets/', {'name': 'Desktop', 'count': '3', 'is_ict': 'on', 'ram': '8 GB',
                                           'space': lab.pk})
        self.again()
        self.assertEqual(AssetTag.objects.filter(is_ict=True, space=lab).count(), 3)
        t = AssetTag.objects.first()
        svc.move_tag(t, self.keeper_user, None, self.teacher, 'FAIR', 'Staff room')
        self.assertEqual(t.events.filter(kind__in=('MOVED', 'CUSTODY', 'CONDITION')).count(), 3)
        self.client.force_login(self.teacher)
        r = self._get(f'/facilities/tag/{t.uid}/')
        self.assertContains(r, t.code)
        self._post(f'/facilities/tag/{t.uid}/', {'title': 'Will not boot', 'category': 'ICT'})
        self.again()
        self.assertEqual(MaintenanceRequest.objects.get().tag, t)

    def test_disposal_needs_admin(self):
        t = svc.add_tag(self.school, self.keeper_user, {'name': 'Old bus', 'cost': '500000'})
        d = svc.request_disposal(t, self.keeper_user, 'SOLD', 'Beyond repair', D('80000'))
        with self.assertRaises(svc.FacilityError):
            svc.decide_disposal(d, self.keeper_user, True)
        svc.decide_disposal(d, self.admin, True)
        t.refresh_from_db()
        self.assertEqual(t.status, 'DISPOSED')
        self.assertEqual(d.gain, D('80000') - d.book_value)

    def test_stock_count_variance(self):
        StockMovement.objects.create(tenant=self.school, asset=self.chalk, movement_type='IN', quantity=20,
                                     reason='Opening')
        sc = svc.start_count(self.school, self.keeper_user, 'Term count')
        line = sc.lines.get()
        self.assertEqual(line.expected, 20)
        with self.assertRaises(svc.FacilityError):
            svc.close_count(sc, self.keeper_user)                          # not counted yet
        svc.save_counts(sc, {str(line.pk): '17'})
        rep = svc.close_count(sc, self.keeper_user, adjust=True)
        self.assertEqual(rep['short'], 1)
        self.assertEqual(self.chalk.current_stock(), 17)

    def test_low_stock_alert_once_a_day(self):
        cfg = svc.settings_for(self.school)
        cfg.alert_phones = '08030000000'
        cfg.save()
        Chemical.objects.create(tenant=self.school, name='Ethanol', quantity=D('100'), reorder_level=D('500'),
                                unit='ml')
        with mock.patch('communications.services.send_sms', return_value=(True, 'ok')) as sms:
            self.assertEqual(svc.send_stock_alerts(self.school), 2)          # chalk (0 ≤ 5) and ethanol
            self.assertEqual(sms.call_count, 1)
            self.assertEqual(svc.send_stock_alerts(self.school), 0)
        self.assertEqual(StockAlert.objects.count(), 2)


class SiteTests(Base):

    def test_maintenance_flow(self):
        mail.outbox.clear()
        job = svc.report_job(self.school, self.teacher, {'title': 'Fan broken', 'category': 'AC'})
        self.assertTrue(any(m.to == ['fix@example.com'] for m in mail.outbox))
        mail.outbox.clear()
        self.client.force_login(self.fixer_user)
        self._post('/facilities/maintenance/', {'op': 'update', 'id': job.pk, 'status': 'DONE',
                                                'resolution': 'Replaced capacitor', 'cost': '2500'})
        self.again()
        job.refresh_from_db()
        self.assertEqual((job.status, job.cost), ('DONE', D('2500')))
        self.assertTrue(any(m.to == ['teach@example.com'] for m in mail.outbox))
        self.client.force_login(self.teacher)
        self.assertNotContains(self._get('/facilities/maintenance/?show=open'), 'Fan broken')

    def test_preventive_task_raises_job_once(self):
        task = PreventiveTask.objects.create(tenant=self.school, title='Service generator', every_days=90,
                                             next_due=timezone.localdate(), assigned_to=self.fixer)
        self.assertEqual(svc.raise_due_jobs(self.school), 1)
        self.assertEqual(svc.raise_due_jobs(self.school), 0)
        job = task.jobs.get()
        self.assertEqual(job.status, 'ASSIGNED')
        svc.update_job(job, self.fixer_user, 'DONE')
        task.refresh_from_db()
        self.assertEqual(task.next_due, timezone.localdate() + timedelta(days=90))

    def test_generator_log(self):
        g = Generator.objects.create(tenant=self.school, name='Main', kva=60, service_every_hours=100)
        svc.log_generator(g, self.fixer_user, {'hour_meter': '1000', 'date': (timezone.localdate() - timedelta(days=1))
                                               .isoformat()})
        svc.log_generator(g, self.fixer_user, {'hour_meter': '1010', 'litres_added': '50', 'price_per_litre': '1200'})
        with self.assertRaises(svc.FacilityError):
            svc.log_generator(g, self.fixer_user, {'hour_meter': '900'})
        s = svc.generator_stats(g)
        self.assertEqual(s['hours'], D('10'))
        self.assertEqual(s['lph'], D('5.0'))
        self.assertEqual(s['cost_per_hour'], D('6000.00'))
        self.assertTrue(s['service_due'])                                     # 1010 h, never serviced
        svc.record_service(g, self.fixer_user)
        self.assertFalse(svc.generator_stats(g)['service_due'])

    def test_utility_bills_and_monthly_costs(self):
        today = timezone.localdate()
        svc.add_bill(self.school, {'kind': 'ELECTRICITY', 'provider': 'JED', 'amount': '85000',
                                   'period_start': today.replace(day=1).isoformat(), 'period_end': today.isoformat(),
                                   'due_date': (today + timedelta(days=3)).isoformat()})
        g = Generator.objects.create(tenant=self.school, name='Main')
        svc.log_generator(g, None, {'run_hours': '5', 'litres_added': '20', 'price_per_litre': '1000'})
        month = svc.monthly_costs(self.school)[today.strftime('%Y-%m')]
        self.assertEqual((month['ELECTRICITY'], month['DIESEL'], month['total']), (D('85000'), D('20000'), D('105000')))
        self.assertEqual(svc.bills_due(self.school).count(), 1)
        mail.outbox.clear()
        self.assertEqual(svc.send_reminders(self.school)['bills'], 1)
        self.assertEqual(svc.send_reminders(self.school)['bills'], 0)

    def test_spaces_and_capacity(self):
        cls = make_class(self.school, 'JSS 1A')
        for i in range(3):
            make_student(self.school, f'A/00{i}', full_name=f'Kid {i}', school_class=cls)
        self.again()
        Space.objects.create(tenant=self.school, name='Room 1', capacity=2, home_class=cls)
        row = svc.space_usage(self.school)[0]
        self.assertEqual((row['students'], row['over'], row['fill']), (3, True, 150))

    def test_cleaning(self):
        task = CleaningTask.objects.create(tenant=self.school, area='Staff room', frequency='DAILY',
                                           checklist='Sweep\nEmpty bins')
        monday = timezone.localdate() - timedelta(days=timezone.localdate().weekday())
        self.assertTrue(svc.due_today(task, monday))
        self.assertFalse(svc.due_today(task, monday + timedelta(days=5)))
        c = svc.save_check(task, self.fixer_user, ['Sweep'], monday)
        self.assertFalse(c.complete)
        c = svc.save_check(task, self.fixer_user, ['Sweep', 'Empty bins', 'Hack'], monday)
        self.assertTrue(c.complete)
        self.assertEqual(c.done, ['Sweep', 'Empty bins'])

    def test_patrol(self):
        p = PatrolPoint.objects.create(tenant=self.school, name='Back fence', every_hours=2)
        self.assertTrue(svc.patrol_status(self.school)[0]['late'])
        self.client.force_login(self.fixer_user)
        mail.outbox.clear()
        self._post(f'/facilities/patrol/{p.uid}/', {'all_clear': '0', 'note': 'Gate open'})
        self.again()
        self.assertTrue(any('Back fence' in m.subject for m in mail.outbox))
        self.assertFalse(svc.patrol_status(self.school)[0]['late'])


class StoresAndHireTests(Base):

    def test_chemicals(self):
        c = Chemical.objects.create(tenant=self.school, name='HCl', quantity=D('500'), unit='ml',
                                    hazards=['CORROSIVE'], expires_on=timezone.localdate() + timedelta(days=10))
        svc.chemical_move(c, self.teacher, 'USE', '120', 'SS2 titration')
        c.refresh_from_db()
        self.assertEqual(c.quantity, D('380'))
        with self.assertRaises(svc.FacilityError):
            svc.chemical_move(c, self.teacher, 'USE', '1000')
        self.assertIn(c, svc.chemicals_expiring(self.school))
        self.client.force_login(self.teacher)
        self.assertContains(self._get('/facilities/chemicals/'), 'Corrosive')

    def test_kitchen(self):
        rice = KitchenItem.objects.create(tenant=self.school, name='Rice', unit='kg')
        svc.kitchen_move(rice, self.keeper_user, 'IN', '50', unit_cost=D('1000'))
        svc.kitchen_move(rice, self.keeper_user, 'IN', '50', unit_cost=D('1200'))
        rice.refresh_from_db()
        self.assertEqual((rice.quantity, rice.unit_cost), (D('100'), D('1100.00')))
        with self.assertRaises(svc.FacilityError):
            svc.kitchen_move(rice, self.keeper_user, 'USE', '10')               # meal needed
        svc.kitchen_move(rice, self.keeper_user, 'USE', '20', 'LUNCH', headcount=200)
        r = svc.meal_costs(self.school)['rows'][0]
        self.assertEqual((r['cost'], r['per_head']), (D('22000.00'), D('110.00')))

    def test_hall_hire(self):
        hall = Space.objects.create(tenant=self.school, name='Main hall', kind='HALL', capacity=300, hireable=True,
                                    hire_rate_hour=D('20000'), hire_rate_day=D('150000'))
        day = timezone.localdate() + timedelta(days=10)
        data = {'client': 'Grace Church', 'event': 'Wedding reception', 'email': 'grace@example.com',
                'starts': f'{day}T10:00', 'ends': f'{day}T13:00', 'caution_fee': '50000'}
        b = svc.create_booking(self.school, self.keeper_user, hall, data)
        self.assertEqual(b.amount, D('60000.00'))                              # 3 hours
        with self.assertRaises(svc.FacilityError):
            svc.create_booking(self.school, self.keeper_user, hall, dict(data, starts=f'{day}T12:00',
                                                                          ends=f'{day}T15:00'))
        mail.outbox.clear()
        svc.confirm_booking(b)
        self.assertTrue(b.invoice_number.startswith('HIRE-'))
        self.assertEqual(mail.outbox[0].to, ['grace@example.com'])
        svc.record_payment(b, self.keeper_user, '100000')
        with self.assertRaises(svc.FacilityError):
            svc.record_payment(b, self.keeper_user, '20000')                   # balance is 10,000
        svc.record_payment(b, self.keeper_user, '50000', 'REFUND')
        b.refresh_from_db()
        self.assertEqual(b.paid, D('50000'))
        self.assertEqual(HireBooking.objects.count(), 1)
        all_day = svc.quote(hall, timezone.now(), timezone.now() + timedelta(hours=9))
        self.assertEqual(all_day, D('150000.00'))

    def test_settings_admin_only(self):
        self.client.force_login(self.keeper_user)
        self.assertEqual(self._get('/facilities/settings/').status_code, 302)
        self.client.force_login(self.admin)
        self._post('/facilities/settings/', {'approval_limit': '75000', 'store_keepers': [self.keeper.pk]})
        self.again()
        self.assertEqual(svc.settings_for(self.school).approval_limit, D('75000'))
        self.assertEqual(PurchaseOrder.objects.count(), 0)
        self.assertEqual(date.today().year, timezone.localdate().year)
