"""
Lesson-by-lesson registers from the timetable, and staff clocking in from
their phone on school grounds.
"""
from datetime import time, timedelta
from decimal import Decimal

from django.test import override_settings
from django.utils import timezone

from idcards.models import StaffClockLog
from safety import clock
from safety.models import SafetySettings
from tenants.middleware import set_current_tenant
from timetable import lessons
from timetable.models import (GeneratedTimetable, LessonMark, LessonRegister, TimeSlot,
                              TimetableConfiguration, TimetableSlot)
from users.models import Attendance, Staff, Subject, User

from .factories import (TenantTestCase, make_admin, make_class, make_school, make_student,
                        make_teacher)

DAYS = ['MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT', 'SUN']


class Base(TenantTestCase):

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha')
        self.admin = make_admin(self.school)
        self.teacher_user = make_teacher(self.school)
        self.jss1 = make_class(self.school, 'JSS 1A')
        set_current_tenant(self.school)
        self.teacher = Staff.objects.get(user=self.teacher_user)
        self.today = timezone.localdate()
        # A weekday in the recent past, so the register is allowed and the timetable has it.
        self.day = self.today - timedelta(days=1)
        while self.day.weekday() > 4:
            self.day -= timedelta(days=1)
        self.maths = Subject.objects.create(tenant=self.school, name='Mathematics')
        self.english = Subject.objects.create(tenant=self.school, name='English')
        p1 = TimeSlot.objects.create(tenant=self.school, name='Period 1', start_time=time(8, 0),
                                     end_time=time(8, 40), order=1)
        p2 = TimeSlot.objects.create(tenant=self.school, name='Period 2', start_time=time(8, 40),
                                     end_time=time(9, 20), order=2)
        config = TimetableConfiguration.objects.create(tenant=self.school, name='First term', is_active=True,
                                                       active_days=['MON', 'TUE', 'WED', 'THU', 'FRI'])
        self.tt = GeneratedTimetable.objects.create(tenant=self.school, config=config, is_published=True)
        code = DAYS[self.day.weekday()]
        self.maths_slot = TimetableSlot.objects.create(tenant=self.school, timetable=self.tt, school_class=self.jss1,
                                                       day=code, time_slot=p1, subject=self.maths, teacher=self.teacher)
        self.english_slot = TimetableSlot.objects.create(tenant=self.school, timetable=self.tt, school_class=self.jss1,
                                                         day=code, time_slot=p2, subject=self.english, teacher=self.teacher)
        self.ada = make_student(self.school, 'STU001', full_name='Ada Bello', school_class=self.jss1)
        self.tobi = make_student(self.school, 'STU002', full_name='Tobi Okafor', school_class=self.jss1)
        self.away = make_student(self.school, 'STU003', full_name='Away Child', school_class=self.jss1)
        for s, status in ((self.ada, 'P'), (self.tobi, 'P'), (self.away, 'A')):
            Attendance.objects.create(tenant=self.school, student=s, school_class=self.jss1, date=self.day,
                                      status=status)


class LessonTests(Base):

    def test_the_days_lessons_for_a_teacher(self):
        rows = lessons.lessons_on(self.school, self.day, teacher=self.teacher)
        self.assertEqual([r['slot'].subject.name for r in rows], ['Mathematics', 'English'])
        self.assertFalse(any(r['taken'] for r in rows))

    def test_the_register_starts_from_the_morning_register(self):
        rows, register = lessons.roster(self.maths_slot, self.day)
        self.assertIsNone(register)
        status = {r['student'].full_name: r['status'] for r in rows}
        self.assertEqual(status, {'Ada Bello': 'P', 'Tobi Okafor': 'P', 'Away Child': 'A'})

    def test_saving_a_register(self):
        reg = lessons.save_register(self.maths_slot, self.day, {
            self.ada.id: ('P', 0), self.tobi.id: ('L', 12), self.away.id: ('A', 0)}, self.teacher)
        marks = {m.student_id: (m.status, m.minutes_late) for m in reg.marks.all()}
        self.assertEqual(marks[self.tobi.id], ('L', 12))
        self.assertIsNotNone(reg.taken_at)
        self.assertTrue(lessons.lessons_on(self.school, self.day, teacher=self.teacher)[0]['taken'])

    def test_saving_again_updates_not_duplicates(self):
        lessons.save_register(self.maths_slot, self.day, {self.ada.id: ('P', 0)}, self.teacher)
        lessons.save_register(self.maths_slot, self.day, {self.ada.id: ('A', 0)}, self.teacher)
        self.assertEqual(LessonMark.objects.filter(student=self.ada).count(), 1)
        self.assertEqual(LessonMark.objects.get(student=self.ada).status, 'A')

    def test_a_child_in_school_who_missed_a_lesson_is_listed(self):
        lessons.save_register(self.maths_slot, self.day, {self.ada.id: ('P', 0), self.tobi.id: ('A', 0),
                                                          self.away.id: ('A', 0)}, self.teacher)
        missed = lessons.missed_lessons(self.school, self.day)
        self.assertEqual([(s.full_name, [x.subject.name for x in slots]) for s, slots in missed],
                         [('Tobi Okafor', ['Mathematics'])])      # not the child absent from school

    def test_a_child_who_left_early_is_not_listed(self):
        Attendance.objects.filter(student=self.tobi).update(remarks='Left 10:15 with Mum')
        lessons.save_register(self.english_slot, self.day, {self.tobi.id: ('A', 0)}, self.teacher)
        self.assertEqual(lessons.missed_lessons(self.school, self.day), [])

    def test_limits(self):
        with self.assertRaisesMessage(lessons.LessonError, "hasn't happened"):
            lessons.save_register(self.maths_slot, self.today + timedelta(days=3), {}, self.teacher)
        with self.assertRaisesMessage(lessons.LessonError, 'older than a week'):
            lessons.save_register(self.maths_slot, self.day - timedelta(days=14), {}, self.teacher)
        wrong_day = self.day - timedelta(days=1)
        while wrong_day.weekday() > 4:
            wrong_day -= timedelta(days=1)
        with self.assertRaisesMessage(lessons.LessonError, 'not on the timetable'):
            lessons.save_register(self.maths_slot, wrong_day, {}, self.teacher)

    def test_another_classes_child_is_ignored(self):
        jss2 = make_class(self.school, 'JSS 2A')
        other = make_student(self.school, 'STU009', school_class=jss2)
        reg = lessons.save_register(self.maths_slot, self.day, {other.id: ('P', 0)}, self.teacher)
        self.assertEqual(reg.marks.count(), 0)

    def test_subject_attendance(self):
        lessons.save_register(self.maths_slot, self.day, {self.ada.id: ('P', 0), self.tobi.id: ('A', 0)})
        lessons.save_register(self.english_slot, self.day, {self.ada.id: ('L', 5), self.tobi.id: ('P', 0)})
        students, subjects, table = lessons.subject_attendance(self.jss1, self.day, self.day)
        self.assertEqual(subjects, ['English', 'Mathematics'])
        self.assertEqual((table[self.ada.id]['English'], table[self.tobi.id]['Mathematics']), (100, 0))

    def test_only_the_published_timetable_counts(self):
        self.tt.is_published = False
        self.tt.save()
        self.assertEqual(lessons.lessons_on(self.school, self.day), [])


class ClockTests(Base):

    def setUp(self):
        super().setUp()
        self.cfg = SafetySettings.objects.create(tenant=self.school, phone_clock_in=True,
                                                 school_latitude=Decimal('6.524379'),
                                                 school_longitude=Decimal('3.379206'), clock_radius_m=150)

    def test_distance(self):
        self.assertEqual(clock.distance_m(6.524379, 3.379206, 6.524379, 3.379206), 0)
        self.assertAlmostEqual(clock.distance_m(6.524379, 3.379206, 6.525379, 3.379206), 111, delta=2)

    def test_clocking_in_on_school_grounds(self):
        log = clock.clock_by_phone(self.teacher, '6.524800', '3.379206', '20')     # ~47 m away
        self.assertEqual((log.direction, log.method), ('IN', 'PHONE'))
        self.assertLess(log.distance_m, 60)
        out = clock.clock_by_phone(self.teacher, '6.524800', '3.379206', '20')
        self.assertEqual(out.direction, 'OUT')

    def test_too_far_away_is_refused(self):
        with self.assertRaisesMessage(clock.ClockError, 'from school'):
            clock.clock_by_phone(self.teacher, '6.535000', '3.379206', '15')        # ~1.2 km
        self.assertFalse(StaffClockLog.objects.exists())

    def test_a_vague_position_is_refused(self):
        with self.assertRaisesMessage(clock.ClockError, 'only accurate'):
            clock.clock_by_phone(self.teacher, '6.524379', '3.379206', '900')

    def test_nonsense_positions_are_refused(self):
        for lat, lng in (('abc', '3.37'), ('95', '3.37'), ('6.5', '200')):
            with self.assertRaises(clock.ClockError):
                clock.clock_by_phone(self.teacher, lat, lng, '10')

    def test_switched_off_or_unset(self):
        self.cfg.phone_clock_in = False
        self.cfg.save()
        with self.assertRaisesMessage(clock.ClockError, 'not switched on'):
            clock.clock_by_phone(self.teacher, '6.524379', '3.379206', '10')
        self.cfg.phone_clock_in, self.cfg.school_latitude = True, None
        self.cfg.save()
        with self.assertRaisesMessage(clock.ClockError, 'location has not been set'):
            clock.clock_by_phone(self.teacher, '6.524379', '3.379206', '10')


@override_settings(ROOT_DOMAIN='localhost', ALLOWED_HOSTS=['*'])
class PageTests(Base):

    def _get(self, path):
        return self.client.get(path, HTTP_HOST=self.host(self.school))

    def _post(self, path, data, **kw):
        return self.client.post(path, data, HTTP_HOST=self.host(self.school), **kw)

    def test_a_teacher_takes_a_register(self):
        self.client.force_login(self.teacher_user)
        page = self._get(f'/timetable/lessons/?date={self.day.isoformat()}')
        self.assertContains(page, 'Mathematics')
        self._post(f'/timetable/lessons/{self.maths_slot.id}/register/',
                   {'date': self.day.isoformat(), f's_{self.ada.id}': 'P', f's_{self.tobi.id}': 'L',
                    f'late_{self.tobi.id}': '10', f's_{self.away.id}': 'A'})
        reg = LessonRegister.objects.get()
        self.assertEqual(reg.taken_by, self.teacher)
        self.assertEqual(reg.marks.get(student=self.tobi).minutes_late, 10)

    def test_a_teacher_cannot_take_someone_elses_register(self):
        other_user = make_teacher(self.school, username='alphateacher2', full_name='Other Teacher')
        set_current_tenant(self.school)
        self.client.force_login(other_user)
        self._post(f'/timetable/lessons/{self.maths_slot.id}/register/',
                   {'date': self.day.isoformat(), f's_{self.ada.id}': 'A'})
        self.assertFalse(LessonRegister.objects.exists())

    def test_the_overview_and_subject_pages(self):
        self.client.force_login(self.admin)
        self.assertEqual(self._get(f'/timetable/lessons/overview/?date={self.day.isoformat()}').status_code, 200)
        self.assertEqual(self._get(f'/timetable/lessons/subjects/?class={self.jss1.id}').status_code, 200)

    def test_phone_clock_in_page(self):
        SafetySettings.objects.create(tenant=self.school, phone_clock_in=True, school_latitude=Decimal('6.5'),
                                      school_longitude=Decimal('3.4'), clock_radius_m=200)
        self.client.force_login(self.teacher_user)
        self.assertEqual(self._get('/safety/clock/').status_code, 200)
        ok = self._post('/safety/clock/', {'lat': '6.5001', 'lng': '3.4', 'accuracy': '15'}).json()
        self.assertTrue(ok['ok'])
        far = self._post('/safety/clock/', {'lat': '6.6', 'lng': '3.4', 'accuracy': '15'})
        self.assertEqual(far.status_code, 400)

    def test_settings_refuse_phone_clock_in_without_a_location(self):
        self.client.force_login(self.admin)
        response = self._post('/safety/settings/', {'school_ends_at': '14:00', 'pass_valid_hours': '6',
                                                    'card_replacement_fee': '1000', 'phone_clock_in': '1',
                                                    'clock_radius_m': '150'}, follow=True)
        self.assertContains(response, 'Set the school&#x27;s location')

    def test_parents_cannot_take_registers_or_clock_in(self):
        parent = User.objects.create_user(username='par9', password='x', tenant=self.school, is_parent=True)
        self.client.force_login(parent)
        for path in ('/timetable/lessons/', '/timetable/lessons/overview/', '/safety/clock/'):
            self.assertNotEqual(self._get(path).status_code, 200, path)
