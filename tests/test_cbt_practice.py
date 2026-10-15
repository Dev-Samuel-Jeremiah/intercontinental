"""
Practising from the question bank (UTME-style mocks and topic drills) and
topic mastery for students, families and teachers.
"""
import json
from datetime import timedelta

from django.utils import timezone

from cbt import bank, practice
from cbt.models import BankQuestion, PracticeAnswer, PracticeSession
from tenants.middleware import set_current_tenant
from users.models import Staff, Subject, User

from .factories import TenantTestCase, make_admin, make_class, make_school, make_student, make_teacher


class Base(TenantTestCase):

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha')
        self.admin = make_admin(self.school)
        self.teacher_user = make_teacher(self.school)
        self.ss1 = make_class(self.school, 'SS 1A')
        self.jss1 = make_class(self.school, 'JSS 1A')
        set_current_tenant(self.school)
        self.teacher = Staff.objects.get(user=self.teacher_user)
        self.maths = Subject.objects.create(tenant=self.school, name='Mathematics')
        self.physics = Subject.objects.create(tenant=self.school, name='Physics')
        for subject, topics in ((self.maths, ['Algebra', 'Geometry']), (self.physics, ['Motion'])):
            for topic in topics:
                for i in range(6):
                    self._q(subject, topic, f'{subject.name} {topic} {i}', level='SS 1')
        self._q(self.maths, 'Algebra', 'Junior question', level='JSS 1')
        self._q(self.maths, 'Algebra', 'Teacher only', level='', for_practice='')
        self.ada = make_student(self.school, 'S1', full_name='Ada Bello', school_class=self.ss1,
                                parent_email='mum@example.com')
        set_current_tenant(self.school)

    def _q(self, subject, topic, text, level='SS 1', for_practice='on'):
        return bank.save_question(self.school, {'text': text, 'question_type': 'MCQ', 'option_0': 'right',
                                                'option_1': 'wrong', 'option_2': 'also wrong', 'correct': '0',
                                                'topic': topic, 'level': level, 'for_practice': for_practice},
                                  self.teacher, subject=subject)

    def _get(self, path, **kw):
        return self.client.get(path, HTTP_HOST=self.host(self.school), **kw)

    def _post(self, path, data=None, **kw):
        return self.client.post(path, data or {}, HTTP_HOST=self.host(self.school), **kw)

    def _answer_all(self, session, right_topics=()):
        for q in BankQuestion.objects.filter(id__in=[i for v in session.paper.values() for i in v]):
            opt = q.options.get(is_correct=True) if q.topic in right_topics else q.options.filter(is_correct=False).first()
            practice.answer(session, q.id, opt.id)


class PoolTests(Base):

    def test_level_matching_and_practice_flag(self):
        ids = practice.pool(self.ada, self.maths)
        texts = set(BankQuestion.objects.filter(id__in=ids).values_list('text', flat=True))
        self.assertEqual(len(ids), 12)                  # SS 1 questions only (class "SS 1A" matches level "SS 1")
        self.assertNotIn('Junior question', texts)
        self.assertNotIn('Teacher only', texts)
        self.assertEqual([s.name for s, n, t in practice.available(self.ada)], ['Mathematics', 'Physics'])


class SessionTests(Base):

    def test_mock_rules(self):
        with self.assertRaises(practice.PracticeError):
            practice.start(self.ada, [])
        extra = [Subject.objects.create(tenant=self.school, name=f'S{i}').id for i in range(4)]
        with self.assertRaises(practice.PracticeError):
            practice.start(self.ada, [self.maths.id] + extra)          # five subjects
        s = practice.start(self.ada, [self.maths.id, self.physics.id], per_subject=5)
        self.assertEqual(s.total, 10)
        self.assertEqual(s.duration_seconds, 420)          # 10 × 40 s, rounded up to whole minutes
        with self.assertRaises(practice.PracticeError):
            practice.start(self.ada, [self.maths.id])                   # one at a time

    def test_topic_drill_draws_from_that_topic(self):
        s = practice.start(self.ada, [self.maths.id], 'TOPIC', 5, 'Geometry')
        topics = set(BankQuestion.objects.filter(id__in=s.paper[str(self.maths.id)]).values_list('topic', flat=True))
        self.assertEqual(topics, {'Geometry'})

    def test_answer_submit_and_breakdown(self):
        s = practice.start(self.ada, [self.maths.id], per_subject=12)
        self._answer_all(s, right_topics=('Algebra',))
        s = practice.submit(s)
        self.assertEqual((s.score, s.total, s.percent), (6, 12, 50))
        by_subject, by_topic = practice.breakdown(s)
        self.assertEqual(by_subject[0]['percent'], 50)
        self.assertEqual([(t['topic'], t['percent']) for t in by_topic], [('Geometry', 0), ('Algebra', 100)])
        with self.assertRaises(practice.PracticeError):
            practice.answer(s, s.paper[str(self.maths.id)][0], None)    # finished

    def test_cannot_answer_a_question_not_in_the_paper(self):
        s = practice.start(self.ada, [self.physics.id], per_subject=5)
        stranger = BankQuestion.objects.get(text='Teacher only')
        with self.assertRaises(practice.PracticeError):
            practice.answer(s, stranger.id, stranger.options.first().id)

    def test_time_up_marks_on_return(self):
        s = practice.start(self.ada, [self.maths.id], per_subject=5)
        self._answer_all(s, right_topics=('Algebra', 'Geometry'))
        PracticeSession.objects.filter(pk=s.pk).update(started_at=timezone.now() - timedelta(hours=3))
        self.client.force_login(self.ada.user)
        self._get(f'/cbt/practice/{s.id}/')
        s.refresh_from_db()
        self.assertEqual((s.finished, s.score), (True, 5))


class PageTests(Base):

    def test_full_flow(self):
        self.client.force_login(self.ada.user)
        self.assertContains(self._get('/cbt/practice/'), 'Physics')
        r = self._post('/cbt/practice/', {'mode': 'JAMB', 'subjects': [self.maths.id, self.physics.id], 'per_subject': 5})
        s = PracticeSession.objects.get()
        self.assertRedirects(r, f'/cbt/practice/{s.id}/', fetch_redirect_response=False)
        self.assertContains(self._get(f'/cbt/practice/{s.id}/'), 'Mathematics')
        q = BankQuestion.objects.get(pk=s.paper[str(self.physics.id)][0])
        r = self._post(f'/cbt/practice/{s.id}/answer/', json.dumps({'question': q.id, 'option': q.options.get(is_correct=True).id}),
                       content_type='application/json')
        self.assertEqual(json.loads(r.content)['answered'], 1)
        self._post(f'/cbt/practice/{s.id}/')
        r = self._get(f'/cbt/practice/{s.id}/result/')
        self.assertContains(r, '1 / 10')
        self.assertContains(r, 'UTME scale')

    def test_others_cannot_open_a_session(self):
        s = practice.start(self.ada, [self.maths.id], per_subject=5)
        bola = make_student(self.school, 'S2', school_class=self.ss1)
        self.client.force_login(bola.user)
        self.assertEqual(self._get(f'/cbt/practice/{s.id}/').status_code, 404)


class MasteryTests(Base):

    def setUp(self):
        super().setUp()
        s = practice.start(self.ada, [self.maths.id], per_subject=12)
        self._answer_all(s, right_topics=('Algebra',))
        practice.submit(s)

    def test_student_mastery(self):
        m = practice.student_mastery(self.ada)
        topics = {t['topic']: t for t in m[0]['topics']}
        self.assertEqual((topics['Algebra']['label'], topics['Geometry']['label']), ('mastered', 'weak'))

    def test_class_view_finds_who_needs_help(self):
        data = practice.class_mastery(self.ss1, self.maths)
        geo = next(a for a in data['averages'] if a['topic'] == 'Geometry')
        self.assertEqual((geo['percent'], [s.full_name for s in geo['weak']]), (0, ['Ada Bello']))

    def test_exam_answers_on_bank_questions_count(self):
        from cbt.models import Exam, ExamAttempt, ExamPart, Question, StudentAnswer
        from academics.models import AcademicSession, Term
        exam = Exam.objects.create(tenant=self.school, title='T', subject=self.physics, start_time=timezone.now(),
                                   end_time=timezone.now() + timedelta(hours=1))
        part = ExamPart.objects.create(exam=exam, part_type='CBT')
        bq = BankQuestion.objects.filter(subject=self.physics).first()
        bank.add_to_part([bq], part)
        q = Question.objects.get(exam_part=part)
        a = ExamAttempt.objects.create(student=self.ada, exam_part=part, completed=True)
        StudentAnswer.objects.create(attempt=a, question=q, selected_option=q.options.get(is_correct=True))
        tally = practice.answers_by_topic([self.ada], self.physics)
        self.assertEqual(tally[(self.ada.id, self.physics.id, 'Motion')], [1, 1])

    def test_pages_and_access(self):
        self.client.force_login(self.teacher_user)
        self.assertContains(self._get(f'/cbt/mastery/?class={self.ss1.id}&subject={self.maths.id}'), 'Geometry')
        self.assertContains(self._get(f'/cbt/mastery/student/{self.ada.id}/'), 'needs work')
        parent = User.objects.create_user(username=f'parent_{self.ada.id}', password='x', tenant=self.school,
                                          is_parent=True, email='mum@example.com')
        self.client.force_login(parent)
        self.assertContains(self._get(f'/cbt/mastery/student/{self.ada.id}/'), 'Algebra')
        other = make_student(self.school, 'S3', school_class=self.ss1, parent_email='other@example.com')
        self.assertEqual(self._get(f'/cbt/mastery/student/{other.id}/').status_code, 302)
