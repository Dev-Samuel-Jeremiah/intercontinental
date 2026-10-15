"""
Short-answer (typed) questions belong in an exam's Essay section, where a
teacher marks them — never in the objective (CBT) section, which only marks
chosen options.
"""
import io
from datetime import timedelta

from django.core.management import call_command
from django.utils import timezone

from cbt.models import Exam, ExamPart, Question
from users.models import Subject

from .factories import TenantTestCase, make_admin, make_school


class SectionTests(TenantTestCase):

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha')
        self.admin = make_admin(self.school)
        maths = Subject.objects.create(tenant=self.school, name='Mathematics')
        self.exam = Exam.objects.create(tenant=self.school, title='Maths', subject=maths, start_time=timezone.now(),
                                        end_time=timezone.now() + timedelta(hours=2), duration_minutes=60)
        self.cbt = ExamPart.objects.create(exam=self.exam, part_type='CBT', duration_minutes=60)

    def test_short_answers_go_to_the_essay_section(self):
        essay = self.cbt.section_for('SA')
        self.assertEqual((essay.part_type, essay.exam), ('ESSAY', self.exam))
        self.assertEqual(self.cbt.section_for('SA'), essay)                 # reused, not duplicated
        self.assertEqual(self.cbt.section_for('MCQ'), self.cbt)
        self.assertEqual(essay.section_for('SA'), essay)

    def test_adding_a_short_answer_to_the_objective_section(self):
        self.client.force_login(self.admin)
        self.client.post('/cbt/admin/exams/questions/add/', {'exam_part': self.cbt.pk, 'question_text': 'Explain why…',
                                                 'question_type': 'SA', 'marks': 2},
                         HTTP_HOST=self.host(self.school))
        q = Question.objects.get(question_text='Explain why…')
        self.assertEqual(q.exam_part.part_type, 'ESSAY')

    def test_the_tidy_up_command(self):
        typed = Question.objects.create(exam_part=self.cbt, question_text='How far in 4 hours?', question_type='SA')
        choice = Question.objects.create(exam_part=self.cbt, question_text='2 + 2?', question_type='MCQ')
        out = io.StringIO()
        call_command('move_short_answers_to_essay', stdout=out)
        typed.refresh_from_db()
        self.assertEqual(typed.exam_part, self.cbt)                           # report only
        call_command('move_short_answers_to_essay', '--apply', stdout=out)
        typed.refresh_from_db()
        choice.refresh_from_db()
        self.assertEqual((typed.exam_part.part_type, typed.question_type), ('ESSAY', None))
        self.assertEqual(choice.exam_part, self.cbt)
