"""
The bell carries everyday events: staff chat messages (direct chats every message, group chats at most
every half hour per person), lesson notes and result sheets sent for review and decided, and the
corrections parents send.
"""
from academics.models import AcademicSession, Term
from curriculum.models import Department, LessonNote
from notifications.models import Notification as N
from results.models import SubjectSheet
from staffchat import chat
from tenants.middleware import set_current_tenant
from users.models import ProfileChangeRequest, Staff, Subject

from .factories import TenantTestCase, make_admin, make_class, make_school, make_student, make_teacher


class BellEventTests(TenantTestCase):

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha')
        self.admin = make_admin(self.school)
        self.t1 = make_teacher(self.school, username='t1', full_name='Grace Okon')
        self.t2 = make_teacher(self.school, username='t2', full_name='Musa Bello')
        set_current_tenant(self.school)
        self.s1, self.s2 = Staff.objects.get(user=self.t1), Staff.objects.get(user=self.t2)

    def mine(self, user):
        return list(N.objects.filter(recipient=user).values_list('title', flat=True))

    def admin_feed(self):
        return list(N.objects.filter(audience=N.ADMINS).values_list('title', flat=True))

    def test_staff_chat_messages_ring_the_bell(self):
        room = chat.direct_room(self.t1, self.t2, self.school)
        chat.send(room, self.t1, self.school, 'Can you cover my 2pm class?')
        self.assertEqual(self.mine(self.t2), ['Message from Grace Okon'])
        self.assertEqual(self.mine(self.t1), [])                       # never about your own message
        group = chat.create_group(self.school, self.t1, 'Science dept', member_ids=[self.t2.pk])
        chat.send(group, self.t1, self.school, 'Meeting at 3')
        chat.send(group, self.t1, self.school, 'Bring your schemes')
        self.assertEqual(self.mine(self.t2).count('New messages in Science dept'), 1)   # not one per message

    def test_lesson_notes_and_result_sheets(self):
        science = Subject.objects.create(tenant=self.school, name='Physics')
        Department.objects.create(tenant=self.school, name='Sciences', hod=self.s2).subjects.add(science)
        session = AcademicSession.objects.create(tenant=self.school, name='2026/2027', is_active=True)
        term = Term.objects.create(tenant=self.school, session=session, name='1st Term', is_active=True)
        cls = make_class(self.school, 'JSS 1A')
        note = LessonNote.objects.create(tenant=self.school, teacher=self.s1, subject=science, school_class=cls,
                                         term=term, week=3, title='Motion', content='…')
        note.status = 'SUBMITTED'
        note.save()
        self.assertIn('Lesson note to review: Grace Okon', self.mine(self.t2))
        note.status, note.reviewed_by = 'RETURNED', self.s2
        note.save()
        self.assertIn('Your lesson note was returned', self.mine(self.t1))

        sheet = SubjectSheet.objects.create(tenant=self.school, school_class=cls, subject=science, term=term)
        sheet.status, sheet.submitted_by = 'SUBMITTED', self.t1
        sheet.save()
        self.assertIn('Result sheet to approve', self.admin_feed())
        self.assertIn('Result sheet to approve', self.mine(self.t2))
        sheet.status, sheet.reviewed_by = 'APPROVED', self.admin
        sheet.save()
        self.assertIn('Your result sheet was approved', self.mine(self.t1))

    def test_parents_corrections(self):
        ada = make_student(self.school, 'A1', 'Ada Obi', parent_email='mum@example.com')
        req = ProfileChangeRequest.objects.create(tenant=self.school, student=ada, field='parent_phone',
                                                  new_value='08031234567')
        self.assertIn('Correction sent by a parent', self.admin_feed())
        req.status = 'APPROVED'
        req.save()
        self.assertTrue(N.objects.filter(audience=N.FAMILY, title='Your correction was made').exists())
