"""
Worked solutions in the answer review: Groq is called in the OpenAI format,
solutions are written once from the teacher's answer key and shared, edits to
a question bring a fresh solution, a disputed key is held back, the page waits
out the provider's per-minute limit, and only the student who sat the exam
(while review is open) can ask.
"""
import json
from datetime import timedelta
from unittest import mock

from django.test import override_settings
from django.utils import timezone

from academics.models import AcademicSession, Term
from assistant.providers import AIError
from cbt import solutions
from cbt.models import Exam, ExamAttempt, ExamPart, Option, Question, QuestionSolution, StudentAnswer
from users.models import Subject

from .factories import TenantTestCase, make_class, make_school, make_student


def groq_reply(payload):
    """A Groq chat-completions body whose content is the JSON the prompt asks for."""
    return {'choices': [{'message': {'content': json.dumps(payload)}}],
            'usage': {'prompt_tokens': 120, 'completion_tokens': 300}}


@override_settings(GROQ_API_KEY='test-key', GROQ_MODEL='openai/gpt-oss-120b')
class SolutionTests(TenantTestCase):

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha')
        jss1 = make_class(self.school, 'JSS 1')
        self.ada = make_student(self.school, 'A1', 'Ada Obi', school_class=jss1)
        self.ben = make_student(self.school, 'A2', 'Ben Eze', school_class=jss1)
        session = AcademicSession.objects.create(tenant=self.school, name='2026/2027', is_active=True)
        term = Term.objects.create(tenant=self.school, session=session, name='1st Term', is_active=True)
        now = timezone.now()
        self.exam = Exam.objects.create(tenant=self.school, title='Maths', session=session, term=term, published=True,
                                        subject=Subject.objects.create(tenant=self.school, name='Mathematics'),
                                        start_time=now - timedelta(hours=1), end_time=now + timedelta(days=2),
                                        duration_minutes=30, answer_review='AFTER_SUBMIT')
        self.exam.classes.add(jss1)
        part = ExamPart.objects.create(exam=self.exam, part_type='CBT', duration_minutes=30)
        self.q1 = Question.objects.create(exam_part=part, question_type='MCQ', question_text='Convert 3 hours 45 minutes to minutes.')
        self.q1_opts = [Option.objects.create(question=self.q1, text=t, is_correct=t == '225 minutes')
                        for t in ('195 minutes', '225 minutes', '345 minutes')]
        self.q2 = Question.objects.create(exam_part=part, question_type='MCQ', question_text='What is 2 + 2?')
        for t in ('4', '5'):
            Option.objects.create(question=self.q2, text=t, is_correct=t == '5')       # a wrong key
        self.attempt = ExamAttempt.objects.create(student=self.ada, exam_part=part, completed=True, submitted_at=now)
        StudentAnswer.objects.create(attempt=self.attempt, question=self.q1, selected_option=self.q1_opts[0])
        self.ben_attempt = ExamAttempt.objects.create(student=self.ben, exam_part=part, completed=True, submitted_at=now)
        self.calls = []

    def fake(self, url, headers, payload):
        self.calls.append((url, headers, payload))
        asked = json.loads(payload['messages'][1]['content'])['questions']
        out = []
        for q in asked:
            if q['id'] == self.q1.id:
                out.append({'id': q['id'], 'steps': ['$3\\times60=180$', '$180+45=225$'], 'answer': 'B',
                            'why_not': {'A': 'Adds only 15 minutes.}', 'C': 'Adds 120 minutes too many.'},
                            'tip': 'Multiply hours by 60 first.', 'disagree': False})
            else:
                out.append({'id': q['id'], 'steps': ['2 + 2 = 4'], 'why_not': {}, 'disagree': True,
                            'note': '2 + 2 is 4, not 5.'})
        return groq_reply({'solutions': out})

    def ask(self, student, attempt, write='1'):
        self.client.force_login(student.user)
        return self.client.post(f'/cbt/student/exam/review/{attempt.id}/solutions/', {'write': write},
                                HTTP_HOST=self.host(self.school))

    def test_groq_is_called_in_the_openai_format(self):
        with mock.patch('assistant.providers._post', side_effect=self.fake):
            self.ask(self.ada, self.attempt)
        url, headers, payload = self.calls[0]
        self.assertEqual(url, 'https://api.groq.com/openai/v1/chat/completions')
        self.assertEqual(headers['Authorization'], 'Bearer test-key')
        self.assertEqual(payload['model'], 'openai/gpt-oss-120b')
        self.assertEqual(payload['response_format'], {'type': 'json_object'})
        self.assertGreaterEqual(payload['max_tokens'], 8000)
        sent = json.loads(payload['messages'][1]['content'])['questions'][0]
        self.assertEqual(sent['official_answer'], ['B'])                 # the key goes to the AI; it never picks it

    def test_written_once_from_the_key_and_shared(self):
        with mock.patch('assistant.providers._post', side_effect=self.fake):
            data = self.ask(self.ada, self.attempt).json()
        self.assertEqual(data['pending'], [])
        s1 = data['solutions'][str(self.q1.id)]
        self.assertEqual(s1['answer'], '225 minutes')                    # the teacher's key, not the AI's "B"
        self.assertEqual(s1['why_not'], {str(self.q1_opts[0].id): 'Adds only 15 minutes.',
                                         str(self.q1_opts[2].id): 'Adds 120 minutes too many.'})
        self.assertEqual(data['solutions'][str(self.q2.id)], {'held': True})   # disputed key: held for the teacher
        self.assertTrue(QuestionSolution.objects.get(question=self.q2).flagged)
        with mock.patch('assistant.providers._post', side_effect=AssertionError('no second call')):
            again = self.ask(self.ben, self.ben_attempt).json()
        self.assertEqual(set(again['solutions']), {str(self.q1.id), str(self.q2.id)})

    def test_editing_a_question_brings_a_fresh_solution(self):
        with mock.patch('assistant.providers._post', side_effect=self.fake):
            self.ask(self.ada, self.attempt)
        Option.objects.filter(question=self.q2).update(is_correct=False)
        Option.objects.filter(question=self.q2, text='4').update(is_correct=True)
        data = self.ask(self.ada, self.attempt, write='0').json()
        self.assertEqual(data['pending'], [self.q2.id])
        self.assertNotIn(str(self.q2.id), data['solutions'])

    def test_rate_limit_asks_the_page_to_wait(self):
        busy = AIError('HTTP 429: Rate limit reached for model on tokens per minute. Please try again in 5.9s.')
        with mock.patch('assistant.providers._post', side_effect=busy):
            data = self.ask(self.ada, self.attempt).json()
        self.assertEqual(data['retry_after'], 7)
        self.assertEqual(len(data['pending']), 2)
        self.assertEqual(data['error'], '')
        self.assertIsNone(solutions.retry_after('HTTP 401: invalid key'))

    def test_only_the_student_while_review_is_open(self):
        self.client.force_login(self.ben.user)
        page = self.client.post(f'/cbt/student/exam/review/{self.attempt.id}/solutions/', HTTP_HOST=self.host(self.school))
        self.assertEqual(page.status_code, 404)
        self.client.force_login(self.ada.user)
        url = f'/cbt/student/exam/review/{self.attempt.id}/solutions/'
        self.assertEqual(self.client.get(url, HTTP_HOST=self.host(self.school)).status_code, 405)
        Exam.objects.filter(pk=self.exam.pk).update(answer_review='NEVER')
        self.assertEqual(self.ask(self.ada, self.attempt).status_code, 403)

    def test_review_page_has_a_solution_panel_per_question(self):
        self.client.force_login(self.ada.user)
        page = self.client.get(f'/cbt/student/exam/review/{self.attempt.id}/', HTTP_HOST=self.host(self.school))
        self.assertContains(page, '<details class="sol"', count=2)
        self.assertContains(page, f'data-q="{self.q1.id}"')
        self.assertContains(page, f'/cbt/student/exam/review/{self.attempt.id}/solutions/')
