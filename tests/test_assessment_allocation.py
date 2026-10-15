"""
The school's continuous-assessment structure (enforced at score entry and
shown on report cards) and the who-teaches-what grid.
"""
from datetime import timedelta

from django.utils import timezone

from academics.models import AcademicSession, Term
from curriculum import allocation
from results import assessment
from results.models import AssessmentScheme, TermResult
from tenants.middleware import set_current_tenant
from users.models import ClassSubject, Staff, StaffSubjectClass, Subject

from .factories import TenantTestCase, make_admin, make_class, make_school, make_student, make_teacher


class Base(TenantTestCase):

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha')
        self.admin = make_admin(self.school)
        self.teacher_user = make_teacher(self.school)
        self.jss1 = make_class(self.school, 'JSS 1A')
        set_current_tenant(self.school)
        self.teacher = Staff.objects.get(user=self.teacher_user)
        today = timezone.localdate()
        self.session = AcademicSession.objects.create(tenant=self.school, name='2026/2027',
                                                      start_date=today - timedelta(days=60), is_active=True)
        self.term = Term.objects.create(tenant=self.school, name='1st Term', session=self.session,
                                        start_date=today - timedelta(days=30), end_date=today + timedelta(days=60),
                                        is_active=True)
        self.maths = Subject.objects.create(tenant=self.school, name='Mathematics')
        self.english = Subject.objects.create(tenant=self.school, name='English')
        ClassSubject.objects.create(tenant=self.school, school_class=self.jss1, subject=self.maths)
        ClassSubject.objects.create(tenant=self.school, school_class=self.jss1, subject=self.english)
        self.ada = make_student(self.school, 'STU001', full_name='Ada Bello', school_class=self.jss1)

    def _get(self, path, **kw):
        return self.client.get(path, HTTP_HOST=self.host(self.school), **kw)

    def _post(self, path, data=None, **kw):
        return self.client.post(path, data or {}, HTTP_HOST=self.host(self.school), **kw)

    def _result(self, **scores):
        return TermResult.objects.create(tenant=self.school, student=self.ada, class_assigned=self.jss1,
                                         subject=self.maths, session=self.session, term=self.term, **scores)


class SchemeRuleTests(Base):

    def test_default_matches_the_old_fixed_columns(self):
        scheme = AssessmentScheme.for_tenant(self.school)
        self.assertIsNone(scheme.pk)
        self.assertEqual([(c['label'], c['max']) for c in scheme.columns],
                         [('TRT', 10), ('Homework', 10), ('Class Part.', 10), ('Exam', 70)])
        self.assertEqual((scheme.total, scheme.ca_max), (100, 30))

    def test_columns_must_add_up_to_100_and_have_an_exam(self):
        two_tests = dict(assessment.PRESETS['two_tests'][1])
        assessment.check(two_tests)
        with self.assertRaises(assessment.AssessmentError):
            assessment.check({**two_tests, 'exam': ('Exam', 50)})
        with self.assertRaises(assessment.AssessmentError):
            assessment.check({**two_tests, 'ca1': ('1st Test', 80), 'exam': ('Exam', 0)})
        with self.assertRaises(assessment.AssessmentError):
            assessment.check({**two_tests, 'ca2': ('1st Test', 20)})

    def test_every_preset_is_valid(self):
        for name, (_, structure) in assessment.PRESETS.items():
            assessment.check(structure)

    def test_cannot_lower_a_column_below_marks_already_entered(self):
        self._result(ca1_score=10, exam_score=60)
        with self.assertRaises(assessment.AssessmentError) as raised:
            assessment.save(self.school, assessment.PRESETS['cbt_theory'][1], self.term)  # exam 70 → 40
        self.assertIn('Objective', str(raised.exception))
        scheme = assessment.save(self.school, assessment.PRESETS['two_tests'][1], self.term)
        self.assertEqual(scheme.summary, '1st Test 20 + 2nd Test 20 + Exam 60')

    def test_clean_score(self):
        self.assertEqual(assessment.clean_score('7.5', 10, 'TRT'), 7.5)
        self.assertEqual(assessment.clean_score('', 10, 'TRT'), 0)
        for bad in ('11', '-1', 'abc'):
            with self.assertRaises(assessment.AssessmentError):
                assessment.clean_score(bad, 10, 'TRT')


class ScoreEntryTests(Base):

    def _entry(self, data):
        url = f'/results/manual-scores/?class={self.jss1.id}&subject={self.maths.id}&term={self.term.id}'
        self._get(url)  # creates the result rows
        tr = TermResult.objects.get(student=self.ada, subject=self.maths)
        payload = {'class': self.jss1.id, 'subject': self.maths.id, 'term': self.term.id}
        payload.update({f'{k}_{tr.id}': v for k, v in data.items()})
        return tr, self._post('/results/manual-scores/', payload)

    def test_over_the_maximum_saves_nothing(self):
        self.client.force_login(self.admin)
        tr, _ = self._entry({'ca1': 8, 'ca2': 9, 'ca3': 10, 'exam': 77})
        tr.refresh_from_db()
        self.assertEqual(tr.total_score, 0)

    def test_scores_follow_the_schools_structure(self):
        assessment.save(self.school, assessment.PRESETS['two_tests'][1], self.term)
        self.client.force_login(self.admin)
        r = self._get(f'/results/manual-scores/?class={self.jss1.id}&subject={self.maths.id}&term={self.term.id}')
        self.assertContains(r, '1st Test')
        self.assertNotContains(r, 'Class Part.')
        tr, _ = self._entry({'ca1': 18.5, 'ca2': 15, 'ca3': 9, 'exam': 55})
        tr.refresh_from_db()
        # The switched-off third column is ignored; half marks are kept.
        self.assertEqual((tr.ca1_score, tr.ca3_score, tr.total_score), (18.5, 0, 88.5))

    def test_essay_column_counts_when_used(self):
        assessment.save(self.school, assessment.PRESETS['cbt_theory'][1], self.term)
        self.client.force_login(self.admin)
        tr, _ = self._entry({'ca1': 10, 'ca2': 12, 'essay': 25, 'exam': 35})
        tr.refresh_from_db()
        self.assertEqual(tr.total_score, 82)

    def test_settings_page(self):
        self.client.force_login(self.teacher_user)
        self.assertEqual(self._get('/results/assessment/').status_code, 302)
        self.client.force_login(self.admin)
        self.assertEqual(self._get('/results/assessment/').status_code, 200)
        self._post('/results/assessment/', {'ca1_label': 'Test', 'ca1_max': 30, 'ca2_label': '', 'ca2_max': 0,
                                            'ca3_label': '', 'ca3_max': 0, 'essay_label': '', 'essay_max': 0,
                                            'exam_label': 'Exam', 'exam_max': 60})  # adds up to 90
        self.assertFalse(AssessmentScheme.objects.exists())
        self._post('/results/assessment/', {'preset': 'waec_style'})
        self.assertEqual(AssessmentScheme.objects.get().exam_max, 60)


class AllocationTests(Base):

    def test_matrix_shows_gaps(self):
        StaffSubjectClass.objects.create(tenant=self.school, staff=self.teacher, subject=self.maths,
                                         school_class=self.jss1)
        grid = allocation.matrix(self.school)
        self.assertEqual(grid['gaps'], 1)
        states = {r['subject'].name: r['cells'][0]['state'] for r in grid['rows']}
        self.assertEqual(states, {'English': 'gap', 'Mathematics': 'ok'})

    def test_set_teacher_replaces_and_adds_the_subject_to_the_class(self):
        other = Staff.objects.get(user=make_teacher(self.school, username='alpha2', full_name='Second'))
        set_current_tenant(self.school)
        jss2 = make_class(self.school, 'JSS 2A')
        set_current_tenant(self.school)
        allocation.set_teacher(self.school, self.maths, self.jss1, self.teacher)
        allocation.set_teacher(self.school, self.maths, self.jss1, other)
        self.assertEqual(list(StaffSubjectClass.objects.filter(subject=self.maths).values_list('staff', flat=True)),
                         [other.id])
        allocation.set_teacher(self.school, self.maths, jss2, other)
        self.assertTrue(ClassSubject.objects.filter(school_class=jss2, subject=self.maths).exists())
        allocation.set_teacher(self.school, self.maths, jss2, None)
        self.assertFalse(StaffSubjectClass.objects.filter(school_class=jss2).exists())

    def test_loads_flag_idle_and_heavy_teachers(self):
        rows = {r['staff'].full_name: r for r in allocation.loads(self.school)['rows']}
        self.assertEqual(rows['Teacher One']['state'], 'idle')
        for i in range(allocation.HEAVY_ALLOCATIONS + 1):
            cls = make_class(self.school, f'C{i}')
            set_current_tenant(self.school)
            StaffSubjectClass.objects.create(tenant=self.school, staff=self.teacher, subject=self.maths, school_class=cls)
        rows = {r['staff'].full_name: r for r in allocation.loads(self.school)['rows']}
        self.assertEqual(rows['Teacher One']['state'], 'heavy')

    def test_page_and_permissions(self):
        self.client.force_login(self.teacher_user)
        self.assertEqual(self._get('/curriculum/allocation/').status_code, 200)
        self._post('/curriculum/allocation/', {f'cell_{self.maths.id}_{self.jss1.id}': self.teacher.id})
        self.assertFalse(StaffSubjectClass.objects.exists())
        self.client.force_login(self.admin)
        self._post('/curriculum/allocation/', {f'cell_{self.maths.id}_{self.jss1.id}': self.teacher.id,
                                               f'cell_{self.english.id}_{self.jss1.id}': ''})
        self.assertEqual(list(StaffSubjectClass.objects.values_list('subject__name', flat=True)), ['Mathematics'])
        self.assertContains(self._get('/curriculum/allocation/'), 'Teacher One')
