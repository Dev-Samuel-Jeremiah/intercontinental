"""
A student or parent only ever sees their own (child's) records: the staff
report sheets, score pages, school-wide dashboards and student-management
tools refuse them, while staff keep their access.
"""
from datetime import date, timedelta

from django.utils import timezone

from academics.models import AcademicSession, Term
from results.models import TermResult
from tenants.middleware import set_current_tenant
from users.models import ClassSubject, Subject, User

from .factories import TenantTestCase, make_admin, make_class, make_school, make_student, make_teacher


class Base(TenantTestCase):

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha')
        self.admin = make_admin(self.school)
        self.teacher = make_teacher(self.school, username='tolu', full_name='Tolu Ade')
        set_current_tenant(self.school)
        self.cls = make_class(self.school, 'Grade 1')
        self.maths = Subject.objects.create(tenant=self.school, name='Mathematics')
        ClassSubject.objects.create(tenant=self.school, school_class=self.cls, subject=self.maths)
        today = timezone.localdate()
        self.session = AcademicSession.objects.create(tenant=self.school, name='2025/2026', start_date=date(2025, 9, 8),
                                                      is_active=True)
        self.term = Term.objects.create(tenant=self.school, session=self.session, name='1st Term', is_active=True,
                                        start_date=today - timedelta(days=30), end_date=today + timedelta(days=40))
        self.me = make_student(self.school, 'A1', 'Ahmed Yakubu', school_class=self.cls)
        self.other = make_student(self.school, 'A2', 'Aisha Abubakar', school_class=self.cls)
        for st, score in ((self.me, 50), (self.other, 80)):
            TermResult.objects.create(tenant=self.school, student=st, class_assigned=self.cls, subject=self.maths,
                                      session=self.session, term=self.term, ca1_score=10, exam_score=score - 10,
                                      published=True)
        self.parent = User.objects.create_user(username=f'parent_{self.me.id}', password='x', tenant=self.school,
                                               is_parent=True, email=self.me.parent_email)

    def get(self, user, path):
        self.client.logout()
        if user:
            self.client.force_login(user)
        return self.client.get(path, HTTP_HOST=self.host(self.school))

    def post(self, user, path, data=None):
        self.client.logout()
        if user:
            self.client.force_login(user)
        return self.client.post(path, data or {}, HTTP_HOST=self.host(self.school))

    def sheet(self, student):
        return (f'/results/term/?class={self.cls.id}&student={student.id}'
                f'&term={self.term.id}&session={self.session.id}')


class ResultSheetTests(Base):

    def test_student_cannot_open_another_students_report(self):
        page = self.get(self.me.user, self.sheet(self.other))
        self.assertEqual(page.status_code, 302)
        self.assertEqual(page['Location'], f'/results/results/{self.session.id}/{self.term.id}/')
        page = self.client.get(page['Location'], HTTP_HOST=self.host(self.school))
        self.assertNotContains(page, 'Aisha Abubakar')

    def test_my_term_results_opens_my_own_report(self):
        page = self.get(self.me.user, '/results/mine/')
        self.assertRedirects(page, f'/results/results/{self.session.id}/{self.term.id}/', fetch_redirect_response=False)

    def test_nothing_published_yet(self):
        TermResult.objects.filter(student=self.me).update(published=False)
        page = self.get(self.me.user, '/results/term/')
        self.assertRedirects(page, '/dashboard/student/', fetch_redirect_response=False)

    def test_parent_is_sent_to_their_own_children(self):
        page = self.get(self.parent, self.sheet(self.other))
        self.assertRedirects(page, '/results/dashboard/', fetch_redirect_response=False)

    def test_other_staff_result_pages_refuse_students(self):
        for path in (f'/results/cumulative/?student={self.other.id}',
                     f'/results/batch-results/?class={self.cls.id}&term={self.term.id}&session={self.session.id}'):
            for user in (self.me.user, self.parent):
                page = self.get(user, path)
                self.assertEqual(page.status_code, 302, path)
        page = self.get(self.me.user, f'/results/ai-comment/get/?student_id={self.other.id}&term_id={self.term.id}'
                                      f'&session_id={self.session.id}')
        self.assertEqual(page.status_code, 403)

    def test_staff_still_see_any_student(self):
        page = self.get(self.teacher, self.sheet(self.other))
        self.assertContains(page, 'Aisha Abubakar')


class DashboardAndToolTests(Base):

    def test_school_dashboards_refuse_students_parents_and_teachers(self):
        for user in (self.me.user, self.parent, self.teacher):
            for path in ('/dashboard/super-admin/', '/dashboard/admin/'):
                page = self.get(user, path)
                self.assertEqual(page.status_code, 302, (user.username, path))
        self.assertEqual(self.get(self.admin, '/dashboard/super-admin/').status_code, 200)

    def test_exam_scores_are_staff_only(self):
        self.assertEqual(self.get(None, '/cbt/exam-results/').status_code, 302)
        self.assertEqual(self.get(self.me.user, '/cbt/exam-results/').status_code, 302)
        self.assertEqual(self.get(self.teacher, '/cbt/exam-results/').status_code, 200)

    def test_student_cannot_reset_someone_elses_password(self):
        before = self.other.user.password
        page = self.post(self.me.user, '/students/generate_password/', {'student_id': self.other.id})
        self.assertEqual(page.status_code, 302)
        self.other.user.refresh_from_db()
        self.assertEqual(self.other.user.password, before)
        self.assertNotContains(self.get(self.me.user, '/students/generate_password/'), 'Aisha Abubakar',
                               status_code=302)

    def test_subject_tools_cannot_delete_results(self):
        path = f'/subjects/remove-student/{self.other.id}/{self.maths.id}/'
        for user in (None, self.me.user, self.parent):
            self.get(user, path)
        self.assertTrue(TermResult.objects.filter(student=self.other, subject=self.maths).exists())
        self.get(self.me.user, f'/subjects/remove-class/{self.cls.id}/{self.maths.id}/')
        self.assertTrue(ClassSubject.objects.filter(school_class=self.cls, subject=self.maths).exists())
        for path in ('/subjects/assign/', f'/subjects/student-manager/?student={self.other.id}',
                     f'/view/{self.cls.id}/'):
            self.assertEqual(self.get(self.me.user, path).status_code, 302, path)

    def test_question_upload_needs_exam_rights(self):
        self.assertEqual(self.get(None, '/cbt/questions/bulk-upload/').status_code, 302)
        page = self.get(self.me.user, '/cbt/questions/bulk-upload/')
        self.assertEqual(page.status_code, 302)
        self.assertEqual(self.get(self.me.user, '/cbt/admin/halls/').status_code, 302)   # refused, not a crash
