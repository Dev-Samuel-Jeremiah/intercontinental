"""
Classroom: who may see and change a course, late submissions, course
announcements, live classes with attendance, and copying a course.
"""
from datetime import timedelta

from django.core import mail
from django.test import override_settings
from django.utils import timezone

from classroom import services as svc
from classroom.models import (Assignment, AssignmentQuestion, Course, CourseAnnouncement, LiveAttendance, LiveClass,
                              Question, Quiz, Section, Submission)
from tenants.middleware import set_current_tenant

from .factories import TenantTestCase, make_admin, make_class, make_school, make_student, make_teacher


class Base(TenantTestCase):

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha')
        self.admin = make_admin(self.school)
        self.teacher = make_teacher(self.school)
        self.other_teacher = make_teacher(self.school, username='alpha2', full_name='Other')
        self.jss1 = make_class(self.school, 'JSS 1A')
        self.jss2 = make_class(self.school, 'JSS 2A')
        set_current_tenant(self.school)
        self.ada = make_student(self.school, 'S1', full_name='Ada Bello', school_class=self.jss1,
                                parent_email='mum@example.com')
        self.outsider = make_student(self.school, 'S2', full_name='Outsider', school_class=self.jss2)
        set_current_tenant(self.school)
        self.course = Course.objects.create(tenant=self.school, name='Basic Science', teacher=self.teacher,
                                            class_assigned=self.jss1)
        self.section = Section.objects.create(tenant=self.school, course=self.course, title='Week 1', order=1)
        self.assignment = Assignment.objects.create(tenant=self.school, section=self.section, title='Essay',
                                                    description='Write', due_date=timezone.now() + timedelta(days=1))
        AssignmentQuestion.objects.create(tenant=self.school, assignment=self.assignment, question_text='Explain.')

    def _get(self, path, **kw):
        return self.client.get(path, HTTP_HOST=self.host(self.school), **kw)

    def _post(self, path, data=None, **kw):
        return self.client.post(path, data or {}, HTTP_HOST=self.host(self.school), **kw)


class AccessTests(Base):

    def test_students_outside_the_course_are_kept_out(self):
        """Regression: any student could open and submit any assignment."""
        self.client.force_login(self.outsider.user)
        self.assertEqual(self._get(f'/classroom/student/assignment/{self.assignment.id}/').status_code, 403)
        self._post(f'/classroom/student/assignment/{self.assignment.id}/', {'answer_1': 'x'})
        self.assertFalse(Submission.objects.exists())
        self.client.force_login(self.ada.user)
        self.assertEqual(self._get(f'/classroom/student/assignment/{self.assignment.id}/').status_code, 200)

    def test_only_the_teacher_or_admin_changes_a_course(self):
        """Regression: any staff member could edit any course."""
        self.client.force_login(self.other_teacher)
        self.assertEqual(self._get(f'/classroom/teacher/course/{self.course.id}/').status_code, 403)
        self.assertEqual(self._get(f'/classroom/assignment/{self.assignment.id}/grade/').status_code, 403)
        self.client.force_login(self.admin)
        self.assertEqual(self._get(f'/classroom/teacher/course/{self.course.id}/').status_code, 200)

    def test_grading_ignores_a_submission_from_another_assignment(self):
        other = Assignment.objects.create(tenant=self.school, section=self.section, title='Other', description='x',
                                          due_date=timezone.now())
        sub = Submission.objects.create(tenant=self.school, assignment=other, student=self.ada.user,
                                        submitted_at=timezone.now())
        self.client.force_login(self.teacher)
        r = self._get(f'/classroom/assignment/{self.assignment.id}/grade/?submission_id={sub.id}')
        self.assertEqual(r.status_code, 404)

    def test_course_list_includes_class_courses(self):
        self.client.force_login(self.ada.user)
        self.assertContains(self._get('/classroom/student/courses/'), 'Basic Science')


class LateTests(Base):

    def test_policy(self):
        a = self.assignment
        due = a.due_date
        self.assertEqual(svc.late_status(a, due)['penalty'], 0)
        a.grace_minutes = 30
        self.assertEqual(svc.late_status(a, due + timedelta(minutes=20))['penalty'], 0)
        s = svc.late_status(a, due + timedelta(hours=26))
        self.assertEqual((s['open'], s['late_days'], s['penalty']), (True, 2, 20))
        self.assertFalse(svc.late_status(a, due + timedelta(days=5))['open'])
        a.allow_late = False
        self.assertFalse(svc.late_status(a, due + timedelta(hours=1))['open'])

    def test_late_submission_records_the_penalty(self):
        Assignment.objects.filter(pk=self.assignment.pk).update(due_date=timezone.now() - timedelta(hours=30))
        q = self.assignment.questions.first()
        self.client.force_login(self.ada.user)
        self.assertContains(self._get(f'/classroom/student/assignment/{self.assignment.id}/'), '20% will come off')
        self._post(f'/classroom/student/assignment/{self.assignment.id}/', {f'answer_{q.id}': 'My answer'})
        sub = Submission.objects.get()
        self.assertEqual((sub.late_days, sub.penalty_percent), (2, 20))
        sub.grade = 8
        sub.save()
        self.assertEqual(sub.final_grade, 6.4)

    def test_closed_after_the_last_late_day(self):
        Assignment.objects.filter(pk=self.assignment.pk).update(due_date=timezone.now() - timedelta(days=10))
        self.client.force_login(self.ada.user)
        self._post(f'/classroom/student/assignment/{self.assignment.id}/', {'answer_1': 'x'})
        self.assertFalse(Submission.objects.exists())


class AnnouncementTests(Base):

    @override_settings(RUN_TASKS_EAGERLY=True)
    def test_post_and_email_parents(self):
        svc.announce(self.course, self.teacher, 'Test on Friday', 'Revise chapter 2.', email_parents=True)
        self.assertEqual(mail.outbox[-1].to, ['mum@example.com'])
        self.assertIn('Test on Friday', mail.outbox[-1].subject)
        with self.assertRaises(svc.ClassroomError):
            svc.announce(self.course, self.teacher, '', 'x')

    def test_pages(self):
        self.client.force_login(self.teacher)
        self._post(f'/classroom/course/{self.course.id}/hub/', {'action': 'announce', 'title': 'Welcome', 'body': 'Hello class'})
        self.client.force_login(self.ada.user)
        self.assertContains(self._get(f'/classroom/course/{self.course.id}/hub/'), 'Hello class')
        self._post(f'/classroom/course/{self.course.id}/hub/', {'action': 'announce', 'title': 'X', 'body': 'Y'})
        self.assertEqual(CourseAnnouncement.objects.count(), 1)               # students can't post
        self.client.force_login(self.outsider.user)
        self.assertEqual(self._get(f'/classroom/course/{self.course.id}/hub/').status_code, 302)


class LiveClassTests(Base):

    def test_schedule_rules(self):
        live = svc.schedule_live(self.course, self.teacher, 'Revision', timezone.now() + timedelta(hours=1))
        self.assertTrue(live.join_url.startswith('https://meet.jit.si/alpha-basic-science-'))
        with self.assertRaises(svc.ClassroomError):
            svc.schedule_live(self.course, self.teacher, 'Zoom', timezone.now(), platform='LINK', url='zoom.us/123')
        z = svc.schedule_live(self.course, self.teacher, 'Zoom', timezone.now(), platform='LINK',
                              url='https://zoom.us/j/123')
        self.assertEqual(z.join_url, 'https://zoom.us/j/123')

    def test_join_window_and_attendance(self):
        early = svc.schedule_live(self.course, self.teacher, 'Later', timezone.now() + timedelta(hours=2))
        with self.assertRaises(svc.ClassroomError):
            svc.join(early, self.ada.user)
        now = svc.schedule_live(self.course, self.teacher, 'Now', timezone.now() + timedelta(minutes=10))
        svc.join(now, self.ada.user)
        svc.join(now, self.ada.user)
        att = LiveAttendance.objects.get()
        self.assertEqual(att.joins, 2)
        rows, present = svc.attendance(now)
        self.assertEqual((len(rows), present), (1, 1))

    def test_pages(self):
        live = svc.schedule_live(self.course, self.teacher, 'Now', timezone.now())
        self.client.force_login(self.ada.user)
        r = self._get(f'/classroom/live/{live.id}/join/')
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r['Location'].startswith('https://meet.jit.si/'))
        self.client.force_login(self.outsider.user)
        self.assertEqual(self._get(f'/classroom/live/{live.id}/join/').status_code, 403)
        self.client.force_login(self.teacher)
        self.assertContains(self._get(f'/classroom/live/{live.id}/attendance/'), '1 of 1 joined')


class CloneTests(Base):

    def test_copy_everything_but_people(self):
        quiz = Quiz.objects.create(tenant=self.school, section=self.section, title='Quick quiz')
        Question.objects.create(tenant=self.school, quiz=quiz, question_text='2+2', option_a='4', option_b='5',
                                correct_option='A')
        Submission.objects.create(tenant=self.school, assignment=self.assignment, student=self.ada.user,
                                  submitted_at=timezone.now())
        new = svc.clone(self.course, self.teacher, 'Basic Science 2', self.jss2, shift_days=91)
        a2 = Assignment.objects.get(section__course=new)
        self.assertEqual(a2.due_date, self.assignment.due_date + timedelta(days=91))
        self.assertEqual(a2.questions.count(), 1)
        self.assertEqual(a2.submissions.count(), 0)
        self.assertEqual(Question.objects.filter(quiz__section__course=new).count(), 1)
        self.assertEqual(list(new.students.all()), [self.outsider.user])

    def test_page(self):
        self.client.force_login(self.teacher)
        r = self._post(f'/classroom/course/{self.course.id}/copy/', {'name': 'Copy', 'shift_days': 0})
        new = Course.objects.get(name='Copy')
        self.assertRedirects(r, f'/classroom/teacher/course/{new.id}/', fetch_redirect_response=False)
        self.client.force_login(self.other_teacher)
        self.assertEqual(self._get(f'/classroom/course/{self.course.id}/copy/').status_code, 403)
