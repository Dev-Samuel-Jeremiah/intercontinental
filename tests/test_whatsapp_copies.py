"""
Email notifications also go to WhatsApp (when the school has WhatsApp on): to the parent's phone,
a staff phone or the phone on My profile. The same news sent by SMS and by email reaches WhatsApp
once, whichever goes first; different news (two children) both arrive.
"""
from unittest import mock

from django.core import mail
from django.core.cache import cache
from django.test import override_settings

from communications import connect
from communications.engage import settings_for
from communications.services import send_email, send_sms
from tenants.middleware import set_current_tenant

from .factories import TenantTestCase, make_school, make_student, make_teacher


@override_settings(WHATSAPP_TOKEN='token', WHATSAPP_PHONE_NUMBER_ID='123',
                   EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class WhatsAppCopyTests(TenantTestCase):

    def setUp(self):
        super().setUp()
        cache.clear()
        self.school = make_school('alpha')
        set_current_tenant(self.school)
        cfg = settings_for(self.school)
        cfg.whatsapp_enabled, cfg.whatsapp_template = True, 'school_notice'
        cfg.save()
        self.ada = make_student(self.school, 'A1', 'Ada Obi', parent_email='mum@example.com')
        self.ben = make_student(self.school, 'A2', 'Ben Obi', parent_email='mum@example.com')
        for s in (self.ada, self.ben):
            s.parent_phone = '0803 123 4567'
            s.save()
        patcher = mock.patch('communications.connect._wa_post', return_value=(True, 'wamid.1'))
        self.wa = patcher.start()
        self.addCleanup(patcher.stop)

    def sent_to(self):
        return [call.args[0]['to'] for call in self.wa.call_args_list]

    def test_an_email_also_goes_to_the_parents_whatsapp(self):
        ok, _ = send_email('mum@example.com', 'Absence today — Ada Obi', '<p>Ada Obi was absent today.</p>', self.ada)
        self.assertTrue(ok)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(self.sent_to(), ['2348031234567'])
        body = self.wa.call_args.args[0]['template']['components'][0]['parameters'][0]['text']
        self.assertIn('Ada Obi was absent today', body)

    def test_the_school_can_turn_the_copies_off(self):
        cfg = settings_for(self.school)
        cfg.whatsapp_copy_email = False
        cfg.save()
        send_email('mum@example.com', 'News', '<p>Hello</p>')
        self.assertEqual(self.sent_to(), [])

    def test_sms_then_email_of_the_same_news_is_one_whatsapp(self):
        send_sms('0803 123 4567', 'Ada Obi was marked absent today at Alpha Academy.')
        send_email('mum@example.com', 'Absence today — Ada Obi',
                   '<p>Dear parent, Ada Obi was marked absent today at Alpha Academy. Please call the school.</p>', self.ada)
        self.assertEqual(len(self.sent_to()), 1)
        self.assertEqual(len(mail.outbox), 1)                    # the email itself still goes

    def test_email_then_sms_of_the_same_news_is_one_whatsapp(self):
        send_email('mum@example.com', 'Absence today — Ada Obi',
                   '<p>Dear parent, Ada Obi was marked absent today at Alpha Academy. Please call the school.</p>', self.ada)
        ok, ref = send_sms('0803 123 4567', 'Ada Obi was marked absent today at Alpha Academy.')
        self.assertTrue(ok)                                      # counted as delivered, no SMS needed
        self.assertEqual(len(self.sent_to()), 1)

    def test_two_children_two_messages(self):
        send_email('mum@example.com', 'Arrival — Ada Obi', '<p>Ada Obi has just arrived safely at school.</p>', self.ada)
        send_email('mum@example.com', 'Arrival — Ben Obi', '<p>Ben Obi has just arrived safely at school.</p>', self.ben)
        self.assertEqual(len(self.sent_to()), 2)

    def test_staff_phone_from_hr_or_profile(self):
        from hr.models import StaffRecord
        from users.models import AccountProfile, Staff
        teacher = make_teacher(self.school)
        teacher.email = 'grace@example.com'
        teacher.save()
        set_current_tenant(self.school)
        StaffRecord.objects.create(tenant=self.school, staff=Staff.objects.get(user=teacher), phone='08051112222')
        send_email('grace@example.com', 'Leave approved', '<p>Your leave is approved.</p>')
        AccountProfile.objects.create(user=teacher, phone='08099998888')       # their own number wins
        send_email('grace@example.com', 'Payslip ready', '<p>Your October payslip is ready.</p>')
        self.assertEqual(self.sent_to(), ['2348051112222', '2348099998888'])

    def test_no_phone_no_whatsapp_but_the_email_goes(self):
        ok, _ = send_email('stranger@example.com', 'Hello', '<p>Hi</p>')
        self.assertTrue(ok)
        self.assertEqual(self.sent_to(), [])
