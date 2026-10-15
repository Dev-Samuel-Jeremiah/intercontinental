"""
Departments, schemes of work and coverage, lesson-note review, homework,
the school calendar and public holidays.
"""
from datetime import date, timedelta

from django.utils import timezone

from academics.models import AcademicSession, PublicHoliday, Term
from curriculum import services as svc
from curriculum.models import CalendarEvent, Department, Homework, LessonNote, SchemeOfWork, SchemeTopic
from tenants.middleware import set_current_tenant
from users.models import ClassSubject, Staff, StaffSubjectClass, Subject, User

from .factories import TenantTestCase, make_admin, make_class, make_school, make_student, make_teacher


class Base(TenantTestCase):

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha')
        self.admin = make_admin(self.school)
        self.teacher_user = make_teacher(self.school)
        self.hod_user = make_teacher(self.school, username='alphahod', full_name='Head Sciences')
        self.jss1 = make_class(self.school, 'JSS 1A')
        set_current_tenant(self.school)
        self.teacher = Staff.objects.get(user=self.teacher_user)
        self.hod = Staff.objects.get(user=self.hod_user)
        self.today = timezone.localdate()
        session = AcademicSession.objects.create(tenant=self.school, name='2026/2027',
                                                 start_date=self.today - timedelta(days=60), is_active=True)
        # Started 30 days ago, so this is week 5.
        self.term = Term.objects.create(tenant=self.school, name='1st Term', session=session,
                                        start_date=self.today - timedelta(days=30),
                                        end_date=self.today + timedelta(days=60), is_active=True)
        self.maths = Subject.objects.create(tenant=self.school, name='Mathematics')
        self.english = Subject.objects.create(tenant=self.school, name='English')
        ClassSubject.objects.create(tenant=self.school, school_class=self.jss1, subject=self.maths)
        ClassSubject.objects.create(tenant=self.school, school_class=self.jss1, subject=self.english)
        StaffSubjectClass.objects.create(tenant=self.school, staff=self.teacher, subject=self.maths,
                                         school_class=self.jss1)
        self.sciences = Department.objects.create(tenant=self.school, name='Sciences', hod=self.hod)
        self.sciences.subjects.add(self.maths)

    def _get(self, path, **kw):
        return self.client.get(path, HTTP_HOST=self.host(self.school), **kw)

    def _post(self, path, data=None, **kw):
        return self.client.post(path, data or {}, HTTP_HOST=self.host(self.school), **kw)

    def _scheme(self, **kw):
        return SchemeOfWork.objects.create(tenant=self.school, subject=self.maths, school_class=self.jss1,
                                           term=self.term, created_by=self.teacher, **kw)


class PermissionTests(Base):

    def test_who_plans_and_reviews(self):
        self.assertTrue(svc.may_plan(self.teacher_user, self.maths, self.jss1))
        self.assertFalse(svc.may_plan(self.teacher_user, self.english, self.jss1))
        self.assertTrue(svc.may_plan(self.hod_user, self.maths, self.jss1))
        self.assertTrue(svc.may_review(self.hod_user, self.maths))
        self.assertFalse(svc.may_review(self.hod_user, self.english))
        self.assertFalse(svc.may_review(self.teacher_user, self.maths))
        self.assertTrue(svc.may_review(self.admin, self.english))

    def test_teaching_pairs(self):
        self.assertEqual(svc.teaching_pairs(self.teacher_user, self.school), [(self.maths, self.jss1)])
        self.assertEqual(svc.teaching_pairs(self.hod_user, self.school), [(self.maths, self.jss1)])
        self.assertEqual(len(svc.teaching_pairs(self.admin, self.school)), 2)


class SchemeTests(Base):

    def test_parse_topics(self):
        pairs = svc.parse_topics('Week 1: Number bases\nFractions\n\n4. Percentages\nRatio', 12)
        self.assertEqual(pairs, [(1, 'Number bases'), (2, 'Fractions'), (4, 'Percentages'), (5, 'Ratio')])
        with self.assertRaises(svc.CurriculumError):
            svc.parse_topics('Week 14: Too late', 12)

    def test_coverage_flags_a_class_falling_behind(self):
        scheme = self._scheme()
        svc.add_topics(scheme, '\n'.join(f'Topic {i}' for i in range(1, 9)))
        self.assertEqual(svc.current_week(self.term), 5)
        for t in scheme.topics.filter(week__lte=2):
            svc.mark_taught(t, self.teacher)
        cov = svc.coverage(scheme)
        self.assertEqual((cov['total'], cov['taught'], cov['due'], cov['behind']), (8, 2, 5, 3))
        self.assertEqual(cov['state'], 'behind')
        for t in scheme.topics.filter(week__lte=5):
            svc.mark_taught(t, self.teacher)
        self.assertEqual(svc.coverage(scheme)['state'], 'on_track')
        svc.mark_taught(scheme.topics.get(week=6), self.teacher)
        self.assertEqual(svc.coverage(scheme)['state'], 'ahead')

    def test_cannot_mark_taught_in_the_future(self):
        t = svc.add_topics(self._scheme(), 'Algebra')[0]
        with self.assertRaises(svc.CurriculumError):
            svc.mark_taught(t, self.teacher, self.today + timedelta(days=1))

    def test_copy_scheme(self):
        source = self._scheme()
        svc.add_topics(source, 'Week 1: Sets\nWeek 2: Venn diagrams')
        jss1b = make_class(self.school, 'JSS 1B')
        set_current_tenant(self.school)
        copy = svc.copy_scheme(source, jss1b, self.term, self.teacher)
        self.assertEqual(list(copy.topics.values_list('week', 'topic')), [(1, 'Sets'), (2, 'Venn diagrams')])
        self.assertFalse(copy.topics.filter(taught_on__isnull=False).exists())
        with self.assertRaises(svc.CurriculumError):
            svc.copy_scheme(source, jss1b, self.term)

    def test_teacher_writes_a_scheme_through_the_page(self):
        self.client.force_login(self.teacher_user)
        r = self._post('/curriculum/schemes/', {'subject': self.maths.id, 'school_class': self.jss1.id,
                                                'term': self.term.id, 'weeks': 12})
        scheme = SchemeOfWork.objects.get()
        self.assertRedirects(r, f'/curriculum/schemes/{scheme.id}/', fetch_redirect_response=False)
        self._post(f'/curriculum/schemes/{scheme.id}/', {'action': 'add', 'topics': 'Week 1: Sets\nIndices'})
        self.assertEqual(scheme.topics.count(), 2)
        topic = scheme.topics.first()
        self._post(f'/curriculum/schemes/{scheme.id}/', {'action': 'taught', 'topic_id': topic.id})
        topic.refresh_from_db()
        self.assertEqual(topic.taught_on, self.today)
        self.assertEqual(self._get(f'/curriculum/schemes/{scheme.id}/').status_code, 200)
        # The teacher can't approve their own scheme; the HOD can.
        self._post(f'/curriculum/schemes/{scheme.id}/', {'action': 'approve'})
        scheme.refresh_from_db()
        self.assertIsNone(scheme.approved_at)
        self.client.force_login(self.hod_user)
        self._post(f'/curriculum/schemes/{scheme.id}/', {'action': 'approve'})
        scheme.refresh_from_db()
        self.assertEqual(scheme.approved_by, self.hod)

    def test_a_teacher_cannot_write_another_subjects_scheme(self):
        self.client.force_login(self.teacher_user)
        self._post('/curriculum/schemes/', {'subject': self.english.id, 'school_class': self.jss1.id,
                                            'term': self.term.id})
        self.assertFalse(SchemeOfWork.objects.exists())


class LessonNoteTests(Base):

    def _note(self, **kw):
        defaults = dict(tenant=self.school, teacher=self.teacher, subject=self.maths, school_class=self.jss1,
                        term=self.term, week=5, title='Fractions',
                        content='Introduction, three worked examples, evaluation and an assignment.')
        defaults.update(kw)
        return LessonNote.objects.create(**defaults)

    def test_submit_and_approve_marks_the_topic_taught(self):
        topic = svc.add_topics(self._scheme(), 'Week 5: Fractions')[0]
        note = self._note(topic=topic)
        svc.submit_note(note)
        self.assertEqual(list(svc.notes_to_review(self.hod_user, self.school)), [note])
        self.assertEqual(list(svc.notes_to_review(self.teacher_user, self.school)), [])
        svc.review_note(note, True, self.hod_user, 'Well done')
        note.refresh_from_db()
        topic.refresh_from_db()
        self.assertEqual(note.status, LessonNote.APPROVED)
        self.assertEqual(note.reviewed_by, self.hod)
        self.assertTrue(topic.taught)

    def test_sending_back_needs_a_comment(self):
        note = svc.submit_note(self._note())
        with self.assertRaises(svc.CurriculumError):
            svc.review_note(note, False, self.hod_user, '')
        svc.review_note(note, False, self.hod_user, 'Add evaluation questions.')
        self.assertEqual(note.status, LessonNote.RETURNED)
        svc.submit_note(note)  # resubmitting after changes is allowed
        self.assertEqual(note.status, LessonNote.SUBMITTED)

    def test_only_the_hod_or_admin_reviews_and_never_their_own(self):
        note = svc.submit_note(self._note())
        with self.assertRaises(svc.CurriculumError):
            svc.review_note(note, True, self.teacher_user)
        english_note = svc.submit_note(self._note(subject=self.english))
        with self.assertRaises(svc.CurriculumError):
            svc.review_note(english_note, True, self.hod_user)
        own = svc.submit_note(self._note(teacher=self.hod))
        with self.assertRaises(svc.CurriculumError):
            svc.review_note(own, True, self.hod_user)
        svc.review_note(english_note, True, self.admin)

    def test_empty_notes_cannot_be_submitted(self):
        with self.assertRaises(svc.CurriculumError):
            svc.submit_note(self._note(content='tbd'))

    def test_the_pages(self):
        self.client.force_login(self.teacher_user)
        r = self._post('/curriculum/notes/new/', {
            'subject': self.maths.id, 'school_class': self.jss1.id, 'week': 5, 'title': 'Ratio',
            'content': 'Introduction, worked examples, class exercise, assignment.', 'submit': '1'})
        note = LessonNote.objects.get()
        self.assertEqual(note.status, LessonNote.SUBMITTED)
        self.assertRedirects(r, f'/curriculum/notes/{note.id}/', fetch_redirect_response=False)
        self.assertEqual(self._get('/curriculum/notes/').status_code, 200)
        # Another teacher can't read it.
        other = make_teacher(self.school, username='alphaother', full_name='Other Teacher')
        self.client.force_login(other)
        self.assertEqual(self._get(f'/curriculum/notes/{note.id}/').status_code, 302)
        self.client.force_login(self.hod_user)
        self.assertContains(self._get('/curriculum/notes/review/'), 'Ratio')
        self._post(f'/curriculum/notes/{note.id}/', {'action': 'return', 'comment': 'Add objectives.'})
        note.refresh_from_db()
        self.assertEqual(note.status, LessonNote.RETURNED)

    def test_teacher_cannot_note_a_subject_they_dont_teach(self):
        self.client.force_login(self.teacher_user)
        self._post('/curriculum/notes/new/', {'subject': self.english.id, 'school_class': self.jss1.id,
                                              'week': 5, 'title': 'Comprehension',
                                              'content': 'A long enough lesson note for English.'})
        self.assertFalse(LessonNote.objects.exists())


class HomeworkTests(Base):

    def test_set_homework_rules(self):
        with self.assertRaises(svc.CurriculumError):
            svc.set_homework(self.teacher, self.jss1, self.maths, 'Ex 1', '', self.today - timedelta(days=1))
        with self.assertRaises(svc.CurriculumError):
            svc.set_homework(self.teacher, self.jss1, self.maths, ' ', '', self.today)
        hw = svc.set_homework(self.teacher, self.jss1, self.maths, 'Exercise 4b', 'Q1-10',
                              self.today + timedelta(days=2))
        self.assertEqual(list(svc.homework_for(self.jss1)), [hw])

    def test_students_and_parents_see_their_class_homework(self):
        ada = make_student(self.school, 'STU001', full_name='Ada Bello', school_class=self.jss1,
                           parent_email='mum@example.com')
        jss2 = make_class(self.school, 'JSS 2A')
        set_current_tenant(self.school)
        svc.set_homework(self.teacher, self.jss1, self.maths, 'Exercise 4b', '', self.today + timedelta(days=2))
        Homework.objects.create(tenant=self.school, school_class=jss2, subject=self.maths, title='Not yours',
                                due_on=self.today + timedelta(days=2))
        CalendarEvent.objects.create(tenant=self.school, title='Inter-house sports', kind='SPORTS',
                                     start_date=self.today + timedelta(days=5))
        CalendarEvent.objects.create(tenant=self.school, title='Staff appraisal', kind='MEETING',
                                     audience='STAFF', start_date=self.today + timedelta(days=5))
        self.client.force_login(ada.user)
        r = self._get('/curriculum/diary/')
        self.assertContains(r, 'Exercise 4b')
        self.assertContains(r, 'Inter-house sports')
        self.assertNotContains(r, 'Not yours')
        self.assertNotContains(r, 'Staff appraisal')
        parent = User.objects.create_user(username=f'parent_{ada.id}', password='x', tenant=self.school,
                                          is_parent=True, email='mum@example.com')
        self.client.force_login(parent)
        r = self._get('/curriculum/diary/')
        self.assertContains(r, 'Exercise 4b')
        self.assertContains(r, 'Ada Bello')
        self.assertNotContains(r, 'Staff appraisal')

    def test_teacher_sets_homework_through_the_page(self):
        self.client.force_login(self.teacher_user)
        self._post('/curriculum/homework/', {'subject': self.maths.id, 'school_class': self.jss1.id,
                                             'title': 'Exercise 5', 'due_on': (self.today + timedelta(days=1)).isoformat()})
        self._post('/curriculum/homework/', {'subject': self.english.id, 'school_class': self.jss1.id,
                                             'title': 'Essay', 'due_on': (self.today + timedelta(days=1)).isoformat()})
        self.assertEqual(list(Homework.objects.values_list('title', flat=True)), ['Exercise 5'])
        self.assertContains(self._get('/curriculum/homework/'), 'Exercise 5')


class CalendarTests(Base):

    def test_easter(self):
        self.assertEqual(svc.easter(2024), date(2024, 3, 31))
        self.assertEqual(svc.easter(2025), date(2025, 4, 20))
        self.assertEqual(svc.easter(2026), date(2026, 4, 5))
        self.assertEqual(svc.easter(2027), date(2027, 3, 28))

    def test_holiday_import_is_idempotent(self):
        added = svc.import_holidays(self.school, 2027)
        self.assertEqual(len(added), 9)
        self.assertTrue(PublicHoliday.objects.filter(date=date(2027, 3, 26), name='Good Friday').exists())
        self.assertEqual(svc.import_holidays(self.school, 2027), [])

    def test_a_holiday_only_changes_its_own_schools_terms(self):
        other = make_school('beta')
        set_current_tenant(other)
        other_session = AcademicSession.objects.create(tenant=other, name='2026/2027', is_active=True,
                                                       start_date=self.term.start_date)
        other_term = Term.objects.create(tenant=other, name='1st Term', session=other_session,
                                         start_date=self.term.start_date, end_date=self.term.end_date)
        set_current_tenant(self.school)
        before_mine, before_theirs = self.term.number_of_school_days, other_term.number_of_school_days
        weekday = self.today + timedelta(days=1)
        while weekday.weekday() > 4:
            weekday += timedelta(days=1)
        PublicHoliday.objects.create(tenant=self.school, date=weekday, name='Special')
        self.term.refresh_from_db()
        other_term.refresh_from_db()
        self.assertEqual(self.term.number_of_school_days, before_mine - 1)
        self.assertEqual(other_term.number_of_school_days, before_theirs)
        # Saving it again must not knock off another day.
        PublicHoliday.objects.get(name='Special').save()
        self.term.refresh_from_db()
        self.assertEqual(self.term.number_of_school_days, before_mine - 1)

    def test_month_grid(self):
        CalendarEvent.objects.create(tenant=self.school, title='Exams', kind='EXAM',
                                     start_date=date(2026, 12, 7), end_date=date(2026, 12, 11))
        PublicHoliday.objects.create(tenant=self.school, date=date(2026, 12, 25), name='Christmas Day')
        weeks = svc.month_grid(self.school, 2026, 12)
        days = {d['date']: d for w in weeks for d in w}
        self.assertEqual(weeks[0][0]['date'].weekday(), 0)
        self.assertEqual([e.title for e in days[date(2026, 12, 9)]['events']], ['Exams'])
        self.assertEqual(days[date(2026, 12, 12)]['events'], [])
        self.assertEqual(days[date(2026, 12, 25)]['holiday'], 'Christmas Day')

    def test_admin_manages_the_calendar(self):
        self.client.force_login(self.admin)
        self._post('/curriculum/calendar/', {'title': 'PTA meeting', 'kind': 'PTA', 'audience': 'ALL',
                                             'start_date': (self.today + timedelta(days=3)).isoformat()})
        self._post('/curriculum/calendar/', {'action': 'holidays', 'year': 2027})
        self.assertTrue(CalendarEvent.objects.filter(title='PTA meeting').exists())
        self.assertEqual(PublicHoliday.objects.filter(date__year=2027).count(), 9)
        self.assertContains(self._get('/curriculum/calendar/'), 'PTA meeting')
        self.client.force_login(self.teacher_user)
        self._post('/curriculum/calendar/', {'title': 'Party', 'start_date': self.today.isoformat()})
        self.assertFalse(CalendarEvent.objects.filter(title='Party').exists())


class PageTests(Base):

    def test_every_staff_page_loads(self):
        scheme = self._scheme()
        svc.add_topics(scheme, 'Sets\nIndices')
        for user in (self.admin, self.teacher_user, self.hod_user):
            self.client.force_login(user)
            for path in ('/curriculum/', '/curriculum/departments/', '/curriculum/schemes/',
                         f'/curriculum/schemes/{scheme.id}/', '/curriculum/notes/', '/curriculum/notes/new/',
                         '/curriculum/homework/', '/curriculum/calendar/'):
                if user == self.admin and '/notes/' in path:
                    continue  # no teacher profile: they review notes rather than write them
                self.assertEqual(self._get(path).status_code, 200, (user.username, path))
        self.client.force_login(self.admin)
        self.assertRedirects(self._get('/curriculum/notes/'), '/curriculum/notes/review/',
                             fetch_redirect_response=False)
        self.assertEqual(self._get('/curriculum/notes/review/').status_code, 200)
        self.client.force_login(self.hod_user)
        self.assertEqual(self._get('/curriculum/notes/review/').status_code, 200)

    def test_departments_are_admin_only(self):
        self.client.force_login(self.teacher_user)
        self._post('/curriculum/departments/', {'name': 'Languages'})
        self.assertFalse(Department.objects.filter(name='Languages').exists())
        self.client.force_login(self.admin)
        self._post('/curriculum/departments/', {'name': 'Languages', 'hod': self.teacher.id,
                                                'subjects': [self.english.id]})
        dept = Department.objects.get(name='Languages')
        self.assertEqual(list(dept.subjects.all()), [self.english])
        self.assertTrue(svc.may_review(self.teacher_user, self.english))

    def test_students_cannot_open_staff_pages(self):
        ada = make_student(self.school, 'STU001', school_class=self.jss1)
        self.client.force_login(ada.user)
        self.assertEqual(self._get('/curriculum/').status_code, 302)
        self.assertEqual(self._get('/curriculum/homework/').status_code, 302)
