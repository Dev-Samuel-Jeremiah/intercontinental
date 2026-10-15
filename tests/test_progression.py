"""
Subject streams, end-of-session promotion by the school's rules, and
graduation with verifiable certificates.
"""
from datetime import timedelta
from decimal import Decimal

from django.test import override_settings
from django.utils import timezone

from academics.models import AcademicSession, Term
from curriculum import progression as prog
from curriculum.models import ClassProgression, PromotionDecision, PromotionPolicy, Stream, StudentStream
from results.models import TermResult
from tenants.middleware import set_current_tenant
from users.models import Attendance, Class, ClassSubject, Student, StudentSubject, Subject

from .factories import TenantTestCase, make_admin, make_class, make_school, make_student, make_teacher


class Base(TenantTestCase):

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha')
        self.admin = make_admin(self.school)
        set_current_tenant(self.school)
        today = timezone.localdate()
        self.session = AcademicSession.objects.create(tenant=self.school, name='2025/2026',
                                                      start_date=today - timedelta(days=300), is_active=True)
        self.terms = [Term.objects.create(tenant=self.school, name=n, session=self.session,
                                          start_date=today - timedelta(days=300 - 100 * i),
                                          end_date=today - timedelta(days=220 - 100 * i))
                      for i, n in enumerate(['1st Term', '2nd Term', '3rd Term'])]
        self.subjects = {n: Subject.objects.create(tenant=self.school, name=n) for n in
                         ['English', 'Mathematics', 'Civic', 'Biology', 'Physics', 'Chemistry',
                          'Literature', 'Government', 'Economics', 'Agric']}

    def _get(self, path, **kw):
        return self.client.get(path, HTTP_HOST=self.host(self.school), **kw)

    def _post(self, path, data=None, **kw):
        return self.client.post(path, data or {}, HTTP_HOST=self.host(self.school), **kw)

    def _klass(self, name, subjects=()):
        c = make_class(self.school, name)
        set_current_tenant(self.school)
        for n in subjects:
            ClassSubject.objects.create(tenant=self.school, school_class=c, subject=self.subjects[n])
        return c

    def _scores(self, student, cls, marks, terms=None):
        """marks: {subject name: total} for every term given."""
        for term in terms or self.terms:
            for name, total in marks.items():
                TermResult.objects.create(tenant=self.school, student=student, class_assigned=cls,
                                          subject=self.subjects[name], session=self.session, term=term,
                                          ca1_score=min(total, 30), exam_score=max(0, total - 30))


class StreamTests(Base):

    def setUp(self):
        super().setUp()
        self.ss1 = self._klass('SS 1A', self.subjects)
        self.science = Stream.objects.create(tenant=self.school, name='Science', levels='SS 1, SS 2, SS 3',
                                             min_electives=1, max_electives=2, min_subjects=6, max_subjects=7)
        self.science.compulsory.set([self.subjects[n] for n in ['English', 'Mathematics', 'Civic',
                                                                 'Biology', 'Physics']])
        self.science.electives.set([self.subjects[n] for n in ['Chemistry', 'Agric', 'Economics']])
        self.ada = make_student(self.school, 'STU001', full_name='Ada Bello', school_class=self.ss1)

    def ids(self, *names):
        return [self.subjects[n].id for n in names]

    def test_stream_setup_checks(self):
        self.assertEqual(prog.check_stream(self.science), [])
        self.science.max_electives = 5
        self.assertTrue(prog.check_stream(self.science))

    def test_applies_to_levels(self):
        jss = self._klass('JSS 1A')
        self.assertTrue(self.science.applies_to(self.ss1))
        self.assertFalse(self.science.applies_to(jss))
        self.assertTrue(Stream(levels='').applies_to(jss))

    def test_choosing_sets_the_subject_list(self):
        prog.choose_stream(self.ada, self.science, self.ids('Chemistry'), self.admin)
        mine = set(StudentSubject.objects.filter(student=self.ada).values_list('subject__name', flat=True))
        self.assertEqual(mine, {'English', 'Mathematics', 'Civic', 'Biology', 'Physics', 'Chemistry'})
        self.assertTrue(prog.offers(self.ada, self.subjects['Chemistry'].id))
        self.assertFalse(prog.offers(self.ada, self.subjects['Literature'].id))
        # Changing electives replaces the old ones.
        prog.choose_stream(self.ada, self.science, self.ids('Agric', 'Economics'), self.admin)
        self.assertFalse(StudentSubject.objects.filter(student=self.ada, subject=self.subjects['Chemistry']).exists())
        self.assertEqual(StudentStream.objects.get(student=self.ada).electives.count(), 2)

    def test_elective_rules(self):
        for bad in ([], self.ids('Chemistry', 'Agric', 'Economics'), self.ids('Literature')):
            with self.assertRaises(prog.ProgressionError):
                prog.check_choice(self.ada, self.science, bad)

    def test_subject_must_be_taught_in_the_class(self):
        ss1b = self._klass('SS 1B', ['English', 'Mathematics', 'Civic', 'Biology', 'Physics'])
        bola = make_student(self.school, 'STU002', school_class=ss1b)
        with self.assertRaises(prog.ProgressionError) as raised:
            prog.check_choice(bola, self.science, self.ids('Chemistry'))
        self.assertIn('Chemistry is not taught in SS 1B', str(raised.exception))

    def test_dropped_subjects_are_marked_not_offering_only_without_marks(self):
        term = self.terms[2]
        empty = TermResult.objects.create(tenant=self.school, student=self.ada, class_assigned=self.ss1,
                                          subject=self.subjects['Literature'], session=self.session, term=term)
        marked = TermResult.objects.create(tenant=self.school, student=self.ada, class_assigned=self.ss1,
                                           subject=self.subjects['Government'], session=self.session, term=term,
                                           ca1_score=20, exam_score=40)
        _, kept = prog.choose_stream(self.ada, self.science, self.ids('Chemistry'))
        empty.refresh_from_db(); marked.refresh_from_db()
        self.assertTrue(empty.is_not_offering)
        self.assertFalse(marked.is_not_offering)
        self.assertEqual(marked.total_score, 60)
        self.assertEqual(kept, 1)

    def test_score_entry_starts_new_rows_as_not_offering(self):
        prog.choose_stream(self.ada, self.science, self.ids('Chemistry'))
        self.client.force_login(self.admin)
        self._get(f'/results/manual-scores/?class={self.ss1.id}&subject={self.subjects["Literature"].id}'
                  f'&term={self.terms[2].id}')
        self.assertTrue(TermResult.objects.get(student=self.ada, subject=self.subjects['Literature']).is_not_offering)

    def test_streams_page(self):
        self.client.force_login(self.admin)
        self.assertEqual(self._get('/curriculum/streams/').status_code, 200)
        self.assertContains(self._get(f'/curriculum/streams/?class={self.ss1.id}'), 'Ada Bello')
        self._post('/curriculum/streams/', {'action': 'assign', 'class_id': self.ss1.id,
                                            f'stream_{self.ada.id}': self.science.id,
                                            f'electives_{self.ada.id}': self.ids('Chemistry')})
        self.assertEqual(StudentStream.objects.get(student=self.ada).stream, self.science)
        self._post('/curriculum/streams/', {'name': 'Arts', 'levels': 'SS 1', 'compulsory': self.ids('English'),
                                            'electives': self.ids('Literature', 'Government'),
                                            'min_electives': 1, 'max_electives': 2, 'min_subjects': 2,
                                            'max_subjects': 3})
        self.assertTrue(Stream.objects.filter(name='Arts').exists())
        teacher = make_teacher(self.school)
        self.client.force_login(teacher)
        self._post('/curriculum/streams/', {'name': 'Commercial'})
        self.assertFalse(Stream.objects.filter(name='Commercial').exists())


class NextClassTests(Base):

    def test_guesses(self):
        names = ['Primary 6', 'JSS 1A', 'JSS 1B', 'JSS 2A', 'JSS 2B', 'JSS 3A', 'SS 1A', 'SS 3A', 'Grade 11', 'Grade 12']
        cls = {n: self._klass(n) for n in names}
        allc = list(Class.objects.filter(tenant=self.school))
        g = lambda n: prog.guess_next(cls[n], allc)
        self.assertEqual(g('JSS 1B')[0], cls['JSS 2B'])
        self.assertEqual(g('JSS 1A')[0], cls['JSS 2A'])
        self.assertEqual(g('JSS 3A')[0], cls['SS 1A'])
        self.assertEqual(g('Primary 6')[0], cls['JSS 1A'])
        self.assertEqual(g('SS 3A'), (None, True))
        self.assertEqual(g('Grade 12'), (None, True))
        self.assertEqual(g('Grade 11')[0], cls['Grade 12'])

    def test_saved_choice_wins(self):
        a, b, c = self._klass('JSS 1A'), self._klass('JSS 2A'), self._klass('Special')
        ClassProgression.objects.create(tenant=self.school, from_class=a, to_class=c)
        self.assertEqual(prog.next_for(a), (c, False))


class PromotionTests(Base):

    def setUp(self):
        super().setUp()
        self.jss1 = self._klass('JSS 1A', ['English', 'Mathematics', 'Civic'])
        self.jss2 = self._klass('JSS 2A')
        self.policy = PromotionPolicy.objects.create(tenant=self.school, pass_average=50, conditional_band=5,
                                                     subject_pass_mark=40)
        self.policy.must_pass.set([self.subjects['English'], self.subjects['Mathematics']])

    def _student(self, adm, name, marks):
        s = make_student(self.school, adm, full_name=name, school_class=self.jss1)
        set_current_tenant(self.school)
        self._scores(s, self.jss1, marks)
        return s

    def test_outcomes(self):
        good = self._student('S1', 'Good', {'English': 70, 'Mathematics': 65, 'Civic': 60})
        near = self._student('S2', 'Near', {'English': 50, 'Mathematics': 45, 'Civic': 45})       # 46.7
        one_fail = self._student('S3', 'OneFail', {'English': 70, 'Mathematics': 35, 'Civic': 70})  # avg 58
        weak = self._student('S4', 'Weak', {'English': 30, 'Mathematics': 30, 'Civic': 40})
        none = make_student(self.school, 'S5', full_name='NoResults', school_class=self.jss1)
        set_current_tenant(self.school)
        out = {d.student.full_name: d for d in prog.prepare(self.jss1, self.session, self.admin)}
        self.assertEqual(out['Good'].outcome, 'PROMOTE')
        self.assertEqual(out['Good'].to_class, self.jss2)
        self.assertEqual(out['Near'].outcome, 'CONDITIONAL')
        self.assertEqual(out['OneFail'].outcome, 'CONDITIONAL')
        self.assertIn('Mathematics', out['OneFail'].reasons)
        self.assertEqual(out['Weak'].outcome, 'REPEAT')
        self.assertEqual(out['NoResults'].outcome, 'REPEAT')
        self.assertEqual(out['Good'].average, Decimal('65.00'))

    def test_last_term_basis(self):
        s = make_student(self.school, 'S1', full_name='Improver', school_class=self.jss1)
        set_current_tenant(self.school)
        self._scores(s, self.jss1, {'English': 30, 'Mathematics': 30, 'Civic': 30}, self.terms[:2])
        self._scores(s, self.jss1, {'English': 70, 'Mathematics': 70, 'Civic': 70}, self.terms[2:])
        self.assertEqual(prog.prepare(self.jss1, self.session)[0].outcome, 'REPEAT')
        self.policy.basis = 'LAST'
        self.policy.save()
        self.assertEqual(prog.prepare(self.jss1, self.session)[0].outcome, 'PROMOTE')

    def test_attendance_rule(self):
        s = self._student('S1', 'Absentee', {'English': 70, 'Mathematics': 70, 'Civic': 70})
        self.policy.min_attendance = 80
        self.policy.save()
        t = self.terms[0]
        for i in range(10):
            Attendance.objects.create(tenant=self.school, student=s, school_class=self.jss1,
                                      date=t.start_date + timedelta(days=i), status='P' if i < 5 else 'A')
        d = prog.prepare(self.jss1, self.session)[0]
        self.assertEqual(d.attendance_rate, 50)
        self.assertEqual(d.outcome, 'CONDITIONAL')

    def test_override_needs_a_reason_and_survives_recalculation(self):
        s = self._student('S1', 'Weak', {'English': 30, 'Mathematics': 30, 'Civic': 30})
        d = prog.prepare(self.jss1, self.session)[0]
        with self.assertRaises(prog.ProgressionError):
            prog.override(d, 'PROMOTE', '', self.admin)
        prog.override(d, 'PROMOTE', 'Was ill all of second term', self.admin)
        d = prog.prepare(self.jss1, self.session)[0]
        self.assertEqual((d.suggested, d.outcome), ('REPEAT', 'PROMOTE'))
        self.assertTrue(d.overridden)

    @override_settings(RUN_TASKS_EAGERLY=True)
    def test_apply_moves_students_and_is_final(self):
        good = self._student('S1', 'Good', {'English': 70, 'Mathematics': 65, 'Civic': 60})
        weak = self._student('S2', 'Weak', {'English': 30, 'Mathematics': 30, 'Civic': 30})
        good.parent_phone = '08031234567'
        good.save()
        decisions = prog.prepare(self.jss1, self.session, self.admin)
        counts = prog.apply(decisions, self.admin, notify=True)
        self.assertEqual(counts, {'PROMOTE': 1, 'REPEAT': 1})
        good.refresh_from_db(); weak.refresh_from_db()
        self.assertEqual((good.class_assigned, good.current_term), (self.jss2, '1st Term'))
        self.assertEqual(weak.class_assigned, self.jss1)
        # Recalculating doesn't touch applied decisions, and applying again does nothing.
        prog.prepare(self.jss2, self.session)
        self.assertEqual(prog.apply(list(PromotionDecision.objects.all()), self.admin), {})
        with self.assertRaises(prog.ProgressionError):
            prog.override(PromotionDecision.objects.get(student=weak), 'PROMOTE', 'x', self.admin)

    def test_apply_needs_a_next_class(self):
        lonely = self._klass('Remedial', ['English'])
        s = make_student(self.school, 'S9', school_class=lonely)
        set_current_tenant(self.school)
        self._scores(s, lonely, {'English': 80})
        decisions = prog.prepare(lonely, self.session)
        self.assertEqual(decisions[0].outcome, 'PROMOTE')
        with self.assertRaises(prog.ProgressionError):
            prog.apply(decisions)

    def test_pages(self):
        self._student('S1', 'Good', {'English': 70, 'Mathematics': 65, 'Civic': 60})
        weak = self._student('S2', 'Weak', {'English': 30, 'Mathematics': 30, 'Civic': 30})
        self.client.force_login(self.admin)
        self.assertEqual(self._get('/curriculum/promotion/').status_code, 200)
        url = f'/curriculum/promotion/{self.jss1.id}/?session={self.session.id}'
        self.assertContains(self._get(url), 'Weak')
        d = PromotionDecision.objects.get(student=weak)
        # Applying saves the changed decision first.
        self._post(url, {'session': self.session.id, 'action': 'apply', f'outcome_{d.id}': 'PROMOTE',
                         f'reason_{d.id}': 'Board decision'})
        weak.refresh_from_db()
        self.assertEqual(weak.class_assigned, self.jss2)
        self._post('/curriculum/promotion/', {'action': 'policy', 'pass_average': 45, 'conditional_band': 0,
                                              'subject_pass_mark': 40, 'min_subjects_passed': 2,
                                              'min_attendance': 0, 'basis': 'SESSION'})
        self.policy.refresh_from_db()
        self.assertEqual((self.policy.pass_average, self.policy.min_subjects_passed), (45, 2))
        teacher = make_teacher(self.school)
        self.client.force_login(teacher)
        self.assertEqual(self._get('/curriculum/promotion/').status_code, 302)


class GraduationTests(Base):

    def setUp(self):
        super().setUp()
        self.ss3 = self._klass('SS 3A', ['English'])
        self.ada = make_student(self.school, 'STU001', full_name='Ada Bello', school_class=self.ss3)
        set_current_tenant(self.school)
        self._scores(self.ada, self.ss3, {'English': 75})

    def test_final_class_graduates(self):
        decisions = prog.prepare(self.ss3, self.session, self.admin)
        self.assertEqual(decisions[0].outcome, 'GRADUATE')
        prog.apply(decisions, self.admin)
        self.ada.refresh_from_db()
        self.assertEqual(self.ada.status, 'Graduated')
        self.assertEqual(self.ada.leaving_class, 'SS 3A')

    def test_certificate_verifies(self):
        with self.assertRaises(prog.ProgressionError):
            prog.certificate(self.ada)
        prog.apply(prog.prepare(self.ss3, self.session), self.admin)
        self.ada.refresh_from_db()
        cert = prog.certificate(self.ada)
        self.assertEqual(prog.verify_certificate(cert['reference'], self.school)['student'], self.ada)
        self.assertIsNone(prog.verify_certificate(cert['reference'] + 'x', self.school))
        self.assertIsNone(prog.verify_certificate(cert['reference'], make_school('beta')))
        set_current_tenant(self.school)
        self.client.force_login(self.admin)
        self.assertContains(self._get('/curriculum/graduation/certificates/'), 'Ada Bello')
        self.client.logout()
        r = self._get(f'/curriculum/graduation/verify/{cert["reference"]}/')
        self.assertContains(r, 'verified')
        self.assertEqual(self._get('/curriculum/graduation/verify/forged/').status_code, 404)
