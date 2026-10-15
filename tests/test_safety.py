"""
Gate and safety: who may collect a child, one-time pickup codes, the pickup
log and family alerts, emergency roll calls, and lost ID cards.
"""
import shutil
import tempfile
from datetime import datetime, time, timedelta
from decimal import Decimal
from unittest import mock

from django.test import override_settings
from django.utils import timezone

from finance.models import Wallet
from idcards.models import IDCard
from safety import services as svc
from safety.models import LostCardReport, PickupPass, PickupRelease, RollCall, SafetySettings
from tenants.middleware import set_current_tenant
from users.lifecycle import change_status
from users.models import Attendance, Guardian, Staff, User

from .factories import (TenantTestCase, make_admin, make_class, make_school, make_student,
                        make_teacher)


@override_settings(RUN_TASKS_EAGERLY=True)
class Base(TenantTestCase):

    def setUp(self):
        super().setUp()
        media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media, ignore_errors=True)
        o = override_settings(MEDIA_ROOT=media)
        o.enable()
        self.addCleanup(o.disable)
        self.school = make_school('alpha')
        self.admin = make_admin(self.school)
        self.jss1 = make_class(self.school, 'JSS 1A')
        set_current_tenant(self.school)
        self.ada = make_student(self.school, 'STU001', full_name='Ada Bello', school_class=self.jss1,
                                parent_email='mum@example.com')
        self.ada.parent_name, self.ada.parent_phone = 'Ngozi Bello', '08031234567'
        self.ada.save()
        self.dad = Guardian.objects.create(tenant=self.school, student=self.ada, full_name='Musa Bello',
                                           relationship='FATHER', phone='08055551234', can_pick_up=True)
        self.uncle = Guardian.objects.create(tenant=self.school, student=self.ada, full_name='Uncle Tunde',
                                             relationship='OTHER', phone='08077770000', can_pick_up=False,
                                             note='Court order — never release to him')
        self.cfg = SafetySettings.objects.create(tenant=self.school, school_ends_at=time(14, 0))


class ReleaseTests(Base):

    def test_the_main_contact_may_collect(self):
        with mock.patch('communications.services.send_sms', return_value=(True, 'ok')) as sms:
            r = svc.release(self.ada, self.admin, collector='main')
        self.assertEqual((r.collector_name, r.checked_by), ('Ngozi Bello', 'LIST'))
        self.assertIn('Ada Bello was collected by Ngozi Bello', sms.call_args.args[1])
        r.refresh_from_db()
        self.assertTrue(r.parent_notified)

    def test_an_approved_guardian_may_collect(self):
        r = svc.release(self.ada, self.admin, collector=str(self.dad.id))
        self.assertEqual((r.guardian, r.relationship), (self.dad, 'Father'))

    def test_a_barred_guardian_is_refused_with_the_note(self):
        with self.assertRaisesMessage(svc.SafetyError, 'NOT allowed to collect'):
            svc.release(self.ada, self.admin, collector=str(self.uncle.id))
        with self.assertRaisesMessage(svc.SafetyError, 'Court order'):
            svc.release(self.ada, self.admin, collector=str(self.uncle.id))
        self.assertFalse(PickupRelease.objects.exists())

    def test_only_an_administrator_can_override_a_bar(self):
        with self.assertRaises(svc.SafetyError):
            svc.release(self.ada, self.admin, collector=str(self.uncle.id),
                        override_reason='Mother phoned', can_override=False)
        r = svc.release(self.ada, self.admin, collector=str(self.uncle.id),
                        override_reason='Mother phoned to confirm', can_override=True)
        self.assertEqual(r.checked_by, 'OVERRIDE')

    def test_someone_unknown_needs_a_code(self):
        with self.assertRaisesMessage(svc.SafetyError, 'needs a pickup code'):
            svc.release(self.ada, self.admin, collector='other', other_name='Stranger')

    def test_another_childs_guardian_cannot_be_used(self):
        tobi = make_student(self.school, 'STU002')
        their = Guardian.objects.create(tenant=self.school, student=tobi, full_name='X', phone='0800',
                                        can_pick_up=True)
        with self.assertRaisesMessage(svc.SafetyError, 'not linked to this child'):
            svc.release(self.ada, self.admin, collector=str(their.id))

    def test_early_pickup_is_noted_on_the_register(self):
        Attendance.objects.create(tenant=self.school, student=self.ada, school_class=self.jss1,
                                  date=timezone.localdate(), status='P')
        with mock.patch('safety.services.is_early', return_value=True):
            r = svc.release(self.ada, self.admin, collector='main', reason='Dentist')
        self.assertTrue(r.early)
        self.assertIn('with Ngozi Bello — Dentist', Attendance.objects.get().remarks)

    def test_a_child_who_has_left_cannot_be_released(self):
        change_status(self.ada, 'Withdrawn', reason='Moved')
        with self.assertRaises(svc.SafetyError):
            svc.release(self.ada, self.admin, collector='main')

    def test_the_family_is_not_told_when_switched_off(self):
        self.cfg.notify_parent_on_pickup = False
        self.cfg.save()
        with mock.patch('communications.services.send_sms') as sms:
            svc.release(self.ada, self.admin, collector='main')
        sms.assert_not_called()


class CodeTests(Base):

    def test_issuing_texts_the_collector(self):
        with mock.patch('communications.services.send_sms', return_value=(True, 'ok')) as sms:
            p = svc.issue_pass(self.ada, 'Aunty Funke', collector_phone='08099990000', relationship='Aunt')
        self.assertEqual(len(p.code), 6)
        self.assertTrue(p.code.isdigit())
        self.assertIn(p.code, sms.call_args.args[1])
        self.assertEqual(sms.call_args.args[0], '08099990000')

    def test_a_code_releases_once(self):
        p = svc.issue_pass(self.ada, 'Aunty Funke')
        r = svc.release(self.ada, self.admin, collector='other', code=p.code)
        self.assertEqual((r.collector_name, r.checked_by, r.pickup_pass), ('Aunty Funke', 'PASS', p))
        p.refresh_from_db()
        self.assertEqual(p.status, 'USED')
        with self.assertRaises(svc.SafetyError):
            svc.release(self.ada, self.admin, collector='other', code=p.code)

    def test_a_wrong_code_is_refused(self):
        svc.issue_pass(self.ada, 'Aunty Funke')
        with self.assertRaisesMessage(svc.SafetyError, 'does not match'):
            svc.release(self.ada, self.admin, collector='other', code='000000')

    def test_guessing_locks_codes_for_the_day(self):
        p = svc.issue_pass(self.ada, 'Aunty Funke')
        for _ in range(svc.MAX_WRONG_CODES):
            with self.assertRaises(svc.SafetyError):
                svc.check_code(self.ada, '1' if p.code != '1' else '2')
        with self.assertRaisesMessage(svc.SafetyError, 'Too many wrong codes'):
            svc.check_code(self.ada, p.code)                    # even the right one now

    def test_an_expired_code_is_refused(self):
        p = svc.issue_pass(self.ada, 'Aunty Funke')
        PickupPass.objects.filter(pk=p.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
        with self.assertRaisesMessage(svc.SafetyError, 'expired'):
            svc.check_code(self.ada, p.code)

    def test_a_code_for_another_child_does_not_work(self):
        tobi = make_student(self.school, 'STU002')
        p = svc.issue_pass(tobi, 'Aunty Funke')
        with self.assertRaises(svc.SafetyError):
            svc.check_code(self.ada, p.code)

    def test_a_cancelled_code_does_not_work(self):
        p = svc.issue_pass(self.ada, 'Aunty Funke')
        svc.cancel_pass(p)
        with self.assertRaises(svc.SafetyError):
            svc.check_code(self.ada, p.code)

    def test_limits(self):
        with self.assertRaisesMessage(svc.SafetyError, 'full name'):
            svc.issue_pass(self.ada, 'Al')
        with self.assertRaisesMessage(svc.SafetyError, 'week ahead'):
            svc.issue_pass(self.ada, 'Aunty Funke', valid_on=timezone.localdate() + timedelta(days=9))
        for i in range(3):
            svc.issue_pass(self.ada, f'Person {i}')
        with self.assertRaisesMessage(svc.SafetyError, 'three pickup codes'):
            svc.issue_pass(self.ada, 'Person 4')


class RollCallTests(Base):

    def setUp(self):
        super().setUp()
        self.tobi = make_student(self.school, 'STU002', full_name='Tobi', school_class=self.jss1)
        self.away = make_student(self.school, 'STU003', full_name='Away', school_class=self.jss1)
        Attendance.objects.create(tenant=self.school, student=self.away, school_class=self.jss1,
                                  date=timezone.localdate(), status='A')
        self.teacher = make_teacher(self.school)
        set_current_tenant(self.school)

    def test_who_is_expected(self):
        roll = svc.start_roll_call(self.school, 'Fire drill', 'DRILL', user=self.admin)
        students, staff, absent, left = svc.expected(roll)
        self.assertEqual({s.full_name for s in students}, {'Ada Bello', 'Tobi'})
        self.assertEqual(absent, 1)
        self.assertEqual(len(staff), Staff.objects.filter(tenant=self.school).count())

    def test_a_child_collected_earlier_is_not_expected(self):
        PickupRelease.objects.create(tenant=self.school, student=self.tobi, collector_name='Mum',
                                     checked_by='LIST', released_at=timezone.now() - timedelta(minutes=5))
        roll = svc.start_roll_call(self.school, 'Drill', 'DRILL')
        students, _staff, _a, left = svc.expected(roll)
        self.assertNotIn(self.tobi, students)
        self.assertEqual(left, 1)

    def test_marking_and_counting(self):
        roll = svc.start_roll_call(self.school, 'Fire drill', 'DRILL')
        svc.mark(roll, self.teacher, student=self.ada, status='SAFE')
        svc.mark(roll, self.teacher, student=self.tobi, status='INJURED', note='Twisted ankle')
        svc.mark(roll, self.teacher, student=self.tobi, status='SAFE')            # updated, not doubled
        s = svc.roll_status(roll)
        self.assertEqual(s['counts']['SAFE'], 2)
        self.assertEqual(s['counts']['UNACCOUNTED'], s['total'] - 2)
        self.assertEqual(roll.entries.count(), 2)

    def test_one_roll_call_at_a_time(self):
        svc.start_roll_call(self.school, 'Drill', 'DRILL')
        with self.assertRaisesMessage(svc.SafetyError, 'already running'):
            svc.start_roll_call(self.school, 'Another', 'DRILL')

    def test_an_ended_roll_call_cannot_be_changed(self):
        roll = svc.start_roll_call(self.school, 'Drill', 'DRILL')
        svc.end_roll_call(roll, 'All out in 4 minutes')
        with self.assertRaises(svc.SafetyError):
            svc.mark(roll, self.teacher, student=self.ada)


class LostCardTests(Base):

    def setUp(self):
        super().setUp()
        self.card = IDCard.objects.create(tenant=self.school, card_type='STUDENT', student=self.ada)
        self.wallet = Wallet.objects.create(tenant=self.school, student=self.ada, balance=Decimal('5000'))

    def test_reporting_switches_the_card_off(self):
        svc.report_lost(self.card, self.admin, 'Lost on the bus')
        self.card.refresh_from_db()
        self.assertFalse(self.card.is_active)
        with self.assertRaises(svc.SafetyError):
            svc.report_lost(self.card, self.admin)

    def test_reissue_gives_a_new_qr_and_charges_the_wallet(self):
        old_uid = self.card.card_uid
        report = svc.report_lost(self.card, self.admin)
        svc.reissue(report, self.admin, 'WALLET')
        self.card.refresh_from_db(); self.wallet.refresh_from_db()
        self.assertTrue(self.card.is_active)
        self.assertNotEqual(self.card.card_uid, old_uid)
        self.assertEqual(self.wallet.balance, Decimal('4000.00'))
        self.assertEqual(report.fee_status, 'WALLET')

    def test_not_enough_in_the_wallet(self):
        self.wallet.balance = Decimal('100')
        self.wallet.save()
        report = svc.report_lost(self.card, self.admin)
        with self.assertRaisesMessage(svc.SafetyError, 'Choose another way'):
            svc.reissue(report, self.admin, 'WALLET')
        self.card.refresh_from_db()
        self.assertFalse(self.card.is_active)

    def test_fee_can_be_owed_or_waived(self):
        report = svc.report_lost(self.card, self.admin)
        svc.reissue(report, self.admin, 'OWED')
        self.assertEqual(report.fee_status, 'OWED')

    def test_the_old_qr_no_longer_opens_the_gate(self):
        old_uid = self.card.card_uid
        report = svc.report_lost(self.card, self.admin)
        svc.reissue(report, self.admin, 'WAIVED')
        self.assertFalse(IDCard.objects.filter(card_uid=old_uid).exists())


@override_settings(ROOT_DOMAIN='localhost', ALLOWED_HOSTS=['*'])
class PageTests(Base):

    def _get(self, path, **kw):
        return self.client.get(path, HTTP_HOST=self.host(self.school), **kw)

    def _post(self, path, data, **kw):
        return self.client.post(path, data, HTTP_HOST=self.host(self.school), **kw)

    def test_the_pages_load(self):
        self.client.force_login(self.admin)
        for path in ('/safety/pickup/', f'/safety/pickup/?student={self.ada.id}', '/safety/pickup/?q=ada',
                     '/safety/pickups/', '/safety/roll-call/', '/safety/lost-cards/', '/safety/settings/'):
            self.assertEqual(self._get(path).status_code, 200, path)

    def test_the_desk_shows_who_may_collect(self):
        self.client.force_login(self.admin)
        page = self._get(f'/safety/pickup/?student={self.ada.id}')
        self.assertContains(page, 'Musa Bello')
        self.assertContains(page, 'NOT allowed')
        self.assertContains(page, 'Court order')

    def test_releasing_from_the_desk(self):
        self.client.force_login(self.admin)
        response = self._post('/safety/pickup/', {'student_id': self.ada.id, 'collector': str(self.dad.id)},
                              follow=True)
        self.assertContains(response, 'released to Musa Bello')
        self.assertEqual(PickupRelease.objects.get().released_by, self.admin)

    def test_a_teacher_without_gate_rights_cannot_release(self):
        teacher = make_teacher(self.school)
        set_current_tenant(self.school)
        self.client.force_login(teacher)
        self._post('/safety/pickup/', {'student_id': self.ada.id, 'collector': 'main'})
        self.assertFalse(PickupRelease.objects.exists())

    def test_gate_staff_cannot_override(self):
        teacher = make_teacher(self.school)
        Staff.objects.filter(user=teacher).update(can_mark_attendance=True)
        set_current_tenant(self.school)
        self.client.force_login(teacher)
        self._post('/safety/pickup/', {'student_id': self.ada.id, 'collector': str(self.uncle.id),
                                       'override_reason': 'I think it is fine'})
        self.assertFalse(PickupRelease.objects.exists())

    def test_a_roll_call_end_to_end(self):
        self.client.force_login(self.admin)
        response = self._post('/safety/roll-call/', {'title': 'Fire drill', 'kind': 'DRILL'})
        roll = RollCall.objects.get()
        self.assertRedirects(response, f'/safety/roll-call/{roll.id}/', fetch_redirect_response=False)
        result = self._post(f'/safety/roll-call/{roll.id}/', {'action': 'mark', 'type': 'student',
                                                              'id': self.ada.id, 'status': 'SAFE'},
                            HTTP_X_REQUESTED_WITH='fetch').json()
        self.assertEqual(result['counts']['SAFE'], 1)
        live = self._get(f'/safety/roll-call/{roll.id}/?format=json').json()
        self.assertIn({'type': 'student', 'id': self.ada.id, 'status': 'SAFE'}, live['people'])
        self._post(f'/safety/roll-call/{roll.id}/', {'action': 'end'})
        roll.refresh_from_db()
        self.assertFalse(roll.is_open)

    def test_reporting_and_reissuing_a_card(self):
        IDCard.objects.create(tenant=self.school, card_type='STUDENT', student=self.ada)
        self.client.force_login(self.admin)
        self._post('/safety/lost-cards/', {'action': 'report', 'student_id': self.ada.id})
        report = LostCardReport.objects.get()
        response = self._post('/safety/lost-cards/', {'action': 'reissue', 'report_id': report.id,
                                                      'fee_mode': 'CASH'})
        self.assertRedirects(response, f'/idcards/student/{self.ada.id}/', fetch_redirect_response=False)

    def test_the_parent_page(self):
        parent = User.objects.create_user(username=f'parent_{self.ada.id}', password='x', tenant=self.school,
                                          is_parent=True, email='mum@example.com')
        self.client.force_login(parent)
        self.assertEqual(self._get('/safety/parent/pickup/').status_code, 200)
        response = self._post('/safety/parent/pickup/', {'action': 'pass', 'child_id': self.ada.id,
                                                         'collector_name': 'Aunty Funke'}, follow=True)
        p = PickupPass.objects.get()
        self.assertContains(response, f'Pickup code {p.code}')
        self.assertEqual(p.issued_by, parent)

    def test_a_parent_cannot_issue_for_another_child(self):
        other = make_student(self.school, 'STU050', parent_email='other@example.com')
        parent = User.objects.create_user(username=f'parent_{self.ada.id}', password='x', tenant=self.school,
                                          is_parent=True, email='mum@example.com')
        self.client.force_login(parent)
        self._post('/safety/parent/pickup/', {'action': 'pass', 'child_id': other.id,
                                              'collector_name': 'Somebody Else'})
        self.assertFalse(PickupPass.objects.exists())

    def test_uploading_a_guardian_photo(self):
        import io
        from PIL import Image, ImageDraw
        from django.core.files.uploadedfile import SimpleUploadedFile
        img = Image.new('RGB', (600, 800), (140, 110, 90)); d = ImageDraw.Draw(img)
        for x in range(0, 600, 20):
            d.line([(x, 0), (x, 800)], fill=(20, 20, 20), width=3)
        buf = io.BytesIO(); img.save(buf, 'JPEG')
        self.client.force_login(self.admin)
        self._post(f'/students/{self.ada.id}/guardians/', {
            'action': 'edit', 'guardian_id': self.dad.id, 'full_name': 'Musa Bello', 'relationship': 'FATHER',
            'phone': '08055551234', 'contact_by': 'BOTH', 'can_pick_up': '1',
            'photo': SimpleUploadedFile('dad.jpg', buf.getvalue(), content_type='image/jpeg')})
        self.dad.refresh_from_db()
        self.assertTrue(self.dad.photo)

    def test_parents_cannot_reach_staff_pages(self):
        parent = User.objects.create_user(username='par9', password='x', tenant=self.school, is_parent=True)
        self.client.force_login(parent)
        for path in ('/safety/pickup/', '/safety/roll-call/', '/safety/lost-cards/', '/safety/settings/'):
            self.assertNotEqual(self._get(path).status_code, 200, path)
