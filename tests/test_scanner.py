"""
The scanner opened from the top bar: who sees it, gate scans and reception clock-ins answered for the
scanner (so it can keep scanning), a card read twice in a moment recorded once, and bad codes refused.
"""
import uuid
from unittest import mock

from idcards.models import GateScanLog, IDCard, ReceptionStation, StaffClockLog
from tenants.middleware import set_current_tenant
from users.models import Staff

from .factories import TenantTestCase, make_admin, make_class, make_school, make_student, make_teacher


class ScannerTests(TenantTestCase):

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha')
        self.admin = make_admin(self.school)
        self.teacher = make_teacher(self.school)
        set_current_tenant(self.school)
        self.ada = make_student(self.school, 'A1', 'Ada Obi', school_class=make_class(self.school, 'JSS 1'))
        self.card = IDCard.objects.create(tenant=self.school, card_type='STUDENT', student=self.ada)
        patcher = mock.patch('idcards.services._notify_parent')
        self.notify = patcher.start()
        self.addCleanup(patcher.stop)

    def get(self, user, path):
        self.client.force_login(user)
        return self.client.get(path, HTTP_HOST=self.host(self.school))

    def test_the_scan_button_is_for_staff(self):
        self.assertContains(self.get(self.teacher, '/dashboard/teacher/'), 'class="scan-btn"')
        self.assertNotContains(self.get(self.ada.user, '/dashboard/student/'), 'class="scan-btn"')
        self.assertEqual(self.get(self.teacher, '/idcards/scanner/').status_code, 200)
        self.assertEqual(self.get(self.ada.user, '/idcards/scanner/').status_code, 302)

    def test_gate_scan_for_the_scanner_and_a_double_read(self):
        url = f'/idcards/scan/{self.card.card_uid}/?format=json'
        first = self.get(self.admin, url).json()
        self.assertTrue(first['ok'])
        self.assertEqual((first['direction'], first['repeat'], first['name']), ('IN', False, 'Ada Obi'))
        again = self.get(self.admin, url).json()                   # the camera read the card again
        self.assertTrue(again['repeat'])
        self.assertEqual(GateScanLog.objects.count(), 1)              # no flip to OUT
        self.assertEqual(self.notify.call_count, 1)                   # and only one message to the parent

    def test_bad_codes_and_no_permission(self):
        self.assertFalse(self.get(self.admin, f'/idcards/scan/{uuid.uuid4()}/?format=json').json()['ok'])
        no_rights = self.get(self.teacher, f'/idcards/scan/{self.card.card_uid}/?format=json').json()
        self.assertFalse(no_rights['ok'])
        self.assertIn('Can Mark Attendance', no_rights['message'])
        self.assertEqual(GateScanLog.objects.count(), 0)

    def test_reception_clock_for_the_scanner(self):
        station = ReceptionStation.get_or_create_default(self.school)
        data = self.get(self.teacher, f'/idcards/reception/scan/{station.station_uid}/?format=json').json()
        self.assertEqual((data['ok'], data['kind'], data['direction']), (True, 'Staff clock', 'IN'))
        self.assertEqual(StaffClockLog.objects.filter(staff=Staff.objects.get(user=self.teacher)).count(), 1)
