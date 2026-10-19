"""
Labs and practical rooms: labs linked to the timetable and the chemicals
register, clash-checked bookings, equipment, breakages and family charges,
safety inspections and incidents, practical registers and marks, chemical
use, WAEC/NECO readiness and sending practical marks to the result sheet.
"""
from datetime import timedelta
from decimal import Decimal

from django.http import QueryDict
from django.utils import timezone

from academics.models import AcademicSession, Term
from facilities.models import Chemical
from finance.models import Wallet
from labs import services as svc
from labs.models import Breakage, Equipment, Lab, PracticalRecord, PracticalSession
from notifications.models import Notification
from results.models import AssessmentScheme, ScoreLock, TermResult
from tenants.middleware import set_current_tenant
from timetable.models import RoomBooking, TeachingRoom
from users.models import Staff, Subject, User

from .factories import TenantTestCase, make_admin, make_class, make_school, make_student, make_teacher


def q(**kw):
    d = QueryDict(mutable=True)
    for k, v in kw.items():
        if isinstance(v, (list, tuple)):
            d.setlist(k, [str(x) for x in v])
        else:
            d[k] = str(v)
    return d


class Base(TenantTestCase):

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha')
        self.admin = make_admin(self.school)
        self.tech_user = make_teacher(self.school, username='tech', full_name='Tunde Lab')
        self.teacher = make_teacher(self.school, username='bisi', full_name='Bisi Chem')
        set_current_tenant(self.school)
        self.tech = Staff.objects.get(user=self.tech_user)
        self.ss2 = make_class(self.school, 'SS 2A')
        self.chem = Subject.objects.create(tenant=self.school, name='Chemistry')
        self.ada = make_student(self.school, 'A1', 'Ada Obi', school_class=self.ss2)
        self.ben = make_student(self.school, 'B1', 'Ben Musa', school_class=self.ss2)
        session = AcademicSession.objects.create(tenant=self.school, name='2026/2027', is_active=True)
        self.term = Term.objects.create(tenant=self.school, session=session, name='1st Term', is_active=True)
        set_current_tenant(self.school)
        self.lab = svc.save_lab(self.school, q(name='Chemistry Lab', kind='CHEMISTRY', capacity=30, in_charge='',
                                               technicians=[self.tech.id], subjects=[self.chem.id], active='on'))
        self.burette = svc.save_equipment(self.lab, self.tech_user, q(name='Burette', category='GLASSWARE', quantity=10,
                                                                      unit_cost='6500', min_quantity=8))
        self.hcl = Chemical.objects.create(tenant=self.school, name='HCl 0.1M', quantity=Decimal('500'), unit='ml',
                                           space=self.lab.space)

    def practical(self, rubric='Readings | 6\nCalculations | 4', chems=()):
        return svc.save_practical(self.school, self.teacher, q(
            title='Titration', subject=self.chem.id, lab=self.lab.id, kind='ASSESSED', rubric=rubric,
            hazards='Acids are corrosive.', chemical=[c.id for c, _ in chems], chemical_qty=[n for _, n in chems]))

    def get(self, user, path):
        self.client.force_login(user)
        return self.client.get(path, HTTP_HOST=self.host(self.school))

    def post(self, user, path, data):
        self.client.force_login(user)
        return self.client.post(path, data, HTTP_HOST=self.host(self.school))


class LabSetupTests(Base):

    def test_lab_is_linked_to_timetable_and_facilities(self):
        self.assertEqual((self.lab.room.name, self.lab.room.kind), ('Chemistry Lab', 'LAB'))
        self.assertEqual((self.lab.space.kind, self.lab.space.teaching_room), ('LAB', self.lab.room))
        svc.save_lab(self.school, q(name='Chemistry Lab 1', kind='CHEMISTRY', capacity=30, active='on'), self.lab)
        self.assertEqual(TeachingRoom.objects.get().name, 'Chemistry Lab 1')        # renamed, not duplicated
        with self.assertRaisesMessage(svc.LabError, 'already a lab'):
            svc.save_lab(self.school, q(name='chemistry lab 1', active='on'))

    def test_only_admins_add_labs_and_students_are_kept_out(self):
        r = self.post(self.teacher, '/labs/new/', {'name': 'Physics Lab', 'kind': 'PHYSICS'})
        self.assertFalse(Lab.objects.filter(name='Physics Lab').exists())
        self.post(self.admin, '/labs/new/', {'name': 'Physics Lab', 'kind': 'PHYSICS', 'capacity': 30, 'active': 'on'})
        self.assertTrue(Lab.objects.filter(name='Physics Lab').exists())
        self.assertRedirects(self.get(self.ada.user, '/labs/'), '/labs/my/', fetch_redirect_response=False)
        for path in ('/labs/', f'/labs/{self.lab.id}/', f'/labs/{self.lab.id}/?tab=equipment',
                     f'/labs/{self.lab.id}/?tab=chemicals', f'/labs/{self.lab.id}/?tab=breakages',
                     f'/labs/{self.lab.id}/?tab=safety', f'/labs/{self.lab.id}/?tab=practicals',
                     '/labs/practicals/', '/labs/practicals/new/', '/labs/readiness/'):
            self.assertEqual(self.get(self.teacher, path).status_code, 200, path)


class BookingTests(Base):

    def test_bookings_are_clash_checked_and_tell_the_technician(self):
        day = timezone.localdate() + timedelta(days=2)
        made, skipped = svc.book(self.lab, self.teacher, q(date=day, start_time='10:00', end_time='11:20',
                                                           school_class=self.ss2.id, subject=self.chem.id,
                                                           prep_notes='8 burettes', repeat_weeks=2))
        self.assertEqual((len(made), skipped), (2, []))
        self.assertEqual(RoomBooking.objects.filter(room=self.lab.room).count(), 2)
        self.assertTrue(Notification.objects.filter(recipient=self.tech_user, title__startswith='Chemistry Lab booked').exists())
        made, skipped = svc.book(self.lab, self.admin, q(date=day, start_time='11:00', end_time='12:00', purpose='Clash'))
        self.assertEqual(made, [])
        self.assertIn('Booked 10:00–11:20', skipped[0][1][0])
        with self.assertRaisesMessage(svc.LabError, 'holds 30'):
            svc.book(self.lab, self.admin, q(date=day, start_time='13:00', end_time='14:00', students=45))
        week = svc.week(self.lab, day)
        self.assertEqual(sum(len(d['items']) for d in week), 1)

    def test_cancelling(self):
        day = timezone.localdate() + timedelta(days=1)
        lb = svc.book(self.lab, self.teacher, q(date=day, start_time='09:00', end_time='10:00', purpose='Revision'))[0][0]
        other = make_teacher(self.school, username='other', full_name='Other Teacher')
        with self.assertRaises(svc.LabError):
            svc.cancel(lb, other)                                                  # not theirs
        svc.cancel(lb, self.teacher)
        self.assertTrue(RoomBooking.objects.get().cancelled)


class EquipmentAndBreakageTests(Base):

    def test_equipment_moves(self):
        svc.move(self.burette, self.tech_user, 'BREAK', 2)
        svc.move(self.burette, self.tech_user, 'REPAIR', 1)
        svc.move(self.burette, self.tech_user, 'WRITEOFF', 1)
        self.burette.refresh_from_db()
        self.assertEqual((self.burette.quantity, self.burette.broken), (9, 0))
        with self.assertRaises(svc.LabError):
            svc.move(self.burette, self.teacher, 'ADD', 5)                       # not lab staff
        svc.move(self.burette, self.tech_user, 'COUNT', 7)
        self.burette.refresh_from_db()
        self.assertTrue(self.burette.low)
        self.assertEqual(self.burette.value, Decimal('45500.00'))

    def test_breakage_charge_and_wallet_payment(self):
        b = svc.report_breakage(self.lab, self.tech_user, q(equipment=self.burette.id, quantity=1, student=self.ada.id,
                                                            cause='CARELESS', charge='6500'))
        self.burette.refresh_from_db()
        self.assertEqual((self.burette.quantity, self.burette.broken, b.cost, b.status), (9, 1, Decimal('6500.00'), 'UNPAID'))
        self.assertTrue(Notification.objects.filter(title='Lab breakage charge: ₦6,500.00').exists())
        with self.assertRaisesMessage(svc.LabError, 'Only lab staff'):
            svc.report_breakage(self.lab, self.teacher, q(item='Beaker', student=self.ada.id, charge='500'))
        parent = User.objects.create_user(username=f'parent_{self.ada.id}', password='x-pass-12345', tenant=self.school,
                                          is_parent=True)
        wallet = Wallet.get_or_create_for_student(self.ada)
        wallet.balance = Decimal('10000')
        wallet.save()
        self.assertContains(self.get(parent, '/labs/my/'), 'Pay from wallet')
        self.post(parent, '/labs/my/', {'breakage': b.id})
        b.refresh_from_db()
        wallet.refresh_from_db()
        self.assertEqual((b.status, wallet.balance), ('PAID', Decimal('3500.00')))

    def test_settling_and_no_charge(self):
        b = svc.report_breakage(self.lab, self.teacher, q(item='Test tube', quantity=3, cost='300'))
        self.assertEqual((b.status, b.cost), ('NONE', Decimal('300.00')))
        b2 = svc.report_breakage(self.lab, self.tech_user, q(item='Beaker', student=self.ben.id, charge='1200'))
        with self.assertRaises(svc.LabError):
            svc.settle(b2, self.teacher, 'WAIVED')
        svc.settle(b2, self.tech_user, 'WAIVED')
        self.assertEqual(Breakage.objects.get(pk=b2.pk).status, 'WAIVED')


class SafetyTests(Base):

    def test_inspection_due_and_failures(self):
        self.assertTrue(svc.inspection(self.lab)['overdue'])                       # never inspected
        data = q(**{f'ok_{i}': 'on' for i in range(len(svc.CHECKLIST)) if i != 0}, note_0='Extinguisher out of date')
        check = svc.record_check(self.lab, self.tech_user, data)
        self.assertFalse(check.passed)
        self.assertEqual(check.failed_items[0]['note'], 'Extinguisher out of date')
        self.assertTrue(Notification.objects.filter(title='Chemistry Lab failed its safety inspection').exists())
        state = svc.inspection(self.lab)
        self.assertEqual((state['overdue'], state['failed']), (False, True))

    def test_incident_tells_families_and_needs_an_action_to_close(self):
        inc = svc.report_incident(self.lab, self.teacher, q(kind='CHEMICAL', severity='MODERATE',
                                                            description='Acid splash on hand.', first_aid='Rinsed.'),
                                  [self.ada.id])
        self.assertTrue(inc.families_told)
        self.assertTrue(Notification.objects.filter(title='Ada was involved in a lab incident',
                                                    student=self.ada).exists())
        with self.assertRaisesMessage(svc.LabError, 'Record what has been done'):
            svc.close_incident(inc, self.tech_user, '')
        svc.close_incident(inc, self.tech_user, 'Goggles and gloves now compulsory for titrations.')
        self.assertTrue(inc.closed)
        near = svc.report_incident(self.lab, self.teacher, q(kind='NEARMISS', description='Bottle nearly fell.'), [self.ben.id])
        self.assertFalse(near.families_told)
        self.assertEqual(self.get(self.teacher, f'/labs/incidents/{inc.id}/').status_code, 200)


class PracticalTests(Base):

    def test_rubric(self):
        p = self.practical()
        self.assertEqual((p.max_score, p.rubric[0]), (10, {'criterion': 'Readings', 'max': 6}))
        with self.assertRaisesMessage(svc.LabError, 'What is marked | marks'):
            self.practical(rubric='Readings six marks')

    def test_session_register_marks_and_chemicals(self):
        p = self.practical(chems=[(self.hcl, '50')])
        day = timezone.localdate() + timedelta(days=3)
        s = svc.plan_session(p, self.teacher, q(school_class=self.ss2.id, date=day, lab=self.lab.id, groups=4,
                                                start_time='10:00', end_time='11:20'))
        self.assertTrue(s.booking_id)
        self.assertEqual(s.term, self.term)
        recs = {r.student_id: r for r in svc.roster(s)}
        a, b = recs[self.ada.id], recs[self.ben.id]
        data = q(**{f'r{a.id}_att': 'P', f'r{a.id}_c0': 5, f'r{a.id}_c1': 3, f'r{b.id}_att': 'A', f'r{b.id}_c0': 6,
                    'safety_briefing': 'on', 'finish': '1'})
        with self.assertRaises(svc.LabError):
            svc.save_register(s, make_teacher(self.school, username='x', full_name='Not Theirs'), data)
        svc.save_register(s, self.teacher, data)
        a.refresh_from_db(); b.refresh_from_db(); s.refresh_from_db()
        self.assertEqual((a.score, b.score, s.status, s.safety_briefing), (8.0, None, 'DONE', True))
        bad = q(**{f'r{a.id}_att': 'P', f'r{a.id}_c0': 9})
        with self.assertRaisesMessage(svc.LabError, 'Readings is out of 6'):
            svc.save_register(s, self.teacher, bad)
        svc.use_chemicals(s, self.teacher)
        self.hcl.refresh_from_db()
        self.assertEqual(self.hcl.quantity, Decimal('300.00'))                     # 50 ml × 4 groups
        with self.assertRaisesMessage(svc.LabError, 'already been deducted'):
            svc.use_chemicals(s, self.teacher)
        self.assertContains(self.get(self.teacher, f'/labs/sessions/{s.id}/'), 'Read out before starting')

    def test_not_enough_chemical(self):
        p = self.practical(chems=[(self.hcl, '200')])
        s = svc.plan_session(p, self.teacher, q(school_class=self.ss2.id, groups=4))
        with self.assertRaisesMessage(svc.LabError, 'Not enough in stock'):
            svc.use_chemicals(s, self.teacher)

    def test_cancel_releases_the_booking(self):
        p = self.practical()
        s = svc.plan_session(p, self.teacher, q(school_class=self.ss2.id, lab=self.lab.id,
                                                date=timezone.localdate() + timedelta(days=1),
                                                start_time='08:00', end_time='09:00'))
        svc.cancel_session(s, self.teacher)
        self.assertTrue(RoomBooking.objects.get().cancelled)


class ReadinessTests(Base):

    def setUp(self):
        super().setUp()
        p = self.practical()
        for scores in ((('P', 9), ('A', None)), (('P', 7), ('P', 4))):
            s = svc.plan_session(p, self.teacher, q(school_class=self.ss2.id))
            recs = {r.student_id: r for r in svc.roster(s)}
            data = q(finish='1')
            for st, (att, mark) in zip((self.ada, self.ben), scores):
                r = recs[st.id]
                data[f'r{r.id}_att'] = att
                if mark is not None:
                    data[f'r{r.id}_c0'] = str(min(mark, 6))
                    data[f'r{r.id}_c1'] = str(max(0, mark - 6))
            svc.save_register(s, self.teacher, data)

    def test_readiness_flags_who_is_at_risk(self):
        data = svc.readiness(self.ss2, self.chem, self.term)
        rows = {r['student'].full_name: r for r in data['rows']}
        self.assertEqual((rows['Ada Obi']['attendance'], rows['Ada Obi']['average'], rows['Ada Obi']['at_risk']), (100, 80, False))
        self.assertEqual((rows['Ben Musa']['attendance'], rows['Ben Musa']['average'], rows['Ben Musa']['at_risk']), (50, 40, True))
        self.assertEqual(rows['Ben Musa']['ca_percent'], 20.0)                    # absence counts as 0
        page = self.get(self.teacher, f'/labs/readiness/?class={self.ss2.id}&subject={self.chem.id}')
        self.assertContains(page, 'At risk')

    def test_send_to_results(self):
        n, col = svc.send_to_results(self.ss2, self.chem, self.term, 'ca3', self.admin)
        scheme = AssessmentScheme.for_tenant(self.school)
        self.assertEqual((n, col['max']), (2, scheme.ca3_max))
        ada = TermResult.objects.get(student=self.ada, subject=self.chem, term=self.term)
        self.assertEqual(ada.ca3_score, round(80 * scheme.ca3_max / 100, 1))
        with self.assertRaises(svc.LabError):
            svc.send_to_results(self.ss2, self.chem, self.term, 'exam', self.admin)     # not a CA column
        ScoreLock.objects.create(tenant=self.school, term=self.term, locks_on=timezone.localdate() - timedelta(days=1))
        with self.assertRaisesMessage(svc.LabError, 'closed'):
            svc.send_to_results(self.ss2, self.chem, self.term, 'ca3', self.admin)

    def test_student_and_parent_see_their_record(self):
        page = self.get(self.ada.user, '/labs/my/')
        self.assertContains(page, 'Titration')
        self.assertContains(page, '2 / 2')
        parent = User.objects.create_user(username=f'parent_{self.ben.id}', password='x-pass-12345', tenant=self.school,
                                          is_parent=True)
        page = self.get(parent, '/labs/my/')
        self.assertContains(page, 'Ben Musa')
        self.assertNotContains(page, 'Ada Obi')
