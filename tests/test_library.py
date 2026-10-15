"""
Library: catalogue and ISBN lookup (F361), desk issue/return by barcode
(F362), reminders and fines (F363), reservations (F364), e-library and past
questions (F365, F376), reading logs and challenges (F366, F374), reports
(F367), stock-taking (F368), purchase requests (F369), barcode labels (F370),
catalogue search (F371), lost books (F372), clearance (F373), textbook
rental (F375), visits (F377), recommendations (F378), donations (F379) and
branches (F380).
"""
from datetime import date, timedelta
from decimal import Decimal as D
from unittest import mock

from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.utils import timezone

from academics.models import AcademicSession, Term
from finance.views import _get_family_wallet_for_student
from library import services as svc
from library.models import (Book, Branch, ChallengeBadge, Charge, Donation, EResource, Loan, PurchaseRequest,
                            ReadingChallenge, ReadingLog, Reservation, Visit)
from tenants.middleware import set_current_tenant
from users.models import Award, Staff, User

from .factories import TenantTestCase, make_admin, make_class, make_school, make_student, make_teacher


@override_settings(RUN_TASKS_EAGERLY=True)
class Base(TenantTestCase):

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha')
        self.admin = make_admin(self.school)
        self.admin.email = 'head@example.com'
        self.admin.save()
        self.lib_user = make_teacher(self.school, username='alphalib', full_name='Mrs Books')
        self.lib_user.email = 'lib@example.com'
        self.lib_user.save()
        Staff.objects.filter(user=self.lib_user).update(role='LIB')
        self.teacher = make_teacher(self.school, username='alphatch', full_name='Mr Teach')
        self.teacher.email = 'teach@example.com'
        self.teacher.save()
        self.jss1 = make_class(self.school, 'JSS 1A')
        self.jss1.level = 'JSS 1'
        self.jss1.save()
        self.ada = make_student(self.school, 'A/001', full_name='Ada Obi', school_class=self.jss1,
                                parent_email='mum@example.com')
        self.chi = make_student(self.school, 'A/003', full_name='Chi Eze', school_class=self.jss1,
                                parent_email='eze@example.com')
        set_current_tenant(self.school)
        self.book = Book.objects.create(tenant=self.school, title='Things Fall Apart', authors='Chinua Achebe',
                                        subject='Fiction', level='JSS', price=D('3000'))
        self.c1, self.c2 = svc.add_copies(self.book, 2)
        self.parent = User.objects.create_user(username=f'parent_{self.ada.id}', password='x', tenant=self.school,
                                               is_parent=True, email='mum@example.com')

    def _get(self, path, **kw):
        return self.client.get(path, HTTP_HOST=self.host(self.school), **kw)

    def _post(self, path, data=None, **kw):
        return self.client.post(path, data or {}, HTTP_HOST=self.host(self.school), **kw)

    def again(self):
        set_current_tenant(self.school)

    def backdate(self, loan, days):
        Loan.objects.filter(pk=loan.pk).update(due_on=timezone.localdate() - timedelta(days=days))
        loan.refresh_from_db()


class CatalogueTests(Base):

    def test_barcodes_and_isbn_check(self):
        self.assertEqual([self.c1.barcode, self.c2.barcode], ['LIB000001', 'LIB000002'])
        self.assertEqual(svc.next_barcode(self.school), 'LIB000003')
        self.assertTrue(svc.isbn_ok('978-0-385-47454-2'))
        self.assertTrue(svc.isbn_ok('0385474547'))
        self.assertFalse(svc.isbn_ok('9780385474543'))
        with self.assertRaises(svc.LibraryError):
            svc.add_copies(self.book, barcodes=['LIB000001'])

    @mock.patch('requests.get')
    def test_isbn_lookup(self, get):
        get.return_value = mock.Mock(status_code=200, json=lambda: {'ISBN:9780385474542': {
            'title': 'Things Fall Apart', 'authors': [{'name': 'Chinua Achebe'}],
            'publishers': [{'name': 'Anchor'}], 'publish_date': '1994', 'subjects': [{'name': 'Fiction'}],
            'cover': {'medium': 'https://covers.example/1.jpg'}}})
        data = svc.lookup_isbn('978-0385474542')
        self.assertEqual(data['authors'], 'Chinua Achebe')
        self.assertEqual(data['year'], 1994)
        self.client.force_login(self.lib_user)
        r = self._get('/library/catalogue/isbn/?isbn=9780385474542')
        self.assertTrue(r.json()['ok'])
        get.side_effect = OSError('offline')
        self.assertIsNone(svc.lookup_isbn('9780385474542'))

    def test_librarian_adds_book_with_copies(self):
        self.client.force_login(self.lib_user)
        self._post('/library/catalogue/', {'title': 'New Maths', 'subject': 'Mathematics', 'level': 'JSS',
                                           'copies': '3', 'price': '2500'})
        self.again()
        b = Book.objects.get(title='New Maths')
        self.assertEqual(b.copies.count(), 3)
        r = self._get(f'/library/labels/?book={b.pk}')
        self.assertContains(r, '<svg')
        self.assertContains(r, 'LIB000005')

    def test_code128(self):
        widths = svc.code128_widths('LIB000001')
        self.assertEqual(sum(map(int, widths)), 11 * (len('LIB000001') + 2) + 13)
        with self.assertRaises(svc.LibraryError):
            svc.code128_widths('é')

    def test_search_and_branches(self):
        junior = Branch.objects.create(tenant=self.school, name='Junior library')
        svc.add_copies(self.book, 1, branch=junior)
        found = svc.search(self.school, 'achebe')
        self.assertEqual(found.get().available, 3)
        self.assertEqual(svc.search(self.school, 'achebe', branch=junior).get().available, 1)
        self.assertEqual(svc.search(self.school, 'LIB000002').get(), self.book)
        self.client.force_login(self.ada.user)
        self.assertContains(self._get('/library/search/?q=fall'), 'Things Fall Apart')

    def test_desk_needs_librarian(self):
        self.client.force_login(self.teacher)
        self.assertEqual(self._get('/library/desk/').status_code, 302)
        self.client.force_login(self.lib_user)
        self.assertEqual(self._get('/library/desk/').status_code, 200)


class LoanTests(Base):

    def test_issue_and_return_at_desk(self):
        self.client.force_login(self.lib_user)
        self._post('/library/desk/', {'borrower': 'A/001', 'action': 'issue', 'barcode': 'LIB000001'})
        self.again()
        loan = Loan.objects.get(student=self.ada)
        self.c1.refresh_from_db()
        self.assertEqual(self.c1.status, 'OUT')
        self.assertEqual(loan.due_on, timezone.localdate() + timedelta(days=14))
        self._post('/library/desk/', {'action': 'return', 'barcode': 'LIB000001'})
        self.again()
        loan.refresh_from_db()
        self.c1.refresh_from_db()
        self.assertIsNotNone(loan.returned_at)
        self.assertEqual(self.c1.status, 'IN')
        self.assertFalse(Charge.objects.exists())

    def test_limits_and_blocks(self):
        svc.issue(self.c1, self.lib_user, self.ada)
        svc.issue(self.c2, self.lib_user, self.ada)
        extra = svc.add_copies(self.book, 1)[0]
        with self.assertRaises(svc.LibraryError):
            svc.issue(extra, self.lib_user, self.ada)          # limit 2
        with self.assertRaises(svc.LibraryError):
            svc.issue(self.c1, self.lib_user, self.chi)        # already out

    def test_late_return_fines_and_wallet(self):
        loan = svc.issue(self.c1, self.lib_user, self.ada)
        self.backdate(loan, 5)
        self.assertEqual(svc.fine_for(loan), D('100'))
        _loan, charges = svc.return_copy(self.c1, self.lib_user)
        self.assertEqual(charges[0].amount, D('100'))
        with self.assertRaises(svc.LibraryError):
            svc.issue(self.c2, self.lib_user, self.ada)        # owes
        wallet = _get_family_wallet_for_student(self.ada)
        wallet.credit(D('500'), 'Top up')
        self.client.force_login(self.parent)
        self.assertContains(self._get('/library/family/'), 'Late fine')
        self._post('/library/family/', {'id': charges[0].pk})
        self.again()
        charges[0].refresh_from_db()
        wallet.refresh_from_db()
        self.assertEqual(charges[0].status, 'PAID')
        self.assertEqual(wallet.balance, D('400'))
        svc.issue(self.c2, self.lib_user, self.ada)

    def test_fine_cap_and_grace(self):
        cfg = svc.settings_for(self.school)
        cfg.grace_days, cfg.fine_cap = 2, D('150')
        cfg.save()
        loan = svc.issue(self.c1, self.lib_user, self.ada)
        self.backdate(loan, 2)
        self.assertEqual(svc.fine_for(loan), D('0'))
        self.backdate(loan, 30)
        self.assertEqual(svc.fine_for(loan), D('150'))

    def test_renew(self):
        loan = svc.issue(self.c1, self.lib_user, self.ada)
        due = loan.due_on
        svc.renew(loan)
        self.assertEqual(loan.due_on, due + timedelta(days=14))
        with self.assertRaises(svc.LibraryError):
            svc.renew(loan)                                     # one renewal only

    def test_lost_book_charged(self):
        loan = svc.issue(self.c1, self.lib_user, self.ada)
        mail.outbox.clear()
        out = svc.mark_lost(loan, self.lib_user)
        self.assertEqual(out[0].kind, 'LOST')
        self.assertEqual(out[0].amount, D('3000'))
        self.c1.refresh_from_db()
        self.assertEqual(self.c1.status, 'LOST')
        self.assertTrue(any('lost' in m.subject for m in mail.outbox))
        svc.found_again(self.c1, self.lib_user)
        self.c1.refresh_from_db()
        self.assertEqual(self.c1.status, 'IN')

    def test_damaged_return_goes_to_repair(self):
        svc.issue(self.c1, self.lib_user, self.ada)
        _loan, charges = svc.return_copy(self.c1, self.lib_user, 'DAMAGED', D('800'), 'Torn cover')
        self.c1.refresh_from_db()
        self.assertEqual(self.c1.status, 'REPAIR')
        self.assertEqual(charges[0].kind, 'DAMAGE')

    def test_staff_borrow_by_username(self):
        st, staff = svc.resolve_borrower(self.school, 'alphatch')
        self.assertIsNone(st)
        loan = svc.issue(self.c1, self.lib_user, staff=staff)
        self.assertEqual(loan.borrower_name, 'Mr Teach')
        with self.assertRaises(svc.LibraryError):
            svc.issue(self.c2, self.lib_user, self.ada, staff)


class ReservationTests(Base):

    def test_reserve_hold_and_issue(self):
        with self.assertRaises(svc.LibraryError):
            svc.reserve(self.book, self.chi)                    # copies on the shelf
        svc.issue(self.c1, self.lib_user, self.ada)
        staff = Staff.objects.get(user=self.teacher)
        svc.issue(self.c2, self.lib_user, staff=staff)
        res = svc.reserve(self.book, self.chi)
        with self.assertRaises(svc.LibraryError):
            svc.reserve(self.book, self.chi)
        mail.outbox.clear()
        svc.return_copy(self.c1, self.lib_user)
        res.refresh_from_db()
        self.c1.refresh_from_db()
        self.assertEqual((res.status, self.c1.status), ('READY', 'HELD'))
        self.assertTrue(any('ready' in m.subject for m in mail.outbox))
        with self.assertRaises(svc.LibraryError):
            svc.issue(self.c1, self.lib_user, self.ada)         # held for Chi
        svc.issue(self.c1, self.lib_user, self.chi)
        res.refresh_from_db()
        self.assertEqual(res.status, 'DONE')

    def test_uncollected_hold_expires(self):
        svc.issue(self.c1, self.lib_user, self.ada)
        svc.issue(self.c2, self.lib_user, staff=Staff.objects.get(user=self.teacher))
        res = svc.reserve(self.book, self.chi)
        svc.return_copy(self.c1, self.lib_user)
        Reservation.objects.filter(pk=res.pk).update(hold_until=timezone.localdate() - timedelta(days=1))
        self.assertEqual(svc.expire_holds(self.school), 1)
        self.c1.refresh_from_db()
        self.assertEqual(self.c1.status, 'IN')

    def test_renew_refused_when_someone_waits(self):
        loan = svc.issue(self.c1, self.lib_user, self.ada)
        svc.issue(self.c2, self.lib_user, staff=Staff.objects.get(user=self.teacher))
        svc.reserve(self.book, self.chi)
        with self.assertRaises(svc.LibraryError):
            svc.renew(loan)

    def test_student_reserves_from_search(self):
        svc.issue(self.c1, self.lib_user, self.chi)
        svc.issue(self.c2, self.lib_user, staff=Staff.objects.get(user=self.teacher))
        self.client.force_login(self.ada.user)
        self._post(f'/library/search/{self.book.pk}/')
        self.again()
        self.assertTrue(Reservation.objects.filter(student=self.ada, book=self.book).exists())
        self.assertContains(self._get('/library/me/'), 'Things Fall Apart')


class ReminderTests(Base):

    def test_due_and_overdue_reminders_once(self):
        soon = svc.issue(self.c1, self.lib_user, self.ada)
        Loan.objects.filter(pk=soon.pk).update(due_on=timezone.localdate() + timedelta(days=1))
        late = svc.issue(self.c2, self.lib_user, self.chi)
        self.backdate(late, 3)
        mail.outbox.clear()
        with mock.patch('communications.services.send_sms', return_value=(True, 'ok')):
            counts = svc.send_reminders(self.school)
        self.assertEqual((counts['due'], counts['overdue']), (1, 1))
        self.assertTrue(any('overdue' in m.subject.lower() for m in mail.outbox))
        self.assertEqual(svc.send_reminders(self.school)['due'], 0)


class RentalTests(Base):

    def test_textbook_rental_for_term(self):
        session = AcademicSession.objects.create(tenant=self.school, name='2026/2027')
        term = Term.objects.create(tenant=self.school, session=session, name='1st Term', is_active=True,
                                   start_date=timezone.localdate() - timedelta(days=10),
                                   end_date=timezone.localdate() + timedelta(days=60))
        maths = Book.objects.create(tenant=self.school, title='New General Maths 1', is_textbook=True,
                                    rental_fee=D('1500'))
        copy = svc.add_copies(maths, 1)[0]
        with self.assertRaises(svc.LibraryError):
            svc.issue(self.c1, self.lib_user, self.ada, rental_term=term)   # not a textbook
        svc.issue(self.c1, self.lib_user, self.ada)
        svc.issue(self.c2, self.lib_user, self.ada)
        loan = svc.issue(copy, self.lib_user, self.ada, rental_term=term)    # rentals ignore the limit
        self.assertEqual(loan.due_on, term.end_date)
        self.assertEqual(Charge.objects.get(kind='RENTAL').amount, D('1500'))
        self.assertEqual(svc.fine_for(loan, term.end_date + timedelta(days=5)), D('0'))


class ReadingTests(Base):

    def test_log_approve_and_badge(self):
        ch = ReadingChallenge.objects.create(tenant=self.school, title='Readathon', target=2,
                                             starts=timezone.localdate() - timedelta(days=10),
                                             ends=timezone.localdate() + timedelta(days=10))
        ch.classes.add(self.jss1)
        self.client.force_login(self.ada.user)
        self._post('/library/me/', {'action': 'log', 'title': 'Book one', 'rating': '5'})
        self._post('/library/me/', {'action': 'log', 'title': 'Book two'})
        self.again()
        self.assertEqual(ReadingLog.objects.filter(student=self.ada, approved=False).count(), 2)
        self.client.force_login(self.lib_user)
        for e in ReadingLog.objects.filter(student=self.ada):
            self._post('/library/reading/', {'id': e.pk, 'action': 'approve'})
        self.again()
        self.assertTrue(ChallengeBadge.objects.filter(challenge=ch, student=self.ada).exists())
        self.assertTrue(Award.objects.filter(student=self.ada, title='Reading Champion').exists())
        self.assertEqual(svc.check_badges(self.ada), [])                     # only once
        self.assertEqual(svc.challenge_board(ch)[0], self.ada)

    def test_future_log_refused(self):
        with self.assertRaises(svc.LibraryError):
            svc.log_reading(self.ada, {'title': 'X', 'finished_on': (timezone.localdate() + timedelta(days=3))
                                       .isoformat()})

    def test_recommendations(self):
        self.assertEqual(svc.level_for(self.ada), 'JSS')
        Book.objects.create(tenant=self.school, title='Senior Chemistry', level='SSS')
        poem = Book.objects.create(tenant=self.school, title='Poems for Juniors', level='JSS', subject='Poetry')
        svc.add_copies(poem, 1)
        svc.log_reading(self.ada, {'title': 'Things Fall Apart', 'rating': '5'}, self.book)
        titles = [b.title for b in svc.recommendations(self.ada)]
        self.assertIn('Poems for Juniors', titles)
        self.assertNotIn('Senior Chemistry', titles)
        self.assertNotIn('Things Fall Apart', titles)


class StockTakeTests(Base):

    def test_count_and_close(self):
        extra = svc.add_copies(self.book, 1)[0]
        svc.issue(extra, self.lib_user, self.ada)
        st = svc.start_stocktake(self.school, self.lib_user)
        with self.assertRaises(svc.LibraryError):
            svc.start_stocktake(self.school, self.lib_user)
        svc.stock_scan(st, 'lib000001', self.lib_user)
        with self.assertRaises(svc.LibraryError):
            svc.stock_scan(st, 'LIB000001', self.lib_user)
        s = svc.close_stocktake(st, self.lib_user)
        self.assertEqual([c.barcode for c in s['missing']], ['LIB000002'])  # loaned copy not expected
        self.c2.refresh_from_db()
        self.assertEqual(self.c2.status, 'MISSING')
        st2 = svc.start_stocktake(self.school, self.lib_user)
        svc.stock_scan(st2, 'LIB000002', self.lib_user)
        self.c2.refresh_from_db()
        self.assertEqual(self.c2.status, 'IN')


class OtherTests(Base):

    def test_purchase_request_flow(self):
        self.client.force_login(self.teacher)
        mail.outbox.clear()
        self._post('/library/requests/', {'title': 'Oxford Atlas', 'reason': 'SS1 Geography', 'copies': '10'})
        self.again()
        pr = PurchaseRequest.objects.get()
        self.assertTrue(any('Book request' in m.subject for m in mail.outbox))
        self.client.force_login(self.lib_user)
        mail.outbox.clear()
        self._post('/library/requests/', {'action': 'decide', 'id': pr.pk, 'status': 'ORDERED'})
        self.again()
        pr.refresh_from_db()
        self.assertEqual(pr.status, 'ORDERED')
        self.assertEqual(mail.outbox[0].to, ['teach@example.com'])

    def test_donation_thank_you(self):
        d = Donation.objects.create(tenant=self.school, donor_name='Class of 2010', donor_email='old@example.com',
                                    date=date(2026, 9, 1))
        svc.add_copies(self.book, 3, donation=d)
        mail.outbox.clear()
        svc.acknowledge_donation(d)
        self.assertIn('Things Fall Apart', mail.outbox[0].body + str(mail.outbox[0].alternatives))
        d.refresh_from_db()
        self.assertIsNotNone(d.acknowledged_at)

    def test_eresources_access(self):
        self.client.force_login(self.lib_user)
        f = SimpleUploadedFile('maths.pdf', b'%PDF-1.4 test', content_type='application/pdf')
        self._post('/library/e-library/', {'title': 'WAEC Maths 2019', 'kind': 'PASTQ', 'exam': 'WAEC',
                                           'year': '2019', 'subject': 'Mathematics', 'file': f,
                                           'for_students': 'on'})      # ticked by default on the form
        self.again()
        r = EResource.objects.get()
        staff_only = EResource.objects.create(tenant=self.school, title='Teacher guide', for_students=False,
                                              file=SimpleUploadedFile('g.pdf', b'%PDF-1.4'))
        self.client.force_login(self.ada.user)
        self.assertContains(self._get('/library/e-library/?exam=WAEC'), 'WAEC Maths 2019')
        self.assertEqual(self._get(f'/library/e-library/{r.pk}/file/').status_code, 200)
        self.assertEqual(self._get(f'/library/e-library/{staff_only.pk}/file/').status_code, 404)
        self.again()
        r.refresh_from_db()
        self.assertEqual(r.downloads, 1)

    def test_visits(self):
        self.client.force_login(self.lib_user)
        self._post('/library/visits/', {'code': 'A/001', 'purpose': 'READ'})
        self._post('/library/visits/', {'school_class': self.jss1.pk, 'headcount': '25', 'purpose': 'LESSON'})
        self.again()
        self.assertEqual(Visit.objects.count(), 2)
        self.assertEqual(dict(svc.visit_stats(self.school)['by_class'])['JSS 1A'], 26)

    def test_clearance_blocks_withdrawal(self):
        svc.issue(self.c1, self.lib_user, self.ada)
        self.assertIn('not returned', svc.clearance_problem(self.ada))
        self.client.force_login(self.admin)
        self._post(f'/students/{self.ada.pk}/status/', {'status': 'Withdrawn', 'reason': 'Relocating'})
        self.again()
        self.ada.refresh_from_db()
        self.assertEqual(self.ada.status, 'Active')
        self.assertContains(self._get(f'/library/clearance/{self.ada.pk}/'), 'Not cleared')
        svc.return_copy(self.c1, self.lib_user)
        self.assertEqual(svc.clearance_problem(self.ada), '')

    def test_report(self):
        svc.issue(self.c1, self.lib_user, self.ada)
        r = svc.report(self.school)
        self.assertEqual(r['loans'], 1)
        self.assertEqual(r['top'][0], self.book)
        self.assertEqual(r['readers'][0], self.ada)
        self.client.force_login(self.lib_user)
        self.assertContains(self._get('/library/reports/'), 'Most borrowed')
