"""
A student's wider record: guardians and who gets messages, the private
document vault with expiry tracking, the school's own fields, and NIN.
"""
import shutil
import tempfile
from datetime import timedelta
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.utils import timezone

from tenants.middleware import set_current_tenant
from users import records
from users.models import (Guardian, StudentChange, StudentDocument, StudentField,
                          StudentFieldValue, User)

from .factories import (TenantTestCase, make_admin, make_class, make_school, make_student,
                        make_teacher)

PDF = b'%PDF-1.4\n%test document\n'
PNG = b'\x89PNG\r\n\x1a\n' + b'\x00' * 32


class DetailsBase(TenantTestCase):

    def setUp(self):
        super().setUp()
        self.private = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.private, ignore_errors=True)
        override = override_settings(PRIVATE_MEDIA_ROOT=self.private)
        override.enable()
        self.addCleanup(override.disable)
        self.school = make_school('alpha')
        self.admin = make_admin(self.school)
        self.jss1 = make_class(self.school, 'JSS 1')
        set_current_tenant(self.school)
        self.ada = make_student(self.school, 'STU001', full_name='Ada Bello',
                                school_class=self.jss1, parent_email='mum@example.com')
        self.ada.parent_phone = '08031234567'
        self.ada.save()

    def _guardian(self, **kwargs):
        defaults = dict(tenant=self.school, student=self.ada, full_name='Musa Bello',
                        relationship='FATHER', phone='0805 555 1234', email='dad@example.com')
        defaults.update(kwargs)
        return Guardian.objects.create(**defaults)


class ContactsTests(DetailsBase):

    def test_the_main_contact_always_comes_first(self):
        self._guardian()
        self.assertEqual(records.contacts(self.ada, 'EMAIL')[0][1], 'mum@example.com')

    def test_a_guardian_gets_messages_on_the_channels_they_chose(self):
        self._guardian(contact_by='SMS')
        self.assertEqual([a for _, a in records.contacts(self.ada, 'SMS')],
                         ['08031234567', '0805 555 1234'])
        self.assertEqual([a for _, a in records.contacts(self.ada, 'EMAIL')], ['mum@example.com'])

    def test_do_not_message_is_respected(self):
        self._guardian(contact_by='NONE')
        self.assertEqual(len(records.contacts(self.ada, 'SMS')), 1)

    def test_fee_messages_only_go_to_guardians_who_pay(self):
        self._guardian(gets_fee_messages=False)
        self._guardian(full_name='Aunty Ngozi', phone='0812 000 0000', email='',
                       gets_fee_messages=True)
        phones = [a for _, a in records.contacts(self.ada, 'SMS', fees=True)]
        self.assertEqual(phones, ['08031234567', '0812 000 0000'])

    def test_nobody_is_messaged_twice(self):
        self._guardian(phone='+234 803 123 4567', email='MUM@example.com')
        self.assertEqual(len(records.contacts(self.ada, 'SMS')), 1)
        self.assertEqual(len(records.contacts(self.ada, 'EMAIL')), 1)

    @override_settings(SMS_ENABLED=True)
    def test_a_quick_message_reaches_every_guardian_who_agreed(self):
        from communications import services
        self._guardian(contact_by='SMS')
        with mock.patch.object(services, 'send_sms', return_value=(True, 'ok')) as sms:
            services.send_quick_message(self.ada, 'SMS', '', 'Hello')
        self.assertEqual(sorted(c.args[0] for c in sms.call_args_list),
                         ['08031234567', '0805 555 1234'])

    def test_fee_reminders_skip_guardians_not_marked_for_fees(self):
        from communications import services
        self._guardian(contact_by='BOTH', gets_fee_messages=False)
        with mock.patch.object(services, '_build_context',
                               return_value={'outstanding_fee': '₦5,000.00'}), \
                mock.patch.object(services, 'send_sms', return_value=(True, 'ok')) as sms, \
                mock.patch.object(services, 'send_email', return_value=(True, 'ok')):
            services.send_fee_reminders(channel='SMS')
        self.assertEqual([c.args[0] for c in sms.call_args_list], ['08031234567'])


class NinTests(DetailsBase):

    def test_a_nin_is_eleven_digits(self):
        self.assertEqual(records.clean_nin('123 4567-8901'), '12345678901')
        self.assertEqual(records.clean_nin(''), '')
        for bad in ('1234567890', '123456789012', '1234567890A'):
            with self.assertRaises(records.RecordError):
                records.clean_nin(bad)

    def test_the_same_nin_cannot_belong_to_two_students(self):
        other = make_student(self.school, 'STU002')
        other.nin = '12345678901'
        other.save()
        self.assertEqual(records.nin_taken_by(self.ada, '12345678901'), other)
        self.assertIsNone(records.nin_taken_by(other, '12345678901'))

    def test_the_history_never_holds_a_full_nin(self):
        self.ada.nin = '12345678901'
        self.ada.save()
        change = StudentChange.objects.get(field='nin')
        self.assertEqual(change.new_value, '•••••••8901')

    def test_the_same_nin_at_another_school_is_fine(self):
        other_school = make_school('beta')
        theirs = make_student(other_school, 'STU001')
        theirs.nin = '12345678901'
        theirs.save()
        set_current_tenant(None)
        self.assertIsNone(records.nin_taken_by(self.ada, '12345678901'))


class UploadCheckTests(DetailsBase):

    def test_real_pdfs_and_images_are_accepted(self):
        self.assertEqual(records.check_upload(SimpleUploadedFile('a.pdf', PDF)), '.pdf')
        self.assertEqual(records.check_upload(SimpleUploadedFile('a.PNG', PNG)), '.png')

    def test_other_file_types_are_refused(self):
        with self.assertRaisesMessage(records.RecordError, 'Upload a PDF or a photo'):
            records.check_upload(SimpleUploadedFile('a.exe', b'MZ...'))

    def test_a_renamed_file_is_refused(self):
        with self.assertRaisesMessage(records.RecordError, "isn't a real PDF"):
            records.check_upload(SimpleUploadedFile('virus.pdf', b'MZ\x90\x00 not a pdf'))

    def test_large_files_are_refused(self):
        big = SimpleUploadedFile('a.pdf', PDF + b'0' * (records.MAX_DOCUMENT_BYTES + 1))
        with self.assertRaisesMessage(records.RecordError, 'larger than 10 MB'):
            records.check_upload(big)


class CustomFieldTests(DetailsBase):

    def test_adding_the_usual_fields_once(self):
        self.assertEqual(records.add_suggested_fields(self.school), len(records.SUGGESTED_FIELDS))
        self.assertEqual(records.add_suggested_fields(self.school), 0)
        state = StudentField.objects.get(label='State of origin')
        self.assertIn('Lagos', state.options)
        self.assertIn('FCT (Abuja)', state.options)
        self.assertEqual(len(state.options), 38)
        self.assertTrue(StudentField.objects.get(label='Genotype').sensitive)

    def test_each_school_has_its_own_fields(self):
        records.add_suggested_fields(self.school)
        other = make_school('beta')
        set_current_tenant(None)
        self.assertEqual(StudentField.objects.filter(tenant=other).count(), 0)

    def test_answers_are_checked_by_type(self):
        number = StudentField.objects.create(tenant=self.school, label='Height', key='height',
                                             field_type='NUMBER')
        choice = StudentField.objects.create(tenant=self.school, label='House', key='house',
                                             field_type='CHOICE', choices='Red\nBlue')
        when = StudentField.objects.create(tenant=self.school, label='Baptised', key='bap',
                                           field_type='DATE')
        self.assertEqual(records.clean_value(number, '1,52'), '152')
        self.assertEqual(records.clean_value(when, '2015-03-02'), '2015-03-02')
        for field, bad in ((number, 'tall'), (choice, 'Green'), (when, '02/03/2015')):
            with self.assertRaises(records.RecordError):
                records.clean_value(field, bad)

    def test_a_required_field_must_be_answered(self):
        f = StudentField.objects.create(tenant=self.school, label='LGA', key='lga', required=True)
        with self.assertRaisesMessage(records.RecordError, 'LGA is required'):
            records.clean_value(f, '  ')

    def test_one_bad_answer_saves_nothing(self):
        good = StudentField.objects.create(tenant=self.school, label='LGA', key='lga')
        bad = StudentField.objects.create(tenant=self.school, label='Height', key='h',
                                          field_type='NUMBER')
        with self.assertRaises(records.RecordError):
            records.save_answers(self.ada, {f'field_{good.id}': 'Ikeja',
                                            f'field_{bad.id}': 'tall'})
        self.assertFalse(StudentFieldValue.objects.exists())

    def test_sensitive_answers_are_hidden_from_the_history_and_from_teachers(self):
        f = StudentField.objects.create(tenant=self.school, label='Genotype', key='genotype',
                                        field_type='CHOICE', choices='AA\nAS\nSS', sensitive=True)
        records.save_answers(self.ada, {f'field_{f.id}': 'AS'}, self.admin)
        change = StudentChange.objects.get(field='custom:genotype')
        self.assertEqual(change.new_value, '(hidden)')
        self.assertEqual(records.answers(self.ada, include_sensitive=False), [])
        self.assertEqual(records.answers(self.ada, include_sensitive=True), [(f, 'AS')])


@override_settings(ROOT_DOMAIN='localhost', ALLOWED_HOSTS=['*'])
class PageTests(DetailsBase):

    def _post(self, path, data, **kwargs):
        return self.client.post(path, data, HTTP_HOST=self.host(self.school), **kwargs)

    def _get(self, path):
        return self.client.get(path, HTTP_HOST=self.host(self.school))

    def _upload(self, content=PDF, name='birth.pdf', **extra):
        data = {'action': 'upload', 'doc_type': 'BIRTH',
                'file': SimpleUploadedFile(name, content, content_type='application/pdf')}
        data.update(extra)
        return self._post(f'/students/{self.ada.id}/documents/', data, follow=True)

    def test_adding_editing_and_removing_a_guardian(self):
        self.client.force_login(self.admin)
        self._post(f'/students/{self.ada.id}/guardians/', {
            'action': 'add', 'full_name': 'Musa Bello', 'relationship': 'FATHER',
            'phone': '08055551234', 'contact_by': 'SMS', 'can_pick_up': '1'})
        g = Guardian.objects.get()
        self.assertEqual((g.relationship, g.contact_by, g.can_pick_up, g.gets_fee_messages),
                         ('FATHER', 'SMS', True, False))
        self._post(f'/students/{self.ada.id}/guardians/', {
            'action': 'edit', 'guardian_id': g.id, 'full_name': 'Musa Bello',
            'relationship': 'FATHER', 'phone': '08055551234', 'contact_by': 'BOTH',
            'note': 'Court order — may not collect'})
        g.refresh_from_db()
        self.assertFalse(g.can_pick_up)
        self._post(f'/students/{self.ada.id}/guardians/', {'action': 'delete', 'guardian_id': g.id})
        self.assertFalse(Guardian.objects.exists())

    def test_a_guardian_needs_a_way_to_be_reached(self):
        self.client.force_login(self.admin)
        response = self._post(f'/students/{self.ada.id}/guardians/',
                              {'action': 'add', 'full_name': 'Nobody'}, follow=True)
        self.assertContains(response, 'Give a phone number or an email')
        self.assertFalse(Guardian.objects.exists())

    def test_uploading_and_opening_a_document(self):
        self.client.force_login(self.admin)
        response = self._upload(title='Birth certificate')
        self.assertContains(response, 'uploaded')
        doc = StudentDocument.objects.get()
        self.assertTrue(doc.file.name.startswith(f'student_documents/{self.school.id}/'))
        self.assertNotIn('birth', doc.file.name)            # stored under a random name
        self.assertEqual(doc.uploaded_by, self.admin)
        opened = self._get(f'/students/documents/{doc.id}/file/')
        self.assertEqual(opened.status_code, 200)
        self.assertEqual(b''.join(opened.streaming_content), PDF)
        self.assertEqual(opened['Cache-Control'], 'private, no-store')
        downloaded = self._get(f'/students/documents/{doc.id}/file/?download=1')
        self.assertIn('attachment', downloaded['Content-Disposition'])

    def test_documents_are_not_under_the_public_media_folder(self):
        from django.conf import settings
        self.client.force_login(self.admin)
        self._upload()
        doc = StudentDocument.objects.get()
        self.assertTrue(doc.file.path.startswith(self.private))
        self.assertFalse(doc.file.path.startswith(str(settings.MEDIA_ROOT)))

    def test_a_teacher_cannot_open_documents(self):
        self.client.force_login(self.admin)
        self._upload()
        doc = StudentDocument.objects.get()
        teacher = make_teacher(self.school)
        set_current_tenant(self.school)
        self.client.force_login(teacher)
        self.assertEqual(self._get(f'/students/documents/{doc.id}/file/').status_code, 404)
        page = self._get(f'/students/{self.ada.id}/')
        self.assertIsNone(page.context['documents'])

    def test_another_schools_admin_cannot_open_a_document(self):
        self.client.force_login(self.admin)
        self._upload()
        doc = StudentDocument.objects.get()
        other = make_school('beta')
        their_admin = make_admin(other)
        self.client.force_login(their_admin)
        response = self.client.get(f'/students/documents/{doc.id}/file/',
                                   HTTP_HOST=self.host(other))
        self.assertEqual(response.status_code, 404)

    def test_a_disguised_file_is_refused(self):
        self.client.force_login(self.admin)
        response = self._upload(content=b'<script>alert(1)</script>', name='x.pdf')
        self.assertContains(response, 'a real PDF or image')
        self.assertFalse(StudentDocument.objects.exists())

    def test_deleting_a_document_removes_the_file(self):
        import os
        self.client.force_login(self.admin)
        self._upload()
        doc = StudentDocument.objects.get()
        path = doc.file.path
        self._post(f'/students/{self.ada.id}/documents/', {'action': 'delete', 'document_id': doc.id})
        self.assertFalse(StudentDocument.objects.exists())
        self.assertFalse(os.path.exists(path))

    def test_the_expiring_documents_page(self):
        self.client.force_login(self.admin)
        today = timezone.localdate()
        self._upload(expires_on=(today - timedelta(days=3)).isoformat())
        self._upload(expires_on=(today + timedelta(days=10)).isoformat())
        self._upload(expires_on=(today + timedelta(days=200)).isoformat())
        page = self._get('/students/documents/expiring/')
        self.assertEqual((len(page.context['expired']), len(page.context['soon'])), (1, 1))

    def test_saving_details_and_nin(self):
        f = StudentField.objects.create(tenant=self.school, label='LGA', key='lga')
        self.client.force_login(self.admin)
        self._post(f'/students/{self.ada.id}/details/', {'nin': '123 4567 8901',
                                                         f'field_{f.id}': 'Ikeja'})
        self.ada.refresh_from_db()
        self.assertEqual(self.ada.nin, '12345678901')
        self.assertEqual(StudentFieldValue.objects.get().value, 'Ikeja')

    def test_a_duplicate_nin_is_refused(self):
        other = make_student(self.school, 'STU002', full_name='Tobi Okafor')
        other.nin = '12345678901'
        other.save()
        self.client.force_login(self.admin)
        response = self._post(f'/students/{self.ada.id}/details/', {'nin': '12345678901'},
                              follow=True)
        self.assertContains(response, 'already recorded for Tobi Okafor')

    def test_a_teacher_sees_a_masked_nin_and_no_sensitive_fields(self):
        self.ada.nin = '12345678901'
        self.ada.save()
        StudentField.objects.create(tenant=self.school, label='Genotype', key='g', sensitive=True)
        teacher = make_teacher(self.school)
        set_current_tenant(self.school)
        self.client.force_login(teacher)
        page = self._get(f'/students/{self.ada.id}/')
        self.assertEqual(page.context['nin_display'], '•••••••8901')
        self.assertNotContains(page, '12345678901')
        self.assertEqual(page.context['answers'], [])

    def test_setting_up_fields(self):
        self.client.force_login(self.admin)
        self._post('/students/fields/', {'action': 'suggested'})
        self._post('/students/fields/', {'action': 'add', 'label': 'House',
                                         'field_type': 'CHOICE', 'choices': 'Red\nBlue\nGreen'})
        self.assertTrue(StudentField.objects.filter(label='House').exists())
        response = self._post('/students/fields/', {'action': 'add', 'label': 'Club',
                                                    'field_type': 'CHOICE', 'choices': 'Chess'},
                              follow=True)
        self.assertContains(response, 'at least two options')

    def test_teachers_cannot_change_details_or_fields(self):
        f = StudentField.objects.create(tenant=self.school, label='LGA', key='lga')
        teacher = make_teacher(self.school)
        set_current_tenant(self.school)
        self.client.force_login(teacher)
        self._post(f'/students/{self.ada.id}/details/', {f'field_{f.id}': 'Ikeja'})
        self._post(f'/students/{self.ada.id}/guardians/', {'action': 'add', 'full_name': 'X',
                                                           'phone': '0800'})
        self._post('/students/fields/', {'action': 'suggested'})
        self.assertFalse(StudentFieldValue.objects.exists())
        self.assertFalse(Guardian.objects.exists())
        self.assertEqual(StudentField.objects.count(), 1)

    def test_a_parent_cannot_reach_any_of_it(self):
        self.client.force_login(self.admin)
        self._upload()
        doc = StudentDocument.objects.get()
        parent = User.objects.create_user(username='par1', password='x', tenant=self.school,
                                          is_parent=True)
        self.client.force_login(parent)
        self.assertEqual(self._get(f'/students/documents/{doc.id}/file/').status_code, 404)
        for path in ('/students/fields/', '/students/documents/expiring/'):
            self.assertNotEqual(self._get(path).status_code, 200, path)
