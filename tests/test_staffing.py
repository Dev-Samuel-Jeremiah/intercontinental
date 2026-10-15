"""
Cover for absent teachers, lesson observations, and clubs.
"""
from datetime import time, timedelta

from django.core import mail
from django.test import override_settings
from django.utils import timezone

from curriculum import staffing as sf
from curriculum.models import (Club, ClubAttendance, ClubMember, CoverAssignment, Department, LessonObservation,
                               ObservationTemplate)
from safety.models import StaffLeave
from tenants.middleware import set_current_tenant
from timetable.models import GeneratedTimetable, TimeSlot, TimetableConfiguration, TimetableSlot
from users.models import Staff, StaffSubjectClass, Subject, User

from .factories import TenantTestCase, make_admin, make_class, make_school, make_student, make_teacher

DAYS = ['MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT', 'SUN']


class Base(TenantTestCase):

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha')
        self.admin = make_admin(self.school)
        self.jss1 = make_class(self.school, 'JSS 1A')
        self.jss2 = make_class(self.school, 'JSS 2A')
        users = {n: make_teacher(self.school, username=f'alpha{n.lower()}', full_name=n)
                 for n in ['Absent', 'Busy', 'Free', 'Maths2']}
        set_current_tenant(self.school)
        for u in users.values():
            u.email = f'{u.username}@example.com'
            u.save()
        self.users = users
        self.staff = {n: Staff.objects.get(user=u) for n, u in users.items()}
        self.maths = Subject.objects.create(tenant=self.school, name='Mathematics')
        self.english = Subject.objects.create(tenant=self.school, name='English')
        StaffSubjectClass.objects.create(tenant=self.school, staff=self.staff['Maths2'], subject=self.maths,
                                         school_class=self.jss2)
        self.today = timezone.localdate()
        self.day = self.today + timedelta(days=1)
        while self.day.weekday() > 4:
            self.day += timedelta(days=1)
        code = DAYS[self.day.weekday()]
        self.p1 = TimeSlot.objects.create(tenant=self.school, name='Period 1', start_time=time(8, 0),
                                          end_time=time(8, 40), order=1)
        p2 = TimeSlot.objects.create(tenant=self.school, name='Period 2', start_time=time(8, 40),
                                     end_time=time(9, 20), order=2)
        cfg = TimetableConfiguration.objects.create(tenant=self.school, name='T', is_active=True,
                                                    active_days=DAYS[:5])
        tt = GeneratedTimetable.objects.create(tenant=self.school, config=cfg, is_published=True)
        self.lesson = TimetableSlot.objects.create(tenant=self.school, timetable=tt, school_class=self.jss1, day=code,
                                                   time_slot=self.p1, subject=self.maths, teacher=self.staff['Absent'])
        TimetableSlot.objects.create(tenant=self.school, timetable=tt, school_class=self.jss1, day=code,
                                     time_slot=p2, subject=self.english, teacher=self.staff['Absent'])
        TimetableSlot.objects.create(tenant=self.school, timetable=tt, school_class=self.jss2, day=code,
                                     time_slot=self.p1, subject=self.english, teacher=self.staff['Busy'])

    def _get(self, path, **kw):
        return self.client.get(path, HTTP_HOST=self.host(self.school), **kw)

    def _post(self, path, data=None, **kw):
        return self.client.post(path, data or {}, HTTP_HOST=self.host(self.school), **kw)


class CoverTests(Base):

    def _leave(self):
        return StaffLeave.objects.create(tenant=self.school, staff=self.staff['Absent'], start_date=self.day,
                                         end_date=self.day, status=StaffLeave.APPROVED)

    def test_teachers_on_leave_show_with_their_lessons(self):
        self._leave()
        rows = sf.absences(self.school, self.day)
        self.assertEqual([r['teacher'] for r in rows], [self.staff['Absent']])
        self.assertEqual(len(rows[0]['lessons']), 2)
        self.assertEqual(sf.absences(self.school, self.day - timedelta(days=7)), [])

    def test_named_absence_without_leave(self):
        rows = sf.absences(self.school, self.day, [self.staff['Absent'].id, self.staff['Free'].id])
        self.assertEqual([r['teacher'] for r in rows], [self.staff['Absent']])  # Free has no lessons

    def test_free_teachers_exclude_busy_and_prefer_subject_teachers(self):
        self._leave()
        free = [r['staff'].full_name for r in sf.free_teachers(self.lesson, self.day)]
        self.assertNotIn('Busy', free)
        self.assertNotIn('Absent', free)
        self.assertEqual(free[0], 'Maths2')
        self.assertIn('Free', free)

    @override_settings(RUN_TASKS_EAGERLY=True)
    def test_assign_emails_and_blocks_double_booking(self):
        c = sf.assign_cover(self.lesson, self.day, self.staff['Free'], self.admin, 'Exercise 3 page 40')
        self.assertEqual(c.absent_teacher, self.staff['Absent'])
        c.refresh_from_db()
        self.assertIsNotNone(c.notified_at)
        self.assertIn('Exercise 3 page 40', mail.outbox[-1].body)
        self.assertEqual(mail.outbox[-1].to, ['alphafree@example.com'])
        # Free is now covering period 1, so can't take another period-1 lesson — and Busy never could.
        with self.assertRaises(sf.StaffingError):
            sf.assign_cover(self.lesson, self.day, self.staff['Busy'], self.admin)
        self.assertNotIn('Free', [r['staff'].full_name for r in sf.free_teachers(
            TimetableSlot.objects.get(teacher=self.staff['Busy']), self.day)])

    def test_cover_rules(self):
        with self.assertRaises(sf.StaffingError):
            sf.assign_cover(self.lesson, self.day + timedelta(days=1), self.staff['Free'], notify=False)  # wrong weekday
        past = self.day - timedelta(days=7)
        while past >= self.today:
            past -= timedelta(days=7)
        with self.assertRaises(sf.StaffingError):
            sf.assign_cover(self.lesson, past, self.staff['Free'], notify=False)

    def test_cover_teacher_sees_and_can_take_the_register(self):
        sf.assign_cover(self.lesson, self.day, self.staff['Free'], notify=False)
        self.client.force_login(self.users['Free'])
        r = self._get(f'/timetable/lessons/?date={self.day.isoformat()}')
        self.assertContains(r, "You're covering")
        self.assertContains(r, 'for Absent')
        r = self._get(f'/timetable/lessons/{self.lesson.id}/register/?date={self.day.isoformat()}')
        self.assertEqual(r.status_code, 200)
        # Without the cover, they can't.
        CoverAssignment.objects.all().delete()
        r = self._get(f'/timetable/lessons/{self.lesson.id}/register/?date={self.day.isoformat()}')
        self.assertEqual(r.status_code, 302)

    def test_page(self):
        self._leave()
        self.client.force_login(self.admin)
        url = f'/curriculum/cover/?date={self.day.isoformat()}'
        r = self._get(url)
        self.assertContains(r, '2 lessons without cover')
        self._post(url, {'date': self.day.isoformat(), 'slot_id': self.lesson.id, 'cover_id': self.staff['Free'].id})
        self.assertTrue(CoverAssignment.objects.filter(cover_teacher=self.staff['Free']).exists())
        self.client.force_login(self.users['Free'])
        self.assertEqual(self._get(url).status_code, 302)


class ObservationTests(Base):

    def setUp(self):
        super().setUp()
        self.criteria = ObservationTemplate(tenant=self.school).criteria_list
        self.data = {'subject': self.maths.id, 'school_class': self.jss1.id, 'topic': 'Fractions',
                     'strengths': 'Clear', 'improvements': 'More questions',
                     **{f'score_{i}': 3 for i in range(len(self.criteria))}}

    def test_record_and_average(self):
        data = {**self.data, 'score_0': 4, 'score_1': 2}
        obs = sf.record_observation(self.staff['Absent'], self.admin, data, self.criteria)
        self.assertEqual(len(obs.scores), 10)
        self.assertEqual(obs.average, 3.0)
        self.assertEqual(obs.percent, 75)
        self.assertFalse(obs.shared)

    def test_every_criterion_needs_a_valid_score(self):
        for bad in ({**self.data, 'score_3': ''}, {**self.data, 'score_3': 5}):
            with self.assertRaises(sf.StaffingError):
                sf.record_observation(self.staff['Absent'], self.admin, bad, self.criteria)

    def test_who_may_observe(self):
        StaffSubjectClass.objects.create(tenant=self.school, staff=self.staff['Absent'], subject=self.maths,
                                         school_class=self.jss1)
        dept = Department.objects.create(tenant=self.school, name='Sciences', hod=self.staff['Maths2'])
        dept.subjects.add(self.maths)
        self.assertTrue(sf.may_observe(self.users['Maths2'], self.staff['Absent']))
        self.assertTrue(sf.may_observe(self.users['Maths2'], self.staff['Absent'], self.maths))
        self.assertFalse(sf.may_observe(self.users['Maths2'], self.staff['Absent'], self.english))
        self.assertFalse(sf.may_observe(self.users['Free'], self.staff['Absent']))
        self.assertFalse(sf.may_observe(self.users['Maths2'], self.staff['Maths2']))

    def test_teacher_sees_it_only_once_shared_and_can_respond(self):
        obs = sf.record_observation(self.staff['Absent'], self.admin, self.data, self.criteria)
        self.client.force_login(self.users['Absent'])
        self.assertEqual(self._get(f'/curriculum/observations/{obs.id}/').status_code, 302)
        self.assertNotContains(self._get('/curriculum/observations/'), 'Fractions')
        sf.share(obs)
        self.assertEqual(self._get(f'/curriculum/observations/{obs.id}/').status_code, 200)
        self._post(f'/curriculum/observations/{obs.id}/', {'action': 'acknowledge', 'response': 'Will do'})
        obs.refresh_from_db()
        self.assertEqual(obs.teacher_response, 'Will do')
        self.assertIsNotNone(obs.acknowledged_at)
        # Another teacher never sees it.
        self.client.force_login(self.users['Free'])
        self.assertEqual(self._get(f'/curriculum/observations/{obs.id}/').status_code, 302)

    def test_pages(self):
        self.client.force_login(self.admin)
        self.assertEqual(self._get('/curriculum/observations/').status_code, 200)
        url = f'/curriculum/observations/new/?teacher={self.staff["Absent"].id}'
        self.assertContains(self._get(url), 'Subject knowledge')
        r = self._post(url, {**self.data, 'teacher': self.staff['Absent'].id, 'share': '1'})
        obs = LessonObservation.objects.get()
        self.assertRedirects(r, f'/curriculum/observations/{obs.id}/', fetch_redirect_response=False)
        self.assertTrue(obs.shared)
        self._post('/curriculum/observations/', {'action': 'rubric', 'criteria': 'One\nTwo\nThree'})
        self.assertEqual(ObservationTemplate.objects.get().criteria_list, ['One', 'Two', 'Three'])
        self.assertEqual(len(LessonObservation.objects.get().scores), 10)   # past ones keep their criteria


class ClubTests(Base):

    def setUp(self):
        super().setUp()
        self.club = Club.objects.create(tenant=self.school, name='Jets Club', capacity=2, meeting_day='WED')
        self.club.patrons.add(self.staff['Free'])
        self.kids = [make_student(self.school, f'S{i}', full_name=f'Kid {i}', school_class=self.jss1)
                     for i in range(3)]
        set_current_tenant(self.school)

    def test_members_capacity_and_roles(self):
        added, full = sf.add_members(self.club, self.kids)
        self.assertEqual((added, full), (2, True))
        a, b = ClubMember.objects.all()
        sf.set_role(a, 'PRESIDENT')
        with self.assertRaises(sf.StaffingError):
            sf.set_role(b, 'PRESIDENT')
        sf.set_role(b, 'SECRETARY')

    def test_meetings_and_attendance_rates(self):
        self.club.capacity = None
        self.club.save()
        sf.add_members(self.club, self.kids)
        sf.record_meeting(self.club, self.today, [self.kids[0].id, self.kids[1].id], 'Robots')
        sf.record_meeting(self.club, self.today - timedelta(days=7), [self.kids[0].id])
        rates = sf.member_rates(self.club)
        self.assertEqual((rates[self.kids[0].id], rates[self.kids[1].id], rates[self.kids[2].id]), (100, 50, 0))
        # Recording the same day again updates rather than duplicates.
        sf.record_meeting(self.club, self.today, [self.kids[2].id])
        self.assertEqual(self.club.meetings.count(), 2)
        self.assertEqual(ClubAttendance.objects.filter(present=True, meeting__date=self.today).count(), 1)
        with self.assertRaises(sf.StaffingError):
            sf.record_meeting(self.club, self.today + timedelta(days=1), [])

    def test_patrons_run_their_club(self):
        self.assertTrue(sf.may_run(self.users['Free'], self.club))
        self.assertFalse(sf.may_run(self.users['Busy'], self.club))
        self.client.force_login(self.users['Busy'])
        self._post(f'/curriculum/clubs/{self.club.id}/', {'action': 'add', 'student_ids': [self.kids[0].id]})
        self.assertFalse(ClubMember.objects.exists())
        self.client.force_login(self.users['Free'])
        self._post(f'/curriculum/clubs/{self.club.id}/', {'action': 'add', 'student_ids': [self.kids[0].id]})
        self.assertTrue(ClubMember.objects.exists())
        self.assertContains(self._get(f'/curriculum/clubs/{self.club.id}/?class={self.jss1.id}'), 'Kid 1')

    def test_admin_creates_and_family_sees_clubs(self):
        self.client.force_login(self.admin)
        self._post('/curriculum/clubs/', {'name': 'Press Club', 'patrons': [self.staff['Busy'].id],
                                          'meeting_day': 'FRI', 'meeting_time': '14:30'})
        press = Club.objects.get(name='Press Club')
        self.assertEqual(press.meeting_time, time(14, 30))
        sf.add_members(press, [self.kids[0]])
        self.client.force_login(self.kids[0].user)
        self.assertContains(self._get('/curriculum/diary/'), 'Press Club')
        parent = User.objects.create_user(username=f'parent_{self.kids[0].id}', password='x', tenant=self.school,
                                          is_parent=True, email=self.kids[0].parent_email)
        self.client.force_login(parent)
        self.assertContains(self._get('/curriculum/diary/'), 'Press Club')
        self.client.force_login(self.users['Busy'])
        self._post('/curriculum/clubs/', {'name': 'Rogue'})
        self.assertFalse(Club.objects.filter(name='Rogue').exists())
