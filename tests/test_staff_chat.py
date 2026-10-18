"""
Staff chat: community rooms everyone is in, one-to-one chats, groups, and
complaints to management (optionally anonymous) — with replies, reactions,
edits, deletions, read receipts, typing, polling and private attachments.
"""
import json
from datetime import timedelta

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from notifications.models import Notification
from staffchat import chat
from staffchat.models import ChatRoom, Member, Message
from tenants.middleware import set_current_tenant
from users.models import Staff

from .factories import TenantTestCase, make_admin, make_school, make_student, make_teacher


class Base(TenantTestCase):

    def setUp(self):
        super().setUp()
        cache.clear()
        self.school = make_school('alpha')
        self.admin = make_admin(self.school)
        self.ada = make_teacher(self.school, username='ada', full_name='Ada Obi')
        self.ben = make_teacher(self.school, username='ben', full_name='Ben Musa')
        self.cal = make_teacher(self.school, username='cal', full_name='Cal Eze')
        set_current_tenant(self.school)

    def login(self, user):
        self.client.force_login(user)

    def get(self, path, **kw):
        return self.client.get(path, HTTP_HOST=self.host(self.school), **kw)

    def post(self, path, data=None, **kw):
        return self.client.post(path, data or {}, HTTP_HOST=self.host(self.school), **kw)

    def api(self, path, data=None):
        r = self.post(path, json.dumps(data or {}), content_type='application/json')
        return r.status_code, json.loads(r.content)

    def room(self, code):
        chat.sync(self.ada, self.school)
        return ChatRoom.objects.get(tenant=self.school, community=code)


class CommunityTests(Base):

    def test_everyone_is_in_the_community_rooms_and_management_runs_them(self):
        for u in (self.ada, self.admin):
            chat.sync(u, self.school)
        everyone = ChatRoom.objects.get(community=ChatRoom.ALL_STAFF)
        news = ChatRoom.objects.get(community=ChatRoom.ANNOUNCEMENTS)
        self.assertEqual(Member.objects.get(room=everyone, user=self.admin).role, Member.ADMIN)
        self.assertEqual(Member.objects.get(room=everyone, user=self.ada).role, Member.MEMBER)
        chat.send(everyone, self.ada, self.school, 'Morning all!')
        with self.assertRaisesMessage(chat.ChatError, 'Only admins'):
            chat.send(news, self.ada, self.school, 'Hello')
        chat.send(news, self.admin, self.school, 'Staff meeting on Friday.')

    def test_students_and_parents_are_kept_out(self):
        pupil = make_student(self.school, 'S1', 'Pupil One')
        self.login(pupil.user)
        self.assertRedirects(self.get('/staff-chat/'), '/dashboard/', fetch_redirect_response=False)
        self.assertEqual(self.get('/staff-chat/api/poll/').status_code, 403)
        self.assertEqual(json.loads(self.get('/staff-chat/api/unread/').content), {'unread': 0})


class MessagingTests(Base):

    def setUp(self):
        super().setUp()
        self.everyone = self.room(ChatRoom.ALL_STAFF)
        for u in (self.ben, self.admin):
            chat.sync(u, self.school)

    def test_send_reply_react_edit_delete(self):
        self.login(self.ada)
        code, r = self.api(f'/staff-chat/api/rooms/{self.everyone.id}/send/', {'body': 'Who has the JSS 2 register?'})
        first = r['message']
        self.assertEqual((code, first['name'], first['mine']), (200, 'You', True))
        self.login(self.ben)
        code, r = self.api(f'/staff-chat/api/rooms/{self.everyone.id}/send/', {'body': 'In the staff room', 'reply_to': first['id']})
        self.assertEqual(r['message']['reply']['name'], 'Ada Obi')
        _, r = self.api(f'/staff-chat/api/messages/{first["id"]}/react/', {'emoji': '👍'})
        self.assertEqual(r['message']['reactions'][0]['count'], 1)
        _, r = self.api(f'/staff-chat/api/messages/{first["id"]}/react/', {'emoji': '👍'})      # tap again: taken back
        self.assertEqual(r['message']['reactions'], [])
        code, _ = self.api(f'/staff-chat/api/messages/{first["id"]}/edit/', {'body': 'Hacked'})
        self.assertEqual(code, 400)                                                           # not Ben's
        code, _ = self.api(f'/staff-chat/api/messages/{first["id"]}/delete/')
        self.assertEqual(code, 403)
        self.login(self.ada)
        _, r = self.api(f'/staff-chat/api/messages/{first["id"]}/edit/', {'body': 'Who has the JSS 2A register?'})
        self.assertTrue(r['message']['edited'])
        Message.objects.filter(pk=first['id']).update(created_at=timezone.now() - timedelta(minutes=30))
        code, _ = self.api(f'/staff-chat/api/messages/{first["id"]}/edit/', {'body': 'Too late'})
        self.assertEqual(code, 400)
        _, r = self.api(f'/staff-chat/api/messages/{first["id"]}/delete/')
        self.assertEqual((r['message']['deleted'], r['message']['body']), (True, ''))

    def test_management_can_remove_anything_in_the_community(self):
        msg = chat.send(self.everyone, self.ada, self.school, 'Something inappropriate')
        chat.delete(msg, self.admin, self.school)
        self.assertIsNotNone(Message.objects.get(pk=msg.pk).deleted_at)

    def test_poll_brings_new_and_changed_messages_typing_and_reads(self):
        old = chat.send(self.everyone, self.ada, self.school, 'First')
        self.login(self.ben)
        r = json.loads(self.get(f'/staff-chat/api/rooms/{self.everyone.id}/').content)
        since, after = r['now'], r['messages'][-1]['id']
        chat.send(self.everyone, self.ada, self.school, 'Second')
        chat.react(old, self.admin, '❤️')
        chat.typing(Member.objects.get(room=self.everyone, user=self.admin))
        r = json.loads(self.get('/staff-chat/api/poll/', data={'room': self.everyone.id, 'after': after,
                                                               'since': since, 'visible': '1'}).content)
        self.assertEqual([m['body'] for m in r['messages']], ['Second'])
        self.assertEqual(r['changed'][0]['reactions'][0]['emoji'], '❤️')
        self.assertEqual(len(r['typing']), 1)
        self.assertEqual(Member.objects.get(room=self.everyone, user=self.ben).last_read_id, r['messages'][-1]['id'])
        self.assertIn('rooms', r)

    def test_unread_counts(self):
        chat.send(self.everyone, self.ada, self.school, 'One')
        chat.send(self.everyone, self.ada, self.school, 'Two')
        self.assertEqual(chat.unread_total(self.ben, self.school), 2)
        self.assertEqual(chat.unread_total(self.ada, self.school), 0)                # your own messages are read
        self.login(self.ben)
        self.get(f'/staff-chat/api/rooms/{self.everyone.id}/')                       # opening the chat reads it
        self.assertEqual(chat.unread_total(self.ben, self.school), 0)

    def test_attachments_are_private(self):
        self.login(self.ada)
        up = SimpleUploadedFile('notes.pdf', b'%PDF-1.4 notes', content_type='application/pdf')
        r = json.loads(self.post(f'/staff-chat/api/rooms/{self.everyone.id}/send/', {'body': '', 'file': up}).content)
        url = r['message']['file']['url']
        self.assertEqual(self.get(url).status_code, 200)
        bad = SimpleUploadedFile('virus.exe', b'MZ', content_type='application/octet-stream')
        r = self.post(f'/staff-chat/api/rooms/{self.everyone.id}/send/', {'file': bad})
        self.assertEqual(r.status_code, 400)
        dm = chat.direct_room(self.ada, self.ben, self.school)
        up = SimpleUploadedFile('private.png', b'\x89PNG', content_type='image/png')
        r = json.loads(self.post(f'/staff-chat/api/rooms/{dm.id}/send/', {'file': up}).content)
        self.login(self.cal)
        self.assertEqual(self.get(r['message']['file']['url']).status_code, 404)    # not in that chat


class DirectAndGroupTests(Base):

    def test_direct_chat_is_private_and_single(self):
        self.login(self.ada)
        r = self.get(f'/staff-chat/direct/{self.ben.id}/')
        dm = ChatRoom.objects.get(kind=ChatRoom.DIRECT)
        self.assertRedirects(r, f'/staff-chat/{dm.id}/', fetch_redirect_response=False)
        self.assertEqual(chat.direct_room(self.ben, self.ada, self.school), dm)        # the same chat both ways
        chat.send(dm, self.ada, self.school, 'Can you cover my class?')
        self.login(self.cal)
        self.assertEqual(self.get(f'/staff-chat/api/rooms/{dm.id}/').status_code, 404)
        self.assertEqual(self.api(f'/staff-chat/api/rooms/{dm.id}/send/', {'body': 'hi'})[0], 404)
        pupil = make_student(self.school, 'S1', 'Pupil One')
        with self.assertRaises(chat.ChatError):
            chat.direct_room(self.ada, pupil.user, self.school)

    def test_group_lifecycle(self):
        self.login(self.ada)
        self.post('/staff-chat/groups/new/', {'name': 'Science Department', 'members': [self.ben.id]})
        g = ChatRoom.objects.get(name='Science Department')
        self.assertEqual(set(g.members.values_list('user_id', 'role')),
                         {(self.ada.id, Member.ADMIN), (self.ben.id, Member.MEMBER)})
        self.post(f'/staff-chat/{g.id}/manage/', {'action': 'add', 'members': [self.cal.id]})
        self.assertTrue(g.members.filter(user=self.cal).exists())
        self.login(self.ben)
        self.post(f'/staff-chat/{g.id}/manage/', {'action': 'remove', 'user': self.cal.id})   # not an admin
        self.assertTrue(g.members.filter(user=self.cal).exists())
        self.login(self.ada)
        self.post(f'/staff-chat/{g.id}/manage/', {'action': 'edit', 'name': 'Sciences', 'only_admins_post': 'on'})
        g.refresh_from_db()
        self.assertEqual((g.name, g.only_admins_post), ('Sciences', True))
        with self.assertRaisesMessage(chat.ChatError, 'Only admins'):
            chat.send(g, self.ben, self.school, 'Hi')
        self.post(f'/staff-chat/{g.id}/manage/', {'action': 'admin', 'user': self.ben.id})
        chat.send(g, self.ben, self.school, 'Now I can post')
        self.post(f'/staff-chat/{g.id}/manage/', {'action': 'leave'})
        self.assertFalse(g.members.filter(user=self.ada).exists())
        self.assertIn('Ada Obi left.', list(g.messages.values_list('body', flat=True)))

    def test_nobody_leaves_the_community(self):
        everyone = self.room(ChatRoom.ALL_STAFF)
        with self.assertRaises(chat.ChatError):
            chat.manage(everyone, self.ada, self.school, 'leave', {})


class ComplaintTests(Base):

    def test_complaint_reaches_management_only(self):
        self.login(self.ada)
        self.post('/staff-chat/complaints/new/', {'subject': 'Broken projector', 'category': 'FACILITIES',
                                                  'details': 'The projector in Room 4 has not worked for two weeks.'})
        c = ChatRoom.objects.get(kind=ChatRoom.COMPLAINT)
        self.assertEqual((c.status, c.category), ('OPEN', 'FACILITIES'))
        self.assertEqual(set(c.members.values_list('user_id', flat=True)), {self.ada.id, self.admin.id})
        self.assertTrue(Notification.objects.filter(title__startswith='New staff complaint from Ada Obi').exists())
        self.login(self.ben)
        self.assertEqual(self.get(f'/staff-chat/api/rooms/{c.id}/').status_code, 404)
        # A new administrator joins every complaint thread the next time they open chat
        boss = make_teacher(self.school, username='boss', full_name='Head Teacher')
        Staff.objects.filter(user=boss).update(role='ADMIN')
        chat.sync(boss, self.school)
        self.assertTrue(c.members.filter(user=boss).exists())

    def test_status_and_replies_notify_the_complainant(self):
        c = chat.create_complaint(self.school, self.ada, 'Pay delay', 'PAY', 'My salary for September is late.')
        chat.send(c, self.admin, self.school, 'We are looking into it.')
        with self.assertRaises(chat.ChatError):
            chat.set_status(c, self.ada, self.school, 'RESOLVED')
        chat.set_status(c, self.admin, self.school, 'RESOLVED')
        titles = list(Notification.objects.filter(recipient=self.ada).values_list('title', flat=True))
        self.assertIn('Management replied to your complaint', titles)
        self.assertIn('Your complaint is now resolved', titles)

    def test_anonymous_complaint_hides_the_name(self):
        c = chat.create_complaint(self.school, self.ada, 'Bullying by a colleague', 'CONDUCT',
                                  'I am being shouted at in front of students.', anonymous=True)
        self.assertTrue(Notification.objects.filter(title='New anonymous staff complaint').exists())
        self.login(self.admin)
        r = json.loads(self.get(f'/staff-chat/api/rooms/{c.id}/').content)
        text = json.dumps(r)
        self.assertNotIn('Ada Obi', text)
        self.assertNotIn(self.ada.id, [m['sender'] for m in r['messages']] + [m['id'] for m in r['members']])
        said = [m for m in r['messages'] if m['kind'] == 'TEXT'][0]
        self.assertEqual((said['name'], said['sender']), (chat.ANONYMOUS, 0))
        self.login(self.ada)
        r = json.loads(self.get(f'/staff-chat/api/rooms/{c.id}/').content)
        self.assertEqual([m for m in r['messages'] if m['kind'] == 'TEXT'][0]['name'], 'You')


class PageTests(Base):

    def test_page_and_sidebar(self):
        self.login(self.ada)
        page = self.get('/staff-chat/')
        self.assertContains(page, 'Staff Chat')
        self.assertContains(page, 'Raise a complaint')
        self.assertContains(page, 'Ben Musa')                          # in the new-chat list
        self.assertContains(self.get('/dashboard/teacher/'), 'navChatBadge')
        everyone = ChatRoom.objects.get(community=ChatRoom.ALL_STAFF)
        self.assertEqual(self.get(f'/staff-chat/{everyone.id}/').status_code, 200)
        dm = chat.direct_room(self.ben, self.cal, self.school)
        self.assertRedirects(self.get(f'/staff-chat/{dm.id}/'), '/staff-chat/', fetch_redirect_response=False)
