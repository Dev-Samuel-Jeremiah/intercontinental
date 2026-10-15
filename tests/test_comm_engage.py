"""
Communications, part 1: message approval (F250), surveys (F252), consent
forms (F253), birthday greetings (F254), emergency broadcast (F249),
delivery reports (F247), the messages calendar (F260) and the reception
notice board (F256).
"""
import json
from datetime import date, timedelta
from unittest import mock

from django.core import mail
from django.test import override_settings
from django.utils import timezone

from announcement.models import Announcement
from communications import engage as eg
from communications.models import (BirthdayGreeting, Campaign, ConsentResponse, EmergencyBroadcast, MessageLog,
                                   Survey, SurveyResponse)
from hr.models import StaffRecord
from tenants.middleware import set_current_tenant
from users.models import Staff, User

from .factories import TenantTestCase, make_admin, make_class, make_school, make_student, make_teacher

SMS_OK = mock.patch('communications.services.send_sms', return_value=(True, 'sms-ref'))


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
        self.parent = User.objects.create_user(username=f'parent_{self.ada.id}', password='x', tenant=self.school,
                                               is_parent=True, email='mum@example.com')

    def _get(self, path, **kw):
        return self.client.get(path, HTTP_HOST=self.host(self.school), **kw)

    def _post(self, path, data=None, **kw):
        return self.client.post(path, data or {}, HTTP_HOST=self.host(self.school), **kw)

    def again(self):
        set_current_tenant(self.school)


class ApprovalTests(Base):

    def _create(self, user):
        self.client.force_login(user)
        self._post('/communications/campaigns/new/', {
            'title': 'Sports day', 'channel': 'EMAIL', 'audience': 'CLASS', 'school_class': self.jss1.pk,
            'subject': 'Sports day on Friday', 'body_email': '<p>Dear {{parent_name}}, sports day is Friday.</p>'})
        self.again()
        return Campaign.objects.latest('id')

    def test_teacher_campaign_waits_and_admin_approves(self):
        mail.outbox.clear()
        c = self._create(self.t_user)
        self.assertEqual(c.status, 'PENDING')
        self.assertIn('head@example.com', [m.to[0] for m in mail.outbox])
        self._post(f'/communications/campaigns/{c.pk}/send/')
        self.again()
        c.refresh_from_db()
        self.assertEqual(c.status, 'PENDING')
        mail.outbox.clear()
        self.client.force_login(self.admin)
        self._post('/communications/approvals/', {'id': c.pk, 'action': 'approve'})
        self.again()
        c.refresh_from_db()
        self.assertEqual(c.status, 'SENT')
        self.assertEqual(c.approved_by, self.admin)
        to = sorted(m.to[0] for m in mail.outbox)
        self.assertIn('ade@example.com', to)            # the author is told
        self.assertIn('mum@example.com', to)            # Ada's family (JSS 1A)
        self.assertIn('eze@example.com', to)

    def test_send_back_needs_a_reason_and_admins_skip_approval(self):
        c = self._create(self.t_user)
        with self.assertRaises(eg.CommError):
            eg.reject(c, self.admin, '')
        eg.reject(c, self.admin, 'Add the time')
        c.refresh_from_db()
        self.assertEqual((c.status, c.review_note), ('REJECTED', 'Add the time'))
        self.assertEqual(self._create(self.admin).status, 'DRAFT')
        cfg = eg.settings_for(self.school)
        cfg.require_approval = False
        cfg.save()
        self.assertEqual(self._create(self.t_user).status, 'DRAFT')


class SurveyTests(Base):

    def _survey(self, **kw):
        return eg.create_survey(self.school, self.admin, 'Term feedback', '', kw.get('audience', 'PARENTS'),
                                kw.get('anonymous', True), timezone.localdate() + timedelta(days=7),
                                'How was the term? | Good | Fair | Poor\n[multi] Clubs? | Chess | Drama | Coding\n'
                                '[rating] Rate the concert\n[text] Anything else?')

    def test_questions_are_parsed(self):
        qs = eg.parse_questions('Pick one | A | B\n[rating] Stars\nWhy?')
        self.assertEqual([q['kind'] for q in qs], ['CHOICE', 'RATING', 'TEXT'])
        with self.assertRaises(eg.CommError):
            eg.parse_questions('Only one option | A')
        with self.assertRaises(eg.CommError):
            eg.parse_questions('[poll] Hmm | A | B')

    def test_parents_answer_once_and_results_add_up(self):
        s = self._survey()
        self.client.force_login(self.parent)
        self.assertContains(self._get('/communications/surveys/'), 'Term feedback')
        self._post(f'/communications/surveys/{s.pk}/', {'q0': 'Good', 'q1': ['Chess', 'Coding'], 'q2': '4',
                                                        'q3': 'More sport please'})
        self.again()
        self.assertEqual(SurveyResponse.objects.count(), 1)
        with self.assertRaises(eg.CommError):
            eg.answer(s, self.parent, {'q0': 'Fair'})
        other = User.objects.create_user(username=f'parent_{self.chi.id}', password='x', tenant=self.school,
                                         is_parent=True, email='eze@example.com')
        eg.answer(s, other, {'q0': 'Poor', 'q2': '2'})
        r = eg.results(s)
        self.assertEqual(r['count'], 2)
        bars = {b['label']: b['pct'] for b in r['questions'][0]['bars']}
        self.assertEqual((bars['Good'], bars['Poor'], bars['Fair']), (50, 50, 0))
        self.assertEqual(r['questions'][2]['average'], 3.0)
        self.assertEqual(r['questions'][3]['texts'], [(None, 'More sport please')])
        header, rows = eg.export_rows(s)
        self.assertNotIn('Name', header)
        self.assertEqual(rows[0][2], 'Chess, Coding')

    def test_staff_cannot_answer_a_parent_survey(self):
        s = self._survey()
        self.client.force_login(self.t_user)
        self.assertEqual(self._get(f'/communications/surveys/{s.pk}/').status_code, 404)
        staff_s = self._survey(audience='STAFF')
        self.assertTrue(eg.may_answer(staff_s, self.t_user))
        self.assertFalse(eg.may_answer(staff_s, self.parent))
        Survey.objects.filter(pk=staff_s.pk).update(closes=timezone.localdate() - timedelta(days=1))
        staff_s.refresh_from_db()
        with self.assertRaises(eg.CommError):
            eg.answer(staff_s, self.t_user, {'q0': 'Good'})


class ConsentTests(Base):

    def _form(self):
        return eg.create_consent(self.school, self.t_user, 'Museum trip', 'Friday, by bus.',
                                 timezone.localdate() + timedelta(days=5), '2,500', [self.jss1.pk])

    def test_parent_signs_for_child_in_the_class(self):
        f = self._form()
        self.assertEqual(f.cost, 2500)
        self.client.force_login(self.parent)
        page = self._get('/communications/consent/')
        self.assertContains(page, 'Museum trip')
        self.assertContains(page, 'Ada Obi')
        self.assertNotContains(page, 'Ben Obi')        # JSS 2A — not on this trip
        self._post('/communications/consent/', {'form': f.pk, 'student': self.ada.pk, 'answer': 'YES',
                                                'signed_name': 'Ngozi Obi', 'note': 'Asthma inhaler'})
        self.again()
        r = ConsentResponse.objects.get(form=f, student=self.ada)
        self.assertEqual((r.answer, r.signed_name, r.signed_by), ('YES', 'Ngozi Obi', self.parent))
        with self.assertRaises(eg.CommError):
            eg.give_consent(f, self.ben, self.parent, 'YES', 'Ngozi Obi')
        with self.assertRaises(eg.CommError):
            eg.give_consent(f, self.ada, self.parent, 'NO', 'N')
        eg.give_consent(f, self.ada, self.parent, 'NO', 'Ngozi Obi')
        sm = eg.consent_summary(f)
        self.assertEqual((sm['yes'], sm['no'], sm['waiting']), (0, 1, 1))

    def test_reminders_go_only_to_families_who_have_not_replied(self):
        f = self._form()
        eg.give_consent(f, self.ada, self.parent, 'YES', 'Ngozi Obi')
        mail.outbox.clear()
        sent = eg.notify_consent(f.pk, reminder=True, channel='EMAIL')
        self.assertEqual(sent['emails'], 1)
        self.assertEqual(mail.outbox[0].to, ['eze@example.com'])
        self.assertIn('Reminder', mail.outbox[0].subject)

    def test_closed_after_deadline(self):
        f = self._form()
        f.deadline = timezone.localdate() - timedelta(days=1)
        f.save()
        with self.assertRaises(eg.CommError):
            eg.give_consent(f, self.ada, self.parent, 'YES', 'Ngozi Obi')


class BirthdayTests(Base):

    def test_greeted_once_and_only_when_switched_on(self):
        today = timezone.localdate()
        self.ada.date_of_birth = today.replace(year=today.year - 12) if not (today.month == 2 and today.day == 29) \
            else date(2012, 2, 29)
        self.ada.save()
        StaffRecord.objects.create(tenant=self.school, staff=self.teacher, date_of_birth=date(1985, today.month, 1)
                                   if today.day != 1 else date(1985, today.month, 2))
        mail.outbox.clear()
        self.assertEqual(eg.send_birthdays(self.school), {'students': 0, 'staff': 0})
        cfg = eg.settings_for(self.school)
        cfg.birthday_students = cfg.birthday_staff = True
        cfg.save()
        self.assertEqual(eg.send_birthdays(self.school), {'students': 1, 'staff': 0})
        self.assertEqual(mail.outbox[0].to, ['mum@example.com'])
        self.assertIn('Happy birthday, Ada Obi', mail.outbox[0].body)
        self.assertEqual(eg.send_birthdays(self.school), {'students': 0, 'staff': 0})
        self.assertEqual(BirthdayGreeting.objects.count(), 1)

    def test_staff_birthday_and_leap_day(self):
        today = timezone.localdate()
        StaffRecord.objects.create(tenant=self.school, staff=self.teacher, date_of_birth=today.replace(year=1985)
                                   if not (today.month == 2 and today.day == 29) else date(1984, 2, 29))
        cfg = eg.settings_for(self.school)
        cfg.birthday_staff = True
        cfg.save()
        mail.outbox.clear()
        self.assertEqual(eg.send_birthdays(self.school)['staff'], 1)
        self.assertEqual(mail.outbox[0].to, ['ade@example.com'])
        self.assertTrue(eg._is_birthday(date(2008, 2, 29), date(2027, 2, 28)))
        self.assertFalse(eg._is_birthday(date(2008, 2, 29), date(2028, 2, 28)))


class EmergencyTests(Base):

    def test_everyone_once_and_confirmation_needed(self):
        StaffRecord.objects.create(tenant=self.school, staff=self.teacher, phone='08031112222')
        self.ada.parent_phone = '08030000001'
        self.ada.save()
        self.ben.parent_phone = '+2348030000001'       # the same phone, written differently
        self.ben.save()
        self.client.force_login(self.admin)
        self._post('/communications/emergency/', {'subject': 'Closed', 'message': 'School closed today.',
                                                  'to_parents': '1', 'to_staff': '1', 'confirm': 'no'})
        self.again()
        self.assertFalse(EmergencyBroadcast.objects.exists())
        mail.outbox.clear()
        with SMS_OK as sms:
            self._post('/communications/emergency/', {'subject': 'Closed', 'message': 'School closed today.',
                                                      'to_parents': '1', 'to_staff': '1', 'confirm': 'send'})
        self.again()
        b = EmergencyBroadcast.objects.get()
        self.assertEqual(b.sms_sent, 2)                 # one parent phone + one staff phone
        self.assertEqual(sms.call_count, 2)
        # Ada and Ben share a parent email; the head has no staff profile, so is not on the staff list.
        self.assertEqual(sorted(m.to[0] for m in mail.outbox), ['ade@example.com', 'eze@example.com', 'mum@example.com'])
        self.assertIsNotNone(b.finished_at)

    def test_only_admins_and_sms_length(self):
        with self.assertRaises(eg.CommError):
            eg.start_broadcast(self.school, self.t_user, 'X', 'Y')
        with self.assertRaises(eg.CommError):
            eg.start_broadcast(self.school, self.admin, 'X', 'a' * 500)


class DeliveryTests(Base):

    def _log(self, status='SENT', ref='abc123', channel='SMS'):
        c = Campaign.objects.create(tenant=self.school, title='T', subject='S', status='SENT')
        return MessageLog.objects.create(tenant=self.school, campaign=c, student=self.ada, channel=channel,
                                         recipient='08030000001', body='Hello', status=status, provider_ref=ref)

    @override_settings(SMS_DLR_TOKEN='s3cret')
    def test_webhook_marks_delivered(self):
        log = self._log()
        self.assertEqual(self._post('/communications/dlr/wrong/', {}).status_code, 404)
        r = self.client.post('/communications/dlr/s3cret/', json.dumps({'message_id': 'abc123', 'status': 'Delivered'}),
                             content_type='application/json', HTTP_HOST=self.host(self.school))
        self.assertEqual(r.json(), {'updated': 1})
        log.refresh_from_db()
        self.assertEqual(log.status, 'DELIVERED')
        self.assertEqual(eg.apply_dlr({'id': 'abc123', 'status': 'DND Active'}), 1)
        log.refresh_from_db()
        self.assertEqual(log.status, 'FAILED')

    def test_webhook_off_without_token(self):
        self.assertEqual(self._post('/communications/dlr/anything/', {}).status_code, 404)

    def test_retry_failed(self):
        log = self._log(status='FAILED', ref='')
        with SMS_OK:
            ok, bad = eg.retry_failed(log.campaign)
        log.refresh_from_db()
        self.assertEqual((ok, bad, log.status, log.provider_ref), (1, 0, 'SENT', 'sms-ref'))
        log.campaign.refresh_from_db()
        self.assertEqual(log.campaign.sms_sent, 1)
        self.client.force_login(self.t_user)
        self.assertContains(self._get(f'/communications/campaigns/{log.campaign.pk}/report/'), '08030000001')


class CalendarAndSignageTests(Base):

    def test_calendar_shows_scheduled_messages(self):
        when = timezone.now() + timedelta(days=3)
        Campaign.objects.create(tenant=self.school, title='Fees reminder', subject='S', status='SCHEDULED',
                                scheduled_at=when)
        local = timezone.localtime(when).date()
        items = eg.month_items(self.school, local.year, local.month)
        self.assertEqual(items[local][0]['title'], 'Fees reminder')
        self.client.force_login(self.t_user)
        self.assertContains(self._get(f'/communications/calendar/?y={local.year}&m={local.month}'), 'Fees reminder')

    def test_notice_board_needs_the_secret_link(self):
        Announcement.objects.create(tenant=self.school, title='Inter-house sports', message='Saturday 9am',
                                    published=True, published_at=timezone.now())
        token = eg.signage_token(self.school)
        self.assertEqual(self._get('/communications/signage/nope/').status_code, 404)
        page = self._get(f'/communications/signage/{token}/')
        self.assertContains(page, 'Inter-house sports')
        eg.signage_token(self.school, renew=True)
        self.assertEqual(self._get(f'/communications/signage/{token}/').status_code, 404)
        self.assertEqual(eg._short('Ada Chioma Obi'), 'Ada O.')
