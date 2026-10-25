"""
Staff attendance times and working days: the school sets when staff are due, the grace before
"late", closing time and the working days. Clocking in on a weekend or holiday is a visit (never
late or absent, not a working day), and the school can refuse clock-ins on days off.
"""
from datetime import datetime, time, timedelta
from unittest import mock

from django.utils import timezone

from idcards.models import ReceptionStation, StaffClockLog
from idcards.services import record_staff_clock
from safety import staff_attendance as sa
from safety.services import settings_for

from .test_visitors_leave import Base


class StaffHoursTests(Base):

    def setUp(self):
        super().setUp()
        monday = self.today - timedelta(days=self.today.weekday() + 7)       # last week, so every day is past
        self.monday, self.saturday = monday, monday + timedelta(days=5)

    def _clock(self, day, at, direction):
        log = StaffClockLog.objects.create(tenant=self.school, staff=self.teacher, direction=direction)
        StaffClockLog.objects.filter(pk=log.pk).update(scanned_at=timezone.make_aware(datetime.combine(day, at)))

    def row(self, day):
        rows, counts, closed = sa.day_report(self.school, day)
        return next(r for r in rows if r['staff'] == self.teacher), counts, closed

    def set_hours(self, **kw):
        data = {'staff_starts_at': '07:30', 'staff_late_grace_minutes': '10', 'staff_ends_at': '15:30',
                'working_days': ['0', '1', '2', '3', '4'], 'off_day_clock_in': '1', **kw}
        data = {k: v for k, v in data.items() if v is not None}
        self.client.force_login(self.admin)
        return self.client.post('/safety/staff/attendance/times/', data, HTTP_HOST=self.host(self.school), follow=True)

    def test_the_school_sets_the_times(self):
        page = self.set_hours()
        self.assertContains(page, 'Staff are due by 7:40 AM and finish at 3:30 PM')
        cfg = settings_for(self.school)
        self.assertEqual((cfg.staff_starts_at, cfg.staff_late_grace_minutes, cfg.staff_ends_at),
                         (time(7, 30), 10, time(15, 30)))
        self._clock(self.monday, time(7, 39), 'IN')
        self.assertEqual(self.row(self.monday)[0]['state'], 'present')         # within the grace
        StaffClockLog.objects.all().delete()
        self._clock(self.monday, time(7, 45), 'IN')
        self._clock(self.monday, time(14, 0), 'OUT')
        row, counts, _ = self.row(self.monday)
        self.assertEqual(row['state'], 'late')
        self.assertTrue(row['left_early'])
        self.assertEqual(counts['left_early'], 1)

    def test_bad_times_are_refused(self):
        self.assertContains(self.set_hours(staff_ends_at='07:00'), 'Closing time must be after the start time')
        self.assertContains(self.set_hours(working_days=None), 'Tick at least one working day')
        self.client.force_login(self.teacher_user)
        self.assertEqual(self.client.get('/safety/staff/attendance/times/', HTTP_HOST=self.host(self.school)).status_code, 302)

    def test_a_weekend_clock_in_is_a_visit(self):
        self._clock(self.saturday, time(11, 0), 'IN')                         # long after the start time
        row, counts, closed = self.row(self.saturday)
        self.assertTrue(closed)
        self.assertEqual((row['state'], row['visit_kind']), ('visit', 'Weekend'))
        self.assertEqual(counts['late'], 0)
        month, _ = sa.month_summary(self.school, self.monday, self.monday + timedelta(days=6))
        mine = next(m for m in month if m['staff'] == self.teacher)
        self.assertEqual((mine['visits'], mine['present'], mine['days']), (1, 0, 5))   # not a working day
        page = self.get_register(self.saturday)
        self.assertContains(page, 'Weekend visit')

    def get_register(self, day):
        self.client.force_login(self.admin)
        return self.client.get(f'/safety/staff/attendance/?date={day:%Y-%m-%d}', HTTP_HOST=self.host(self.school))

    def test_saturday_can_be_a_working_day(self):
        self.set_hours(working_days=['0', '1', '2', '3', '4', '5'])
        self._clock(self.saturday, time(7, 35), 'IN')
        row, _, closed = self.row(self.saturday)
        self.assertFalse(closed)
        self.assertEqual(row['state'], 'present')

    def test_days_off_can_refuse_clock_ins(self):
        self.set_hours(off_day_clock_in=None)
        station = ReceptionStation.get_or_create_default(self.school)
        with mock.patch('safety.staff_attendance.timezone.localdate', return_value=self.saturday):
            with self.assertRaises(sa.ClockClosed):
                record_staff_clock(self.teacher, station=station)
            self.client.force_login(self.teacher_user)
            data = self.client.get(f'/idcards/reception/scan/{station.station_uid}/?format=json',
                                   HTTP_HOST=self.host(self.school)).json()
            self.assertFalse(data['ok'])
            self.assertIn('days off', data['message'])
            sa.check_clock_in(self.teacher, 'OUT')                            # leaving is always allowed
        self.assertEqual(StaffClockLog.objects.count(), 0)
        with mock.patch('safety.staff_attendance.timezone.localdate', return_value=self.monday):
            record_staff_clock(self.teacher, station=station)                 # a working day is fine
        self.assertEqual(StaffClockLog.objects.count(), 1)
