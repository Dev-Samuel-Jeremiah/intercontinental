"""
School events with replies and reminders (F151), and after-school lessons
billed separately from school fees (F156).
"""
from datetime import time, timedelta
from decimal import Decimal

from django.core import mail
from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone

from curriculum import events
from curriculum.models import CalendarEvent, EventRSVP
from finance.models import Transaction, Wallet
from tenants.middleware import set_current_tenant
from timetable import extras
from timetable.models import ExtraLessonEnrolment, ExtraLessonGroup, TeachingRoom
from users.models import Staff, User

from .factories import TenantTestCase, make_admin, make_class, make_school, make_student, make_teacher


@override_settings(RUN_TASKS_EAGERLY=True)
class Base(TenantTestCase):

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha')
        self.admin = make_admin(self.school)
        self.admin.email = 'head@alpha.example.com'
        self.admin.save()
        self.teacher_user = make_teacher(self.school, username='alphat', full_name='Mr Okon')
        self.teacher_user.email = 'okon@example.com'
        self.teacher_user.save()
        self.jss1 = make_class(self.school, 'JSS 1A')
        self.ss3 = make_class(self.school, 'SS 3A')
        set_current_tenant(self.school)
        self.teacher = Staff.objects.get(user=self.teacher_user)
        self.today = timezone.localdate()
        self.ada = make_student(self.school, 'A/001', full_name='Ada Obi', school_class=self.ss3,
                                parent_email='mum@example.com')
        self.ben = make_student(self.school, 'A/002', full_name='Ben Obi', school_class=self.jss1,
                                parent_email='mum@example.com')
        self.parent = User.objects.create_user(username=f'parent_{self.ada.id}', password='x', tenant=self.school,
                                               is_parent=True, email='mum@example.com')

    def _get(self, path, **kw):
        return self.client.get(path, HTTP_HOST=self.host(self.school), **kw)

    def _post(self, path, data=None, **kw):
        return self.client.post(path, data or {}, HTTP_HOST=self.host(self.school), **kw)


class EventTests(Base):

    def _event(self, **kw):
        kw.setdefault('start_date', self.today + timedelta(days=5))
        return CalendarEvent.objects.create(tenant=self.school, title='Prize-giving day', kind='CULTURAL', **kw)

    def test_replies_count_guests_and_respect_capacity(self):
        ev = self._event(rsvp=True, capacity=4)
        events.reply(ev, self.parent, 'YES', guests=2)
        with self.assertRaises(events.EventError) as raised:
            events.reply(ev, self.teacher_user, 'YES', guests=1)
        self.assertIn('Only 1 place left', str(raised.exception))
        events.reply(ev, self.teacher_user, 'YES')
        self.assertEqual(events.summary(ev)['headcount'], 4)
        # Changing your own reply doesn't count you twice.
        events.reply(ev, self.parent, 'YES', guests=2, note='Two grandparents')
        self.assertEqual(EventRSVP.objects.count(), 2)
        events.reply(ev, self.parent, 'NO', guests=5)
        self.assertEqual(EventRSVP.objects.get(user=self.parent).guests, 0)

    def test_replies_close_after_the_deadline_and_staff_events_are_hidden(self):
        ev = self._event(rsvp=True, rsvp_deadline=self.today - timedelta(days=1))
        with self.assertRaises(events.EventError):
            events.reply(ev, self.parent, 'YES')
        staff_only = self._event(rsvp=True, audience='STAFF')
        with self.assertRaises(events.EventError):
            events.reply(staff_only, self.parent, 'YES')
        self.client.force_login(self.parent)
        self.assertEqual(self._get(f'/curriculum/events/{staff_only.pk}/').status_code, 404)

    def test_parent_replies_on_the_page(self):
        ev = self._event(rsvp=True, venue='School hall', start_time=time(10, 0))
        self.client.force_login(self.parent)
        page = self._get(f'/curriculum/events/{ev.pk}/')
        self.assertContains(page, 'School hall')
        self.assertContains(page, 'Are you coming?')
        self._post(f'/curriculum/events/{ev.pk}/', {'answer': 'MAYBE', 'note': 'Travelling'})
        self.assertEqual(EventRSVP.objects.get().answer, 'MAYBE')

    def test_reminders_skip_people_not_coming_and_go_once(self):
        ev = self._event(rsvp=True, reminder_days=7, audience='ALL')
        events.reply(ev, self.teacher_user, 'NO')
        sent = events.send_reminder(ev)
        to = sorted(m.to[0] for m in mail.outbox)
        self.assertIn('mum@example.com', to)
        self.assertNotIn('okon@example.com', to)
        self.assertEqual(sent, len(to))
        mail.outbox.clear()
        self.assertEqual(events.due_reminders(self.school), [])

    def test_daily_command_sends_only_due_reminders(self):
        due = self._event(reminder_days=5)
        later = self._event(reminder_days=2, start_date=self.today + timedelta(days=20))
        call_command('send_event_reminders', stdout=open('/dev/null', 'w'))
        due.refresh_from_db()
        later.refresh_from_db()
        self.assertIsNotNone(due.reminded_at)
        self.assertIsNone(later.reminded_at)
        self.assertTrue(mail.outbox)

    def test_admin_adds_an_event_with_replies(self):
        self.client.force_login(self.admin)
        r = self._post('/curriculum/calendar/', {
            'title': 'Inter-house sports', 'kind': 'SPORTS', 'audience': 'ALL',
            'start_date': (self.today + timedelta(days=10)).isoformat(), 'start_time': '09:00', 'end_time': '15:00',
            'venue': 'Field', 'rsvp': '1', 'capacity': '300', 'reminder_days': '2'})
        ev = CalendarEvent.objects.get(title='Inter-house sports')
        self.assertRedirects(r, f'/curriculum/events/{ev.pk}/', fetch_redirect_response=False)
        self.assertEqual((ev.venue, ev.capacity, ev.reminder_days, ev.rsvp), ('Field', 300, 2, True))
        self.assertEqual(ev.created_by, self.admin)
        events.reply(ev, self.parent, 'YES', guests=1)
        self.assertContains(self._get(f'/curriculum/events/{ev.pk}/'), '2 coming')
        csv = self._post(f'/curriculum/events/{ev.pk}/', {'action': 'csv'})
        self.assertIn('mum@example.com', csv.content.decode())


class ExtraLessonTests(Base):

    def setUp(self):
        super().setUp()
        self.lab = TeachingRoom.objects.create(tenant=self.school, name='Lab 1')
        self.group = ExtraLessonGroup.objects.create(
            tenant=self.school, name='WAEC Maths', teacher=self.teacher, day='TUE', start_time=time(14, 30),
            end_time=time(16, 0), room=self.lab, fee=Decimal('15000'))
        self.group.classes.set([self.ss3])
        self.wallet = Wallet.objects.create(tenant=self.school, student=self.ada, balance=Decimal('20000'))

    def test_clashes_are_refused(self):
        other = ExtraLessonGroup(tenant=self.school)
        other.teacher = self.teacher
        with self.assertRaises(extras.ExtraError) as raised:
            extras.save_group(other, {'name': 'Physics', 'day': 'TUE', 'start': '15:00', 'end': '16:30', 'fee': '0'})
        self.assertIn('Mr Okon also runs WAEC Maths', str(raised.exception))
        extras.save_group(other, {'name': 'Physics', 'day': 'TUE', 'start': '16:00', 'end': '17:00', 'fee': '0'})
        self.assertTrue(other.pk)

    def test_only_listed_classes_join_and_capacity_holds(self):
        with self.assertRaises(extras.ExtraError):
            extras.enrol(self.group, self.ben)
        self.group.capacity = 1
        self.group.save()
        extras.enrol(self.group, self.ada)
        other = make_student(self.school, 'A/003', school_class=self.ss3)
        with self.assertRaises(extras.ExtraError):
            extras.enrol(self.group, other)

    def test_wallet_payment_and_refund(self):
        e = extras.enrol(self.group, self.ada)
        self.assertEqual(e.status, 'DUE')
        extras.pay_from_wallet(e)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('5000'))
        self.assertEqual(e.status, 'PAID')
        refund = extras.withdraw(e)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('20000'))
        self.assertEqual(refund.category, 'REFUND')
        self.assertFalse(ExtraLessonEnrolment.objects.exists())

    def test_not_enough_in_the_wallet(self):
        self.wallet.balance = Decimal('100')
        self.wallet.save()
        e = extras.enrol(self.group, self.ada)
        with self.assertRaises(extras.ExtraError):
            extras.pay_from_wallet(e)
        e.refresh_from_db()
        self.assertEqual(e.status, 'DUE')
        self.assertFalse(Transaction.objects.exists())

    def test_free_group_needs_no_payment_and_totals(self):
        e = extras.enrol(self.group, self.ada)
        extras.mark_paid(e, 'Cash, receipt 44')
        self.assertEqual(extras.totals(self.group)['paid'], Decimal('15000'))
        free = ExtraLessonGroup.objects.create(tenant=self.school, name='Chess', day='FRI', start_time=time(14, 0),
                                               end_time=time(15, 0))
        self.assertEqual(extras.enrol(free, self.ben).status, 'WAIVED')

    def test_parent_joins_and_pays_on_the_portal(self):
        self.client.force_login(self.parent)
        page = self._get('/timetable/extras/family/')
        self.assertContains(page, 'WAEC Maths')
        self._post('/timetable/extras/family/', {'action': 'join', 'group': self.group.pk, 'child': self.ada.pk})
        e = ExtraLessonEnrolment.objects.get()
        self._post('/timetable/extras/family/', {'action': 'join', 'group': self.group.pk, 'child': self.ben.pk})
        self.assertEqual(ExtraLessonEnrolment.objects.count(), 1)          # Ben's class isn't listed
        self._post('/timetable/extras/family/', {'action': 'pay', 'id': e.pk})
        e.refresh_from_db()
        self.assertEqual(e.status, 'PAID')

    def test_staff_pages(self):
        self.client.force_login(self.admin)
        self._post('/timetable/extras/', {'name': 'Coding club', 'day': 'WED', 'start': '14:00', 'end': '15:30',
                                          'fee': '5,000', 'open_to_parents': '1', 'classes': [self.jss1.pk]})
        g = ExtraLessonGroup.objects.get(name='Coding club')
        self.assertEqual(g.fee, Decimal('5000.00'))
        self._post(f'/timetable/extras/{g.pk}/', {'action': 'enrol', 'students': [self.ben.pk, self.ada.pk]})
        self.assertEqual(list(g.enrolments.values_list('student__full_name', flat=True)), ['Ben Obi'])
        self.assertContains(self._get(f'/timetable/extras/{g.pk}/'), 'Ben Obi')
        self.client.force_login(self.teacher_user)
        self.assertEqual(self._get(f'/timetable/extras/{g.pk}/').status_code, 302)    # not their group
        self.assertEqual(self._get(f'/timetable/extras/{self.group.pk}/').status_code, 200)
