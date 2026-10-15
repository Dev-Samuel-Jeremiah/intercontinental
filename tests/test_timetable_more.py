"""
Timetable part 2: room booking, free period finder, version history,
parent–teacher meeting slots, calendar feeds, the bell display and
printable teacher timetables.
"""
from datetime import date, time, timedelta

from django.core import mail
from django.test import override_settings
from django.utils import timezone

from tenants.middleware import set_current_tenant
from timetable import ical, scheduling
from timetable.generator import generate_timetable
from timetable.models import (CalendarFeed, MeetingBooking, MeetingEvening, RoomBooking, TeacherSubjectClass,
                              TeacherUnavailability, TeachingRoom, TimetableConfiguration, TimetableSlot,
                              TimetableVersion, TimeSlot)
from users.models import Staff, Subject, User

from .factories import TenantTestCase, make_admin, make_class, make_school, make_student, make_teacher

DAYS = ['MON', 'TUE', 'WED', 'THU', 'FRI']


def next_weekday(code):
    today = timezone.localdate()
    target = DAYS.index(code)
    return today + timedelta(days=(target - today.weekday()) % 7 or 7)


@override_settings(RUN_TASKS_EAGERLY=True)
class Base(TenantTestCase):

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha')
        self.admin = make_admin(self.school)
        users = [make_teacher(self.school, username=f'alpha{n}', full_name=n) for n in ('Maths T', 'English T')]
        self.c1 = make_class(self.school, 'JSS 1A')
        set_current_tenant(self.school)
        for u in users:
            u.email = f'{u.username}@example.com'
            u.save()
        self.maths_t, self.eng_t = [Staff.objects.get(user=u) for u in users]
        self.maths_user, self.eng_user = users
        self.maths, self.eng = [Subject.objects.create(tenant=self.school, name=n) for n in ('Mathematics', 'English')]
        self.periods = []
        for i, (name, brk) in enumerate([('P1', False), ('P2', False), ('Break', True), ('P3', False)]):
            self.periods.append(TimeSlot.objects.create(tenant=self.school, name=name, start_time=time(8 + i, 0),
                                                        end_time=time(8 + i, 40), order=i, is_break=brk))
        self.config = TimetableConfiguration.objects.create(tenant=self.school, name='Term 1', active_days=DAYS,
                                                            is_active=True)
        self.config.time_slots.set(self.periods)
        for staff, subject in ((self.maths_t, self.maths), (self.eng_t, self.eng)):
            TeacherSubjectClass.objects.create(tenant=self.school, config=self.config, staff=staff, subject=subject,
                                               school_class=self.c1, periods_per_week=3)
        self.lab = TeachingRoom.objects.create(tenant=self.school, name='Main hall', kind='HALL')
        self.tt = generate_timetable(self.config.pk)
        self.tt.publish()

    def _get(self, path, **kw):
        return self.client.get(path, HTTP_HOST=self.host(self.school), **kw)

    def _post(self, path, data=None, **kw):
        return self.client.post(path, data or {}, HTTP_HOST=self.host(self.school), **kw)


class RoomBookingTests(Base):

    def test_book_and_refuse_an_overlap(self):
        d = next_weekday('MON')
        created, skipped = scheduling.book_room(self.lab, d, time(13, 0), time(14, 0), 'PTA meeting', self.admin)
        self.assertEqual((len(created), skipped), (1, []))
        with self.assertRaises(scheduling.SchedulingError):
            scheduling.book_room(self.lab, d, time(14, 0), time(13, 0), 'Backwards', self.admin)
        _, skipped = scheduling.book_room(self.lab, d, time(13, 30), time(15, 0), 'Debate', self.admin)
        self.assertEqual(len(skipped), 1)
        self.assertIn('PTA meeting', skipped[0][1][0])
        # Touching end-to-start is fine.
        created, _ = scheduling.book_room(self.lab, d, time(14, 0), time(15, 0), 'Debate', self.admin)
        self.assertEqual(len(created), 1)

    def test_timetabled_lessons_in_the_room_block_a_booking(self):
        e = TimetableSlot.objects.filter(timetable=self.tt).first()
        e.room = self.lab
        e.save()
        d = next_weekday(e.day)
        problems = scheduling.room_conflicts(self.lab, d, e.time_slot.start_time, e.time_slot.end_time)
        self.assertTrue(any('timetabled' in p for p in problems))

    def test_weekly_repeat_skips_clashing_weeks(self):
        d = next_weekday('TUE')
        scheduling.book_room(self.lab, d + timedelta(weeks=1), time(15, 0), time(16, 0), 'Exam', self.admin)
        created, skipped = scheduling.book_room(self.lab, d, time(15, 0), time(16, 0), 'Chess club', self.admin,
                                                repeat_weeks=3)
        self.assertEqual(len(created), 2)
        self.assertEqual(skipped[0][0], d + timedelta(weeks=1))

    def test_page_books_and_only_the_booker_or_admin_cancels(self):
        d = next_weekday('WED')
        self.client.force_login(self.maths_user)
        r = self._post('/timetable/rooms/bookings/', {'date': d.isoformat(), 'room': self.lab.pk, 'start': '15:00',
                                                      'end': '16:00', 'purpose': 'Maths clinic'})
        self.assertEqual(r.status_code, 302)
        b = RoomBooking.objects.get()
        self.assertEqual(b.booked_by, self.maths_user)
        self.assertContains(self._get(f'/timetable/rooms/bookings/?date={d.isoformat()}'), 'Maths clinic')
        self.client.force_login(self.eng_user)
        self._post('/timetable/rooms/bookings/', {'date': d.isoformat(), 'action': 'cancel', 'id': b.pk})
        b.refresh_from_db()
        self.assertFalse(b.cancelled)
        self.client.force_login(self.admin)
        self._post('/timetable/rooms/bookings/', {'date': d.isoformat(), 'action': 'cancel', 'id': b.pk})
        b.refresh_from_db()
        self.assertTrue(b.cancelled)


class FreeFinderTests(Base):

    def test_free_only_when_every_teacher_is_free(self):
        TeacherUnavailability.objects.create(tenant=self.school, config=self.config, staff=self.eng_t, day='MON',
                                             time_slot=self.periods[0])
        grid = scheduling.free_grid(self.tt, [self.maths_t, self.eng_t])
        busy = {(e.day, e.time_slot_id) for e in self.tt.slots.all()}
        busy.add(('MON', self.periods[0].pk))
        for row in grid['rows']:
            self.assertFalse(row['period'].is_break)
            for c in row['cells']:
                self.assertEqual(c['free'], (c['day'], row['period'].pk) not in busy)
        self.assertEqual(grid['free_count'], 5 * 3 - len(busy))

    def test_date_limits_to_one_day_and_counts_room_bookings(self):
        d = next_weekday('THU')
        scheduling.book_room(self.lab, d, time(8, 0), time(8, 30), 'Assembly rehearsal', self.admin)
        grid = scheduling.free_grid(self.tt, [], self.lab, d)
        self.assertEqual([c for c, _ in grid['days']], ['THU'])
        first = grid['rows'][0]['cells'][0]
        self.assertFalse(first['free'])
        self.assertIn('Assembly rehearsal', first['reasons'][0])

    def test_page(self):
        self.client.force_login(self.maths_user)
        r = self._get(f'/timetable/free-periods/?t={self.maths_t.pk}&t={self.eng_t.pk}')
        self.assertContains(r, 'free period')


class VersionTests(Base):

    def test_publishing_saves_a_version_and_compare_shows_moves(self):
        self.client.force_login(self.admin)
        self._post(f'/timetable/admin/generated/{self.tt.pk}/publish/')
        v1 = TimetableVersion.objects.get(config=self.config)
        self.assertEqual((v1.number, v1.entry_count), (1, 6))
        e = self.tt.slots.exclude(subject=self.eng).first()     # the generator is random: pick one that will change
        TimetableSlot.objects.filter(pk=e.pk).update(subject=self.eng, teacher=self.eng_t)
        diff = scheduling.compare(v1.entries, scheduling.snapshot(self.tt))
        self.assertEqual(len(diff), 1)
        self.assertEqual(diff[0]['kind'], 'changed')
        self.assertContains(self._get(f'/timetable/admin/config/{self.config.pk}/versions/?a={v1.pk}'), '1</b> lesson')

    def test_restore_brings_an_old_version_back(self):
        v1 = scheduling.save_version(self.tt, self.admin)
        TimetableSlot.objects.filter(timetable=self.tt).delete()
        tt2, skipped = scheduling.restore(v1, self.admin)
        self.assertEqual(skipped, 0)
        self.assertTrue(tt2.is_published)
        self.assertEqual(tt2.slots.count(), 6)
        self.tt.refresh_from_db()
        self.assertFalse(self.tt.is_published)
        self.assertEqual(TimetableVersion.objects.filter(config=self.config).count(), 2)

    def test_restore_skips_lessons_of_deleted_teachers(self):
        v1 = scheduling.save_version(self.tt, self.admin)
        self.eng_t.delete()
        _, skipped = scheduling.restore(v1, self.admin)
        self.assertEqual(skipped, 3)

    def test_teachers_cannot_see_versions(self):
        self.client.force_login(self.maths_user)
        self.assertEqual(self._get(f'/timetable/admin/config/{self.config.pk}/versions/').status_code, 302)


class MeetingTests(Base):

    def setUp(self):
        super().setUp()
        self.ada = make_student(self.school, 'A/001', full_name='Ada Obi', school_class=self.c1,
                                parent_email='mum@example.com')
        self.ben = make_student(self.school, 'A/002', full_name='Ben Obi', school_class=self.c1,
                                parent_email='mum@example.com')
        self.ev = MeetingEvening.objects.create(tenant=self.school, title='Consultation',
                                                date=timezone.localdate() + timedelta(days=5),
                                                start_time=time(12, 0), end_time=time(13, 0), slot_minutes=10)
        self.parent = User.objects.create_user(username=f'parent_{self.ada.id}', password='x', tenant=self.school,
                                               is_parent=True, email='mum@example.com')

    def test_slots_and_teachers_from_the_timetable(self):
        self.assertEqual(len(scheduling.slot_times(self.ev)), 6)
        names = [s.full_name for s, _ in scheduling.teachers_for(self.ev, self.ada)]
        self.assertEqual(names, ['English T', 'Maths T'])

    def test_booking_rules(self):
        scheduling.book_meeting(self.ev, self.maths_t, self.ada, time(12, 0))
        with self.assertRaises(scheduling.SchedulingError):      # same teacher, same time, another family
            other = make_student(self.school, 'A/003', school_class=self.c1, parent_email='dad@example.com')
            scheduling.book_meeting(self.ev, self.maths_t, other, time(12, 0))
        with self.assertRaises(scheduling.SchedulingError):      # same family can't be in two places
            scheduling.book_meeting(self.ev, self.eng_t, self.ben, time(12, 0))
        with self.assertRaises(scheduling.SchedulingError):      # one slot per teacher per child
            scheduling.book_meeting(self.ev, self.maths_t, self.ada, time(12, 30))
        with self.assertRaises(scheduling.SchedulingError):      # not a slot start
            scheduling.book_meeting(self.ev, self.eng_t, self.ada, time(12, 5))
        self.ev.is_open = False
        with self.assertRaises(scheduling.SchedulingError):
            scheduling.book_meeting(self.ev, self.eng_t, self.ada, time(12, 10))

    def test_class_limited_session_hides_other_classes(self):
        c2 = make_class(self.school, 'JSS 2A')
        self.ev.classes.set([c2])
        self.assertEqual(scheduling.teachers_for(self.ev, self.ada), [])

    def test_parent_books_and_cancels_with_emails(self):
        self.client.force_login(self.parent)
        page = self._get('/timetable/meetings/book/')
        self.assertContains(page, 'Maths T')
        self.assertContains(page, 'Ben Obi')
        self._post('/timetable/meetings/book/', {'evening': self.ev.pk, 'child': self.ada.pk,
                                                 'teacher': self.maths_t.pk, 'time': '12:20', 'note': 'Fractions'})
        b = MeetingBooking.objects.get()
        self.assertEqual((b.start_time, b.note), (time(12, 20), 'Fractions'))
        self.assertTrue(any('mum@example.com' in m.to for m in mail.outbox))
        mail.outbox.clear()
        self._post('/timetable/meetings/book/', {'evening': self.ev.pk, 'action': 'cancel', 'id': b.pk})
        self.assertFalse(MeetingBooking.objects.exists())
        self.assertTrue(any('cancelled' in m.subject for m in mail.outbox))

    def test_another_familys_booking_cannot_be_cancelled(self):
        other = make_student(self.school, 'A/009', school_class=self.c1, parent_email='dad@example.com')
        b = scheduling.book_meeting(self.ev, self.maths_t, other, time(12, 0))
        self.client.force_login(self.parent)
        self._post('/timetable/meetings/book/', {'evening': self.ev.pk, 'action': 'cancel', 'id': b.pk})
        self.assertTrue(MeetingBooking.objects.filter(pk=b.pk).exists())

    def test_admin_creates_a_session_and_teacher_sees_bookings(self):
        self.client.force_login(self.admin)
        d = (timezone.localdate() + timedelta(days=9)).isoformat()
        self._post('/timetable/admin/meetings/', {'title': 'Second term', 'date': d, 'start': '10:00',
                                                  'end': '11:00', 'minutes': '15', 'classes': [self.c1.pk]})
        ev = MeetingEvening.objects.get(title='Second term')
        self.assertEqual(len(scheduling.slot_times(ev)), 4)
        scheduling.book_meeting(ev, self.eng_t, self.ada, time(10, 15))
        self.assertContains(self._get(f'/timetable/admin/meetings/{ev.pk}/'), 'Ada Obi')
        self.client.force_login(self.eng_user)
        self.assertContains(self._get('/timetable/meetings/mine/'), 'Ada Obi')


class CalendarTests(Base):

    def test_teacher_feed_has_weekly_lessons(self):
        feed = ical.feed_for(self.maths_user, self.school)
        self.assertEqual(ical.feed_for(self.maths_user, self.school).pk, feed.pk)
        r = self._get(f'/timetable/calendar/{feed.token}.ics')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r['Content-Type'], 'text/calendar; charset=utf-8')
        body = r.content.decode()
        self.assertTrue(body.startswith('BEGIN:VCALENDAR\r\n'))
        self.assertEqual(body.count('BEGIN:VEVENT'), 3)
        self.assertIn('RRULE:FREQ=WEEKLY;UNTIL=', body)
        self.assertIn('Mathematics — JSS 1A', body)
        self.assertNotIn('English', body.split('BEGIN:VEVENT', 1)[1])
        self.assertTrue(all(len(line.encode()) <= 75 for line in body.split('\r\n')))

    def test_reset_kills_the_old_link_and_other_schools_cannot_use_it(self):
        old = ical.feed_for(self.maths_user, self.school).token
        new = ical.reset_feed(self.maths_user, self.school).token
        self.assertNotEqual(old, new)
        self.assertEqual(self._get(f'/timetable/calendar/{old}.ics').status_code, 404)
        other = make_school('beta')
        r = self.client.get(f'/timetable/calendar/{new}.ics', HTTP_HOST=self.host(other))
        self.assertEqual(r.status_code, 404)

    def test_parent_feed_has_meetings(self):
        ada = make_student(self.school, 'A/001', school_class=self.c1, parent_email='mum@example.com')
        ev = MeetingEvening.objects.create(tenant=self.school, title='Consultation', date=date.today() + timedelta(days=3),
                                           start_time=time(12, 0), end_time=time(13, 0))
        scheduling.book_meeting(ev, self.maths_t, ada, time(12, 10))
        parent = User.objects.create_user(username='mum', password='x', tenant=self.school, is_parent=True,
                                          email='mum@example.com')
        body = ical.build(parent, self.school)
        self.assertIn('Meeting: Maths T', body)
        self.assertNotIn('RRULE', body)

    def test_sync_page(self):
        self.client.force_login(self.maths_user)
        r = self._get('/timetable/calendar/')
        self.assertContains(r, 'webcal://')
        self.assertEqual(CalendarFeed.objects.count(), 1)


class DisplayAndPrintTests(Base):

    def test_bell_display_lists_todays_periods(self):
        self.client.force_login(self.maths_user)
        r = self._get('/timetable/bells/')
        self.assertEqual(r.status_code, 200)
        if timezone.localdate().weekday() < 5:
            self.assertContains(r, 'P1')

    def test_teacher_prints_own_timetable_and_admin_prints_all(self):
        self.client.force_login(self.maths_user)
        r = self._get('/timetable/my-timetable/print/')
        self.assertContains(r, 'Maths T — teaching timetable')
        self.assertNotContains(r, 'English T — teaching timetable')
        self.assertEqual(self._get(f'/timetable/admin/generated/{self.tt.pk}/print/teachers/').status_code, 302)
        self.client.force_login(self.admin)
        r = self._get(f'/timetable/admin/generated/{self.tt.pk}/print/teachers/')
        self.assertContains(r, 'English T — teaching timetable')
        self.assertContains(r, 'Maths T — teaching timetable')
