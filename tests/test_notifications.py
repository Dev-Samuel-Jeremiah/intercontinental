"""
In-app notifications: who sees what, read state, the bell's feed, and the
school events that create them.
"""
import json
from datetime import timedelta

from django.test import Client
from django.utils import timezone

from academics.models import AcademicSession, Term
from announcement.models import Announcement
from communications.models import ChatMessage, Conversation
from family.services import parent_user
from idcards.models import GateScanLog
from notifications import events, services as svc
from notifications.models import Notification
from safety.models import StaffLeave
from users.models import Attendance, Staff, Subject, User

from .factories import TenantTestCase, make_admin, make_class, make_school, make_student, make_teacher


class Base(TenantTestCase):

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha')
        self.admin = make_admin(self.school)
        self.teacher_user = make_teacher(self.school)
        self.teacher = Staff.objects.get(user=self.teacher_user)
        self.jss1, self.ss2 = make_class(self.school, 'JSS 1'), make_class(self.school, 'SS 2')
        self.amina = make_student(self.school, 'A/001', 'Amina Bello', school_class=self.jss1,
                                  parent_email='bello@example.com')
        self.musa = make_student(self.school, 'A/002', 'Musa Bello', school_class=self.ss2,
                                 parent_email='bello@example.com')
        self.zainab = make_student(self.school, 'A/003', 'Zainab Umar', school_class=self.jss1,
                                   parent_email='umar@example.com')
        self.bello_parent = parent_user(self.musa)        # siblings share the family feed
        self.umar_parent = parent_user(self.zainab)

    def titles(self, user):
        return [n.title for n in svc.visible_to(user, self.school)]


class AudienceTests(Base):

    def test_a_family_notice_reaches_every_sibling_account_and_no_one_else(self):
        svc.to_family(self.amina, 'gate', 'Amina Bello arrived at school', admin_feed=False)
        self.assertIn('Amina Bello arrived at school', self.titles(self.bello_parent))
        self.assertIn('Amina Bello arrived at school', self.titles(parent_user(self.amina)))
        self.assertNotIn('Amina Bello arrived at school', self.titles(self.umar_parent))
        self.assertNotIn('Amina Bello arrived at school', self.titles(self.admin))     # routine: not in admin feed

    def test_class_notices_go_to_that_class_and_its_parents(self):
        svc.notify(self.school, 'exam', 'New exam: Maths', Notification.CLASS_STUDENTS, school_class=self.jss1)
        svc.notify(self.school, 'homework', 'Homework: Fractions', Notification.CLASS_PARENTS, school_class=self.jss1)
        self.assertIn('New exam: Maths', self.titles(self.amina.user))
        self.assertNotIn('New exam: Maths', self.titles(self.musa.user))
        self.assertIn('Homework: Fractions', self.titles(self.bello_parent))         # Amina is in JSS 1
        self.assertNotIn('Homework: Fractions', self.titles(self.teacher_user))

    def test_admins_see_school_events_but_not_private_ones(self):
        svc.to_admins(self.school, 'admission', 'New admission application')
        svc.to_user(self.teacher_user, 'message', 'Message from a parent', admin_feed=False)
        self.assertEqual(self.titles(self.admin), ['New admission application'])
        self.assertEqual(self.titles(self.teacher_user), ['Message from a parent'])

    def test_nobody_is_notified_about_their_own_action(self):
        svc.to_admins(self.school, 'result', 'Results published', actor=self.admin)
        self.assertEqual(self.titles(self.admin), [])


class ReadStateTests(Base):

    def test_read_one_then_all(self):
        a = svc.to_admins(self.school, 'admission', 'One')
        svc.to_admins(self.school, 'admission', 'Two')
        self.assertEqual(svc.unread(self.admin, self.school).count(), 2)
        svc.mark_read(self.admin, self.school, [a.pk])
        self.assertEqual(svc.unread(self.admin, self.school).count(), 1)
        svc.mark_all_read(self.admin)
        self.assertEqual(svc.unread(self.admin, self.school).count(), 0)
        svc.to_admins(self.school, 'admission', 'Three')
        self.assertEqual([n.title for n in svc.unread(self.admin, self.school)], ['Three'])

    def test_you_cannot_mark_someone_elses_notice(self):
        n = svc.to_family(self.amina, 'gate', 'Arrived', admin_feed=False)
        self.assertEqual(svc.mark_read(self.teacher_user, self.school, [n.pk]), 0)

    def test_dedupe_records_an_event_once(self):
        svc.to_admins(self.school, 'admission', 'Once', dedupe='app:1')
        svc.to_admins(self.school, 'admission', 'Once', dedupe='app:1')
        self.assertEqual(Notification.objects.filter(dedupe_key='app:1').count(), 1)


class FeedTests(Base):

    def client_for(self, user):
        c = Client(HTTP_HOST=self.host(self.school))
        c.force_login(user)
        return c

    def test_feed_polling_and_reading(self):
        first = svc.to_admins(self.school, 'admission', 'Older')
        c = self.client_for(self.admin)
        data = json.loads(c.get('/notifications/feed/').content)
        self.assertEqual((data['unread'], data['latest'], [i['title'] for i in data['items']]), (1, first.pk, ['Older']))
        newer = svc.to_admins(self.school, 'leave', 'Newer')
        data = json.loads(c.get(f'/notifications/feed/?after={first.pk}').content)
        self.assertEqual(([i['title'] for i in data['items']], data['unread']), (['Newer'], 2))
        data = json.loads(c.post('/notifications/read/', {'id': [newer.pk]}).content)
        self.assertEqual(data['unread'], 1)
        data = json.loads(c.post('/notifications/read/', {'all': '1'}).content)
        self.assertEqual(data['unread'], 0)

    def test_opening_marks_read_and_follows_the_link(self):
        n = svc.to_admins(self.school, 'admission', 'Open me', url='/admissions/')
        r = self.client_for(self.admin).get(f'/notifications/{n.pk}/open/')
        self.assertEqual(r.headers['Location'], '/admissions/')
        self.assertEqual(svc.unread(self.admin, self.school).count(), 0)

    def test_someone_elses_notice_is_not_found_and_signed_out_is_refused(self):
        n = svc.to_family(self.amina, 'gate', 'Arrived', admin_feed=False, url='/parent/dashboard/')
        self.assertEqual(self.client_for(self.teacher_user).get(f'/notifications/{n.pk}/open/').status_code, 404)
        self.assertEqual(Client(HTTP_HOST=self.host(self.school)).get('/notifications/feed/').status_code, 401)

    def test_the_page_lists_notices(self):
        svc.to_admins(self.school, 'admission', 'Listed on the page')
        self.assertContains(self.client_for(self.admin).get('/notifications/'), 'Listed on the page')


class SchoolEventTests(Base):

    def test_a_published_announcement_reaches_the_class_and_its_parents(self):
        a = Announcement.objects.create(tenant=self.school, title='Sports day', message='Friday', school_class=self.jss1,
                                        audience='both')
        self.assertNotIn('Sports day', self.titles(self.amina.user))     # draft: nothing yet
        a.publish()
        self.assertIn('Sports day', self.titles(self.amina.user))
        self.assertIn('Sports day', self.titles(self.bello_parent))
        self.assertNotIn('Sports day', self.titles(self.musa.user))

    def test_an_exam_is_announced_when_published_not_before(self):
        from cbt.models import Exam
        maths = Subject.objects.create(tenant=self.school, name='Mathematics')
        exam = Exam.objects.create(tenant=self.school, title='Maths test', subject=maths,
                                   start_time=timezone.now(), end_time=timezone.now() + timedelta(hours=1))
        exam.classes.add(self.jss1)
        self.assertEqual(self.titles(self.amina.user), [])
        exam.published = True
        exam.save()
        exam.save()
        self.assertEqual(self.titles(self.amina.user), ['New exam: Maths test'])

    def test_leave_request_and_decision(self):
        lv = StaffLeave.objects.create(tenant=self.school, staff=self.teacher, leave_type='SICK',
                                       start_date=timezone.localdate(), end_date=timezone.localdate())
        self.assertIn(f'Leave request: {self.teacher.full_name}', self.titles(self.admin))
        lv.status, lv.reviewed_by = 'APPROVED', self.admin
        lv.save()
        self.assertIn(f"{self.teacher.full_name}'s leave was approved", self.titles(self.teacher_user))

    def test_chat_goes_to_the_other_side_and_never_to_admins(self):
        conv = Conversation.objects.create(tenant=self.school, student=self.amina, teacher=self.teacher,
                                           parent=self.bello_parent, subject='Homework')
        ChatMessage.objects.create(tenant=self.school, conversation=conv, sender=self.bello_parent, body='Hello')
        ChatMessage.objects.create(tenant=self.school, conversation=conv, sender=self.teacher_user, body='Hi')
        self.assertEqual(self.titles(self.teacher_user), ['Message from the parent of Amina Bello'])
        self.assertEqual(self.titles(self.bello_parent), [f'New message from {self.teacher.full_name}'])
        self.assertEqual(self.titles(self.admin), [])

    def test_gate_scans_tell_the_family(self):
        GateScanLog.objects.create(tenant=self.school, student=self.amina, direction='IN')
        self.assertEqual(self.titles(self.bello_parent), ['Amina Bello arrived at school'])

    def test_results_publishing_tells_each_family_and_child_and_admins_once(self):
        session = AcademicSession.objects.create(tenant=self.school, name='2026/2027')
        term = Term.objects.create(tenant=self.school, session=session, name='1st Term')
        events.results_published(self.school, [self.amina.pk, self.zainab.pk], term.pk)
        events.results_published(self.school, [self.amina.pk, self.zainab.pk], term.pk)     # repeated click
        self.assertEqual(self.titles(self.amina.user), ['Your 1st Term result is ready'])
        self.assertEqual(self.titles(self.umar_parent), ["Zainab Umar's 1st Term result is ready"])
        self.assertEqual(self.titles(self.admin), ['Results published for 2 students'])

    def test_todays_absences_tell_the_family_once(self):
        Attendance.objects.create(tenant=self.school, student=self.zainab, school_class=self.jss1, status='A')
        events.absences_marked(self.school, self.jss1, timezone.localdate(), actor=self.teacher_user)
        events.absences_marked(self.school, self.jss1, timezone.localdate(), actor=self.teacher_user)
        self.assertEqual(self.titles(self.umar_parent), ['Zainab Umar was marked absent'])
        self.assertEqual(self.titles(self.admin), ['1 absence in JSS 1'])
        self.assertEqual(self.titles(self.teacher_user), [])        # their own register
