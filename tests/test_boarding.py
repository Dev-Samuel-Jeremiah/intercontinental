"""
Boarding life: bed map (F321), roll call and prep (F322, F327, F336), exeat
requests (F323), visiting days (F328), inspections (F329), sick bay (F330),
diets and menu (F333, F334), housemaster reports (F337), room changes (F338)
and the night emergency sheet (F340).
"""
from datetime import time, timedelta

from django.core import mail
from django.test import override_settings
from django.utils import timezone

from academics.models import AcademicSession, Term
from hostel import boarding as bd
from hostel.models import (Bed, BoarderProfile, BoardingReport, ExeatRequest, Hostel, Inspection, RollCall, Room,
                           RoomChangeRequest, SickBayVisit, VisitBooking, VisitingDay)
from hostel.services import assign_bed
from tenants.middleware import set_current_tenant
from users.models import User

from .factories import TenantTestCase, make_admin, make_class, make_school, make_student, make_teacher


@override_settings(RUN_TASKS_EAGERLY=True)
class Base(TenantTestCase):

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha')
        self.admin = make_admin(self.school)
        self.admin.email = 'head@example.com'
        self.admin.save()
        self.warden = make_teacher(self.school, username='alphaw', full_name='Mr Warden')
        self.warden.email = 'warden@example.com'
        self.warden.save()
        self.ss1 = make_class(self.school, 'SS 1A')
        self.ada = make_student(self.school, 'A/001', full_name='Ada Obi', school_class=self.ss1,
                                parent_email='mum@example.com')
        self.chi = make_student(self.school, 'A/003', full_name='Chi Eze', school_class=self.ss1,
                                parent_email='eze@example.com')
        set_current_tenant(self.school)
        self.hostel = Hostel.objects.create(tenant=self.school, name='Queen Amina', gender='FEMALE',
                                            warden=self.warden)
        self.room = Room.objects.create(tenant=self.school, hostel=self.hostel, room_number='101', capacity=4)
        self.room2 = Room.objects.create(tenant=self.school, hostel=self.hostel, room_number='102', capacity=4)
        self.beds = [Bed.objects.create(tenant=self.school, room=self.room, bed_number=f'B{i}') for i in (1, 2)]
        self.bed3 = Bed.objects.create(tenant=self.school, room=self.room2, bed_number='B1')
        self.b_ada = BoarderProfile.objects.create(tenant=self.school, student=self.ada, student_type='BOARDER',
                                                   emergency_name='Mrs Obi', emergency_phone='08030000001',
                                                   medical_conditions='Asthma')
        self.b_chi = BoarderProfile.objects.create(tenant=self.school, student=self.chi, student_type='BOARDER')
        assign_bed(self.b_ada, self.beds[0], performed_by=self.admin)
        assign_bed(self.b_chi, self.beds[1], performed_by=self.admin)
        self.parent = User.objects.create_user(username=f'parent_{self.ada.id}', password='x', tenant=self.school,
                                               is_parent=True, email='mum@example.com')

    def _get(self, path, **kw):
        return self.client.get(path, HTTP_HOST=self.host(self.school), **kw)

    def _post(self, path, data=None, **kw):
        return self.client.post(path, data or {}, HTTP_HOST=self.host(self.school), **kw)

    def again(self):
        set_current_tenant(self.school)


class BedMapAndRollCallTests(Base):

    def test_bed_map_and_assign(self):
        floors = bd.bed_map(self.hostel)
        rooms = dict((room.room_number, beds) for room, beds in floors[0][1])
        self.assertEqual(rooms['101'][0][1], self.b_ada)
        newbie = make_student(self.school, 'A/004', full_name='Ngozi Ibe', school_class=self.ss1)
        self.again()
        b = BoarderProfile.objects.create(tenant=self.school, student=newbie, student_type='BOARDER')
        self.assertIn(b, list(bd.unbedded(self.school)))
        self.client.force_login(self.warden)
        self._post('/hostel/bed-map/', {'bed': self.bed3.pk, 'boarder': b.pk})
        self.again()
        b.refresh_from_db()
        self.assertEqual(b.bed, self.bed3)
        self.assertContains(self._get(f'/hostel/bed-map/?hostel={self.hostel.pk}'), 'Ngozi Ibe')

    def test_night_roll_call_alerts_on_absence(self):
        SickBayVisit.objects.create(tenant=self.school, student=self.chi, complaint='Fever')
        rc = bd.roll_call(self.hostel, 'NIGHT', timezone.localdate(), self.warden)
        statuses = dict(rc.marks.values_list('boarder_id', 'status'))
        self.assertEqual(statuses[self.b_chi.pk], 'SICK')                 # in the sick bay
        mail.outbox.clear()
        self.client.force_login(self.warden)
        self._post('/hostel/roll-call/', {'hostel': self.hostel.pk, 'kind': 'NIGHT',
                                          'date': timezone.localdate().isoformat(), f's_{self.b_ada.pk}': 'ABSENT'})
        self.again()
        self.assertEqual(sorted(m.to[0] for m in mail.outbox), ['head@example.com', 'warden@example.com'])
        self.assertIn('Ada Obi', mail.outbox[0].body)
        mail.outbox.clear()
        prep = bd.roll_call(self.hostel, 'PREP', timezone.localdate(), self.warden)
        bd.save_roll(prep, {self.b_ada.pk: 'ABSENT'}, self.warden)
        self.assertEqual(len(mail.outbox), 0)                              # prep absences do not raise an alarm
        self.assertEqual(RollCall.objects.count(), 2)


class ExeatTests(Base):

    def test_parent_asks_school_approves_gate_signs(self):
        self.client.force_login(self.parent)
        leave = timezone.localtime() + timedelta(days=2)
        back = leave + timedelta(days=2)
        mail.outbox.clear()
        self._post('/hostel/family/', {'action': 'exeat', 'boarder': self.b_ada.pk,
                                       'leave_at': leave.strftime('%Y-%m-%dT%H:%M'),
                                       'return_by': back.strftime('%Y-%m-%dT%H:%M'), 'reason': 'Family wedding',
                                       'pickup_person': 'Mr Obi', 'pickup_phone': '0803'})
        self.again()
        r = ExeatRequest.objects.get()
        self.assertEqual(r.status, 'SCHOOL')
        self.assertIn('warden@example.com', [m.to[0] for m in mail.outbox])
        with self.assertRaises(bd.BoardingError):          # one at a time
            bd.request_exeat(self.b_ada, self.parent, leave, back, 'Again', 'Mr Obi')
        mail.outbox.clear()
        self.client.force_login(self.warden)
        self._post('/hostel/exeats/', {'action': 'approve', 'id': r.pk})
        self.again()
        r.refresh_from_db()
        self.assertEqual(r.status, 'APPROVED')
        self.assertEqual(mail.outbox[0].to, ['mum@example.com'])
        self._post('/hostel/exeats/', {'action': 'out', 'id': r.pk})
        self.again()
        self.b_ada.refresh_from_db()
        self.assertEqual(self.b_ada.status, 'EXEAT')
        self._post('/hostel/exeats/', {'action': 'in', 'id': r.pk})
        self.again()
        r.refresh_from_db()
        self.b_ada.refresh_from_db()
        self.assertEqual((r.status, self.b_ada.status), ('RETURNED', 'ACTIVE'))

    def test_school_arranged_exeat_needs_parent(self):
        leave = timezone.now() + timedelta(days=1)
        r = bd.request_exeat(self.b_ada, self.warden, leave, leave + timedelta(hours=6), 'Dentist', 'Mrs Obi',
                             by_parent=False)
        self.assertEqual(r.status, 'PARENT')
        with self.assertRaises(bd.BoardingError):
            bd.school_decide(r, self.warden, True)
        self.client.force_login(self.parent)
        self.assertContains(self._get('/hostel/family/'), 'The school arranged an exeat')
        self._post('/hostel/family/', {'action': 'approve', 'id': r.pk})
        self.again()
        r.refresh_from_db()
        self.assertEqual(r.status, 'SCHOOL')
        self.client.force_login(make_teacher(self.school, username='alphax', full_name='X'))
        self.assertNotEqual(self._get('/hostel/family/').status_code, 200)


class VisitInspectSickTests(Base):

    def test_visiting_day_capacity(self):
        day = VisitingDay.objects.create(tenant=self.school, date=timezone.localdate() + timedelta(days=3),
                                         start=time(10, 0), end=time(12, 0), slot_minutes=60, families_per_slot=1)
        self.assertEqual([s['time'] for s in bd.slots(day)], [time(10, 0), time(11, 0)])
        bd.book_visit(day, self.b_ada, self.parent, time(10, 0), 2, 'Mr and Mrs Obi')
        with self.assertRaises(bd.BoardingError):
            bd.book_visit(day, self.b_chi, self.parent, time(10, 0), 1, 'Mr Eze')     # slot full
        with self.assertRaises(bd.BoardingError):
            bd.book_visit(day, self.b_ada, self.parent, time(11, 0), 1, 'Again')      # already booked
        bd.book_visit(day, self.b_chi, self.parent, time(11, 0), 1, 'Mr Eze')
        self.client.force_login(self.warden)
        bk = VisitBooking.objects.get(boarder=self.b_ada)
        self._post(f'/hostel/visiting-days/?day={day.pk}', {'action': 'arrived', 'id': bk.pk})
        self.again()
        bk.refresh_from_db()
        self.assertIsNotNone(bk.arrived_at)

    def test_inspection_league_and_sick_bay(self):
        data = {f's{i}': '4' for i in range(len(bd.CRITERIA))}
        i = bd.inspect(self.room, self.warden, data)
        self.assertEqual(float(i.total), 80.0)
        bd.inspect(self.room2, self.warden, {f's{i}': '5' for i in range(len(bd.CRITERIA))})
        self.assertEqual([r.room_number for r in bd.room_league(self.hostel)], ['102', '101'])
        with self.assertRaises(bd.BoardingError):
            bd.inspect(self.room, self.warden, {'s0': '9'})
        mail.outbox.clear()
        v = bd.admit(self.ada, self.warden, 'Headache', '37.9')
        self.assertEqual(mail.outbox[0].to, ['mum@example.com'])
        with self.assertRaises(bd.BoardingError):
            bd.admit(self.ada, self.warden, 'Again')
        bd.discharge(v, 'DORM')
        v.refresh_from_db()
        self.assertIsNotNone(v.discharged_at)
        self.assertEqual(Inspection.objects.count(), 2)


class ReportsRoomsSheetTests(Base):

    def test_room_change_and_report(self):
        self.client.force_login(self.parent)
        self._post('/hostel/family/', {'action': 'room', 'boarder': self.b_ada.pk, 'reason': 'Too noisy at night'})
        self._post('/hostel/family/', {'action': 'diet', 'boarder': self.b_ada.pk, 'diet': 'ALLERGY',
                                       'diet_note': 'Groundnuts'})
        self.again()
        r = RoomChangeRequest.objects.get()
        self.client.force_login(self.warden)
        self._post('/hostel/room-changes/', {'id': r.pk, 'action': 'approve', 'bed': self.bed3.pk})
        self.again()
        self.b_ada.refresh_from_db()
        self.assertEqual(self.b_ada.bed, self.bed3)
        self.assertEqual([b.student for b in bd.diet_list(self.hostel)], [self.ada])
        session = AcademicSession.objects.create(tenant=self.school, name='2026/2027', is_active=True)
        term = Term.objects.create(tenant=self.school, session=session, name='1st Term', is_active=True)
        self._post(f'/hostel/reports/{self.b_ada.pk}/', {'conduct': '5', 'neatness': '4', 'relationships': '5',
                                                         'comment': 'A model boarder.', 'published': '1'})
        self.again()
        rep = BoardingReport.objects.get()
        self.assertEqual((rep.term, rep.conduct), (term, 5))
        self.client.force_login(self.parent)
        page = self._get('/hostel/family/')
        self.assertContains(page, 'A model boarder.')
        self.assertContains(page, 'Groundnuts')

    def test_night_sheet(self):
        self.client.force_login(self.warden)
        page = self._get(f'/hostel/night-sheet/?hostel={self.hostel.pk}')
        self.assertContains(page, '08030000001')
        self.assertContains(page, 'Asthma')
        self.client.force_login(self.parent)
        self.assertEqual(self._get('/hostel/night-sheet/').status_code, 302)
