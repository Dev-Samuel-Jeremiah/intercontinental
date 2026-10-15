"""
Classroom: discussion, rubrics, similar submissions, completion
certificates, whiteboard photos and voice feedback.
"""
import io
from datetime import timedelta

from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from classroom import extras
from classroom.models import (Assignment, AssignmentQuestion, Course, CourseCertificate, Lesson, LessonProgress, Material,
                              Post, Question, Quiz, QuizSubmission, RubricCriterion, Section, Submission,
                              SubmissionAnswer, Thread)
from tenants.middleware import set_current_tenant

from .factories import TenantTestCase, make_admin, make_class, make_school, make_student, make_teacher

ESSAY = ('Photosynthesis is the process by which green plants use sunlight, water and carbon dioxide to make glucose '
         'and release oxygen into the air, and it takes place mainly in the leaves inside the chloroplasts which contain '
         'the green pigment chlorophyll that absorbs light energy for the reaction.')
PNG = b'\x89PNG\r\n\x1a\n' + b'0' * 64


class Base(TenantTestCase):

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha')
        self.admin = make_admin(self.school)
        self.teacher = make_teacher(self.school)
        self.jss1 = make_class(self.school, 'JSS 1A')
        set_current_tenant(self.school)
        self.ada = make_student(self.school, 'S1', full_name='Ada Bello', school_class=self.jss1)
        self.bola = make_student(self.school, 'S2', full_name='Bola Ade', school_class=self.jss1)
        self.out = make_student(self.school, 'S3', full_name='Outsider', school_class=make_class(self.school, 'JSS 2A'))
        set_current_tenant(self.school)
        self.course = Course.objects.create(tenant=self.school, name='Biology', teacher=self.teacher, class_assigned=self.jss1)
        self.section = Section.objects.create(tenant=self.school, course=self.course, title='Plants', order=1)
        self.assignment = Assignment.objects.create(tenant=self.school, section=self.section, title='Essay',
                                                    description='Explain', due_date=timezone.now() + timedelta(days=2))
        self.q = AssignmentQuestion.objects.create(tenant=self.school, assignment=self.assignment, question_text='Explain.')

    def _get(self, path, **kw):
        return self.client.get(path, HTTP_HOST=self.host(self.school), **kw)

    def _post(self, path, data=None, **kw):
        return self.client.post(path, data or {}, HTTP_HOST=self.host(self.school), **kw)

    def _submit(self, student, text, file=None):
        s = Submission.objects.create(tenant=self.school, assignment=self.assignment, student=student.user,
                                      submitted_at=timezone.now(), file=file)
        SubmissionAnswer.objects.create(tenant=self.school, submission=s, student=student.user, question=self.q,
                                        answer_text=text)
        return s


class DiscussionTests(Base):

    def test_flow(self):
        t = extras.start_thread(self.course, self.ada.user, 'What is chlorophyll?', 'Please explain.')
        p = extras.reply(t, self.bola.user, 'The green pigment.')
        with self.assertRaises(extras.ExtrasError):
            extras.mark_answer(p, self.bola.user)            # not the asker or teacher
        extras.mark_answer(p, self.ada.user)
        self.assertTrue(Post.objects.get().is_answer)
        with self.assertRaises(extras.ExtrasError):
            extras.start_thread(self.course, self.out.user, 'x', 'y')
        t.locked = True
        t.save()
        with self.assertRaises(extras.ExtrasError):
            extras.reply(t, self.bola.user, 'late')
        extras.reply(t, self.teacher, 'Teacher can still reply')

    def test_pages(self):
        self.client.force_login(self.ada.user)
        r = self._post(f'/classroom/course/{self.course.id}/discussion/', {'title': 'Question', 'body': 'Body'})
        t = Thread.objects.get()
        self.assertRedirects(r, f'/classroom/discussion/{t.id}/', fetch_redirect_response=False)
        self._post(f'/classroom/discussion/{t.id}/', {'action': 'reply', 'body': 'A reply'})
        self.assertContains(self._get(f'/classroom/discussion/{t.id}/'), 'A reply')
        self.client.force_login(self.out.user)
        self.assertEqual(self._get(f'/classroom/discussion/{t.id}/').status_code, 302)


class RubricTests(Base):

    def _rubric(self):
        extras.set_rubric(self.assignment, [('Understanding', '', 'Excellent 4, Good 3, Fair 2, Poor 1'),
                                            ('Spelling', '', 'Good 2, Weak 0')])

    def test_parse_and_rules(self):
        self.assertEqual(extras.parse_levels('Excellent 4 | Good: 3; Fair=2'), [('Excellent', 4), ('Good', 3), ('Fair', 2)])
        for bad in ('Excellent', 'Good 3', 'Good 3, Fair 3'):
            with self.assertRaises(extras.ExtrasError):
                extras.parse_levels(bad)

    def test_mark_out_of_ten(self):
        self._rubric()
        s = self._submit(self.ada, 'answer')
        c1, c2 = RubricCriterion.objects.all()
        extras.mark_with_rubric(s, {str(c1.id): c1.levels.get(points=3).id, str(c2.id): c2.levels.get(points=2).id})
        self.assertEqual(s.grade, round(10 * 5 / 6, 1))
        with self.assertRaises(extras.ExtrasError):
            extras.mark_with_rubric(s, {str(c1.id): c1.levels.first().id})            # a criterion missed
        with self.assertRaises(extras.ExtrasError):
            self._rubric()                                                            # locked once used

    def test_pages(self):
        self.client.force_login(self.teacher)
        self._post(f'/classroom/assignment/{self.assignment.id}/rubric/', {'name_0': 'Clarity', 'levels_0': 'Clear 2, Unclear 0'})
        self.assertEqual(RubricCriterion.objects.get().name, 'Clarity')
        self.client.force_login(self.ada.user)
        self.assertContains(self._get(f'/classroom/student/assignment/{self.assignment.id}/'), 'How this will be marked')
        s = self._submit(self.ada, 'x')
        self.client.force_login(self.teacher)
        c = RubricCriterion.objects.get()
        self._post(f'/classroom/assignment/{self.assignment.id}/grade/?submission_id={s.id}',
                   {f'rc_{c.id}': c.levels.get(points=2).id, 'feedback': 'Nice'})
        s.refresh_from_db()
        self.assertEqual((s.grade, s.feedback), (10, 'Nice'))


class SimilarityTests(Base):

    def test_copied_text_and_identical_files(self):
        self._submit(self.ada, ESSAY)
        self._submit(self.bola, ESSAY.replace('mainly', 'mostly'))
        pairs = extras.similar_pairs(self.assignment)
        self.assertEqual(len(pairs), 1)
        self.assertGreaterEqual(pairs[0]['score'], 70)

    def test_short_or_different_answers_are_not_flagged(self):
        self._submit(self.ada, 'Plants make food.')
        self._submit(self.bola, 'Plants make food.')
        self.assertEqual(extras.similar_pairs(self.assignment), [])

    def test_identical_uploads(self):
        self._submit(self.ada, 'a', SimpleUploadedFile('a.pdf', b'%PDF-1.4 same content'))
        self._submit(self.bola, 'b', SimpleUploadedFile('b.pdf', b'%PDF-1.4 same content'))
        self.assertEqual(extras.similar_pairs(self.assignment)[0]['score'], 100)

    def test_shown_on_the_marking_list(self):
        self._submit(self.ada, ESSAY)
        self._submit(self.bola, ESSAY)
        self.client.force_login(self.teacher)
        self.assertContains(self._get(f'/classroom/assignment/{self.assignment.id}/grade/'), 'Submissions that look alike')


class CertificateTests(Base):

    def setUp(self):
        super().setUp()
        self.lesson = Lesson.objects.create(tenant=self.school, section=self.section, title='Intro', video='x.mp4', order=1)
        self.quiz = Quiz.objects.create(tenant=self.school, section=self.section, title='Check', pass_mark=50)
        Question.objects.create(tenant=self.school, quiz=self.quiz, question_text='q', option_a='a', option_b='b',
                                correct_option='A')

    def test_progress_and_issue(self):
        self.assertIsNone(extras.certificate_for(self.course, self.ada.user))
        LessonProgress.objects.create(tenant=self.school, student=self.ada.user, lesson=self.lesson, watched=True)
        self._submit(self.ada, 'answer')
        QuizSubmission.objects.create(tenant=self.school, quiz=self.quiz, student=self.ada.user, score=0,
                                      total_questions=1, submitted_at=timezone.now())
        p = extras.progress(self.course, self.ada.user)
        self.assertEqual((p['done'], p['total'], p['complete']), (2, 3, False))       # failed the quiz
        QuizSubmission.objects.filter(student=self.ada.user).update(score=1)
        cert = extras.certificate_for(self.course, self.ada.user)
        self.assertIsNotNone(cert)
        self.assertEqual(extras.certificate_for(self.course, self.ada.user), cert)
        self.assertEqual(extras.verify(extras.reference(cert), self.school), cert)
        self.assertIsNone(extras.verify(extras.reference(cert) + 'x', self.school))

    def test_pages(self):
        self.client.force_login(self.ada.user)
        self.assertContains(self._get(f'/classroom/course/{self.course.id}/certificate/'), '0% complete')
        self.client.force_login(self.teacher)
        self.assertContains(self._get(f'/classroom/course/{self.course.id}/completion/'), 'Ada Bello')
        self.client.force_login(self.out.user)
        self.assertEqual(self._get(f'/classroom/course/{self.course.id}/certificate/').status_code, 302)


class BoardAndVoiceTests(Base):

    def test_board_photo(self):
        self.client.force_login(self.teacher)
        self._post(f'/classroom/course/{self.course.id}/board-photo/',
                   {'section': self.section.id, 'photo': SimpleUploadedFile('board.png', PNG, content_type='image/png')})
        m = Material.objects.get()
        self.assertTrue(m.title.startswith('Board notes'))
        with self.assertRaises(extras.ExtrasError):
            extras.add_board_photo(self.section, SimpleUploadedFile('x.png', b'not an image'))

    def test_voice_feedback_only_for_the_student_and_teacher(self):
        s = self._submit(self.ada, 'answer')
        extras.set_voice_feedback(s, SimpleUploadedFile('fb.webm', b'\x1aE\xdf\xa3 audio'))
        with self.assertRaises(extras.ExtrasError):
            extras.set_voice_feedback(s, SimpleUploadedFile('fb.exe', b'MZ'))
        self.client.force_login(self.ada.user)
        r = self._get(f'/classroom/submission/{s.id}/voice/')
        self.assertEqual((r.status_code, r['Content-Type']), (200, 'audio/webm'))
        self.client.force_login(self.bola.user)
        self.assertEqual(self._get(f'/classroom/submission/{s.id}/voice/').status_code, 404)
