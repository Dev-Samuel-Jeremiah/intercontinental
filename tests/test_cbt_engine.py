"""
Sitting a CBT paper: each candidate's fixed order, marking from saved
answers when time runs out, invigilator extensions and console, integrity
events, answer review, paper printing, and passing scores to the result
sheet only when the school's rules allow.
"""
import json
from datetime import timedelta

from django.utils import timezone

from academics.models import AcademicSession, Term
from cbt import engine
from cbt.models import Exam, ExamAttempt, ExamPart, IntegrityEvent, Option, Question, StudentAnswer
from results import locks
from results.assessment import PRESETS, save as save_scheme
from results.models import ResultSheetSettings, TermResult
from tenants.middleware import set_current_tenant
from users.models import ClassSubject, Subject

from .factories import TenantTestCase, make_admin, make_class, make_school, make_student, make_teacher


class Base(TenantTestCase):

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha')
        self.admin = make_admin(self.school)
        self.jss1 = make_class(self.school, 'JSS 1A')
        set_current_tenant(self.school)
        today = timezone.localdate()
        self.session = AcademicSession.objects.create(tenant=self.school, name='2026/2027',
                                                      start_date=today - timedelta(days=60), is_active=True)
        self.term = Term.objects.create(tenant=self.school, name='1st Term', session=self.session,
                                        start_date=today - timedelta(days=30), end_date=today + timedelta(days=60),
                                        is_active=True)
        self.subject = Subject.objects.create(tenant=self.school, name='Mathematics')
        ClassSubject.objects.create(tenant=self.school, school_class=self.jss1, subject=self.subject)
        self.ada = make_student(self.school, 'S1', full_name='Ada Bello', school_class=self.jss1)
        self.bola = make_student(self.school, 'S2', full_name='Bola Ade', school_class=self.jss1)
        set_current_tenant(self.school)
        now = timezone.now()
        self.exam = Exam.objects.create(tenant=self.school, title='Maths exam', subject=self.subject,
                                        session=self.session, term=self.term, exam_type='EXAM',
                                        start_time=now - timedelta(minutes=10), end_time=now + timedelta(hours=2),
                                        published=True, duration_minutes=60, shuffle_questions=True,
                                        shuffle_options=True, answer_review='AFTER_SUBMIT')
        self.exam.classes.add(self.jss1)
        self.part = ExamPart.objects.create(exam=self.exam, part_type='CBT', duration_minutes=60, total_marks=10)
        self.questions = []
        for i in range(10):
            q = Question.objects.create(exam_part=self.part, question_text=f'What is {i} + 1?', question_type='MCQ',
                                        marks=1, explanation=f'{i} + 1 = {i + 1}')
            for v in (i + 1, i + 2, i + 3, i + 4):
                Option.objects.create(question=q, text=str(v), is_correct=(v == i + 1))
            self.questions.append(q)

    def _get(self, path, **kw):
        return self.client.get(path, HTTP_HOST=self.host(self.school), **kw)

    def _post(self, path, data=None, **kw):
        return self.client.post(path, data or {}, HTTP_HOST=self.host(self.school), **kw)

    def _attempt(self, student=None, **kw):
        return ExamAttempt.objects.create(student=student or self.ada, exam_part=self.part, **kw)

    def _answer(self, attempt, q, correct=True):
        opt = q.options.get(is_correct=correct) if correct else q.options.filter(is_correct=False).first()
        return StudentAnswer.objects.create(attempt=attempt, question=q, selected_option=opt)


class PaperTests(Base):

    def test_each_candidate_has_a_fixed_shuffled_order(self):
        a, b = self._attempt(), self._attempt(self.bola)
        first = [q.id for q in engine.paper(a)]
        self.assertEqual(first, [q.id for q in engine.paper(a)])           # same on reload
        a.refresh_from_db()
        self.assertEqual(a.question_order, first)
        self.assertEqual(sorted(first), sorted(q.id for q in self.questions))
        self.assertNotEqual(first, [q.id for q in engine.paper(b)])        # different candidate, different order
        opts = [o.id for o in engine.paper(a)[0].ordered_options]
        self.assertEqual(opts, [o.id for o in engine.paper(a)[0].ordered_options])

    def test_no_shuffle_keeps_the_written_order(self):
        Exam.objects.filter(pk=self.exam.pk).update(shuffle_questions=False, shuffle_options=False)
        a = ExamAttempt.objects.select_related('exam_part__exam').get(pk=self._attempt().pk)
        self.assertEqual([q.id for q in engine.paper(a)], [q.id for q in self.questions])

    def test_questions_added_later_go_at_the_end(self):
        a = self._attempt()
        engine.paper(a)
        late = Question.objects.create(exam_part=self.part, question_text='Late?', question_type='TF')
        self.assertEqual(engine.paper(a)[-1].id, late.id)

    def test_page_uses_the_fixed_order(self):
        self.client.force_login(self.ada.user)
        r1 = self._get(f'/cbt/student/exams/{self.exam.id}/take/CBT/')
        order = [i['question'].id for i in r1.context['questions_list']]
        r2 = self._get(f'/cbt/student/exams/{self.exam.id}/take/CBT/')
        self.assertEqual(order, [i['question'].id for i in r2.context['questions_list']])


class MarkingTests(Base):

    def test_time_up_marks_saved_answers(self):
        """Regression: a paper auto-submitted when time ran out used to score 0."""
        a = self._attempt(started_at=timezone.now() - timedelta(minutes=61))
        for q in self.questions[:7]:
            self._answer(a, q)
        self._answer(a, self.questions[7], correct=False)
        self.client.force_login(self.ada.user)
        self._get(f'/cbt/student/exams/{self.exam.id}/take/CBT/')
        a.refresh_from_db()
        self.assertTrue(a.completed)
        self.assertEqual(a.score, 7)
        self.assertEqual(TermResult.objects.get(student=self.ada).exam_score, 7)

    def test_submit_marks_from_posted_answers(self):
        self.client.force_login(self.ada.user)
        self._get(f'/cbt/student/exams/{self.exam.id}/take/CBT/')
        data = {'submit_exam': '1'}
        for q in self.questions[:4]:
            data[f'question_{q.id}'] = q.options.get(is_correct=True).id
        self._post(f'/cbt/student/exams/{self.exam.id}/take/CBT/', data)
        a = ExamAttempt.objects.get(student=self.ada)
        self.assertEqual((a.completed, a.score), (True, 4))

    def test_an_option_from_another_question_never_counts(self):
        a = self._attempt()
        StudentAnswer.objects.create(attempt=a, question=self.questions[0],
                                     selected_option=self.questions[1].options.get(is_correct=True))
        self.assertEqual(engine.grade(a), 0)


class ResultSheetTests(Base):

    def _finish(self, n_right):
        a = self._attempt()
        for q in self.questions[:n_right]:
            self._answer(a, q)
        return engine.finish(a)

    def test_score_fills_the_exam_column(self):
        self._finish(9)
        tr = TermResult.objects.get(student=self.ada)
        self.assertEqual((tr.raw_exam_score, tr.exam_score), (9, 9))

    def test_locked_term_keeps_the_score_as_reference_only(self):
        locks.set_deadline(self.term, timezone.localdate() - timedelta(days=1))
        self._finish(9)
        tr = TermResult.objects.get(student=self.ada)
        self.assertEqual((tr.raw_exam_score, tr.exam_score), (9, 0))

    def test_score_above_the_column_maximum_is_not_copied(self):
        save_scheme(self.school, {'ca1': ('Test', 95), 'ca2': ('', 0), 'ca3': ('', 0), 'essay': ('', 0),
                                  'exam': ('Exam', 5)}, self.term)
        self._finish(9)
        tr = TermResult.objects.get(student=self.ada)
        self.assertEqual((tr.raw_exam_score, tr.exam_score), (9, 0))

    def test_approved_sheet_is_left_alone(self):
        from results import release
        ResultSheetSettings.objects.update_or_create(tenant=self.school, defaults={'require_approval': True})
        TermResult.objects.create(tenant=self.school, student=self.ada, class_assigned=self.jss1, subject=self.subject,
                                  session=self.session, term=self.term, exam_score=50)
        sheet = release.sheet_for(self.jss1, self.subject, self.term)
        sheet.status = 'APPROVED'
        sheet.save()
        self._finish(9)
        self.assertEqual(TermResult.objects.get(student=self.ada).exam_score, 50)


class InvigilationTests(Base):

    def test_extension_and_time(self):
        a = self._attempt(started_at=timezone.now() - timedelta(minutes=58))
        self.assertLess(engine.seconds_left(a), 180)
        engine.extend(a, 10)
        self.assertGreater(engine.seconds_left(a), 600)
        with self.assertRaises(engine.EngineError):
            engine.extend(a, 500)

    def test_extension_lets_a_candidate_continue_past_the_window(self):
        a = self._attempt(started_at=timezone.now() - timedelta(minutes=50))
        Exam.objects.filter(pk=self.exam.pk).update(end_time=timezone.now() - timedelta(minutes=2))
        a.refresh_from_db()
        self.assertFalse(engine.may_continue(a))
        engine.extend(a, 15)
        a = ExamAttempt.objects.select_related('exam_part__exam').get(pk=a.pk)
        self.assertTrue(engine.may_continue(a))
        self.client.force_login(self.ada.user)
        self.assertEqual(self._get(f'/cbt/student/exams/{self.exam.id}/take/CBT/').status_code, 200)

    def test_console(self):
        a = self._attempt()
        self._answer(a, self.questions[0])
        engine.record_event(a, 'HIDDEN', 12)
        data = engine.console(self.part)
        rows = {r['student'].full_name: r for r in data['rows']}
        self.assertEqual(rows['Ada Bello']['state'], 'writing')
        self.assertEqual((rows['Ada Bello']['answered'], rows['Ada Bello']['events']), (1, 1))
        self.assertTrue(rows['Ada Bello']['connected'])
        self.assertEqual(rows['Bola Ade']['state'], 'not_started')
        self.assertEqual(data['counts'], {'not_started': 1, 'writing': 1, 'submitted': 0})

    def test_console_pages_and_actions(self):
        a = self._attempt()
        self._answer(a, self.questions[0])
        self.client.force_login(self.admin)
        url = f'/cbt/admin/exams/{self.exam.id}/invigilate/'
        self.assertContains(self._get(url), 'Ada Bello')
        d = json.loads(self._get(url + '?format=json').content)
        self.assertEqual(d['counts']['writing'], 1)
        self._post(url + f'?part={self.part.id}', {'attempt_id': a.id, 'action': 'extend', 'minutes': 5})
        a.refresh_from_db()
        self.assertEqual(a.extra_seconds, 300)
        self._post(url + f'?part={self.part.id}', {'attempt_id': a.id, 'action': 'submit'})
        a.refresh_from_db()
        self.assertEqual((a.completed, a.score), (True, 1))
        teacher = make_teacher(self.school)
        self.client.force_login(teacher)
        self.assertEqual(self._get(url).status_code, 302)


class IntegrityTests(Base):

    def test_events_are_logged_once_per_burst(self):
        a = self._attempt()
        engine.record_event(a, 'BLUR')
        engine.record_event(a, 'BLUR')
        engine.record_event(a, 'PASTE')
        self.assertEqual(IntegrityEvent.objects.count(), 2)
        self.assertIsNone(engine.record_event(a, 'NONSENSE'))

    def test_endpoint(self):
        a = self._attempt()
        self.client.force_login(self.ada.user)
        r = self._post('/cbt/student/exam/integrity/', json.dumps({'attempt_id': a.id, 'kind': 'HIDDEN', 'seconds': 20}),
                       content_type='application/json')
        self.assertEqual(json.loads(r.content)['count'], 1)
        self.client.force_login(self.bola.user)
        r = self._post('/cbt/student/exam/integrity/', json.dumps({'attempt_id': a.id, 'kind': 'HIDDEN'}),
                       content_type='application/json')
        self.assertEqual(r.status_code, 404)


class ReviewAndPrintTests(Base):

    def test_review_rules(self):
        a = self._attempt()
        self.assertFalse(engine.review_open(a))                       # not submitted
        engine.finish(a)
        a.refresh_from_db()
        self.assertTrue(engine.review_open(a))                        # AFTER_SUBMIT
        Exam.objects.filter(pk=self.exam.pk).update(answer_review='AFTER_CLOSE')
        a = ExamAttempt.objects.select_related('exam_part__exam').get(pk=a.pk)
        self.assertFalse(engine.review_open(a))
        Exam.objects.filter(pk=self.exam.pk).update(answer_review='NEVER')
        a = ExamAttempt.objects.select_related('exam_part__exam').get(pk=a.pk)
        self.assertFalse(engine.review_open(a))

    def test_review_page(self):
        a = self._attempt()
        self._answer(a, self.questions[0])
        self._answer(a, self.questions[1], correct=False)
        engine.finish(a)
        self.client.force_login(self.ada.user)
        r = self._get(f'/cbt/student/exam/review/{a.id}/')
        self.assertContains(r, '0 + 1 = 1')
        self.assertContains(r, '1 of 10 objective questions right')
        self.client.force_login(self.bola.user)
        self.assertEqual(self._get(f'/cbt/student/exam/review/{a.id}/').status_code, 404)

    def test_print_paper(self):
        self.client.force_login(self.admin)
        r = self._get(f'/cbt/admin/exams/{self.exam.id}/print/')
        self.assertContains(r, 'What is 0 + 1?')
        self.assertNotContains(r, 'Answer key')
        r = self._get(f'/cbt/admin/exams/{self.exam.id}/print/?key=1')
        self.assertContains(r, 'Answer key')
        self.assertContains(r, '1. <b>A</b>')
