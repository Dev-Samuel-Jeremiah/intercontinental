"""
Editing a term: an administrator corrects the dates, the school days are counted again,
impossible or overlapping dates are refused, and nobody else can do it.
"""
from datetime import date

from academics.models import AcademicSession, Term
from security.models import AuditEvent
from tenants.middleware import set_current_tenant

from .factories import TenantTestCase, make_admin, make_school, make_teacher


class EditTermTests(TenantTestCase):

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha')
        self.admin = make_admin(self.school)
        set_current_tenant(self.school)
        self.session = AcademicSession.objects.create(tenant=self.school, name='2026/2027', is_active=True,
                                                      start_date=date(2026, 9, 1), end_date=date(2027, 7, 31))
        self.first = Term.objects.create(tenant=self.school, session=self.session, name='1st Term', is_active=True,
                                         start_date=date(2026, 9, 7), end_date=date(2026, 12, 18))
        self.second = Term.objects.create(tenant=self.school, session=self.session, name='2nd Term',
                                          start_date=date(2027, 1, 11), end_date=date(2027, 4, 9))

    def post(self, term, user=None, **data):
        self.client.force_login(user or self.admin)
        return self.client.post(f'/academics/terms/{term.pk}/edit/', data, HTTP_HOST=self.host(self.school), follow=True)

    def test_the_terms_page_links_to_the_edit_page(self):
        self.client.force_login(self.admin)
        host = self.host(self.school)
        page = self.client.get(f'/academics/sessions/{self.session.pk}/terms/', HTTP_HOST=host)
        self.assertContains(page, f'/academics/terms/{self.second.pk}/edit/')
        edit = self.client.get(f'/academics/terms/{self.second.pk}/edit/', HTTP_HOST=host)
        self.assertContains(edit, 'value="2027-01-11"')

    def test_correcting_the_dates(self):
        page = self.post(self.second, start_date='2027-01-04', end_date='2027-04-02',
                         resumption_date_next_term='2027-04-26')
        self.assertContains(page, '2nd Term updated')
        self.second.refresh_from_db()
        self.assertEqual((self.second.start_date, self.second.end_date, self.second.resumption_date_next_term),
                         (date(2027, 1, 4), date(2027, 4, 2), date(2027, 4, 26)))
        self.assertEqual(self.second.number_of_school_days, self.second.calculate_school_days())
        self.assertTrue(AuditEvent.objects.filter(action='UPDATED', summary__contains='Term corrected').exists())

    def test_bad_dates_are_refused(self):
        cases = [
            ({'start_date': '2027-04-09', 'end_date': '2027-01-11'}, 'must start before it ends'),
            ({'start_date': '2027-01-11', 'end_date': '2027-04-09', 'resumption_date_next_term': '2027-03-01'},
             'resumption date must be after'),
            ({'start_date': '2026-12-01', 'end_date': '2027-04-09'}, 'overlap 1st Term'),
            ({'start_date': '', 'end_date': '2027-04-09'}, 'required'),
        ]
        for data, error in cases:
            self.assertContains(self.post(self.second, **data), error)
        self.second.refresh_from_db()
        self.assertEqual(self.second.start_date, date(2027, 1, 11))

    def test_making_it_the_active_term(self):
        self.post(self.second, start_date='2027-01-11', end_date='2027-04-09', is_active='on')
        self.first.refresh_from_db()
        self.second.refresh_from_db()
        self.assertTrue(self.second.is_active)
        self.assertFalse(self.first.is_active)

    def test_only_administrators_and_only_their_school(self):
        teacher = make_teacher(self.school)
        self.post(self.second, user=teacher, start_date='2027-01-01', end_date='2027-04-09')
        self.second.refresh_from_db()
        self.assertEqual(self.second.start_date, date(2027, 1, 11))
        other = make_school('beta')
        set_current_tenant(other)
        theirs = Term.objects.create(tenant=other, name='1st Term',
                                     session=AcademicSession.objects.create(tenant=other, name='2026/2027'),
                                     start_date=date(2026, 9, 7), end_date=date(2026, 12, 18))
        self.client.force_login(self.admin)
        resp = self.client.post(f'/academics/terms/{theirs.pk}/edit/', {'start_date': '2026-09-01', 'end_date': '2026-12-01'},
                                HTTP_HOST=self.host(self.school))
        self.assertEqual(resp.status_code, 404)
