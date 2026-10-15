"""
Students can find "Review answers" after an exam: on Available Exams (even when
retakes are allowed) and on the dashboard's My Exams tab (even once the exam
has closed), following the exam's review setting.
"""
from datetime import timedelta

from django.utils import timezone

from academics.models import AcademicSession, Term
from cbt import engine
from cbt.models import Exam, ExamAttempt, ExamPart
from users.models import Subject

from .factories import TenantTestCase, make_class, make_school, make_student


class ReviewLinkTests(TenantTestCase):

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha')
        self.jss1 = make_class(self.school, 'JSS 1')
        self.student = make_student(self.school, 'A1', 'Amina Bello', school_class=self.jss1)
        session = AcademicSession.objects.create(tenant=self.school, name='2026/2027', is_active=True)
        term = Term.objects.create(tenant=self.school, session=session, name='1st Term', is_active=True)
        now = timezone.now()
        self.exam = Exam.objects.create(tenant=self.school, title='Maths', session=session, term=term, published=True,
                                        subject=Subject.objects.create(tenant=self.school, name='Mathematics'),
                                        start_time=now - timedelta(hours=1), end_time=now + timedelta(days=2),
                                        duration_minutes=30, answer_review='AFTER_SUBMIT', allow_retake=True)
        self.exam.classes.add(self.jss1)
        self.part = ExamPart.objects.create(exam=self.exam, part_type='CBT', duration_minutes=30)
        self.attempt = ExamAttempt.objects.create(student=self.student, exam_part=self.part, completed=True,
                                                  submitted_at=now)

    def page(self, path):
        self.client.force_login(self.student.user)
        return self.client.get(path, HTTP_HOST=self.host(self.school)).content.decode()

    def test_review_link_states(self):
        self.assertEqual(engine.review_link(self.attempt), {'attempt': self.attempt.id})
        Exam.objects.filter(pk=self.exam.pk).update(answer_review='AFTER_CLOSE')
        self.attempt.refresh_from_db()
        self.assertEqual(engine.review_link(self.attempt), {'opens': self.exam.end_time})
        Exam.objects.filter(pk=self.exam.pk).update(answer_review='NEVER')
        self.attempt.refresh_from_db()
        self.assertIsNone(engine.review_link(self.attempt))

    def test_available_exams_shows_review_even_with_retakes(self):
        self.assertIn(f'/cbt/student/exam/review/{self.attempt.id}/', self.page('/cbt/student/exams/'))

    def test_dashboard_shows_review_and_when_it_opens(self):
        self.assertIn(f'/cbt/student/exam/review/{self.attempt.id}/', self.page('/dashboard/student/'))
        Exam.objects.filter(pk=self.exam.pk).update(answer_review='AFTER_CLOSE')
        html = self.page('/dashboard/student/')
        self.assertNotIn('/cbt/student/exam/review/', html)
        self.assertIn('Review from', html)
