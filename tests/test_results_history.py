"""
Whole-class report card downloads, progress across terms, families asking
for a score to be checked, and transcripts.
"""
import io
import zipfile
from datetime import timedelta
from unittest import mock

from django.core import mail
from django.test import override_settings
from django.utils import timezone

from academics.models import AcademicSession, Term
from results import history
from results.models import ReportBundle, ResultQuery, TermResult
from tenants.middleware import set_current_tenant
from users.models import ClassSubject, Staff, StaffSubjectClass, Subject, User

from .factories import TenantTestCase, make_admin, make_class, make_school, make_student, make_teacher


class Base(TenantTestCase):

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha')
        self.admin = make_admin(self.school)
        self.teacher_user = make_teacher(self.school)
        self.jss1 = make_class(self.school, 'JSS 1A')
        set_current_tenant(self.school)
        self.teacher = Staff.objects.get(user=self.teacher_user)
        today = timezone.localdate()
        self.session = AcademicSession.objects.create(tenant=self.school, name='2025/2026',
                                                      start_date=today - timedelta(days=300), is_active=True)
        self.terms = [Term.objects.create(tenant=self.school, name=n, session=self.session,
                                          start_date=today - timedelta(days=300 - 100 * i),
                                          end_date=today - timedelta(days=220 - 100 * i))
                      for i, n in enumerate(['1st Term', '2nd Term', '3rd Term'])]
        self.maths = Subject.objects.create(tenant=self.school, name='Mathematics')
        self.english = Subject.objects.create(tenant=self.school, name='English')
        for s in (self.maths, self.english):
            ClassSubject.objects.create(tenant=self.school, school_class=self.jss1, subject=s)
        StaffSubjectClass.objects.create(tenant=self.school, staff=self.teacher, subject=self.maths, school_class=self.jss1)
        self.ada = make_student(self.school, 'S1', full_name='Ada Bello', school_class=self.jss1,
                                parent_email='mum@example.com')
        self.bola = make_student(self.school, 'S2', full_name='Bola Ade', school_class=self.jss1,
                                 parent_email='dad@example.com')
        set_current_tenant(self.school)

    def _get(self, path, **kw):
        return self.client.get(path, HTTP_HOST=self.host(self.school), **kw)

    def _post(self, path, data=None, **kw):
        return self.client.post(path, data or {}, HTTP_HOST=self.host(self.school), **kw)

    def _score(self, student, subject, term, total, published=True):
        return TermResult.objects.create(tenant=self.school, student=student, class_assigned=self.jss1, subject=subject,
                                         session=self.session, term=term, ca1_score=min(total, 30),
                                         exam_score=max(0, total - 30), published=published)

    def _parent(self, student):
        return User.objects.create_user(username=f'parent_{student.id}', password='x', tenant=self.school,
                                        is_parent=True, email=student.parent_email)


@override_settings(RUN_TASKS_EAGERLY=True)
class BundleTests(Base):

    def test_zip_of_report_cards(self):
        self._score(self.ada, self.maths, self.terms[0], 70)
        self._score(self.bola, self.maths, self.terms[0], 60, published=False)
        with mock.patch('results.history.html_to_pdf', side_effect=lambda html: b'%PDF-fake ' + html[:20].encode()):
            b = history.request_bundle(self.jss1, self.terms[0], self.admin)
        b.refresh_from_db()
        self.assertEqual((b.status, b.count), ('DONE', 2))
        names = zipfile.ZipFile(io.BytesIO(b.file.read())).namelist()
        self.assertEqual(sorted(names), ['ada-bello-s1.pdf', 'bola-ade-s2.pdf'])
        with mock.patch('results.history.html_to_pdf', return_value=b'%PDF'):
            b2 = history.request_bundle(self.jss1, self.terms[0], self.admin, published_only=True)
        b2.refresh_from_db()
        self.assertEqual(b2.count, 1)

    def test_failure_is_reported(self):
        self._score(self.ada, self.maths, self.terms[0], 70)
        with mock.patch('results.history.html_to_pdf', side_effect=OSError('wkhtmltopdf missing')):
            b = history.request_bundle(self.jss1, self.terms[0], self.admin)
        b.refresh_from_db()
        self.assertEqual(b.status, 'FAILED')
        self.assertIn('wkhtmltopdf', b.error)

    def test_no_results_no_bundle(self):
        with self.assertRaises(history.HistoryError):
            history.request_bundle(self.jss1, self.terms[0], self.admin)

    def test_real_pdf(self):
        self._score(self.ada, self.maths, self.terms[0], 70)
        b = history.request_bundle(self.jss1, self.terms[0], self.admin)
        b.refresh_from_db()
        self.assertEqual(b.status, 'DONE', b.error)
        pdf = zipfile.ZipFile(io.BytesIO(b.file.read())).read('ada-bello-s1.pdf')
        self.assertTrue(pdf.startswith(b'%PDF'))

    def test_pages(self):
        self._score(self.ada, self.maths, self.terms[0], 70)
        self.client.force_login(self.admin)
        with mock.patch('results.history.html_to_pdf', return_value=b'%PDF'):
            self._post('/results/bundles/', {'class': self.jss1.id, 'term': self.terms[0].id})
        b = ReportBundle.objects.get()
        self.assertContains(self._get('/results/bundles/'), '1 report card')
        r = self._get(f'/results/bundles/{b.id}/download/')
        self.assertEqual(r['Content-Type'], 'application/zip')
        self.client.force_login(self.teacher_user)
        self.assertEqual(self._get(f'/results/bundles/{b.id}/download/').status_code, 404)


class ProgressTests(Base):

    def test_series_and_change(self):
        for term, m, e in ((self.terms[0], 50, 60), (self.terms[1], 70, 55)):
            self._score(self.ada, self.maths, term, m)
            self._score(self.ada, self.english, term, e)
        self._score(self.ada, self.maths, self.terms[2], 90, published=False)
        data = history.progress(self.ada)
        self.assertEqual([t['average'] for t in data['terms']], [55, 62.5])
        maths = next(s for s in data['subjects'] if s['subject'] == self.maths)
        self.assertEqual((maths['series'], maths['change']), ([50, 70], 20))
        self.assertEqual(len(history.progress(self.ada, published_only=False)['terms']), 3)
        self.assertEqual(history.chart_points([0, 100], 100, 100, 0), '0.0,100.0 100.0,0.0')

    def test_who_sees_it(self):
        self._score(self.ada, self.maths, self.terms[0], 50)
        self._score(self.ada, self.maths, self.terms[1], 80, published=False)
        self.client.force_login(self._parent(self.ada))
        r = self._get(f'/results/progress/{self.ada.id}/')
        self.assertContains(r, 'Mathematics')
        self.assertNotContains(r, '2nd Term')                   # unpublished
        self.assertEqual(self._get(f'/results/progress/{self.bola.id}/').status_code, 302)
        self.client.force_login(self.teacher_user)
        self.assertContains(self._get(f'/results/progress/{self.ada.id}/'), '2nd Term')


@override_settings(RUN_TASKS_EAGERLY=True)
class QueryTests(Base):

    def setUp(self):
        super().setUp()
        self.tr = self._score(self.ada, self.maths, self.terms[2], 55)

    def test_raise_rules(self):
        with self.assertRaises(history.HistoryError):
            history.raise_query(self.ada, self.terms[2], self.english, None, 'Score looks wrong to us')  # no result
        with self.assertRaises(history.HistoryError):
            history.raise_query(self.ada, self.terms[2], self.maths, None, 'wrong')                       # too short
        q = history.raise_query(self.ada, self.terms[2], self.maths, None, 'Her test script said 18/20.')
        self.assertEqual(q.score_when_raised, 55)
        with self.assertRaises(history.HistoryError):
            history.raise_query(self.ada, self.terms[2], self.maths, None, 'Asking again about this')

    def test_answer_and_correct(self):
        q = history.raise_query(self.ada, self.terms[2], self.maths, None, 'Her test script said 18/20.')
        other = make_teacher(self.school, username='alphao', full_name='Other')
        set_current_tenant(self.school)
        with self.assertRaises(history.HistoryError):
            history.answer(q, other, 'Checked')                      # doesn't teach it
        with self.assertRaises(history.HistoryError):
            history.answer(q, self.teacher_user, 'Fixed', corrected=True)   # score not actually changed
        self.tr.exam_score += 5
        self.tr.save()
        history.answer(q, self.teacher_user, 'You were right — a test was missed.', corrected=True)
        q.refresh_from_db()
        self.assertEqual((q.status, q.score_after), ('CORRECTED', 55 + 5))
        self.assertIn('corrected from 55 to 60', mail.outbox[-1].body)
        with self.assertRaises(history.HistoryError):
            history.answer(q, self.teacher_user, 'again')

    def test_pages(self):
        parent = self._parent(self.ada)
        self.client.force_login(parent)
        url = f'/results/ask/{self.ada.id}/{self.terms[2].id}/'
        self.assertContains(self._get(url), 'Mathematics')
        self._post(url, {'subject': self.maths.id, 'message': 'The exam score should be higher.'})
        self.assertEqual(ResultQuery.objects.count(), 1)
        self.assertEqual(self._get(f'/results/ask/{self.bola.id}/{self.terms[2].id}/').status_code, 302)
        self.client.force_login(self.teacher_user)
        self.assertContains(self._get('/results/questions/'), 'The exam score should be higher.')
        q = ResultQuery.objects.get()
        self._post('/results/questions/', {'id': q.id, 'response': 'Checked the script, it is correct.', 'corrected': '0'})
        q.refresh_from_db()
        self.assertEqual(q.status, 'ANSWERED')
        self.client.force_login(parent)
        self.assertContains(self._get(url), 'Checked the script')


class TranscriptTests(Base):

    def test_transcript_and_verification(self):
        for term, m in zip(self.terms, (50, 60, 70)):
            self._score(self.ada, self.maths, term, m)
            self._score(self.ada, self.english, term, m + 10)
        t = history.transcript(self.ada)
        self.assertEqual(len(t['sessions']), 1)
        self.assertEqual([x['average'] for x in t['sessions'][0]['terms']], [55, 65, 75])
        self.assertEqual((t['sessions'][0]['average'], t['overall']), (65, 65))
        ref = history.transcript_reference(self.ada)
        v = history.verify_transcript(ref, self.school)
        self.assertFalse(v['changed'])
        self._score(self.ada, self.english, Term.objects.create(tenant=self.school, name='1st Term',
                                                                session=AcademicSession.objects.create(
                                                                    tenant=self.school, name='2026/2027'),
                                                                start_date=timezone.localdate()), 80)
        self.assertTrue(history.verify_transcript(ref, self.school)['changed'])
        self.assertIsNone(history.verify_transcript(ref + 'x', self.school))

    def test_pages(self):
        self._score(self.ada, self.maths, self.terms[0], 50)
        self.client.force_login(self.admin)
        r = self._get(f'/results/transcript/{self.ada.id}/?purpose=University application')
        self.assertContains(r, 'Academic Transcript')
        self.assertContains(r, 'University application')
        ref = history.transcript_reference(self.ada)
        self.client.logout()
        self.assertContains(self._get(f'/results/transcript/verify/{ref}/'), 'verified')
        self.client.force_login(self.teacher_user)
        self.assertEqual(self._get(f'/results/transcript/{self.ada.id}/').status_code, 302)
