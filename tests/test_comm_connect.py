"""
Communications, part 2: family preferences (F255), templates in several
languages (F251), parent–teacher chat (F242), announcement read receipts
(F259), SMS units (F246), WhatsApp (F241) and the replies inbox (F258).
"""
import hashlib
import hmac
import json
from datetime import datetime
from unittest import mock

from django.core import mail
from django.test import override_settings
from django.utils import timezone

from announcement.models import Announcement
from communications import connect as cn
from communications import engage as eg
from communications.models import (AnnouncementReminder, Campaign, ChatMessage, FamilyPreference, InboundMessage,
                                   MessageLog, MessageTemplate, MessageTemplateTranslation, OptOut, SmsTopUp,
                                   SmsUsageDay, SmsWallet)
from communications.services import send_campaign, send_sms
from tenants.middleware import clear_current_tenant, set_current_tenant
from users.models import Staff, StaffSubjectClass, Subject, User

from .factories import TenantTestCase, make_admin, make_class, make_school, make_student, make_teacher


def fake_response(data, status=200):
    r = mock.Mock()
    r.status_code = status
    r.json.return_value = data
    return r


@override_settings(RUN_TASKS_EAGERLY=True)
class Base(TenantTestCase):

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha')
        self.admin = make_admin(self.school)
        self.admin.email = 'head@example.com'
        self.admin.save()
        self.t_user = make_teacher(self.school, username='alphat', full_name='Mrs Ade')
        self.t_user.email = 'ade@example.com'
        self.t_user.save()
        self.jss1 = make_class(self.school, 'JSS 1A')
        self.jss2 = make_class(self.school, 'JSS 2A')
        self.ada = make_student(self.school, 'A/001', full_name='Ada Obi', school_class=self.jss1,
                                parent_email='mum@example.com')
        self.ben = make_student(self.school, 'A/002', full_name='Ben Obi', school_class=self.jss2,
                                parent_email='mum@example.com')
        self.chi = make_student(self.school, 'A/003', full_name='Chi Eze', school_class=self.jss1,
                                parent_email='eze@example.com')
        set_current_tenant(self.school)
        self.teacher = Staff.objects.get(user=self.t_user)
        maths = Subject.objects.create(tenant=self.school, name='Mathematics')
        StaffSubjectClass.objects.create(tenant=self.school, staff=self.teacher, subject=maths, school_class=self.jss1)
        self.parent = User.objects.create_user(username=f'parent_{self.ada.id}', password='x', tenant=self.school,
                                               is_parent=True, email='mum@example.com', first_name='Ngozi')
        self.parent2 = User.objects.create_user(username=f'parent_{self.chi.id}', password='x', tenant=self.school,
                                                is_parent=True, email='eze@example.com')

    def _get(self, path, **kw):
        return self.client.get(path, HTTP_HOST=self.host(self.school), **kw)

    def _post(self, path, data=None, **kw):
        return self.client.post(path, data or {}, HTTP_HOST=self.host(self.school), **kw)

    def again(self):
        set_current_tenant(self.school)


class PreferenceTests(Base):

    def test_family_choices_shape_what_they_get(self):
        self.client.force_login(self.parent)
        self.assertContains(self._get('/communications/preferences/'), 'Ben Obi')
        self._post('/communications/preferences/', {'language': 'ha', 'channel': 'EMAIL', 'topics': ['SURVEYS']})
        self.again()
        pref = FamilyPreference.objects.get(key='mum@example.com')
        self.assertEqual((pref.language, sorted(pref.topics_off)), ('ha', ['BIRTHDAY', 'DIGEST', 'GENERAL']))
        self.assertEqual(set(OptOut.objects.values_list('student__full_name', 'channel')),
                         {('Ada Obi', 'SMS'), ('Ben Obi', 'SMS')})
        self.assertEqual(cn.channel_for(self.ada), 'EMAIL')
        news = Campaign.objects.create(tenant=self.school, title='News', subject='Newsletter', body_email='<p>Hi</p>',
                                       channel='EMAIL', topic='GENERAL')
        mail.outbox.clear()
        send_campaign(news.pk)
        self.assertEqual([m.to[0] for m in mail.outbox], ['eze@example.com'])
        fees = Campaign.objects.create(tenant=self.school, title='Fees', subject='Fees due', body_email='<p>Pay</p>',
                                       channel='EMAIL', topic='FEES')
        mail.outbox.clear()
        send_campaign(fees.pk)
        self.assertIn('mum@example.com', [m.to[0] for m in mail.outbox])

    def test_bad_choice_refused(self):
        with self.assertRaises(cn.ConnectError):
            cn.save_preferences([self.ada], self.parent, 'xx', 'BOTH', [])


class TranslationTests(Base):

    def setUp(self):
        super().setUp()
        self.tpl = MessageTemplate.objects.create(tenant=self.school, name='Resumption', subject='School resumes',
                                                  body_email='<p>School resumes on Monday.</p>',
                                                  body_sms='School resumes Monday.')
        FamilyPreference.objects.create(tenant=self.school, key='mum@example.com', language='ha')

    def test_family_gets_their_language_when_template_is_unchanged(self):
        cn.save_translation(self.tpl, 'ha', {'subject': 'Makaranta za ta koma', 'body_email': '<p>Litinin</p>',
                                             'body_sms': 'Litinin'}, self.admin)
        c = Campaign.objects.create(tenant=self.school, title='Back', template=self.tpl, subject=self.tpl.subject,
                                    body_email=self.tpl.body_email, body_sms=self.tpl.body_sms, channel='EMAIL')
        self.assertEqual(cn.texts_for(c, self.ada)[0], 'Makaranta za ta koma')
        self.assertEqual(cn.texts_for(c, self.chi)[0], 'School resumes')
        mail.outbox.clear()
        send_campaign(c.pk)
        subjects = {m.to[0]: m.subject for m in mail.outbox}
        self.assertEqual(subjects['mum@example.com'], 'Makaranta za ta koma')
        self.assertEqual(subjects['eze@example.com'], 'School resumes')
        c.subject = 'School resumes on TUESDAY'
        self.assertEqual(cn.texts_for(c, self.ada)[0], 'School resumes on TUESDAY')

    @mock.patch('assistant.writers.translate', side_effect=[('Taken', 'Litinin SMS'), ('Taken', '<p>Litinin</p>')])
    def test_ai_draft_is_saved_and_marked(self, _tr):
        tr = cn.draft_translation(self.tpl, 'ha', self.school, self.admin)
        self.assertTrue(tr.ai_drafted)
        self.assertEqual((tr.body_sms, tr.body_email), ('Litinin SMS', '<p>Litinin</p>'))
        with self.assertRaises(cn.ConnectError):
            cn.save_translation(self.tpl, 'en', {}, self.admin)
        self.client.force_login(self.t_user)
        self.assertContains(self._get(f'/communications/templates/{self.tpl.pk}/translations/'), 'AI draft')
        self.assertEqual(MessageTemplateTranslation.objects.count(), 1)


class ChatTests(Base):

    def test_parent_and_teacher_talk(self):
        self.assertEqual(cn.chat_teachers(self.ada), [self.teacher])
        self.assertEqual(cn.chat_teachers(self.ben), [])
        mail.outbox.clear()
        with mock.patch('communications.connect.in_office_hours', return_value=True):
            self.client.force_login(self.parent)
            self._post('/communications/chat/', {'student': self.ada.pk, 'teacher': self.teacher.pk,
                                                 'subject': 'Homework', 'body': 'Ada missed the homework.'})
        self.again()
        conv = self.teacher.conversations.get()
        self.assertEqual(mail.outbox[0].to, ['ade@example.com'])
        with self.assertRaises(cn.ConnectError):
            cn.start_conversation(self.parent, self.ben, self.teacher, 'Hi', 'Hello')
        mail.outbox.clear()
        self.client.force_login(self.t_user)
        page = self._get(f'/communications/chat/{conv.pk}/')
        self.assertContains(page, 'Ada missed the homework.')
        self.again()
        self.assertIsNotNone(conv.messages.get().read_at)
        self._post(f'/communications/chat/{conv.pk}/', {'action': 'send', 'body': 'Thanks, I will check.'})
        self.again()
        self.assertEqual(mail.outbox[0].to, ['mum@example.com'])
        self.client.force_login(self.parent2)
        self.assertEqual(self._get(f'/communications/chat/{conv.pk}/').status_code, 404)

    def test_no_email_to_teacher_outside_office_hours(self):
        with mock.patch('communications.connect.in_office_hours', return_value=False):
            mail.outbox.clear()
            cn.start_conversation(self.parent, self.ada, self.teacher, 'Late', 'Running late')
        self.assertEqual(len(mail.outbox), 0)

    def test_report_hide_and_close(self):
        conv = cn.start_conversation(self.parent, self.ada, self.teacher, 'Fees', 'Rude message')
        msg = conv.messages.get()
        mail.outbox.clear()
        cn.flag(msg, self.t_user, 'Rude')
        self.assertEqual(mail.outbox[0].to, ['head@example.com'])
        self.client.force_login(self.admin)
        self._post(f'/communications/chat/{conv.pk}/', {'action': 'hide', 'id': msg.pk})
        self.again()
        self.assertTrue(ChatMessage.objects.get(pk=msg.pk).hidden)
        self.client.force_login(self.parent)
        self.assertContains(self._get(f'/communications/chat/{conv.pk}/'), 'removed by the school')
        self.again()
        conv.closed = True
        conv.save()
        with self.assertRaises(cn.ConnectError):
            cn.post(conv, self.parent, 'Hello?')

    def test_office_hours(self):
        cfg = eg.settings_for(self.school)
        monday_10 = timezone.make_aware(datetime(2026, 10, 5, 10, 0))
        saturday_10 = timezone.make_aware(datetime(2026, 10, 10, 10, 0))
        monday_18 = timezone.make_aware(datetime(2026, 10, 5, 18, 0))
        self.assertTrue(cn.in_office_hours(self.school, monday_10))
        self.assertFalse(cn.in_office_hours(self.school, saturday_10))
        self.assertFalse(cn.in_office_hours(self.school, monday_18))
        self.assertEqual(cfg.office_days, 'MON,TUE,WED,THU,FRI')


class ReadReceiptTests(Base):

    def test_who_read_and_reminders(self):
        ann = Announcement.objects.create(tenant=self.school, title='PTA meeting', message='Saturday 10am',
                                          school_class=self.jss1, audience='parents', published=True,
                                          published_at=timezone.now())
        self.client.force_login(self.parent)
        self._get(f'/announcement/{ann.pk}/')
        self.again()
        rep = cn.read_report(ann)
        self.assertEqual((rep['read'], rep['total']), (1, 2))
        mail.outbox.clear()
        n = cn.remind_unread(ann.pk, self.admin, channel='EMAIL')
        self.assertEqual(n, 1)
        self.assertEqual([m.to[0] for m in mail.outbox], ['eze@example.com'])
        self.assertEqual(AnnouncementReminder.objects.get().families, 1)
        self.client.force_login(self.t_user)
        self.assertContains(self._get(f'/communications/announcements/{ann.pk}/reads/'), '1 of 2 families')


class SmsUnitTests(Base):

    def test_pages(self):
        self.assertEqual(cn.sms_pages('a' * 160), 1)
        self.assertEqual(cn.sms_pages('a' * 161), 2)
        self.assertEqual(cn.sms_pages('Hello 😀'), 1)
        self.assertEqual(cn.sms_pages('😀' * 40), 2)

    @override_settings(SMS_METERING=True, SMS_ENABLED=True)
    def test_metered_school_needs_units(self):
        with mock.patch('communications.services.send_sms_termii', return_value=(True, 't1')) as termii:
            ok, why = send_sms('08030000001', 'Hello')
            self.assertFalse(ok)
            self.assertIn('Not enough SMS units', why)
            self.assertEqual(termii.call_count, 0)
            cn.grant_units(self.school, 10)
            self.assertTrue(send_sms('08030000001', 'Hello')[0])
            self.assertTrue(send_sms('08030000001', 'x' * 200)[0])
        w = SmsWallet.objects.get(tenant=self.school)
        self.assertEqual((w.balance, w.used_total), (7, 3))
        self.assertEqual(SmsUsageDay.objects.get().messages, 2)
        with mock.patch('communications.services.send_sms_termii', return_value=(False, 'down')), \
                mock.patch('communications.services.send_sms_africastalking', return_value=(False, 'down')):
            self.assertFalse(send_sms('08030000001', 'Hello')[0])
        self.assertEqual(SmsWallet.objects.get(tenant=self.school).balance, 7)   # refunded

    @override_settings(PAYSTACK_SECRET_KEY='sk_test_x')
    def test_paystack_top_up_adds_units_once(self):
        init = fake_response({'status': True, 'data': {'authorization_url': 'https://checkout.paystack.com/abc'}})
        with mock.patch('requests.post', return_value=init) as post:
            url = cn.start_topup(self.school, self.admin, 1000, 'http://x/cb')
        self.assertEqual(url, 'https://checkout.paystack.com/abc')
        self.assertEqual(post.call_args.kwargs['json']['amount'], 400000)   # ₦4,000 in kobo
        top = SmsTopUp.objects.get()
        ok = fake_response({'status': True, 'data': {'status': 'success', 'amount': 400000}})
        self.client.force_login(self.admin)
        with mock.patch('requests.get', return_value=ok):
            self._post('/communications/sms-units/')  # no units chosen: error, nothing created
            self._get(f'/communications/sms-units/callback/?reference={top.reference}')
            self.again()
            cn.verify_topup(top.reference)
        self.assertEqual(SmsWallet.objects.get(tenant=self.school).balance, 1000)
        self.assertEqual(SmsTopUp.objects.get().status, 'PAID')
        with self.assertRaises(cn.ConnectError):
            cn.start_topup(self.school, self.admin, 123, 'http://x/cb')


@override_settings(WHATSAPP_TOKEN='tok', WHATSAPP_PHONE_NUMBER_ID='555', WHATSAPP_VERIFY_TOKEN='verify-me',
                   WHATSAPP_APP_SECRET='app-secret')
class WhatsAppTests(Base):

    def test_whatsapp_first_then_sms(self):
        cfg = eg.settings_for(self.school)
        cfg.whatsapp_enabled, cfg.whatsapp_template = True, 'school_notice'
        cfg.save()
        self.assertEqual(cn.wa_number('0803 123 4567'), '2348031234567')
        sent = fake_response({'messages': [{'id': 'wamid.1'}]})
        with mock.patch('requests.post', return_value=sent) as post:
            ok, ref = send_sms('08031234567', 'Line one\nLine two')
        self.assertEqual((ok, ref), (True, 'wa:wamid.1'))
        body = post.call_args.kwargs['json']
        self.assertEqual(body['template']['name'], 'school_notice')
        self.assertEqual(body['template']['components'][0]['parameters'][0]['text'], 'Line one Line two')
        failed = fake_response({'error': {'message': 'not on WhatsApp'}}, status=400)
        with mock.patch('requests.post', return_value=failed):
            ok, ref = send_sms('08031234567', 'Hello')
        self.assertEqual(ref, 'sms-disabled')    # fell back to SMS (switched off in tests)

    def _signed(self, payload):
        body = json.dumps(payload).encode()
        sig = 'sha256=' + hmac.new(b'app-secret', body, hashlib.sha256).hexdigest()
        return self.client.post('/hooks/whatsapp/', body, content_type='application/json',
                                HTTP_X_HUB_SIGNATURE_256=sig, HTTP_HOST=self.host(self.school))

    def test_webhook_verification_messages_and_statuses(self):
        r = self._get('/hooks/whatsapp/?hub.mode=subscribe&hub.verify_token=verify-me&hub.challenge=42')
        self.assertEqual(r.content, b'42')
        self.assertEqual(self._get('/hooks/whatsapp/?hub.mode=subscribe&hub.verify_token=no').status_code, 403)
        self.ada.parent_phone = '08031234567'
        self.ada.save()
        c = Campaign.objects.create(tenant=self.school, title='T', subject='S', status='SENT')
        log = MessageLog.objects.create(tenant=self.school, campaign=c, student=self.ada, channel='SMS',
                                        recipient='08031234567', body='Hi', status='SENT', provider_ref='wa:wamid.9')
        payload = {'entry': [{'changes': [{'value': {
            'messages': [{'from': '2348031234567', 'id': 'wamid.in1', 'type': 'text', 'text': {'body': 'Noted, thanks'}}],
            'statuses': [{'id': 'wamid.9', 'status': 'read'}]}}]}]}
        self.assertEqual(self._signed(payload).json(), {'stored': 1})
        self.again()
        msg = InboundMessage.objects.get()
        self.assertEqual((msg.channel, msg.student, msg.body), ('WA', self.ada, 'Noted, thanks'))
        log.refresh_from_db()
        self.assertEqual(log.status, 'DELIVERED')
        self.assertEqual(self._signed(payload).json(), {'stored': 0})      # same message id: not stored twice
        bad = self.client.post('/hooks/whatsapp/', json.dumps(payload), content_type='application/json',
                               HTTP_X_HUB_SIGNATURE_256='sha256=nope', HTTP_HOST=self.host(self.school))
        self.assertEqual(bad.status_code, 403)


class InboxTests(Base):

    @override_settings(SMS_INBOUND_TOKEN='in-tok')
    def test_inbound_sms_and_reply(self):
        self.chi.parent_phone = '08035550000'
        self.chi.save()
        self.assertEqual(self._post('/hooks/sms-inbound/wrong/', {}).status_code, 404)
        self._post('/hooks/sms-inbound/in-tok/', {'from': '+2348035550000', 'text': 'Who is picking Chi?', 'id': 'at1'})
        self.again()
        msg = InboundMessage.objects.get()
        self.assertEqual((msg.channel, msg.student), ('SMS', self.chi))
        self.client.force_login(self.t_user)
        self.assertContains(self._get('/communications/inbox/'), 'Who is picking Chi?')
        with mock.patch('communications.services.send_sms', return_value=(True, 'r1')) as sms:
            self._post('/communications/inbox/', {'id': msg.pk, 'action': 'reply', 'body': 'Her aunt, at 2pm.'})
        self.again()
        sms.assert_called_once_with('+2348035550000', 'Her aunt, at 2pm.')
        msg.refresh_from_db()
        self.assertTrue(msg.handled)
        self.assertEqual(msg.replies.get().ok, True)

    def test_shared_number_routes_to_the_school_that_messaged_last(self):
        c = Campaign.objects.create(tenant=self.school, title='T', subject='S', status='SENT')
        MessageLog.objects.create(tenant=self.school, campaign=c, student=self.chi, channel='SMS',
                                  recipient='08035550000', body='Hi', status='SENT')
        clear_current_tenant()
        self.assertEqual(cn.route_tenant('+2348035550000'), self.school)
        self.assertIsNone(cn.route_tenant('+2348099999999'))
