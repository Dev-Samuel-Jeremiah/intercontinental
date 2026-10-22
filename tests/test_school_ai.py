"""
School intelligence: the numpy learning algorithms, the term-by-term memory,
training with champion/challenger promotion, explained predictions, the
knowledge base (sync, audience, answers, teaching, ratings), and who may see
what.
"""
import io
import json
import random
from datetime import date, timedelta

import numpy as np
from django.core.management import call_command

from academics.models import AcademicSession, Term
from curriculum.models import CalendarEvent
from notifications.models import Notification
from results.models import TermResult
from schoolai import knowledge, memory, ml, training
from schoolai.models import AskedQuestion, FeatureRow, KnowledgeItem, ModelVersion, Prediction
from tenants.middleware import set_current_tenant
from users.models import Attendance, Class, Staff, Subject, User

from .factories import TenantTestCase, make_admin, make_class, make_school, make_student, make_teacher


class AlgorithmTests(TenantTestCase):

    def test_ridge_recovers_a_relationship(self):
        rng = np.random.default_rng(1)
        X = rng.normal(size=(300, 3))
        y = 50 + 6 * X[:, 0] - 3 * X[:, 1] + rng.normal(scale=0.5, size=300)
        scaler = ml.Scaler().fit(X)
        w, b = ml.ridge_fit(scaler.transform(X), y, alpha=0.1)
        pred = scaler.transform(X) @ w + b
        self.assertLess(ml.regression_metrics(y, pred)['mae'], 1.0)
        self.assertGreater(abs(w[0]), abs(w[2]) * 5)

    def test_logistic_and_auc(self):
        rng = np.random.default_rng(2)
        X = rng.normal(size=(400, 2))
        y = (X[:, 0] + 0.3 * rng.normal(size=400) > 0.5).astype(float)
        Z = ml.Scaler().fit(X).transform(X)
        w, b = ml.logistic_fit(Z, y)
        m = ml.classification_metrics(y, ml.logistic_predict(Z, w, b))
        self.assertGreater(m['auc'], 0.9)
        self.assertEqual(ml.auc([0, 0, 1, 1], [0.1, 0.2, 0.8, 0.9]), 1.0)
        self.assertEqual(ml.auc([0, 1], [0.5, 0.5]), 0.5)

    def test_missing_values_and_trend(self):
        X = np.array([[1.0, np.nan], [3.0, 4.0], [5.0, np.nan]])
        Z = ml.Scaler().fit(X).transform(X)
        self.assertFalse(np.isnan(Z).any())
        slope, intercept, _ = ml.trend([0, 1, 2, 3], [10, 12, 14, 16])
        self.assertAlmostEqual(slope, 2.0)


class SchoolWithHistory(TenantTestCase):
    """Three terms of results for 60 students, where attendance drives results."""

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha')
        self.admin = make_admin(self.school)
        self.teacher = make_teacher(self.school, username='tolu', full_name='Tolu Ade')
        set_current_tenant(self.school)
        self.cls = make_class(self.school, 'JSS 1A')
        self.other = make_class(self.school, 'JSS 1B')
        self.subjects = [Subject.objects.create(tenant=self.school, name=n) for n in ('Maths', 'English')]
        session = AcademicSession.objects.create(tenant=self.school, name='2025/2026', start_date=date(2025, 9, 8))
        self.terms = [Term.objects.create(tenant=self.school, session=session, name=n, start_date=s, end_date=s + timedelta(days=60))
                      for n, s in (('1st Term', date(2025, 9, 8)), ('2nd Term', date(2026, 1, 5)), ('3rd Term', date(2026, 4, 20)))]
        rng = random.Random(3)
        self.students = []
        tr, att = [], []
        for i in range(60):
            st = make_student(self.school, f'S{i}', f'Student {i}', school_class=self.cls if i % 2 else self.other)
            ability, attend = rng.gauss(55, 14), rng.uniform(55, 99)
            for term in self.terms:
                attend = max(40, min(99, attend + rng.gauss(0, 5)))
                ability += (attend - 80) * 0.25
                for d in range(0, 60, 6):
                    att.append(Attendance(tenant=self.school, student=st, school_class=st.class_assigned,
                                          date=term.start_date + timedelta(days=d),
                                          status='P' if rng.random() * 100 < attend else 'A'))
                for sub in self.subjects:
                    score = max(3, min(98, ability + rng.gauss(0, 6)))
                    tr.append(TermResult(tenant=self.school, student=st, class_assigned=st.class_assigned, subject=sub,
                                         session=session, term=term, exam_score=score, total_score=score))
            self.students.append(st)
        TermResult.objects.bulk_create(tr)
        Attendance.objects.bulk_create(att)
        set_current_tenant(self.school)


class MemoryAndTrainingTests(SchoolWithHistory):

    def test_memory_rows_and_labels(self):
        written = memory.collect(self.school)
        self.assertEqual(written, 180)
        row = FeatureRow.objects.get(student=self.students[0], term=self.terms[0])
        self.assertIn('attendance', row.features)
        self.assertEqual(set(row.labels), {'next_avg', 'fail'})
        self.assertEqual(FeatureRow.objects.get(student=self.students[0], term=self.terms[2]).labels, {})
        self.assertIsNotNone(FeatureRow.objects.get(student=self.students[0], term=self.terms[1]).features['trend'])
        snap = memory.snapshot(self.school)
        self.assertEqual(snap.metrics['enrolled'], 60)

    def test_learning_promotes_only_better_models(self):
        run = training.learn(self.school, 'MANUAL', self.admin)
        self.assertTrue(run.ok, run.steps)
        avg = training.active(self.school, 'NEXT_AVG')
        risk = training.active(self.school, 'FAIL_RISK')
        self.assertIsNotNone(avg)
        self.assertLess(avg.metrics['validation']['mae'], 15)
        self.assertGreater(risk.metrics['validation']['auc'], 0.75)
        self.assertEqual(Prediction.objects.filter(kind='FAIL_RISK').count(), 60)
        p = Prediction.objects.filter(kind='FAIL_RISK').order_by('-value').first()
        self.assertTrue(p.reasons)
        self.assertIn(p.reasons[0]['feature'], memory.FEATURE_KEYS)
        # The same data again: the challenger is not better, the champion stays
        training.learn(self.school)
        self.assertEqual(training.active(self.school, 'NEXT_AVG').pk, avg.pk)
        self.assertTrue(ModelVersion.objects.filter(kind='NEXT_AVG', status='REJECTED').exists())
        # A weak champion is replaced by a better challenger
        params = dict(avg.params, w=[0.0] * len(avg.params['w']), b=20.0)
        ModelVersion.objects.filter(pk=avg.pk).update(params=params)
        training.learn(self.school)
        self.assertNotEqual(training.active(self.school, 'NEXT_AVG').pk, avg.pk)
        self.assertEqual(ModelVersion.objects.get(pk=avg.pk).status, 'RETIRED')

    def test_high_risk_students_are_reported_once(self):
        training.learn(self.school)
        high = Prediction.objects.filter(kind='FAIL_RISK', band='HIGH').count()
        notes = Notification.objects.filter(title__startswith='School AI:')
        self.assertEqual(notes.count(), 1 if high else 0)

    def test_maturity_and_command(self):
        self.assertEqual(training.maturity(self.school)['key'], 'COLLECTING')
        out = io.StringIO()
        call_command('school_ai_learn', stdout=out)
        self.assertIn('Train: next-term average', out.getvalue())
        self.assertIn(training.maturity(self.school)['key'], ('LEARNING', 'RELIABLE'))


class TooLittleDataTests(TenantTestCase):

    def test_collecting_mode(self):
        school = make_school('beta')
        set_current_tenant(school)
        run = training.learn(school)
        self.assertTrue(run.ok)
        waiting = [s for s in run.steps if s['ok'] is None]
        self.assertTrue(any('Needs at least' in s['result'] for s in waiting))
        self.assertFalse(ModelVersion.objects.exclude(kind='ENROLMENT').exists())
        self.assertEqual(training.maturity(school)['label'], 'Collecting')


class KnowledgeTests(TenantTestCase):

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha')
        self.admin = make_admin(self.school)
        self.teacher = make_teacher(self.school, username='tolu', full_name='Tolu Ade')
        set_current_tenant(self.school)
        self.pupil = make_student(self.school, 'A1', 'Ada Obi', school_class=make_class(self.school, 'JSS 1'))
        session = AcademicSession.objects.create(tenant=self.school, name='2026/2027', start_date=date(2026, 9, 7))
        Term.objects.create(tenant=self.school, session=session, name='1st Term', start_date=date(2026, 9, 14),
                            end_date=date(2026, 12, 11), is_active=True)
        CalendarEvent.objects.create(tenant=self.school, title='Inter-house sports day', start_date=date(2026, 11, 20),
                                     audience='ALL', venue='School field')
        CalendarEvent.objects.create(tenant=self.school, title='Staff appraisal meeting', start_date=date(2026, 10, 2),
                                     audience='STAFF')
        set_current_tenant(self.school)
        knowledge.sync(self.school)

    def test_sync_and_audience(self):
        titles = set(KnowledgeItem.objects.values_list('title', flat=True))
        self.assertTrue(any('sports day' in t.lower() for t in titles))
        self.assertTrue(any('1st Term 2026/2027' in t for t in titles))
        staff_hits = [i.title for i, _ in knowledge.search(self.school, 'staff appraisal meeting', 'STAFF')]
        family_hits = [i.title for i, _ in knowledge.search(self.school, 'staff appraisal meeting', 'FAMILY')]
        self.assertTrue(any('appraisal' in t.lower() for t in staff_hits))
        self.assertFalse(any('appraisal' in t.lower() for t in family_hits))
        CalendarEvent.objects.filter(title='Inter-house sports day').delete()
        knowledge.sync(self.school)
        self.assertFalse(KnowledgeItem.objects.get(title__icontains='sports day').active)

    def test_answer_unsure_teach_and_learn(self):
        q, _ = knowledge.ask(self.school, self.pupil.user, 'When does the first term resume?')
        self.assertEqual(q.status, 'ANSWERED')
        self.assertIn('14 September 2026', q.answer)
        q2, _ = knowledge.ask(self.school, self.pupil.user, 'What colour is the school uniform?')
        self.assertEqual(q2.status, 'UNSURE')
        self.assertTrue(Notification.objects.filter(title='The school AI has questions it could not answer').exists())
        knowledge.teach(q2, self.admin, 'Navy blue trousers or skirt, white shirt and the school tie.',
                        keywords='dress code clothes')
        q2.refresh_from_db()
        self.assertEqual(q2.status, 'RESOLVED')
        self.assertTrue(Notification.objects.filter(recipient=self.pupil.user, title='The school answered your question').exists())
        q3, _ = knowledge.ask(self.school, self.teacher, 'what is the uniform?')
        self.assertEqual(q3.status, 'ANSWERED')
        self.assertIn('Navy blue', q3.answer)
        knowledge.rate(q3, self.teacher, helpful=True)
        self.assertEqual(KnowledgeItem.objects.get(source='ANSWER').helpful, 1)
        with self.assertRaises(knowledge.KnowledgeError):
            knowledge.rate(q3, self.admin, helpful=False)                     # not their question


class PageTests(SchoolWithHistory):

    def get(self, user, path):
        self.client.force_login(user)
        return self.client.get(path, HTTP_HOST=self.host(self.school))

    def test_who_sees_what(self):
        training.learn(self.school)
        self.assertContains(self.get(self.admin, '/school-ai/'), 'School')
        self.assertContains(self.get(self.admin, '/school-ai/models/FAIL_RISK/'), 'Versions')
        self.assertRedirects(self.get(self.teacher, '/school-ai/'), '/school-ai/ask/', fetch_redirect_response=False)
        self.assertRedirects(self.get(self.teacher, '/school-ai/students/'), '/school-ai/ask/', fetch_redirect_response=False)
        self.cls.form_teachers.set([Staff.objects.get(user=self.teacher)])
        page = self.get(self.teacher, '/school-ai/students/').content.decode()
        mine = [s for s in self.students if s.class_assigned_id == self.cls.pk]
        theirs = [s for s in self.students if s.class_assigned_id != self.cls.pk]
        shown = Prediction.objects.filter(kind='FAIL_RISK', band__in=('HIGH', 'MEDIUM', 'LOW'))
        self.assertTrue(any(f'/students/{s.id}/' in page for s in mine if shown.filter(student=s).exists()))
        self.assertFalse(any(f'/students/{s.id}/"' in page for s in theirs))
        pupil = self.students[0]
        self.assertEqual(self.get(pupil.user, '/school-ai/ask/').status_code, 200)
        self.assertContains(self.get(self.admin, f'/students/{pupil.id}/'), 'School AI outlook')
        other_teacher = make_teacher(self.school, username='x', full_name='X')
        self.assertNotContains(self.get(other_teacher, f'/students/{pupil.id}/'), 'School AI outlook')

    def test_ask_api_and_teaching_page(self):
        self.client.force_login(self.students[0].user)
        r = self.client.post('/school-ai/ask/', json.dumps({'question': 'Is there a swimming pool?'}),
                             content_type='application/json', HTTP_HOST=self.host(self.school))
        data = json.loads(r.content)
        self.assertEqual(data['status'], 'UNSURE')
        q = AskedQuestion.objects.get()
        self.client.force_login(self.admin)
        self.client.post('/school-ai/questions/', {'id': q.id, 'op': 'teach', 'answer': 'No, but pupils swim at the city pool on Fridays.',
                                                   'audience': 'ALL'}, HTTP_HOST=self.host(self.school))
        self.assertEqual(KnowledgeItem.objects.filter(source='ANSWER').count(), 1)
        self.assertContains(self.get(self.admin, '/school-ai/knowledge/'), 'swimming pool')
