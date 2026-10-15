"""
The CBT exam window: a start date/time and an end date/time, which may be on
different days, with the duration fitting inside it.
"""
from datetime import date, datetime, time, timedelta

from django.utils import timezone

from academics.models import AcademicSession, Term
from cbt.forms import ExamForm
from cbt.models import Exam
from users.models import Subject

from .factories import TenantTestCase, make_admin, make_class, make_school


class ExamWindowTests(TenantTestCase):

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha')
        self.admin = make_admin(self.school)
        self.jss1 = make_class(self.school, 'JSS 1')
        self.maths = Subject.objects.create(tenant=self.school, name='Mathematics')
        self.session = AcademicSession.objects.create(tenant=self.school, name='2026/2027')
        self.term = Term.objects.create(tenant=self.school, session=self.session, name='1st Term')
        self.day = date.today() + timedelta(days=3)

    def create(self, follow=False, **window):
        data = {'title': 'Maths test', 'exam_type': 'EXAM', 'subject': self.maths.pk, 'session': self.session.pk,
                'term': self.term.pk, 'duration_minutes': '60', 'classes': [self.jss1.pk],
                'start_date': self.day.isoformat(), 'start_time': '08:00', 'end_time': '10:00', **window}
        self.client.force_login(self.admin)
        # Errors come back to this page; success goes on to adding the exam's parts
        return self.client.post('/cbt/admin/exams/create/', data, HTTP_HOST=self.host(self.school), follow=follow)

    def test_an_exam_can_run_over_several_days(self):
        self.create(end_date=(self.day + timedelta(days=2)).isoformat(), end_time='18:00')
        exam = Exam.objects.get(title='Maths test')
        self.assertEqual(timezone.localtime(exam.start_time).replace(tzinfo=None), datetime.combine(self.day, time(8)))
        self.assertEqual(timezone.localtime(exam.end_time).replace(tzinfo=None),
                         datetime.combine(self.day + timedelta(days=2), time(18)))

    def test_the_end_date_defaults_to_the_start_date(self):
        self.create(exam_date=self.day.isoformat(), start_date='')          # the old single-date form
        exam = Exam.objects.get(title='Maths test')
        self.assertEqual(timezone.localtime(exam.end_time).date(), self.day)

    def test_end_before_start_is_refused(self):
        r = self.create(follow=True, end_date=(self.day - timedelta(days=1)).isoformat())
        self.assertContains(r, 'must end after it starts')
        self.assertFalse(Exam.objects.exists())

    def test_the_duration_must_fit_in_the_window(self):
        r = self.create(follow=True, end_time='08:30')
        self.assertContains(r, 'only 30 minutes')
        self.assertFalse(Exam.objects.exists())

    def test_missing_dates_do_not_crash(self):
        r = self.create(follow=True, start_date='')
        self.assertContains(r, 'Choose the start date')

    def test_editing_checks_the_window_too(self):
        start = timezone.make_aware(datetime.combine(self.day, time(8)))
        form = ExamForm(data={'title': 'T', 'exam_type': 'EXAM', 'subject': self.maths.pk, 'duration_minutes': 60,
                              'start_time': start, 'end_time': start - timedelta(hours=1)})
        self.assertFalse(form.is_valid())
        self.assertIn('must end after it starts', str(form.errors))
