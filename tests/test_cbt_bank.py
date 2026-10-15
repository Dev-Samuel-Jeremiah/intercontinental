"""
The question bank, putting bank questions on a paper, random pools, and
item analysis after an exam.
"""
from datetime import timedelta

from django.utils import timezone

from academics.models import AcademicSession, Term
from cbt import analysis, bank, engine
from cbt.models import BankQuestion, Exam, ExamAttempt, ExamPart, Option, Question, StudentAnswer
from tenants.middleware import set_current_tenant
from users.models import Staff, Subject

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
        session = AcademicSession.objects.create(tenant=self.school, name='2026/2027',
                                                 start_date=today - timedelta(days=60), is_active=True)
        self.term = Term.objects.create(tenant=self.school, name='1st Term', session=session,
                                        start_date=today - timedelta(days=30), end_date=today + timedelta(days=60),
                                        is_active=True)
        self.maths = Subject.objects.create(tenant=self.school, name='Mathematics')
        self.english = Subject.objects.create(tenant=self.school, name='English')
        now = timezone.now()
        self.exam = Exam.objects.create(tenant=self.school, title='Maths exam', subject=self.maths, session=session,
                                        term=self.term, start_time=now - timedelta(minutes=5),
                                        end_time=now + timedelta(hours=1), published=True, duration_minutes=30,
                                        created_by=self.teacher)
        self.exam.classes.add(self.jss1)
        self.part = ExamPart.objects.create(exam=self.exam, part_type='CBT', duration_minutes=30)

    def _get(self, path, **kw):
        return self.client.get(path, HTTP_HOST=self.host(self.school), **kw)

    def _post(self, path, data=None, **kw):
        return self.client.post(path, data or {}, HTTP_HOST=self.host(self.school), **kw)

    def _bank_q(self, text='2 + 2 = ?', subject=None, **kw):
        data = {'text': text, 'question_type': 'MCQ', 'option_0': '4', 'option_1': '3', 'option_2': '5',
                'correct': '0', 'level': 'JSS 1', 'topic': 'Addition', 'difficulty': 'EASY', 'for_practice': 'on'}
        data.update(kw)
        return bank.save_question(self.school, data, self.teacher, subject=subject or self.maths)


class BankTests(Base):

    def test_save_rules(self):
        q = self._bank_q()
        self.assertEqual((q.options.count(), q.options.get(is_correct=True).text), (3, '4'))
        with self.assertRaises(bank.BankError):
            self._bank_q(correct='-1')                 # no correct option
        with self.assertRaises(bank.BankError):
            self._bank_q(option_1='', option_2='')     # one option only
        tf = self._bank_q('The sun is a star.', question_type='TF', correct='0')
        self.assertEqual([(o.text, o.is_correct) for o in tf.options.all()], [('True', True), ('False', False)])

    def test_search(self):
        self._bank_q()
        self._bank_q('Spell "receive".', subject=self.english, topic='Spelling', level='JSS 2', difficulty='HARD')
        self.assertEqual(bank.search(self.school, subject=self.english).count(), 1)
        self.assertEqual(bank.search(self.school, topic='addit').count(), 1)
        self.assertEqual(bank.search(self.school, difficulty='HARD', level='jss 2').count(), 1)
        self.assertEqual(bank.search(self.school, text='receive').count(), 1)

    def test_add_to_exam_copies_once_and_checks_subject(self):
        q = self._bank_q()
        self.assertEqual(bank.add_to_part([q], self.part), 1)
        self.assertEqual(bank.add_to_part([q], self.part), 0)
        copy = Question.objects.get(exam_part=self.part)
        self.assertEqual((copy.bank_question, copy.options.filter(is_correct=True).first().text), (q, '4'))
        with self.assertRaises(bank.BankError):
            bank.add_to_part([self._bank_q('x', subject=self.english)], self.part)

    def test_exam_questions_into_the_bank(self):
        eq = Question.objects.create(exam_part=self.part, question_text='3 x 3?', question_type='MCQ')
        Option.objects.create(question=eq, text='9', is_correct=True)
        Option.objects.create(question=eq, text='6')
        b = bank.to_bank(eq, self.teacher, 'JSS 1', 'Multiplication')
        self.assertEqual((b.subject, b.topic, b.options.count()), (self.maths, 'Multiplication', 2))
        self.assertEqual(bank.to_bank(eq), b)                     # not twice

    def test_pages(self):
        self.client.force_login(self.teacher_user)
        self._post('/cbt/admin/bank/new/', {'subject': self.maths.id, 'text': 'What is 10 / 2?', 'question_type': 'MCQ',
                                            'option_0': '5', 'option_1': '2', 'correct': '0', 'topic': 'Division'})
        q = BankQuestion.objects.get()
        self.assertContains(self._get(f'/cbt/admin/bank/?subject={self.maths.id}'), 'What is 10 / 2?')
        self._post('/cbt/admin/bank/add-to-exam/', {'part': self.part.id, 'ids': [q.id]})
        self.assertEqual(Question.objects.filter(exam_part=self.part).count(), 1)
        r = self._post('/cbt/admin/bank/new/', {'subject': self.maths.id, 'text': 'Unfinished', 'question_type': 'MCQ',
                                                'option_0': 'a'})
        self.assertContains(r, 'Unfinished')                       # what was typed is kept on error
        other = make_teacher(self.school, username='alpha2', full_name='Other')
        self.client.force_login(other)
        self.assertEqual(self._get(f'/cbt/admin/bank/{q.id}/').status_code, 302)   # not theirs to edit
        self._post('/cbt/admin/bank/add-to-exam/', {'part': self.part.id, 'ids': [q.id]})   # not their exam
        self.assertEqual(Question.objects.filter(exam_part=self.part).count(), 1)


class PoolTests(Base):

    def setUp(self):
        super().setUp()
        for i in range(10):
            Question.objects.create(exam_part=self.part, question_text=f'Q{i}', question_type='TF')
        self.part.pool_size = 4
        self.part.save()
        self.ada = make_student(self.school, 'S1', school_class=self.jss1)
        self.bola = make_student(self.school, 'S2', school_class=self.jss1)
        set_current_tenant(self.school)

    def test_each_candidate_gets_a_fixed_draw(self):
        a = ExamAttempt.objects.create(student=self.ada, exam_part=self.part)
        b = ExamAttempt.objects.create(student=self.bola, exam_part=self.part)
        pa = [q.id for q in engine.paper(a)]
        self.assertEqual(len(pa), 4)
        self.assertEqual(pa, [q.id for q in engine.paper(a)])
        self.assertNotEqual(set(pa), {q.id for q in engine.paper(b)})
        Question.objects.create(exam_part=self.part, question_text='late', question_type='TF')
        self.assertEqual(len(engine.paper(a)), 4)                   # a started paper doesn't grow

    def test_pool_set_from_the_analysis_page(self):
        self.client.force_login(self.teacher_user)
        url = f'/cbt/admin/exams/{self.exam.id}/analysis/'
        self._post(url, {'pool_size': 6})
        self.part.refresh_from_db()
        self.assertEqual(self.part.pool_size, 6)
        self._post(url, {'pool_size': 50})                          # bigger than the section = everyone gets all
        self.part.refresh_from_db()
        self.assertEqual(self.part.pool_size, 0)


class ItemAnalysisTests(Base):

    def setUp(self):
        super().setUp()
        self.good = Question.objects.create(exam_part=self.part, question_text='Good', question_type='MCQ')
        self.g_ok = Option.objects.create(question=self.good, text='right', is_correct=True)
        self.g_no = Option.objects.create(question=self.good, text='wrong')
        self.bad = Question.objects.create(exam_part=self.part, question_text='Bad key', question_type='MCQ')
        self.b_key = Option.objects.create(question=self.bad, text='marked right', is_correct=True)
        self.b_real = Option.objects.create(question=self.bad, text='actually right')
        kids = [make_student(self.school, f'S{i}', school_class=self.jss1) for i in range(8)]
        set_current_tenant(self.school)
        # The 4 strongest answer "good" correctly and pick the real answer on "bad";
        # the 4 weakest get "good" wrong and stumble on the keyed option.
        for i, kid in enumerate(kids):
            strong = i < 4
            a = ExamAttempt.objects.create(student=kid, exam_part=self.part, completed=True,
                                           score=10 - i)
            StudentAnswer.objects.create(attempt=a, question=self.good, selected_option=self.g_ok if strong else self.g_no)
            StudentAnswer.objects.create(attempt=a, question=self.bad, selected_option=self.b_real if strong else self.b_key)

    def test_flags_a_wrong_key(self):
        data = analysis.item_analysis(self.part)
        rows = {r['question'].question_text: r for r in data['rows']}
        self.assertEqual((rows['Good']['p'], rows['Good']['d']), (50, 1.0))
        self.assertEqual(rows['Good']['flags'], [])
        self.assertEqual(rows['Bad key']['d'], -1.0)
        self.assertTrue(any('answer key' in msg for _, msg in rows['Bad key']['flags']))
        self.assertEqual(data['summary']['flagged'], 1)
        dist = {o['option'].text: o['percent'] for o in rows['Bad key']['options']}
        self.assertEqual(dist, {'marked right': 50, 'actually right': 50})

    def test_page(self):
        self.client.force_login(self.admin)
        r = self._get(f'/cbt/admin/exams/{self.exam.id}/analysis/')
        self.assertContains(r, 'check the answer key')
        self.assertContains(r, 'Separates -1.0')
