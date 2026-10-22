"""
AI part 2: content safety on class discussions, at-risk early warning,
questions about school data, the course tutor, essay feedback and study
plans. No real AI is called (see FakeAI in test_assistant).
"""
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.core import mail
from django.test import override_settings
from django.utils import timezone

from academics.models import AcademicSession, Term
from assistant import ask, learning, risk, safety
from assistant.models import AdminQuery, EssayFeedback, StudyPlan, TutorMessage
from assistant.providers import AIError
from classroom import extras
from classroom.models import Assignment, Course, Post, Section
from finance.models import ChildBilling, ParentBilling, Wallet
from results.models import TermResult
from tenants.middleware import set_current_tenant
from users.models import Attendance, Staff, Subject, User

from .factories import TenantTestCase, make_admin, make_class, make_school, make_student, make_teacher
from .test_assistant import KEYS, FakeAI


@override_settings(**KEYS)
class Base(TenantTestCase):

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha')
        self.admin = make_admin(self.school)
        self.teacher = make_teacher(self.school, username='alphat', full_name='Mr Bello')
        self.teacher.email = 'bello@example.com'
        self.teacher.save()
        self.jss2a, self.jss2b, self.ss1 = (make_class(self.school, n) for n in ('JSS 2A', 'JSS 2B', 'SS 1A'))
        set_current_tenant(self.school)
        self.ada = make_student(self.school, 'A1', full_name='Ada Obi', school_class=self.jss2a,
                                parent_email='obi@example.com')
        self.ben = make_student(self.school, 'A2', full_name='Ben Eze', school_class=self.jss2b,
                                parent_email='eze@example.com')
        self.chi = make_student(self.school, 'A3', full_name='Chi Musa', school_class=self.ss1,
                                parent_email='musa@example.com')
        set_current_tenant(self.school)
        self.course = Course.objects.create(tenant=self.school, name='Biology', teacher=self.teacher,
                                            class_assigned=self.jss2a)
        self.section = Section.objects.create(tenant=self.school, course=self.course, title='Plants', order=1)
        self.assignment = Assignment.objects.create(tenant=self.school, section=self.section, title='Photosynthesis essay',
                                                    description='Explain photosynthesis in 300 words.',
                                                    due_date=timezone.now() + timedelta(days=3))
        self.session = AcademicSession.objects.create(tenant=self.school, name='2026/2027', is_active=True)
        self.term = Term.objects.create(tenant=self.school, session=self.session, name='1st Term', is_active=True,
                                        start_date=timezone.localdate() - timedelta(days=40))
        self.maths = Subject.objects.create(tenant=self.school, name='Mathematics')
        # The safety check would call OpenAI's moderation endpoint: by default it finds nothing.
        quiet = mock.patch('assistant.providers._post',
                           side_effect=lambda url, h, p: {'results': [{'flagged': False, 'categories': {}}]})
        quiet.start()
        self.addCleanup(quiet.stop)

    def fake(self, *replies, **kw):
        fake = FakeAI(*replies, **kw)
        patcher = mock.patch('assistant.providers._post', side_effect=fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return fake

    def owe(self, student, fee, paid=0):
        wallet, _ = Wallet.objects.get_or_create(student=student, defaults={'tenant': self.school})
        pb = ParentBilling.objects.create(tenant=self.school, wallet=wallet, session=self.session, term=self.term)
        return ChildBilling.objects.create(tenant=self.school, student=student, parent_billing=pb,
                                           total_fee=Decimal(fee), paid=Decimal(paid))

    def _get(self, path, **kw):
        return self.client.get(path, HTTP_HOST=self.host(self.school), **kw)

    def _post(self, path, data=None, **kw):
        return self.client.post(path, data or {}, HTTP_HOST=self.host(self.school), **kw)


class SafetyTests(Base):

    def test_phrase_list(self):
        self.assertTrue(safety.local_check('sometimes I want to die').urgent)
        self.assertEqual(safety.local_check("you're so stupid").category, 'bullying')
        self.assertFalse(safety.local_check('The stupid mistake was mine, sorry').flagged)
        self.assertFalse(safety.local_check('Cells die when they lack oxygen').flagged)

    def test_urgent_post_is_held_and_the_teacher_is_emailed(self):
        t = extras.start_thread(self.course, self.ada.user, 'Quietpost', 'I want to die, nothing matters')
        self.assertTrue(t.held)
        self.assertIn('hurting', t.held_reason)
        self.assertEqual(mail.outbox[0].to, ['bello@example.com'])
        other = make_student(self.school, 'A9', full_name='Classmate', school_class=self.jss2a)
        self.client.force_login(other.user)
        self.assertNotContains(self._get(f'/classroom/course/{self.course.pk}/discussion/'), 'Quietpost')
        self.assertEqual(self._get(f'/classroom/discussion/{t.pk}/').status_code, 302)
        self.client.force_login(self.teacher)
        self.assertContains(self._get(f'/classroom/course/{self.course.pk}/discussion/'), 'Quietpost')

    def test_teacher_releases_a_held_reply(self):
        t = extras.start_thread(self.course, self.ada.user, 'Leaves', 'Why are leaves green?')
        p = extras.reply(t, self.ada.user, "Ben you're so dumb")
        self.assertTrue(p.held)
        self.client.force_login(self.teacher)
        self._post(f'/classroom/discussion/{t.pk}/', {'action': 'release', 'post': p.pk})
        p.refresh_from_db()
        self.assertFalse(p.held)
        self.assertEqual(extras.reply(t, self.teacher, "you're so stupid").held, False)   # teachers aren't checked

    def test_ai_moderation_catches_what_the_list_misses(self):
        def moderation(url, headers, payload):
            return {'results': [{'flagged': True, 'categories': {'harassment': True, 'violence': False}}]}
        with mock.patch('assistant.providers._post', side_effect=moderation):
            t = extras.start_thread(self.course, self.ada.user, 'Re: Ben', 'Everyone should ignore Ben forever.')
        self.assertTrue(t.held)
        self.assertIn('bullying', t.held_reason)

    def test_failed_moderation_never_blocks(self):
        with mock.patch('assistant.providers._post', side_effect=AIError('HTTP 500')):
            t = extras.start_thread(self.course, self.ada.user, 'Leaves', 'Why are leaves green?')
        self.assertFalse(t.held)

    def test_queue_page(self):
        t = extras.start_thread(self.course, self.ada.user, 'Help', 'nobody likes you Ben')
        self.client.force_login(self.teacher)
        self.assertContains(self._get('/assistant/safety/'), 'nobody likes you')
        self._post('/assistant/safety/', {'kind': 'thread', 'id': t.pk, 'action': 'delete'})
        self.assertFalse(Post.objects.exists())
        from classroom.models import Thread
        self.assertFalse(Thread.objects.filter(pk=t.pk).exists())


class RiskTests(Base):

    def setUp(self):
        super().setUp()
        today = timezone.localdate()
        for i in range(10):
            Attendance.objects.create(tenant=self.school, student=self.ada, school_class=self.jss2a,
                                      date=today - timedelta(days=i + 1), status='A' if i < 3 else 'P')
        for score in (30, 35, 38, 60):
            subject = Subject.objects.create(tenant=self.school, name=f'Subject {score}')
            TermResult.objects.create(tenant=self.school, student=self.ada, class_assigned=self.jss2a, subject=subject,
                                      session=self.session, term=self.term, exam_score=score)
        self.owe(self.ada, 40000, 0)

    def test_scores_and_reasons(self):
        rows = {r['student'].id: r for r in risk.assess([self.ada, self.ben], self.school, with_fees=True)}
        ada = rows[self.ada.id]
        self.assertEqual(ada['level'], 'high')
        self.assertTrue(any('Absent 3 of the last 10' in x for x in ada['reasons']))
        self.assertTrue(any('Below 40 in 3 subjects' in x for x in ada['reasons']))
        self.assertIn('₦40,000', ada['fee_reason'])
        self.assertEqual(rows[self.ben.id]['level'], 'low')
        no_fees = risk.assess([self.ada], self.school, with_fees=False)[0]
        self.assertEqual(no_fees['fee_reason'], '')

    def test_teachers_see_their_form_class_and_get_suggestions(self):
        staff = Staff.objects.get(user=self.teacher)
        self.jss2a.form_teachers.set([staff])
        self.fake({'steps': ['Talk to Ada privately this week.', 'Call the family.', 'Arrange extra maths help.']})
        self.client.force_login(self.teacher)
        page = self._get('/assistant/at-risk/')
        self.assertContains(page, 'Ada Obi')
        self.assertNotContains(page, '₦40,000')                          # no fee access
        r = self._post('/assistant/at-risk/', {'student': self.ada.pk})
        self.assertContains(r, 'Call the family.')
        self.assertEqual(list(risk.students_for(self.teacher, self.school)), [self.ada])


class AskTests(Base):

    def setUp(self):
        super().setUp()
        self.owe(self.ada, 80000, 10000)        # owes 70,000
        self.owe(self.ben, 30000, 0)            # owes 30,000
        self.owe(self.chi, 90000, 0)            # SS1 — not JSS2

    def test_owing_question(self):
        fake = self.fake({'tool': 'students_owing', 'settings': {'min_amount': '₦50,000', 'class_name': 'JSS2'}})
        report, tool, params = ask.answer(self.school, self.admin, 'How many JSS2 students owe more than ₦50,000?',
                                          may_see_fees=True)
        self.assertEqual(tool, 'students_owing')
        self.assertEqual([r[0] for r in report.rows], ['Ada Obi'])
        self.assertIn('1 student owe', report.summary)
        self.assertIn('JSS 2A', fake.prompt())
        self.assertEqual(AdminQuery.objects.get().rows, 1)

    def test_fee_reports_need_fee_access(self):
        fake = self.fake({'tool': 'students_owing', 'settings': {}})
        with self.assertRaises(AIError) as raised:
            ask.answer(self.school, self.teacher, 'Who owes fees?', may_see_fees=False)
        self.assertIn('fee access', str(raised.exception))
        self.assertNotIn('students_owing', fake.prompt())

    def test_no_report_fits_and_unknown_class(self):
        self.fake({'tool': None, 'reply': 'I can count students or list absences.'})
        with self.assertRaises(AIError) as raised:
            ask.answer(self.school, self.admin, 'What is the weather?', True)
        self.assertIn('count students', str(raised.exception))
        with self.assertRaises(AIError):
            ask.classes_matching(self.school, 'Primary 6')
        self.assertEqual(len(ask.classes_matching(self.school, 'jss 2')), 2)

    def test_counts_and_page_access(self):
        self.assertEqual(ask.student_count(self.school, 'JSS2').rows[0][1], 1)
        self.client.force_login(self.teacher)
        self.assertEqual(self._get('/assistant/ask/').status_code, 302)
        self.fake({'tool': 'student_count', 'settings': {}})
        self.client.force_login(self.admin)
        self.assertContains(self._post('/assistant/ask/', {'question': 'How many students?'}), '3 students on roll')


class TutorTests(Base):

    def test_off_until_the_teacher_switches_it_on(self):
        with self.assertRaises(AIError):
            learning.tutor_reply(self.course, self.ada.user, 'Explain photosynthesis')
        self.client.force_login(self.teacher)
        self._post(f'/assistant/tutor/{self.course.pk}/manage/', {'enabled': '1', 'daily_limit': '1',
                                                                   'instructions': 'Use Nigerian plants.'})
        fake = self.fake('Plants use light to make food. What do you think the leaves need from the air?')
        self.client.force_login(self.ada.user)
        self._post(f'/assistant/tutor/{self.course.pk}/', {'text': 'Explain photosynthesis'})
        self.assertEqual(list(TutorMessage.objects.values_list('role', flat=True)), ['user', 'assistant'])
        system = fake.calls[0][1]['messages'][0]['content']
        self.assertIn('NEVER give the final answer', system)
        self.assertIn('ASSIGNMENT (graded — do not answer it): Photosynthesis essay', system)
        self.assertIn('Use Nigerian plants.', system)
        with self.assertRaises(AIError) as raised:          # daily limit of 1
            learning.tutor_reply(self.course, self.ada.user, 'And respiration?')
        self.assertIn('limit', str(raised.exception))
        self.assertContains(self._get(f'/assistant/tutor/{self.course.pk}/'), 'What do you think')

    def test_worrying_message_goes_to_the_teacher_not_the_ai(self):
        s = learning.tutor_settings(self.course)
        s.enabled = True
        s.save()
        fake = self.fake('should not be used')
        reply = learning.tutor_reply(self.course, self.ada.user, 'I want to kill myself')
        self.assertIn('let your teacher know', reply.text)
        self.assertEqual(fake.calls, [])
        self.assertTrue(TutorMessage.objects.get(role='user').held)
        self.assertEqual(mail.outbox[0].to, ['bello@example.com'])

    def test_only_course_members(self):
        self.client.force_login(self.chi.user)
        self.assertEqual(self._get(f'/assistant/tutor/{self.course.pk}/').status_code, 302)


class EssayAndPlanTests(Base):

    def test_essay_feedback(self):
        with self.assertRaises(AIError):
            learning.essay_feedback(self.school, self.ada.user, 'Too short.')
        fake = self.fake({'strengths': ['Clear opening'], 'improve': ['Add an example of a plant'],
                          'questions': ['Where does the oxygen go?'], 'next_step': 'Add a conclusion.'})
        self.client.force_login(self.ada.user)
        text = ' '.join(['Photosynthesis is how green plants make food using sunlight.'] * 8)
        r = self._post('/assistant/essay/', {'text': text, 'assignment': self.assignment.pk})
        self.assertContains(r, 'Add an example of a plant')
        fb = EssayFeedback.objects.get()
        self.assertEqual(fb.assignment, self.assignment)
        self.assertIn('Do NOT rewrite', fake.prompt())
        self.assertIn('Explain photosynthesis in 300 words', fake.prompt())

    def test_study_plan(self):
        with self.assertRaises(AIError):
            learning.study_plan(self.ada, self.ada.user)          # nothing to plan from yet
        TermResult.objects.create(tenant=self.school, student=self.ada, class_assigned=self.jss2a, subject=self.maths,
                                  session=self.session, term=self.term, exam_score=42)
        fake = self.fake({'summary': 'Focus on maths.', 'days': [{'day': 'Mon 06 Oct', 'tasks': ['30 min: fractions']}]})
        plan = learning.study_plan(self.ada, self.ada.user)
        self.assertEqual(plan.plan['days'][0]['tasks'], ['30 min: fractions'])
        self.assertIn('Mathematics (42%)', fake.prompt())
        with self.assertRaises(AIError):
            learning.study_plan(self.ada, self.ada.user)          # one a day for students
        parent = User.objects.create_user(username=f'parent_{self.ada.id}', password='x', tenant=self.school,
                                          is_parent=True, email='obi@example.com')
        self.client.force_login(parent)
        self.assertContains(self._get(f'/assistant/study-plan/{self.ada.pk}/'), '30 min: fractions')
        self.assertEqual(self._get(f'/assistant/study-plan/{self.chi.pk}/').status_code, 302)
        self.assertEqual(StudyPlan.objects.count(), 1)
