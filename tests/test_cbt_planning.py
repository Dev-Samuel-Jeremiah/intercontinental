"""
Seating candidates in exam halls and building a clash-free exam timetable.
"""
from datetime import date, timedelta

from django.utils import timezone

from academics.models import PublicHoliday
from cbt import planning
from cbt.models import Exam, ExamHall, ExamTimetable, ExamTimetableEntry, Seat
from tenants.middleware import set_current_tenant
from users.models import ClassSubject, Subject

from .factories import TenantTestCase, make_admin, make_class, make_school, make_student, make_teacher


def next_monday():
    d = timezone.localdate() + timedelta(days=7)
    return d - timedelta(days=d.weekday())


class Base(TenantTestCase):

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha')
        self.admin = make_admin(self.school)
        self.classes = [make_class(self.school, n) for n in ('JSS 1A', 'JSS 1B', 'JSS 2A')]
        set_current_tenant(self.school)
        self.subjects = [Subject.objects.create(tenant=self.school, name=n)
                         for n in ('Mathematics', 'English', 'Basic Science', 'Civic', 'Agric')]

    def _get(self, path, **kw):
        return self.client.get(path, HTTP_HOST=self.host(self.school), **kw)

    def _post(self, path, data=None, **kw):
        return self.client.post(path, data or {}, HTTP_HOST=self.host(self.school), **kw)


class SeatingTests(Base):

    def setUp(self):
        super().setUp()
        self.kids = []
        for c in self.classes:
            for i in range(6):
                self.kids.append(make_student(self.school, f'{c.name}-{i}', full_name=f'{c.name} kid {i}', school_class=c))
        set_current_tenant(self.school)
        now = timezone.now()
        self.exam = Exam.objects.create(tenant=self.school, title='Maths', subject=self.subjects[0],
                                        start_time=now + timedelta(days=1), end_time=now + timedelta(days=1, hours=2))
        self.exam.classes.set(self.classes)
        self.hall = ExamHall.objects.create(tenant=self.school, name='Main hall', rows=4, columns=5)

    def test_everyone_seated_with_mixed_neighbours(self):
        n = planning.allocate(self.exam, [self.hall])
        self.assertEqual(n, 18)
        self.assertEqual(Seat.objects.filter(exam=self.exam).values('student').distinct().count(), 18)
        self.assertEqual(planning.neighbours_same_class(self.exam), 0)

    def test_not_enough_seats(self):
        small = ExamHall.objects.create(tenant=self.school, name='Small', rows=2, columns=5)
        with self.assertRaises(planning.PlanningError) as raised:
            planning.allocate(self.exam, [small])
        self.assertIn('8 more seats', str(raised.exception))
        self.assertEqual(planning.allocate(self.exam, [small, self.hall]), 18)    # two halls together

    def test_reseating_replaces_the_plan(self):
        planning.allocate(self.exam, [self.hall])
        planning.allocate(self.exam, [self.hall])
        self.assertEqual(Seat.objects.filter(exam=self.exam).count(), 18)

    def test_pages_and_student_sees_their_seat(self):
        self.client.force_login(self.admin)
        self._post(f'/cbt/admin/exams/{self.exam.id}/seating/', {'halls': [self.hall.id]})
        r = self._get(f'/cbt/admin/exams/{self.exam.id}/seating/')
        self.assertContains(r, 'Door list')
        self.assertContains(r, 'no neighbours from the same class')
        seat = Seat.objects.get(student=self.kids[0])
        self.assertEqual(seat.label[0], 'ABCD'[seat.row - 1])
        teacher = make_teacher(self.school)
        self.client.force_login(teacher)
        self.assertEqual(self._get(f'/cbt/admin/exams/{self.exam.id}/seating/').status_code, 302)


class TimetableTests(Base):

    def setUp(self):
        super().setUp()
        for c in self.classes:
            for s in self.subjects:
                ClassSubject.objects.create(tenant=self.school, school_class=c, subject=s)
        start = next_monday()
        self.tt = ExamTimetable.objects.create(tenant=self.school, name='First term exams', start_date=start,
                                               end_date=start + timedelta(days=4))
        self.tt.classes.set(self.classes)

    def test_arms_share_a_paper(self):
        p = planning.papers(self.tt)
        self.assertEqual(len(p), 10)                      # 5 subjects × 2 levels (JSS 1 arms together, JSS 2)
        maths_jss1 = next(cl for s, cl in p if s.name == 'Mathematics' and len(cl) == 2)
        self.assertEqual([c.name for c in maths_jss1], ['JSS 1A', 'JSS 1B'])

    def test_no_clashes_and_spread(self):
        unplaced = planning.generate(self.tt)
        self.assertEqual(unplaced, [])
        self.assertEqual(self.tt.entries.count(), 10)
        self.assertEqual(planning.clashes(self.tt), [])
        per_day = {}
        for e in self.tt.entries.prefetch_related('classes'):
            for c in e.classes.all():
                per_day[(c.name, e.date)] = per_day.get((c.name, e.date), 0) + 1
        self.assertTrue(all(v == 1 for v in per_day.values()))     # 5 papers over 5 days → one a day

    def test_weekends_and_holidays_skipped(self):
        PublicHoliday.objects.create(tenant=self.school, date=self.tt.start_date + timedelta(days=2), name='Holiday')
        self.tt.end_date = self.tt.start_date + timedelta(days=6)
        self.tt.save()
        days = {d for d, s in planning.sittings(self.tt)}
        self.assertEqual(len(days), 4)
        self.assertTrue(all(d.weekday() < 5 for d in days))

    def test_too_short_leaves_papers_unplaced(self):
        self.tt.end_date = self.tt.start_date
        self.tt.afternoon = False
        self.tt.save()
        unplaced = planning.generate(self.tt)
        self.assertEqual(len(unplaced), 8)               # one paper per level fits on the single morning

    def test_move_refuses_a_clash(self):
        planning.generate(self.tt)
        a, b = [e for e in self.tt.entries.prefetch_related('classes') if any(c.name == 'JSS 2A' for c in e.classes.all())][:2]
        with self.assertRaises(planning.PlanningError):
            planning.move(a, b.date, b.sitting)
        free = [(d, s) for d, s in planning.sittings(self.tt)
                if not self.tt.entries.filter(date=d, sitting=s, classes__name='JSS 2A').exists()][0]
        planning.move(a, *free)

    def test_create_exam_from_an_entry(self):
        planning.generate(self.tt)
        e = self.tt.entries.first()
        exam = planning.make_exam(e)
        self.assertEqual((exam.subject, exam.published, exam.duration_minutes), (e.subject, False, 90))
        self.assertEqual(set(exam.classes.all()), set(e.classes.all()))
        self.assertEqual(planning.make_exam(e), exam)

    def test_pages_and_families(self):
        self.client.force_login(self.admin)
        r = self._post('/cbt/admin/exam-timetables/', {'name': 'Mocks', 'start_date': self.tt.start_date.isoformat(),
                                                        'end_date': self.tt.end_date.isoformat(), 'afternoon': 'on',
                                                        'paper_minutes': 60, 'classes': [c.id for c in self.classes]})
        tt = ExamTimetable.objects.get(name='Mocks')
        self.assertContains(self._get(f'/cbt/admin/exam-timetables/{tt.id}/'), 'no clashes')
        ada = make_student(self.school, 'S1', full_name='Ada', school_class=self.classes[0])
        set_current_tenant(self.school)
        self.client.force_login(ada.user)
        self.assertNotContains(self._get('/curriculum/diary/'), 'Exam timetable')
        tt.published = True
        tt.save()
        self.assertContains(self._get('/curriculum/diary/'), 'Exam timetable')
