"""
Attendance: lateness from the gate, absence alerts, reasons and excuses,
follow-up flags, heatmaps, and report-card counts limited to the term.
"""
from datetime import date, datetime, time, timedelta
from unittest import mock

from django.test import override_settings
from django.utils import timezone

from academics.models import AcademicSession, Term
from tenants.middleware import set_current_tenant
from users import attendance as att
from users.models import Attendance, AttendanceSettings, Guardian, User

from .factories import (TenantTestCase, make_admin, make_class, make_school, make_student,
                        make_teacher)


def at(day, hh, mm):
    return timezone.make_aware(datetime.combine(day, time(hh, mm)))


@override_settings(RUN_TASKS_EAGERLY=True)   # alerts are queued; run them inline here
class Base(TenantTestCase):

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha')
        self.admin = make_admin(self.school)
        self.jss1 = make_class(self.school, 'JSS 1A')
        set_current_tenant(self.school)
        self.today = timezone.localdate()
        self.session = AcademicSession.objects.create(tenant=self.school, name='2026/2027',
                                                      start_date=self.today - timedelta(days=60),
                                                      is_active=True)
        self.term = Term.objects.create(tenant=self.school, name='1st Term', session=self.session,
                                        start_date=self.today - timedelta(days=30),
                                        end_date=self.today + timedelta(days=60), is_active=True)
        self.ada = make_student(self.school, 'STU001', full_name='Ada Bello', school_class=self.jss1,
                                parent_email='mum@example.com')
        self.ada.parent_phone = '08031234567'
        self.ada.save()
        self.cfg = AttendanceSettings.objects.create(tenant=self.school, school_starts_at=time(8, 0),
                                                     late_grace_minutes=10)

    def mark(self, day, status='P', student=None, **kw):
        return Attendance.objects.create(tenant=self.school, student=student or self.ada,
                                         school_class=self.jss1, date=day, status=status, **kw)


class LatenessTests(Base):

    def test_minutes_late_respects_the_grace(self):
        self.assertEqual(att.minutes_late(self.school, at(self.today, 8, 9)), 0)
        self.assertEqual(att.minutes_late(self.school, at(self.today, 8, 25)), 15)

    def test_a_gate_arrival_marks_the_day(self):
        mark = att.record_arrival(self.ada, at(self.today, 8, 40))
        self.assertEqual((mark.status, mark.minutes_late, mark.arrived_at), ('P', 30, time(8, 40)))

    def test_only_the_first_arrival_counts(self):
        att.record_arrival(self.ada, at(self.today, 7, 50))
        mark = att.record_arrival(self.ada, at(self.today, 9, 30))       # came back after an errand
        self.assertEqual((mark.minutes_late, mark.arrived_at), (0, time(7, 50)))

    def test_arriving_after_being_marked_absent_makes_them_present_and_late(self):
        self.mark(self.today, 'A')
        mark = att.record_arrival(self.ada, at(self.today, 9, 0))
        self.assertEqual((mark.status, mark.minutes_late), ('P', 50))
        self.assertEqual(Attendance.objects.count(), 1)

    def test_the_school_can_turn_gate_marking_off(self):
        self.cfg.gate_marks_present = False
        self.cfg.save()
        self.assertIsNone(att.record_arrival(self.ada, at(self.today, 8, 40)))
        self.assertFalse(Attendance.objects.exists())

    def test_the_monthly_lateness_report(self):
        self.mark(self.today - timedelta(days=1), minutes_late=20)
        self.mark(self.today - timedelta(days=2), minutes_late=10)
        tobi = make_student(self.school, 'STU002', full_name='Tobi', school_class=self.jss1)
        self.mark(self.today - timedelta(days=1), student=tobi, minutes_late=5)
        self.mark(self.today - timedelta(days=3))                              # on time
        rows = att.lateness_report(self.school, self.today - timedelta(days=10), self.today)
        self.assertEqual([(r['student'].full_name, r['days'], r['minutes']) for r in rows],
                         [('Ada Bello', 2, 30), ('Tobi', 1, 5)])


class AlertTests(Base):

    def test_absent_families_are_told(self):
        self.mark(self.today, 'A')
        with mock.patch('communications.services.send_sms', return_value=(True, 'ok')) as sms:
            told = att.send_absence_alerts(self.school, self.jss1, self.today)
        self.assertEqual(told, 1)
        self.assertEqual(sms.call_args.args[0], '08031234567')
        self.assertIn('Ada Bello was marked absent today', sms.call_args.args[1])
        self.assertIsNotNone(Attendance.objects.get().alerted_at)

    def test_every_guardian_who_agreed_is_told(self):
        Guardian.objects.create(tenant=self.school, student=self.ada, full_name='Dad',
                                phone='0805 555 1234', contact_by='SMS')
        self.mark(self.today, 'A')
        with mock.patch('communications.services.send_sms', return_value=(True, 'ok')) as sms:
            att.send_absence_alerts(self.school, self.jss1, self.today)
        self.assertEqual(sms.call_count, 2)

    def test_nobody_is_told_twice(self):
        self.mark(self.today, 'A')
        with mock.patch('communications.services.send_sms', return_value=(True, 'ok')) as sms:
            att.send_absence_alerts(self.school, self.jss1, self.today)
            att.send_absence_alerts(self.school, self.jss1, self.today)
        self.assertEqual(sms.call_count, 1)

    def test_no_alert_when_the_parent_already_explained(self):
        att.give_reason(self.ada, self.today, 'MEDICAL', 'Dentist')
        with mock.patch('communications.services.send_sms', return_value=(True, 'ok')) as sms:
            told = att.send_absence_alerts(self.school, self.jss1, self.today)
        self.assertEqual((told, sms.call_count), (0, 0))

    def test_no_alerts_for_a_past_register(self):
        self.mark(self.today - timedelta(days=3), 'A')
        self.assertEqual(att.send_absence_alerts(self.school, self.jss1, self.today - timedelta(days=3)), 0)

    def test_alerts_can_be_switched_off(self):
        self.cfg.absence_alerts = False
        self.cfg.save()
        self.mark(self.today, 'A')
        self.assertEqual(att.send_absence_alerts(self.school, self.jss1, self.today), 0)

    def test_email_alerts(self):
        self.cfg.alert_channel = 'EMAIL'
        self.cfg.save()
        self.mark(self.today, 'A')
        with mock.patch('communications.services.send_email', return_value=(True, 'ok')) as email, \
                mock.patch('communications.services.send_sms') as sms:
            att.send_absence_alerts(self.school, self.jss1, self.today)
        self.assertEqual(email.call_args.args[0], 'mum@example.com')
        sms.assert_not_called()


class ReasonTests(Base):

    def test_a_parents_reason_waits_for_review(self):
        self.mark(self.today - timedelta(days=1), 'A')
        mark = att.give_reason(self.ada, self.today - timedelta(days=1), 'SICK', 'Fever')
        self.assertEqual((mark.excused, mark.reason_from), ('PENDING', 'PARENT'))
        att.review_reason(mark, accept=True)
        self.assertEqual(mark.excused, 'EXCUSED')

    def test_staff_reasons_are_accepted_at_once(self):
        mark = att.give_reason(self.ada, self.today, 'FAMILY', by='STAFF')
        self.assertEqual(mark.excused, 'EXCUSED')

    def test_a_reason_in_advance_creates_the_absence(self):
        day = self.today + timedelta(days=3)
        mark = att.give_reason(self.ada, day, 'RELIGIOUS')
        self.assertEqual((mark.date, mark.status), (day, 'A'))

    def test_the_register_keeps_the_reason(self):
        att.give_reason(self.ada, self.today, 'MEDICAL', 'Dentist')
        self.client.force_login(self.admin)
        self.client.post(f'/mark/{self.jss1.id}/',
                         {'date': self.today.isoformat(), f'status_{self.ada.id}': 'A'},
                         HTTP_HOST=self.host(self.school))
        mark = Attendance.objects.get()
        self.assertEqual((mark.absence_reason, mark.reason_note), ('MEDICAL', 'Dentist'))

    def test_limits(self):
        with self.assertRaisesMessage(att.AttendanceError, '30 days ahead'):
            att.give_reason(self.ada, self.today + timedelta(days=40), 'SICK')
        with self.assertRaisesMessage(att.AttendanceError, 'too long ago'):
            att.give_reason(self.ada, self.today - timedelta(days=90), 'SICK')
        with self.assertRaises(att.AttendanceError):
            att.give_reason(self.ada, self.today, 'HOLIDAY')

    def test_cannot_explain_a_day_they_were_present(self):
        self.mark(self.today - timedelta(days=1), 'P')
        with self.assertRaisesMessage(att.AttendanceError, 'was present'):
            att.give_reason(self.ada, self.today - timedelta(days=1), 'SICK')


class CountTests(Base):

    def test_term_counts_ignore_other_terms(self):
        self.mark(self.term.start_date - timedelta(days=10), 'A')          # last term
        self.mark(self.today - timedelta(days=2), 'P', minutes_late=5)
        self.mark(self.today - timedelta(days=1), 'A', absence_reason='SICK', excused='EXCUSED')
        c = att.term_counts(self.ada, self.term)
        self.assertEqual((c['present'], c['absent'], c['late'], c['excused']), (1, 1, 1, 1))

    def test_a_day_with_subject_marks_counts_once(self):
        from users.models import Subject
        maths = Subject.objects.create(tenant=self.school, name='Maths')
        english = Subject.objects.create(tenant=self.school, name='English')
        day = self.today - timedelta(days=1)
        self.mark(day, 'P', subject=maths)
        self.mark(day, 'P', subject=english)
        self.assertEqual(att.term_counts(self.ada, self.term)['days'], 1)

    def test_report_card_days_use_this_term_only(self):
        self.mark(self.term.start_date - timedelta(days=5), 'A')
        self.mark(self.term.start_date - timedelta(days=6), 'A')
        self.mark(self.today - timedelta(days=1), 'P')
        self.assertEqual(att.report_card_days(self.ada, self.term, self.session), (1, 0))

    def test_a_manual_figure_wins(self):
        from results.models import StudentTermAttendance
        Term.objects.filter(pk=self.term.pk).update(number_of_school_days=60)
        self.term.refresh_from_db()
        StudentTermAttendance.objects.create(tenant=self.school, student=self.ada, term=self.term,
                                             session=self.session, days_present=55)
        self.assertEqual(att.report_card_days(self.ada, self.term, self.session), (55, 5))


class FollowUpTests(Base):

    def test_low_attendance_is_flagged(self):
        for i in range(1, 11):
            self.mark(self.today - timedelta(days=i), 'A' if i <= 2 else 'P')   # 80%
        rows = att.needing_follow_up(self.school, self.term)
        self.assertEqual(rows[0]['rate'], 80)
        self.assertIn('80% attendance this term', rows[0]['reasons'])

    def test_a_current_absence_streak_is_flagged(self):
        for i in range(1, 30):
            self.mark(self.today - timedelta(days=i), 'A' if i <= 3 else 'P')
        reasons = att.needing_follow_up(self.school, self.term)[0]['reasons']
        self.assertIn('absent 3 school days running', reasons)

    def test_excused_absences_do_not_make_a_streak(self):
        for i in range(1, 30):
            excused = 'EXCUSED' if i <= 3 else ''
            self.mark(self.today - timedelta(days=i), 'A' if i <= 3 else 'P', excused=excused,
                      absence_reason='SICK' if excused else '')
        rows = att.needing_follow_up(self.school, self.term)
        self.assertTrue(all('running' not in r for row in rows for r in row['reasons']))

    def test_good_attendance_is_not_flagged(self):
        for i in range(1, 11):
            self.mark(self.today - timedelta(days=i))
        self.assertEqual(att.needing_follow_up(self.school, self.term), [])


class HeatmapTests(Base):

    def test_a_student_calendar(self):
        monday = self.today - timedelta(days=self.today.weekday() + 7)
        self.mark(monday, 'P')
        self.mark(monday + timedelta(days=1), 'P', minutes_late=12)
        self.mark(monday + timedelta(days=2), 'A')
        self.mark(monday + timedelta(days=3), 'A', excused='EXCUSED', absence_reason='SICK')
        weeks = att.student_heatmap(self.ada, monday, monday + timedelta(days=4))
        self.assertEqual([d['state'] for d in weeks[0]], ['present', 'late', 'absent', 'excused', 'none'])

    def test_a_class_calendar(self):
        tobi = make_student(self.school, 'STU002', school_class=self.jss1)
        monday = self.today - timedelta(days=self.today.weekday() + 7)
        self.mark(monday, 'P')
        self.mark(monday, 'A', student=tobi)
        weeks, average = att.class_heatmap(self.jss1, monday, monday + timedelta(days=4))
        self.assertEqual((weeks[0][0]['rate'], weeks[0][0]['level'], average), (50, 'l1', 50))


@override_settings(ROOT_DOMAIN='localhost', ALLOWED_HOSTS=['*'])
class PageTests(Base):

    def _get(self, path):
        return self.client.get(path, HTTP_HOST=self.host(self.school))

    def _post(self, path, data, **kw):
        return self.client.post(path, data, HTTP_HOST=self.host(self.school), **kw)

    def test_saving_the_register_alerts_parents(self):
        self.client.force_login(self.admin)
        with mock.patch('communications.services.send_sms', return_value=(True, 'ok')) as sms:
            response = self._post(f'/mark/{self.jss1.id}/', {'date': self.today.isoformat(),
                                                            f'status_{self.ada.id}': 'A'}, follow=True)
        self.assertContains(response, 'told their child is absent')
        self.assertEqual(sms.call_count, 1)

    def test_a_future_register_is_refused(self):
        self.client.force_login(self.admin)
        response = self._post(f'/mark/{self.jss1.id}/', {
            'date': (self.today + timedelta(days=2)).isoformat(), f'status_{self.ada.id}': 'P'}, follow=True)
        self.assertContains(response, "hasn&#x27;t happened yet")
        self.assertFalse(Attendance.objects.exists())

    def test_the_pages_load(self):
        self.client.force_login(self.admin)
        for path in ('/attendance/settings/', '/attendance/lateness/', '/attendance/follow-up/',
                     f'/attendance/heatmap/?class={self.jss1.id}', '/attendance/reasons/',
                     f'/students/{self.ada.id}/'):
            self.assertEqual(self._get(path).status_code, 200, path)

    def test_saving_settings(self):
        self.client.force_login(self.admin)
        self._post('/attendance/settings/', {'school_starts_at': '07:45', 'late_grace_minutes': '5',
                                             'chronic_rate': '85', 'chronic_streak': '4',
                                             'alert_channel': 'BOTH', 'absence_alerts': '1'})
        self.cfg.refresh_from_db()
        self.assertEqual((self.cfg.school_starts_at, self.cfg.alert_channel, self.cfg.gate_marks_present),
                         (time(7, 45), 'BOTH', False))

    def test_a_parent_explains_an_absence(self):
        self.mark(self.today - timedelta(days=1), 'A')
        parent = User.objects.create_user(username=f'parent_{self.ada.id}', password='x',
                                          tenant=self.school, is_parent=True, email='mum@example.com')
        self.client.force_login(parent)
        page = self._get('/parent/absences/')
        self.assertEqual(len(page.context['unexplained']), 1)
        self._post('/parent/absences/', {'child_id': self.ada.id,
                                         'date': (self.today - timedelta(days=1)).isoformat(),
                                         'reason': 'SICK', 'note': 'Malaria'})
        mark = Attendance.objects.get()
        self.assertEqual((mark.absence_reason, mark.excused), ('SICK', 'PENDING'))

    def test_staff_excuse_from_the_queue(self):
        self.mark(self.today - timedelta(days=1), 'A')
        mark = att.give_reason(self.ada, self.today - timedelta(days=1), 'SICK')
        teacher = make_teacher(self.school)
        set_current_tenant(self.school)
        self.client.force_login(teacher)
        self._post('/attendance/reasons/', {'action': 'accept', 'mark_id': mark.id})
        mark.refresh_from_db()
        self.assertEqual(mark.excused, 'EXCUSED')

    def test_a_gate_scan_marks_the_register(self):
        from idcards.models import IDCard
        from idcards.services import record_gate_scan
        card = IDCard.objects.create(tenant=self.school, card_type='STUDENT', student=self.ada)
        with mock.patch('idcards.services._notify_parent'):
            record_gate_scan(card)
        self.assertEqual(Attendance.objects.get().status, 'P')

    def test_parents_cannot_reach_staff_pages(self):
        parent = User.objects.create_user(username='par9', password='x', tenant=self.school, is_parent=True)
        self.client.force_login(parent)
        for path in ('/attendance/settings/', '/attendance/reasons/', '/attendance/lateness/'):
            self.assertNotEqual(self._get(path).status_code, 200, path)

    def test_another_schools_parent_page_shows_nothing_of_ours(self):
        other = make_school('beta')
        theirs = make_student(other, 'STU001')
        parent = User.objects.create_user(username=f'parent_{theirs.id}', password='x', tenant=other,
                                          is_parent=True)
        self.client.force_login(parent)
        response = self.client.post('/parent/absences/', {'child_id': self.ada.id,
                                                          'date': self.today.isoformat(), 'reason': 'SICK'},
                                    HTTP_HOST=self.host(other))
        self.assertFalse(Attendance.objects.filter(student=self.ada).exists())
