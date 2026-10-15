"""
Transport: routes and stops (F341), fees (F342, F358), scans and trip
attendance (F344, F356), live position (F343), drivers (F345), maintenance and
fuel (F346, F347), stop order (F348), delays (F349), field trips (F350),
documents (F351), seats (F352), pickup changes (F354), incidents (F355),
reports (F359) and manifests (F360).
"""
from datetime import date, time, timedelta
from decimal import Decimal as D
from unittest import mock

from django.core import mail
from django.test import override_settings
from django.utils import timezone

from finance.views import _get_family_wallet_for_student
from tenants.middleware import set_current_tenant
from transport import services as svc
from transport.models import (Driver, FieldTrip, FuelLog, Incident, MaintenanceLog, PickupChange, Rider, Route,
                              Stop, TransportBill, Trip, TripScan, Vehicle, VehicleDocument)
from users.models import Staff, User

from .factories import TenantTestCase, make_admin, make_class, make_school, make_student, make_teacher


@override_settings(RUN_TASKS_EAGERLY=True)
class Base(TenantTestCase):

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha')
        self.admin = make_admin(self.school)
        self.admin.email = 'head@example.com'
        self.admin.save()
        self.driver_user = make_teacher(self.school, username='alphadrv', full_name='Mr Driver')
        self.other_user = make_teacher(self.school, username='alphaoth', full_name='Mrs Other')
        self.js1 = make_class(self.school, 'JSS 1A')
        self.ada = make_student(self.school, 'A/001', full_name='Ada Obi', school_class=self.js1,
                                parent_email='mum@example.com')
        self.ben = make_student(self.school, 'A/002', full_name='Ben Obi', school_class=self.js1,
                                parent_email='mum@example.com')
        self.chi = make_student(self.school, 'A/003', full_name='Chi Eze', school_class=self.js1,
                                parent_email='eze@example.com')
        set_current_tenant(self.school)
        self.bus = Vehicle.objects.create(tenant=self.school, name='Bus 1', plate='LAG123', capacity=2)
        self.drv = Driver.objects.create(tenant=self.school, name='Mr Driver', phone='08030000009',
                                         staff=Staff.objects.get(user=self.driver_user))
        self.route = Route.objects.create(tenant=self.school, name='Lekki', vehicle=self.bus, driver=self.drv,
                                          morning_departs=time(6, 30), afternoon_departs=time(14, 30),
                                          fee_term=D('30000'), fee_month=D('12000'))
        self.s1 = Stop.objects.create(tenant=self.school, route=self.route, name='Stop One', order=1,
                                      morning_time=time(6, 40), afternoon_time=time(15, 20))
        self.s2 = Stop.objects.create(tenant=self.school, route=self.route, name='Stop Two', order=2,
                                      morning_time=time(6, 55), afternoon_time=time(15, 5))
        self.parent = User.objects.create_user(username=f'parent_{self.ada.id}', password='x', tenant=self.school,
                                               is_parent=True, email='mum@example.com')

    def _get(self, path, **kw):
        return self.client.get(path, HTTP_HOST=self.host(self.school), **kw)

    def _post(self, path, data=None, **kw):
        return self.client.post(path, data or {}, HTTP_HOST=self.host(self.school), **kw)

    def again(self):
        set_current_tenant(self.school)

    def enrol_both(self):
        svc.enrol(self.ada, self.route, self.s2)
        svc.enrol(self.ben, self.route, self.s1)


class RouteTests(Base):

    def test_seat_limit(self):
        self.enrol_both()
        self.assertEqual(svc.seats_left(self.route), 0)
        with self.assertRaises(svc.TransportError):
            svc.enrol(self.chi, self.route, self.s1)

    def test_manifest_order_reverses_in_afternoon(self):
        self.enrol_both()
        am = [stop.name for stop, _t, _r in svc.manifest(self.route, 'AM')]
        pm = [stop.name for stop, _t, _r in svc.manifest(self.route, 'PM')]
        self.assertEqual(am, ['Stop One', 'Stop Two'])
        self.assertEqual(pm, ['Stop Two', 'Stop One'])
        self.client.force_login(self.admin)
        r = self._get(f'/transport/routes/{self.route.pk}/manifest/?leg=PM')
        self.assertContains(r, 'Ada Obi')

    def test_morning_only_rider_not_on_pm(self):
        svc.enrol(self.ada, self.route, self.s1, direction='AM')
        self.assertEqual(svc.manifest(self.route, 'PM'), [])

    def test_office_adds_route_and_rider(self):
        self.client.force_login(self.admin)
        self._post('/transport/', {'name': 'Ajah', 'vehicle': self.bus.pk, 'fee_term': '20000', 'fee_month': '0'})
        self.again()
        ajah = Route.objects.get(name='Ajah')
        self._post(f'/transport/routes/{ajah.pk}/', {'action': 'stop', 'name': 'Gate'})
        self._post(f'/transport/routes/{ajah.pk}/', {'action': 'rider', 'student': self.chi.pk,
                                                     'stop': Stop.objects.get(name='Gate').pk})
        self.again()
        self.assertEqual(Rider.objects.get(student=self.chi).route, ajah)

    def test_suggest_order(self):
        cfg = svc.settings_for(self.school)
        cfg.school_lat, cfg.school_lng = D('6.430000'), D('3.420000')
        cfg.save()
        # Stop One is farthest, Stop Two nearest school — the better run goes One, Three, Two.
        self.s1.lat, self.s1.lng = D('6.500000'), D('3.600000')
        self.s1.save()
        self.s2.lat, self.s2.lng = D('6.440000'), D('3.440000')
        self.s2.save()
        Stop.objects.create(tenant=self.school, route=self.route, name='Stop Three', order=3,
                            lat=D('6.470000'), lng=D('3.520000'))
        res = svc.suggest_order(self.route)
        self.assertLess(res['after_km'], res['before_km'])
        svc.apply_order(self.route, res['order'])
        self.assertEqual([s.name for s in self.route.stops.all()], ['Stop One', 'Stop Three', 'Stop Two'])


class TripTests(Base):

    def setUp(self):
        super().setUp()
        self.enrol_both()
        self.trip = svc.trip_for(self.route, 'AM', user=self.driver_user)

    def test_scan_on_off_notifies_family(self):
        with mock.patch('communications.services.send_sms', return_value=(True, 'ok')):
            svc.scan(self.trip, self.ada, 'ON', self.driver_user)
        cfg = svc.settings_for(self.school)
        cfg.scan_channel = 'EMAIL'
        cfg.save()
        mail.outbox.clear()
        svc.scan(self.trip, self.ada, 'OFF', self.driver_user)
        self.assertTrue(any('got off' in m.subject for m in mail.outbox))
        self.trip.refresh_from_db()
        self.assertIsNotNone(self.trip.started_at)
        with self.assertRaises(svc.TransportError):
            svc.scan(self.trip, self.ada, 'OFF', self.driver_user)   # twice
        with self.assertRaises(svc.TransportError):
            svc.scan(self.trip, self.chi, 'ON', self.driver_user)    # not on this bus
        with self.assertRaises(svc.TransportError):
            svc.scan(self.trip, self.ben, 'OFF', self.driver_user)   # never boarded

    def test_end_trip_alerts_if_child_still_on(self):
        cfg = svc.settings_for(self.school)
        cfg.notify_scans = False
        cfg.save()
        svc.scan(self.trip, self.ada, 'ON', self.driver_user)
        mail.outbox.clear()
        att = svc.end_trip(self.trip, self.driver_user)
        self.assertEqual(att['still_on'], [self.ada])
        self.assertEqual(att['missing'], [self.ben])
        self.assertTrue(any('URGENT' in m.subject for m in mail.outbox))
        with self.assertRaises(svc.TransportError):
            svc.scan(self.trip, self.ben, 'ON', self.driver_user)

    def test_scan_by_admission_number_and_card(self):
        self.assertEqual(svc.resolve_code(self.school, 'a/002'), self.ben)
        from idcards.models import IDCard
        card = IDCard.objects.create(tenant=self.school, student=self.ada, card_type='STUDENT')
        self.assertEqual(svc.resolve_code(self.school, f'https://x/verify/{card.card_uid}/'), self.ada)
        self.assertIsNone(svc.resolve_code(self.school, 'nobody'))

    def test_crew_page_scan_and_position(self):
        cfg = svc.settings_for(self.school)
        cfg.notify_scans = False
        cfg.save()
        self.client.force_login(self.driver_user)
        r = self._get(f'/transport/trips/route/{self.route.pk}/?leg=AM')
        self.assertContains(r, 'Ada Obi')
        self._post(f'/transport/trips/route/{self.route.pk}/', {'leg': 'AM', 'action': 'ON', 'code': 'A/001'})
        self.again()
        self.assertTrue(TripScan.objects.filter(trip=self.trip, student=self.ada, kind='ON').exists())
        r = self._post(f'/transport/trips/{self.trip.pk}/position/', {'lat': '6.45', 'lng': '3.41'})
        self.assertEqual(r.json(), {'ok': True})
        r = self._post(f'/transport/trips/{self.trip.pk}/position/', {'lat': '600', 'lng': '3.41'})
        self.assertEqual(r.status_code, 400)
        self.again()
        self.trip.refresh_from_db()
        self.assertEqual(self.trip.last_lat, D('6.450000'))

    def test_other_staff_cannot_run_trip(self):
        self.client.force_login(self.other_user)
        r = self._get(f'/transport/trips/route/{self.route.pk}/')
        self.assertEqual(r.status_code, 302)
        r = self._post(f'/transport/trips/{self.trip.pk}/position/', {'lat': '6.45', 'lng': '3.41'})
        self.assertEqual(r.status_code, 404)

    def test_delay_texts_families(self):
        self.ada.parent_phone = '08031111111'
        self.ada.save()
        with mock.patch('communications.services.send_sms', return_value=(True, 'ok')) as sms:
            svc.set_delay(self.trip, 5, 'traffic')
            self.assertEqual(sms.call_count, 0)
            svc.set_delay(self.trip, 20, 'traffic')
            self.assertEqual(sms.call_count, 1)
        self.trip.refresh_from_db()
        self.assertEqual(self.trip.delay_minutes, 20)
        with self.assertRaises(svc.TransportError):
            svc.set_delay(self.trip, 'soon')

    def test_parent_sees_bus_and_position(self):
        svc.record_position(self.trip, '6.45', '3.41')
        self.client.force_login(self.parent)
        r = self._get('/transport/family/')
        self.assertContains(r, 'Lekki')
        self.assertContains(r, 'Stop Two')
        r = self._get(f'/transport/family/position/{self.trip.pk}/')
        self.assertEqual(r.json()['lat'], 6.45)
        other = Route.objects.create(tenant=self.school, name='Other')
        t2 = svc.trip_for(other, 'AM')
        self.assertEqual(self._get(f'/transport/family/position/{t2.pk}/').status_code, 404)


class FeeTests(Base):

    def test_generate_and_pay_from_wallet(self):
        self.enrol_both()
        Rider.objects.filter(student=self.ben).update(plan='MONTH')
        self.assertEqual(svc.generate_bills(self.school, 'TERM', '1st Term'), 1)
        self.assertEqual(svc.generate_bills(self.school, 'TERM', '1st Term'), 0)      # no double billing
        self.assertEqual(svc.generate_bills(self.school, 'MONTH', 'Oct 2026'), 1)
        bill = TransportBill.objects.get(student=self.ada)
        self.assertEqual(bill.amount, D('30000'))
        wallet = _get_family_wallet_for_student(self.ada)
        wallet.credit(D('40000'), 'Top up')
        self.client.force_login(self.parent)
        self._post('/transport/family/', {'action': 'pay', 'student': self.ada.pk, 'id': bill.pk})
        self.again()
        bill.refresh_from_db()
        wallet.refresh_from_db()
        self.assertEqual(bill.status, 'PAID')
        self.assertEqual(wallet.balance, D('10000'))
        with self.assertRaises(svc.TransportError):
            svc.pay_from_wallet(bill, self.parent)

    def test_mark_waived(self):
        self.enrol_both()
        svc.generate_bills(self.school, 'TERM', 'T1')
        bill = TransportBill.objects.get(student=self.ben)
        svc.mark_bill(bill, 'WAIVED', 'Staff child')
        self.assertEqual(bill.status, 'WAIVED')
        with self.assertRaises(svc.TransportError):
            svc.mark_bill(bill, 'MAYBE')


class FleetTests(Base):

    def test_service_due_and_fuel_stats(self):
        today = timezone.localdate()
        MaintenanceLog.objects.create(tenant=self.school, vehicle=self.bus, date=today - timedelta(days=30),
                                      description='Oil change', cost=D('25000'), odometer=10000,
                                      next_due_km=15000)
        self.assertIsNone(svc.service_due(self.bus))
        FuelLog.objects.create(tenant=self.school, vehicle=self.bus, date=today - timedelta(days=10), litres=D('50'),
                               cost=D('50000'), odometer=14000)
        FuelLog.objects.create(tenant=self.school, vehicle=self.bus, date=today, litres=D('40'), cost=D('40000'),
                               odometer=14600)
        self.assertIn('15,000', svc.service_due(self.bus))
        st = svc.fuel_stats(self.bus)
        self.assertEqual(st['km'], 600)
        self.assertEqual(st['km_per_litre'], 15.0)
        self.assertEqual(st['cost_per_km'], 150.0)

    def test_document_and_licence_reminders_sent_once(self):
        soon = timezone.localdate() + timedelta(days=5)
        VehicleDocument.objects.create(tenant=self.school, vehicle=self.bus, kind='INSURANCE', expires_on=soon)
        self.drv.licence_expires = soon
        self.drv.save()
        mail.outbox.clear()
        self.assertEqual(svc.send_reminders(self.school), 2)
        self.assertTrue(mail.outbox)
        self.assertEqual(svc.send_reminders(self.school), 0)

    def test_fleet_page_logs_fuel(self):
        self.client.force_login(self.admin)
        self._post('/transport/fleet/', {'action': 'fuel', 'vehicle': self.bus.pk, 'litres': '30', 'cost': '30000'})
        self.again()
        self.assertEqual(FuelLog.objects.filter(vehicle=self.bus).count(), 1)
        self.assertContains(self._get('/transport/fleet/'), 'LAG123')


class FieldTripTests(Base):

    def test_clash_with_school_run_and_other_booking(self):
        monday = timezone.localdate() + timedelta(days=(7 - timezone.localdate().weekday()))
        data = {'title': 'Museum', 'destination': 'National Museum', 'date': monday.isoformat(),
                'depart': '07:00', 'back': '09:00', 'vehicle': self.bus.pk}
        with self.assertRaises(svc.TransportError):
            svc.book_field_trip(self.school, self.admin, data)       # clashes with 06:30 run
        data.update(depart='10:00', back='13:00')
        ft = svc.book_field_trip(self.school, self.admin, data)
        self.assertEqual(ft.vehicle, self.bus)
        with self.assertRaises(svc.TransportError):
            svc.book_field_trip(self.school, self.admin, dict(data, depart='12:00', back='13:30', title='Zoo'))
        with self.assertRaises(svc.TransportError):
            svc.book_field_trip(self.school, self.admin, dict(data, depart='13:30', back='14:00', seats_needed='9'))
        self.assertEqual(FieldTrip.objects.count(), 1)


class ChangeAndIncidentTests(Base):

    def test_parent_requests_and_office_approves(self):
        self.enrol_both()
        self.client.force_login(self.parent)
        self._post('/transport/family/', {'action': 'change', 'student': self.ada.pk, 'stop': self.s1.pk,
                                          'from_date': timezone.localdate().isoformat(), 'reason': 'Moved house'})
        self.again()
        pc = PickupChange.objects.get(student=self.ada)
        self.client.force_login(self.admin)
        mail.outbox.clear()
        self._post('/transport/pickup-changes/', {'id': pc.pk, 'action': 'approve', 'stop': self.s1.pk})
        self.again()
        self.assertEqual(Rider.objects.get(student=self.ada).stop, self.s1)
        self.assertTrue(any('approved' in m.subject.lower() for m in mail.outbox))

    def test_change_needs_future_date(self):
        self.enrol_both()
        with self.assertRaises(svc.TransportError):
            svc.request_change(self.ada, self.parent, self.s1, '', date(2020, 1, 1), 'x')
        with self.assertRaises(svc.TransportError):
            svc.request_change(self.chi, self.parent, self.s1, '', timezone.localdate(), 'x')

    def test_crew_reports_incident(self):
        self.enrol_both()
        self.client.force_login(self.driver_user)
        mail.outbox.clear()
        self._post('/transport/incidents/', {'kind': 'BREAKDOWN', 'description': 'Flat tyre', 'route': self.route.pk,
                                             'students': [self.ada.pk], 'tell_parents': '1'})
        self.again()
        inc = Incident.objects.get()
        self.assertTrue(inc.parents_told)
        self.assertEqual(list(inc.students.all()), [self.ada])
        self.assertTrue(any('incident' in m.subject.lower() for m in mail.outbox))


class AccessAndReportTests(Base):

    def test_office_pages_need_manager(self):
        self.client.force_login(self.other_user)
        for path in ('/transport/', '/transport/fleet/', '/transport/fees/', '/transport/reports/'):
            self.assertEqual(self._get(path).status_code, 302, path)
        self.client.force_login(self.admin)
        self._post('/transport/reports/', {'managers': [Staff.objects.get(user=self.other_user).pk],
                                           'scan_channel': 'BOTH', 'reminder_days': '21'})
        self.client.force_login(self.other_user)
        self.assertEqual(self._get('/transport/').status_code, 200)

    def test_route_report(self):
        self.enrol_both()
        t = svc.trip_for(self.route, 'AM')
        t.delay_minutes = 15
        t.save()
        svc.trip_for(self.route, 'PM')
        row = svc.route_report(self.school)[0]
        self.assertEqual(row['riders'], 2)
        self.assertEqual(row['use'], 100)
        self.assertEqual(row['on_time'], 50)
        self.assertEqual(Trip.objects.count(), 2)

    def test_parent_without_bus_is_turned_away(self):
        self.client.force_login(self.parent)
        self.assertEqual(self._get('/transport/family/').status_code, 302)
