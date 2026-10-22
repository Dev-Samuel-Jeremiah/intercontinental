"""
Student experience: opt-in leaderboards (F284), e-portfolio (F286), careers
and subject combinations (F287, F288), handbook (F290), anonymous reports
(F291), wellbeing check-ins (F292), peer tutoring (F293), student voice
surveys (F295), goals (F296) and shared notes (F297).
"""
from datetime import timedelta

from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.utils import timezone

from communications import engage as eg
from communications.models import Survey
from studentlife import careers as cr
from studentlife import services as svc
from studentlife.models import (Checkin, Goal, HandbookAck, PortfolioItem, SafetyReport, SharedNote, TutorOffer,
                                TutorSession)
from tenants.middleware import set_current_tenant
from users.models import House, HousePoint, Staff, Subject, User

from .factories import TenantTestCase, make_admin, make_class, make_school, make_student, make_teacher


def pdf(name='work.pdf'):
    return SimpleUploadedFile(name, b'%PDF-1.4 test', content_type='application/pdf')


@override_settings(RUN_TASKS_EAGERLY=True)
class Base(TenantTestCase):

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha')
        self.admin = make_admin(self.school)
        self.admin.email = 'head@example.com'
        self.admin.save()
        self.t_user = make_teacher(self.school, username='alphat', full_name='Mrs Ade')
        self.t_user.email = 'ade@example.com'
        self.t_user.save()
        self.jss1 = make_class(self.school, 'JSS 1A')
        self.jss2 = make_class(self.school, 'JSS 2A')
        set_current_tenant(self.school)
        self.teacher = Staff.objects.get(user=self.t_user)
        self.jss1.level = 'JSS 1'
        self.jss1.save()
        self.jss1.form_teachers.set([self.teacher])
        self.jss2.level = 'JSS 2'
        self.jss2.save()
        self.ada = make_student(self.school, 'A/001', full_name='Ada Obi', school_class=self.jss1,
                                parent_email='mum@example.com')
        self.chi = make_student(self.school, 'A/003', full_name='Chi Eze', school_class=self.jss1,
                                parent_email='eze@example.com')
        self.ben = make_student(self.school, 'A/002', full_name='Ben Obi', school_class=self.jss2,
                                parent_email='other@example.com')
        set_current_tenant(self.school)
        self.maths = Subject.objects.create(tenant=self.school, name='Mathematics')

    def _get(self, path, **kw):
        return self.client.get(path, HTTP_HOST=self.host(self.school), **kw)

    def _post(self, path, data=None, **kw):
        return self.client.post(path, data or {}, HTTP_HOST=self.host(self.school), **kw)

    def again(self):
        set_current_tenant(self.school)


class LeaderboardTests(Base):

    def test_only_students_who_join_appear(self):
        house = House.objects.create(tenant=self.school, name='Blue')
        HousePoint.objects.create(tenant=self.school, house=house, points=10, reason='Quiz', student=self.ada)
        HousePoint.objects.create(tenant=self.school, house=house, points=30, reason='Sports', student=self.chi)
        self.assertEqual(svc.board(self.school, 'points'), [])
        self.client.force_login(self.ada.user)
        self._post('/life/leaderboards/', {'action': 'join', 'nickname': 'Ace'})
        self.again()
        rows = svc.board(self.school, 'points', level='JSS 1')
        self.assertEqual([(r['name'], r['value']) for r in rows], [('Ace', 10)])     # Chi did not join
        self.assertContains(self._get('/life/leaderboards/'), 'Ace')
        self.assertEqual(svc.board(self.school, 'points', level='JSS 2'), [])
        self._post('/life/leaderboards/', {'action': 'leave'})
        self.again()
        self.assertEqual(svc.board(self.school, 'points'), [])


class PortfolioTests(Base):

    def test_portfolio_sharing(self):
        self.client.force_login(self.ada.user)
        self._post('/life/portfolio/', {'action': 'add', 'title': 'Science fair volcano', 'kind': 'PROJECT',
                                        'description': 'Won 2nd place', 'share_with_family': '1', 'file': pdf()})
        self._post('/life/portfolio/', {'action': 'add', 'title': 'My diary poem', 'kind': 'ESSAY',
                                        'description': 'Private'})
        self.again()
        self.assertEqual(PortfolioItem.objects.count(), 2)
        item = PortfolioItem.objects.get(title='Science fair volcano')
        self.assertEqual(self._get(f'/life/portfolio/file/{item.pk}/').status_code, 200)
        parent = User.objects.create_user(username=f'parent_{self.ada.id}', password='x', tenant=self.school,
                                          is_parent=True, email='mum@example.com')
        self.client.force_login(parent)
        page = self._get(f'/life/portfolio/student/{self.ada.pk}/')
        self.assertContains(page, 'Science fair volcano')
        self.assertNotContains(page, 'My diary poem')
        self.client.force_login(self.chi.user)
        self.assertEqual(self._get(f'/life/portfolio/student/{self.ada.pk}/').status_code, 302)
        self.assertEqual(self._get(f'/life/portfolio/file/{item.pk}/').status_code, 404)
        self.client.force_login(self.t_user)
        self._post(f'/life/portfolio/student/{self.ada.pk}/', {'action': 'comment', 'id': item.pk,
                                                               'comment': 'Excellent work'})
        self.again()
        item.refresh_from_db()
        self.assertEqual((item.teacher_comment, item.commented_by), ('Excellent work', self.teacher))

    def test_needs_content(self):
        with self.assertRaises(svc.LifeError):
            svc.add_item(self.ada, {'title': 'Empty', 'kind': 'OTHER'})
        with self.assertRaises(svc.LifeError):
            svc.add_item(self.ada, {'title': 'Bad link', 'kind': 'OTHER', 'link': 'javascript:alert(1)'})


class CareerTests(Base):

    def test_combination_checker(self):
        med = cr.course('Medicine and Surgery')
        self.assertTrue(cr.check(med, ['English', 'Biology', 'Chemistry', 'Physics', 'Maths'])['ok'])
        res = cr.check(med, ['English Language', 'Biology', 'Chemistry', 'Economics'])
        self.assertEqual(res['missing'], ['Physics'])
        law = cr.course('Law')
        self.assertTrue(cr.check(law, ['English', 'Literature', 'Government', 'CRS'])['ok'])
        self.assertFalse(cr.check(law, ['English', 'Literature', 'Government'])['ok'])
        cs = cr.check(cr.course('Computer Science'), ['English', 'Mathematics', 'Physics'])
        self.assertEqual(len(cs['choose']), 1)
        self.assertEqual(cr.canonical('Agric'), cr.AGRIC)

    def test_quiz_and_page(self):
        self.client.force_login(self.ada.user)
        answers = {f'q{i}': '3' if cluster == 'HEALTH' else '1' for i, (_t, cluster) in enumerate(cr.QUIZ)}
        self._post('/life/careers/', {'action': 'quiz', **answers})
        self.again()
        self.assertEqual(self.ada.career.clusters[0], 'HEALTH')
        self._post('/life/careers/', {'action': 'choose', 'course': 'Pharmacy'})
        self.again()
        self.ada.career.refresh_from_db()
        self.assertEqual(self.ada.career.chosen, ['Pharmacy'])
        self.assertContains(self._get('/life/careers/'), 'Pharmacy')
        self.assertContains(self._get('/life/careers/combination/?course=Law&s=English&s=Literature'), 'Not yet')


class HandbookTests(Base):

    def test_acknowledge_and_new_version(self):
        self.client.force_login(self.ada.user)
        self.assertContains(self._get('/life/handbook/'), 'Behaviour and respect')
        self._post('/life/handbook/', {'action': 'ack'})
        self.again()
        self.assertTrue(svc.acknowledged(self.ada.user, self.school))
        rep = svc.ack_report(self.school)
        self.assertEqual((rep['done'], rep['total']), (1, 3))
        svc.new_version(self.school)
        self.assertFalse(svc.acknowledged(self.ada.user, self.school))
        self.assertEqual(HandbookAck.objects.count(), 1)


class SafetyReportTests(Base):

    def test_anonymous_report_and_follow_up(self):
        mail.outbox.clear()
        self.client.force_login(self.ada.user)
        page = self._post('/life/report/', {'kind': 'BULLYING', 'description': 'Older boys take my lunch money.',
                                            'where': 'Hostel', 'anonymous': '1'})
        self.again()
        r = SafetyReport.objects.get()
        self.assertIsNone(r.reporter)                               # really anonymous
        self.assertEqual(mail.outbox[0].to, ['head@example.com'])
        self.assertNotIn('lunch money', mail.outbox[0].body)        # details stay on the portal
        code = page.context['made']['code']
        self.assertContains(page, code)
        bad = self._post('/life/report/check/', {'reference': r.reference, 'code': 'XXXX-XXXX-XXXX'})
        self.assertNotContains(bad, 'lunch money')
        svc.report_reply(r, 'We are looking into this today.', user=self.admin, status='REVIEWING')
        svc.report_reply(r, 'Speak to hostel master', user=self.admin, internal=True)
        ok = self._post('/life/report/check/', {'reference': r.reference.lower(), 'code': code.lower()})
        self.assertContains(ok, 'We are looking into this today.')
        self.assertNotContains(ok, 'Speak to hostel master')
        self.client.force_login(self.t_user)
        self.assertEqual(self._get('/life/reports/').status_code, 302)   # not a safeguarding lead
        svc.settings_for(self.school).safeguarding_leads.add(self.teacher)
        self.assertContains(self._get(f'/life/reports/{r.pk}/'), 'lunch money')


class WellbeingTests(Base):

    def test_alerts_and_follow_up(self):
        mail.outbox.clear()
        self.client.force_login(self.ada.user)
        self._post('/life/checkin/', {'mood': '4', 'feelings': ['Happy']})
        self.again()
        self.assertFalse(Checkin.objects.get().flagged)
        self.assertEqual(len(mail.outbox), 0)
        self._post('/life/checkin/', {'mood': '2', 'wants_talk': '1', 'feelings': ['Worried']})
        self.again()
        c = Checkin.objects.get()
        self.assertTrue(c.flagged)
        self.assertEqual(sorted(m.to[0] for m in mail.outbox), ['ade@example.com', 'head@example.com'])
        self.client.force_login(self.t_user)
        self.assertContains(self._get('/life/wellbeing/'), 'Ada Obi')
        self._post('/life/wellbeing/', {'id': c.pk})
        self.again()
        c.refresh_from_db()
        self.assertEqual(c.followed_up_by, self.teacher)

    def test_three_low_days_flag(self):
        today = timezone.localdate()
        for i in (2, 1):
            Checkin.objects.create(tenant=self.school, student=self.chi, date=today - timedelta(days=i), mood=2)
        c = svc.check_in(self.chi, {'mood': '2'})
        self.assertTrue(c.flagged)


class TutoringTests(Base):

    def test_offer_approve_request_and_rate(self):
        self.client.force_login(self.chi.user)
        self._post('/life/tutoring/', {'action': 'offer', 'subject': self.maths.pk, 'about': 'Fractions'})
        self.again()
        offer = TutorOffer.objects.get()
        self.assertFalse(svc.tutors(self.school).exists())          # not yet approved
        self.client.force_login(self.t_user)
        self._post('/life/tutoring/manage/', {'id': offer.pk, 'action': 'approve'})
        self.again()
        self.client.force_login(self.ada.user)
        self.assertContains(self._get('/life/tutoring/'), 'Chi Eze')
        self._post('/life/tutoring/', {'action': 'request', 'id': offer.pk, 'topic': 'Adding fractions',
                                       'proposed': 'Break, Tuesday'})
        self.again()
        s = TutorSession.objects.get()
        with self.assertRaises(svc.LifeError):
            svc.update_session(s, self.ada, 'accept')                # only the tutor accepts
        svc.update_session(s, self.chi, 'accept')
        svc.update_session(s, self.ada, 'done')
        svc.update_session(s, self.ada, 'rate', '5')
        s.refresh_from_db()
        self.assertEqual((s.status, s.rating), ('DONE', 5))
        with self.assertRaises(svc.LifeError):
            svc.request_session(self.chi, offer, 'Self', '')


class GoalAndNoteTests(Base):

    def test_goal_reaches_100(self):
        self.client.force_login(self.ada.user)
        self._post('/life/goals/', {'action': 'add', 'title': 'Read 5 novels', 'category': 'SKILL'})
        self.again()
        g = Goal.objects.get()
        self._post('/life/goals/', {'action': 'progress', 'id': g.pk, 'progress': '100', 'note': 'Finished!'})
        self.again()
        g.refresh_from_db()
        self.assertEqual(g.status, 'ACHIEVED')
        self.assertIsNotNone(g.achieved_at)
        self.client.force_login(self.t_user)
        self._post('/life/goals/class/', {'id': g.pk, 'comment': 'Well done!'})
        self.again()
        g.refresh_from_db()
        self.assertEqual(g.teacher_comment, 'Well done!')

    def test_notes_are_checked_and_stay_in_the_year_group(self):
        n = svc.share_note(self.ada, self.maths, 'Fractions summary', 'Add numerators when denominators match.')
        self.assertEqual(n.status, 'PENDING')
        self.assertNotIn(n, list(svc.notes_for(self.chi)))
        self.assertIn(n, list(svc.notes_for(self.ada)))             # the author sees their own
        self.client.force_login(self.t_user)
        self._post('/life/notes/review/', {'id': n.pk, 'action': 'approve'})
        self.again()
        self.assertIn(n.pk, [x.pk for x in svc.notes_for(self.chi)])
        self.assertEqual(list(svc.notes_for(self.ben)), [])          # JSS 2 does not see JSS 1 notes
        self.assertTrue(svc.like(SharedNote.objects.get(pk=n.pk), self.chi))
        self.assertFalse(svc.like(SharedNote.objects.get(pk=n.pk), self.chi))   # second tap removes it
        for _ in range(3):
            svc.report_note(SharedNote.objects.get(pk=n.pk))
        self.assertEqual(SharedNote.objects.get(pk=n.pk).status, 'HIDDEN')


class StudentVoiceTests(Base):

    def test_students_answer_student_surveys(self):
        s = eg.create_survey(self.school, self.admin, 'Canteen menu', '', Survey.STUDENTS, True,
                             timezone.localdate() + timedelta(days=3), 'Favourite lunch? | Jollof | Beans | Yam')
        parent = User.objects.create_user(username=f'parent_{self.ada.id}', password='x', tenant=self.school,
                                          is_parent=True, email='mum@example.com')
        self.assertTrue(eg.may_answer(s, self.ada.user))
        self.assertFalse(eg.may_answer(s, parent))
        self.assertFalse(eg.may_answer(s, self.t_user))
        self.client.force_login(self.ada.user)
        self.assertContains(self._get('/communications/surveys/'), 'Canteen menu')
        self._post(f'/communications/surveys/{s.pk}/', {'q0': 'Jollof'})
        self.again()
        self.assertEqual(s.responses.count(), 1)
