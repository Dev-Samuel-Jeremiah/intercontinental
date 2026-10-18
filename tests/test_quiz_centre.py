"""
The student Quiz Centre — quick quizzes with difficulty and size, instant
feedback, fixing mistakes, the daily challenge, points, streaks and the class
leaderboard — and teachers releasing bank questions to it.
"""
import json
from datetime import timedelta

from django.utils import timezone

from cbt import bank, practice
from cbt.models import BankQuestion, Exam, ExamPart, Option, PracticeSession, Question

from .factories import make_student, make_teacher
from .test_cbt_practice import Base


class QuizBase(Base):

    def setUp(self):
        super().setUp()
        hard = BankQuestion.objects.filter(subject=self.maths, topic='Geometry')[:2]
        BankQuestion.objects.filter(pk__in=[q.pk for q in hard]).update(difficulty='HARD')

    def quick(self, student=None, **kw):
        args = dict(per_subject=5, feedback='INSTANT', timed=False)
        args.update(kw)
        return practice.start(student or self.ada, [self.maths.id], 'QUICK', **args)

    def answer(self, session, right=True, n=None):
        ids = [q for v in session.paper.values() for q in v][:n]
        for q in BankQuestion.objects.filter(id__in=ids):
            practice.answer(session, q.id, q.options.filter(is_correct=right).first().id)


class QuickQuizTests(QuizBase):

    def test_difficulty_size_and_untimed(self):
        s = self.quick(difficulty='HARD')
        self.assertEqual(s.total, 2)                      # only two hard maths questions
        self.assertEqual(set(BankQuestion.objects.filter(id__in=s.paper[str(self.maths.id)])
                             .values_list('difficulty', flat=True)), {'HARD'})
        self.assertIsNone(practice.seconds_left(s))
        self.assertEqual(s.duration_seconds, 0)

    def test_one_subject_and_nothing_to_draw(self):
        with self.assertRaises(practice.PracticeError):
            practice.start(self.ada, [self.maths.id, self.physics.id], 'QUICK')
        with self.assertRaisesMessage(practice.PracticeError, 'no easy Physics'):
            practice.start(self.ada, [self.physics.id], 'QUICK', difficulty='EASY')

    def test_instant_feedback_locks_the_answer(self):
        s = self.quick()
        q = BankQuestion.objects.get(pk=s.paper[str(self.maths.id)][0])
        wrong, right = q.options.get(text='wrong'), q.options.get(is_correct=True)
        verdict = practice.answer(s, q.id, wrong.id)
        self.assertEqual((verdict['correct'], verdict['correct_option']), (False, right.id))
        with self.assertRaisesMessage(practice.PracticeError, 'already answered'):
            practice.answer(s, q.id, right.id)

    def test_end_feedback_reveals_nothing_and_allows_changes(self):
        s = self.quick(feedback='END')
        q = BankQuestion.objects.get(pk=s.paper[str(self.maths.id)][0])
        self.assertEqual(practice.answer(s, q.id, q.options.get(text='wrong').id), {})
        practice.answer(s, q.id, q.options.get(is_correct=True).id)
        self.assertTrue(s.answers.get().correct)

    def test_points(self):
        s = self.quick(difficulty='HARD')
        self.answer(s)
        s = practice.submit(s)
        self.assertEqual(s.points, 2 * practice.POINTS['HARD'])        # fewer than 5 questions: no perfect bonus
        s = self.quick(topic='Algebra')
        self.answer(s)
        s = practice.submit(s)
        self.assertEqual(s.points, 5 * practice.POINTS['MEDIUM'] + practice.PERFECT_BONUS)

    def test_a_left_quiz_is_marked_and_does_not_block(self):
        s = self.quick()
        PracticeSession.objects.filter(pk=s.pk).update(started_at=timezone.now() - timedelta(hours=5))
        self.quick()
        s.refresh_from_db()
        self.assertTrue(s.finished)


class MistakesAndDailyTests(QuizBase):

    def test_fix_my_mistakes(self):
        s = self.quick(per_subject=10)
        self.answer(s, right=False, n=4)
        practice.submit(s)
        wrong = set(practice.mistakes(self.ada))
        self.assertEqual(len(wrong), 4)
        retry = practice.start(self.ada, [], 'MISTAKES')
        self.assertEqual({q for v in retry.paper.values() for q in v}, wrong)
        self.answer(retry, right=True, n=2)
        practice.submit(retry)
        self.assertEqual(len(practice.mistakes(self.ada)), 2)          # the two put right are cleared

    def test_retry_one_quiz_includes_skipped(self):
        s = self.quick(per_subject=5)
        self.answer(s, right=True, n=2)
        practice.submit(s)
        retry = practice.start(self.ada, [], 'MISTAKES', source=s)
        self.assertEqual(retry.total, 3)

    def test_nothing_to_fix(self):
        with self.assertRaisesMessage(practice.PracticeError, 'No mistakes'):
            practice.start(self.ada, [], 'MISTAKES')

    def test_daily_challenge_is_shared_and_once_a_day(self):
        bola = make_student(self.school, 'S2', full_name='Bola Ade', school_class=self.ss1)
        a = practice.start(self.ada, [], 'DAILY')
        b = practice.start(bola, [], 'DAILY')
        self.assertEqual(a.total, practice.DAILY_SIZE)
        self.assertEqual(sorted(q for v in a.paper.values() for q in v), sorted(q for v in b.paper.values() for q in v))
        self.assertEqual((a.feedback, a.timed), ('INSTANT', False))
        self.answer(a, n=1)
        a = practice.submit(a)
        first = BankQuestion.objects.get(pk=[q for v in a.paper.values() for q in v][0])
        self.assertEqual(a.points, practice.POINTS[first.difficulty] + practice.DAILY_BONUS)
        with self.assertRaisesMessage(practice.PracticeError, 'already taken'):
            practice.start(self.ada, [], 'DAILY')


class StreakAndBoardTests(QuizBase):

    def _done(self, student, days_ago, points):
        when = timezone.now() - timedelta(days=days_ago)
        s = PracticeSession.objects.create(tenant=self.school, student=student, mode='QUICK', total=5, score=3,
                                           points=points, started_at=when, submitted_at=when)
        return s

    def test_streak(self):
        self.assertEqual(practice.streak(self.ada), 0)
        for d in (1, 2, 3, 5):
            self._done(self.ada, d, 10)
        self.assertEqual(practice.streak(self.ada), 3)                 # yesterday counts until today ends
        self._done(self.ada, 0, 10)
        self.assertEqual(practice.streak(self.ada), 4)

    def test_class_leaderboard(self):
        bola = make_student(self.school, 'S2', full_name='Bola Ade', school_class=self.ss1)
        junior = make_student(self.school, 'J1', full_name='Junior Kid', school_class=self.jss1)
        self._done(self.ada, 1, 30)
        self._done(bola, 1, 50)
        self._done(bola, 20, 100)                                      # outside this week
        self._done(junior, 1, 999)                                     # another class
        top, me = practice.leaderboard(self.ada)
        self.assertEqual([(r['name'], r['points']) for r in top], [('Bola Ade', 50), ('Ada Bello', 30)])
        self.assertEqual(me['rank'], 2)
        top, _ = practice.leaderboard(self.ada, days=None)
        self.assertEqual(top[0]['points'], 150)
        self.assertEqual(practice.stats(self.ada)['points'], 30)


class QuizPageTests(QuizBase):

    def test_home_and_a_full_instant_quiz(self):
        self.client.force_login(self.ada.user)
        page = self._get('/cbt/practice/')
        self.assertContains(page, 'Quiz')
        self.assertContains(page, 'Daily challenge')
        self.assertContains(page, 'leaderboard')
        self._post('/cbt/practice/', {'mode': 'QUICK', 'subject': self.maths.id, 'per_subject': 5,
                                      'difficulty': '', 'feedback': 'INSTANT'})
        s = PracticeSession.objects.get()
        self.assertEqual((s.mode, s.feedback, s.timed, s.total), ('QUICK', 'INSTANT', False, 5))
        q = BankQuestion.objects.get(pk=s.paper[str(self.maths.id)][0])
        r = self._post(f'/cbt/practice/{s.id}/answer/', json.dumps({'question': q.id, 'option': q.options.get(is_correct=True).id}),
                       content_type='application/json')
        self.assertEqual(json.loads(r.content)['correct'], True)
        r = self._post(f'/cbt/practice/{s.id}/answer/', json.dumps({'question': q.id, 'option': q.options.get(text='wrong').id}),
                       content_type='application/json')
        self.assertEqual(r.status_code, 400)
        self.assertContains(self._get(f'/cbt/practice/{s.id}/'), 'Correct!')
        self._post(f'/cbt/practice/{s.id}/')
        r = self._get(f'/cbt/practice/{s.id}/result/')
        self.assertContains(r, 'points earned')
        self.assertContains(r, 'Retry my 4 mistakes')
        self._post('/cbt/practice/', {'mode': 'MISTAKES', 'source': s.id, 'feedback': 'INSTANT', 'per_subject': 60})
        self.assertEqual(PracticeSession.objects.filter(mode='MISTAKES').get().total, 4)

    def test_quiz_page_does_not_leak_answers_before_answering(self):
        s = self.quick()
        self.client.force_login(self.ada.user)
        html = self._get(f'/cbt/practice/{s.id}/').content.decode()
        self.assertNotIn(' ok"', html)                 # no option marked right
        self.assertNotIn('class="fb show', html)      # no verdict shown


class ReleaseTests(Base):

    def setUp(self):
        super().setUp()
        self.hidden = BankQuestion.objects.get(text='Teacher only')

    def test_imported_exam_questions_stay_hidden_until_released(self):
        exam = Exam.objects.create(tenant=self.school, title='Maths test', subject=self.maths, start_time=timezone.now(),
                                   end_time=timezone.now() + timedelta(hours=1), duration_minutes=30, created_by=self.teacher)
        part = ExamPart.objects.create(exam=exam, part_type='CBT', duration_minutes=30)
        q = Question.objects.create(exam_part=part, question_text='3 + 4?', question_type='MCQ')
        Option.objects.create(question=q, text='7', is_correct=True)
        b = bank.to_bank(q, self.teacher)
        self.assertFalse(b.for_practice)
        self.assertNotIn(b.id, practice.pool(self.ada, self.maths))

    def test_teacher_releases_and_hides(self):
        self.client.force_login(self.teacher_user)
        self._post('/cbt/admin/bank/release/', {'ids': [self.hidden.id], 'action': 'release', 'next': '/cbt/admin/bank/'})
        self.hidden.refresh_from_db()
        self.assertTrue(self.hidden.for_practice)
        self._post('/cbt/admin/bank/release/', {'toggle': self.hidden.id})
        self.hidden.refresh_from_db()
        self.assertFalse(self.hidden.for_practice)

    def test_release_all_matching_a_filter(self):
        BankQuestion.objects.filter(subject=self.physics).update(for_practice=False)
        self.client.force_login(self.admin)
        page = self._get(f'/cbt/admin/bank/?subject={self.physics.id}')
        self.assertContains(page, 'Release all 6')
        self.assertContains(page, 'Hidden from students')
        self._post('/cbt/admin/bank/release/', {'scope': 'filter', 'action': 'release', 'subject': self.physics.id})
        self.assertFalse(BankQuestion.objects.filter(subject=self.physics, for_practice=False).exists())
        self.assertFalse(BankQuestion.objects.get(pk=self.hidden.pk).for_practice)    # other subjects untouched

    def test_others_questions_are_left_alone(self):
        other = make_teacher(self.school, username='t2')
        self.client.force_login(other)
        self._post('/cbt/admin/bank/release/', {'ids': [self.hidden.id], 'action': 'release'})
        self.hidden.refresh_from_db()
        self.assertFalse(self.hidden.for_practice)

    def test_students_cannot_release(self):
        self.client.force_login(self.ada.user)
        self._post('/cbt/admin/bank/release/', {'ids': [self.hidden.id], 'action': 'release'})
        self.hidden.refresh_from_db()
        self.assertFalse(self.hidden.for_practice)
