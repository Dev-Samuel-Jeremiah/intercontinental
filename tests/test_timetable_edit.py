"""
Timetable: teacher availability, double periods and rooms in generation,
hand edits with clash checks, change notifications, load and clash reports,
printable timetables, and exams pausing normal lessons.
"""
import json
from datetime import time, timedelta

from django.core import mail
from django.test import override_settings
from django.utils import timezone

from cbt.models import ExamTimetable
from tenants.middleware import set_current_tenant
from timetable import editing
from timetable.generator import TimetableGenerationError, generate_timetable
from timetable.lessons import lessons_on
from timetable.models import (TeacherSubjectClass, TeacherUnavailability, TeachingRoom, TimetableChange,
                              TimetableConfiguration, TimetableSlot, TimeSlot)
from users.models import Staff, Subject

from .factories import TenantTestCase, make_admin, make_class, make_school, make_teacher

DAYS = ['MON', 'TUE', 'WED', 'THU', 'FRI']


class Base(TenantTestCase):

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha')
        self.admin = make_admin(self.school)
        users = [make_teacher(self.school, username=f'alpha{n}', full_name=n) for n in ('Maths T', 'English T', 'Chem T')]
        self.c1, self.c2 = make_class(self.school, 'JSS 1A'), make_class(self.school, 'JSS 1B')
        set_current_tenant(self.school)
        for u in users:
            u.email = f'{u.username}@example.com'
            u.save()
        self.maths_t, self.eng_t, self.chem_t = [Staff.objects.get(user=u) for u in users]
        self.maths, self.eng, self.chem = [Subject.objects.create(tenant=self.school, name=n)
                                           for n in ('Mathematics', 'English', 'Chemistry')]
        self.periods = []
        start = 8
        for i, (name, brk) in enumerate([('P1', False), ('P2', False), ('Break', True), ('P3', False), ('P4', False)]):
            self.periods.append(TimeSlot.objects.create(tenant=self.school, name=name, start_time=time(start + i, 0),
                                                        end_time=time(start + i, 40), order=i, is_break=brk))
        self.config = TimetableConfiguration.objects.create(tenant=self.school, name='Term 1', active_days=DAYS,
                                                            is_active=True)
        self.config.time_slots.set(self.periods)

    def _assign(self, staff, subject, cls, n, **kw):
        return TeacherSubjectClass.objects.create(tenant=self.school, config=self.config, staff=staff, subject=subject,
                                                  school_class=cls, periods_per_week=n, **kw)

    def _get(self, path, **kw):
        return self.client.get(path, HTTP_HOST=self.host(self.school), **kw)

    def _post(self, path, data=None, **kw):
        return self.client.post(path, data or {}, HTTP_HOST=self.host(self.school), **kw)


class GeneratorTests(Base):

    def test_unavailable_periods_are_respected(self):
        self._assign(self.maths_t, self.maths, self.c1, 6)
        for d in DAYS:
            TeacherUnavailability.objects.create(tenant=self.school, config=self.config, staff=self.maths_t, day=d,
                                                 time_slot=self.periods[0])
        tt = generate_timetable(self.config.pk)
        self.assertFalse(tt.slots.filter(time_slot=self.periods[0]).exists())
        self.assertEqual(tt.slots.count(), 6)

    def test_unavailability_counts_against_capacity(self):
        self._assign(self.maths_t, self.maths, self.c1, 18)
        for d in DAYS:
            for p in self.periods[:2]:
                TeacherUnavailability.objects.create(tenant=self.school, config=self.config, staff=self.maths_t,
                                                     day=d, time_slot=p)
        with self.assertRaises(TimetableGenerationError) as raised:
            generate_timetable(self.config.pk)
        self.assertIn('when they are available', str(raised.exception))

    def test_doubles_are_back_to_back_and_never_across_the_break(self):
        self._assign(self.chem_t, self.chem, self.c1, 4, double_periods=2)
        tt = generate_timetable(self.config.pk)
        order = {p.pk: i for i, p in enumerate(self.periods)}
        by_day = {}
        for e in tt.slots.all():
            by_day.setdefault(e.day, []).append(order[e.time_slot_id])
        pairs = [sorted(v) for v in by_day.values()]
        self.assertEqual(sorted(len(p) for p in pairs), [2, 2])
        for a, b in pairs:
            self.assertEqual(b - a, 1)                       # adjacent, so P2+P3 (across the break) never happens

    def test_lab_lessons_get_a_free_lab(self):
        lab = TeachingRoom.objects.create(tenant=self.school, name='Lab 1', kind='LAB')
        self._assign(self.chem_t, self.chem, self.c1, 3, room_kind='LAB')
        self._assign(self.maths_t, self.chem, self.c2, 3, room_kind='LAB')
        tt = generate_timetable(self.config.pk)
        lab_uses = list(tt.slots.filter(room=lab).values_list('day', 'time_slot'))
        self.assertEqual(len(lab_uses), 6)
        self.assertEqual(len(set(lab_uses)), 6)               # never double-booked

    def test_missing_room_kind_is_explained(self):
        self._assign(self.chem_t, self.chem, self.c1, 2, room_kind='ICT')
        with self.assertRaises(TimetableGenerationError) as raised:
            generate_timetable(self.config.pk)
        self.assertIn('Computer room', str(raised.exception))


class EditTests(Base):

    def setUp(self):
        super().setUp()
        self._assign(self.maths_t, self.maths, self.c1, 2)
        self._assign(self.eng_t, self.eng, self.c1, 2)
        self._assign(self.maths_t, self.maths, self.c2, 2)
        self.tt = generate_timetable(self.config.pk)

    def _free(self, cls, teacher=None):
        for d in DAYS:
            for p in self.periods:
                if p.is_break:
                    continue
                if TimetableSlot.objects.filter(timetable=self.tt, school_class=cls, day=d, time_slot=p).exists():
                    continue
                if teacher and TimetableSlot.objects.filter(timetable=self.tt, teacher=teacher, day=d, time_slot=p).exists():
                    continue
                return d, p

    def test_move_to_a_free_period(self):
        e = TimetableSlot.objects.filter(timetable=self.tt, school_class=self.c1, subject=self.eng).first()
        d, p = self._free(self.c1, self.eng_t)
        editing.move(e, d, p, self.admin, notify=False)
        e.refresh_from_db()
        self.assertEqual((e.day, e.time_slot), (d, p))
        self.assertEqual(TimetableChange.objects.count(), 1)
        self.assertEqual(editing.clash_report(self.tt), [])

    def test_refuses_a_teacher_clash_and_breaks(self):
        maths_c2 = TimetableSlot.objects.filter(timetable=self.tt, school_class=self.c2).first()
        maths_c1 = TimetableSlot.objects.filter(timetable=self.tt, school_class=self.c1, subject=self.maths).first()
        free_c1 = self._free(self.c1)
        # Put c1's maths where the maths teacher already teaches c2 → refused.
        with self.assertRaises(editing.EditError) as raised:
            editing.move(maths_c1, maths_c2.day, maths_c2.time_slot)
        self.assertIn('already teaches JSS 1B', str(raised.exception))
        with self.assertRaises(editing.EditError):
            editing.move(maths_c1, free_c1[0], self.periods[2])        # the break

    def test_dropping_on_another_lesson_swaps(self):
        from timetable.models import GeneratedTimetable
        tt = GeneratedTimetable.objects.create(tenant=self.school, config=self.config)
        P1, P3 = self.periods[0], self.periods[3]
        a = TimetableSlot.objects.create(tenant=self.school, timetable=tt, school_class=self.c1, day='MON', time_slot=P1,
                                         subject=self.eng, teacher=self.eng_t)
        b = TimetableSlot.objects.create(tenant=self.school, timetable=tt, school_class=self.c1, day='MON', time_slot=P3,
                                         subject=self.maths, teacher=self.maths_t)
        editing.move(a, 'MON', P3, notify=False)
        a.refresh_from_db(); b.refresh_from_db()
        self.assertEqual((a.time_slot, b.time_slot), (P3, P1))
        self.assertEqual(TimetableChange.objects.count(), 2)
        # A swap that would clash is refused: the maths teacher is busy with JSS 1B at P3 on Tuesday.
        TimetableSlot.objects.create(tenant=self.school, timetable=tt, school_class=self.c2, day='TUE', time_slot=P3,
                                     subject=self.maths, teacher=self.maths_t)
        c = TimetableSlot.objects.create(tenant=self.school, timetable=tt, school_class=self.c1, day='TUE', time_slot=P1,
                                         subject=self.maths, teacher=self.maths_t)
        TimetableSlot.objects.create(tenant=self.school, timetable=tt, school_class=self.c1, day='TUE', time_slot=P3,
                                     subject=self.eng, teacher=self.eng_t)
        with self.assertRaises(editing.EditError):
            editing.move(c, 'TUE', P3, notify=False)

    @override_settings(RUN_TASKS_EAGERLY=True)
    def test_published_change_emails_the_teacher(self):
        self.tt.publish()
        e = TimetableSlot.objects.filter(timetable=self.tt, school_class=self.c1, subject=self.eng).first()
        d, p = self._free(self.c1, self.eng_t)
        editing.move(e, d, p, self.admin)
        self.assertEqual(mail.outbox[-1].to, ['alphaEnglish T@example.com'])
        self.assertIn('Your timetable has changed', mail.outbox[-1].subject)

    def test_pages_and_endpoints(self):
        self.client.force_login(self.admin)
        self.assertContains(self._get(f'/timetable/admin/generated/{self.tt.pk}/edit/'), 'Drag a lesson')
        e = TimetableSlot.objects.filter(timetable=self.tt, school_class=self.c1, subject=self.eng).first()
        targets = json.loads(self._get(f'/timetable/admin/lessons/{e.pk}/targets/').content)['targets']
        self.assertIn('is a break', targets[f'MON-{self.periods[2].pk}'])
        d, p = self._free(self.c1, self.eng_t)
        r = self._post(f'/timetable/admin/lessons/{e.pk}/move/', json.dumps({'day': d, 'slot': p.pk}),
                       content_type='application/json')
        self.assertTrue(json.loads(r.content)['ok'])
        self.assertContains(self._get(f'/timetable/admin/generated/{self.tt.pk}/reports/'), 'Teaching load')
        self.assertContains(self._get(f'/timetable/admin/generated/{self.tt.pk}/print/'), 'class timetable')
        teacher = self.maths_t.user
        self.client.force_login(teacher)
        self.assertEqual(self._get(f'/timetable/admin/generated/{self.tt.pk}/edit/').status_code, 302)
        r = self._post(f'/timetable/admin/lessons/{e.pk}/move/', json.dumps({'day': 'MON', 'slot': p.pk}),
                       content_type='application/json')
        self.assertEqual(r.status_code, 403)


class ReportTests(Base):

    def test_load_and_clash_report(self):
        self._assign(self.maths_t, self.maths, self.c1, 4)
        tt = generate_timetable(self.config.pk)
        rows, days = editing.teacher_load(tt)
        self.assertEqual(rows[0]['week'], 4)
        self.assertEqual(sum(rows[0]['per_day']), 4)
        self.assertEqual(editing.clash_report(tt), [])
        TimetableSlot.objects.filter(timetable=tt).first().delete()
        self.assertIn('3 of 4 periods', editing.clash_report(tt)[0])


class ExamPauseTests(Base):

    def test_lessons_pause_during_published_exams(self):
        self._assign(self.maths_t, self.maths, self.c1, 10)
        self._assign(self.eng_t, self.eng, self.c2, 10)
        generate_timetable(self.config.pk).publish()
        day = timezone.localdate() + timedelta(days=7)
        day -= timedelta(days=day.weekday())                  # a Monday
        self.assertEqual({e['slot'].school_class for e in lessons_on(self.school, day)}, {self.c1, self.c2})
        et = ExamTimetable.objects.create(tenant=self.school, name='Exams', start_date=day, end_date=day + timedelta(days=4),
                                          published=True)
        et.classes.set([self.c1])
        self.assertEqual({e['slot'].school_class for e in lessons_on(self.school, day)}, {self.c2})


class AvailabilityPageTests(Base):

    def test_mark_periods(self):
        self.client.force_login(self.admin)
        url = f'/timetable/admin/config/{self.config.pk}/availability/'
        self._post(url, {'teacher': self.maths_t.pk, 'off': [f'MON-{self.periods[0].pk}', f'FRI-{self.periods[4].pk}',
                                                                f'MON-{self.periods[2].pk}']})   # the break is ignored
        self.assertEqual(TeacherUnavailability.objects.count(), 2)
        self.assertContains(self._get(url + f'?teacher={self.maths_t.pk}'), 'checked')
        self._post('/timetable/admin/rooms/', {'name': 'Physics lab', 'kind': 'LAB'})
        self.assertTrue(TeachingRoom.objects.filter(name='Physics lab').exists())
