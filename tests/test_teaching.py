"""
Teacher productivity: gradebook (F304), behaviour points (F305), comment
bank and report remarks (F308), marking queue (F307), resources (F309),
tasks (F311), duty roster (F312), handover notes (F313), the class
dashboard (F303) and the name picker (F310).
"""
from datetime import date, time, timedelta
from decimal import Decimal

from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.utils import timezone

from academics.models import AcademicSession, Term
from classroom.models import Assignment, Course, Section, Submission
from family import services as family
from results.models import SubjectTeacherRemark, TeacherRemark, TermResult
from safety.models import StaffLeave
from teaching import services as svc
from teaching.models import (BehaviourEntry, CommentTemplate, DutyAssignment, DutyPost, GradeItem, Gradebook,
                             HandoverNote, Resource, TeacherTask)
from tenants.middleware import set_current_tenant
from users.models import House, HousePoint, Staff, StaffSubjectClass, Subject

from .factories import TenantTestCase, make_admin, make_class, make_school, make_student, make_teacher

D = Decimal


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
        self.o_user = make_teacher(self.school, username='alphao', full_name='Mr Okon')
        self.o_user.email = 'okon@example.com'
        self.o_user.save()
        self.jss1 = make_class(self.school, 'JSS 1A')
        self.jss2 = make_class(self.school, 'JSS 2A')
        set_current_tenant(self.school)
        self.teacher = Staff.objects.get(user=self.t_user)
        self.other = Staff.objects.get(user=self.o_user)
        self.jss1.form_teacher = self.teacher
        self.jss1.save()
        self.ada = make_student(self.school, 'A/001', full_name='Ada Obi', school_class=self.jss1,
                                parent_email='mum@example.com')
        self.chi = make_student(self.school, 'A/003', full_name='Chi Eze', school_class=self.jss1,
                                parent_email='eze@example.com')
        set_current_tenant(self.school)
        self.maths = Subject.objects.create(tenant=self.school, name='Mathematics')
        StaffSubjectClass.objects.create(tenant=self.school, staff=self.teacher, subject=self.maths,
                                         school_class=self.jss1)
        self.session = AcademicSession.objects.create(tenant=self.school, name='2026/2027', is_active=True)
        self.term = Term.objects.create(tenant=self.school, session=self.session, name='1st Term', is_active=True)

    def _get(self, path, **kw):
        return self.client.get(path, HTTP_HOST=self.host(self.school), **kw)

    def _post(self, path, data=None, **kw):
        return self.client.post(path, data or {}, HTTP_HOST=self.host(self.school), **kw)

    def again(self):
        set_current_tenant(self.school)


class GradebookTests(Base):

    def test_weighted_averages_and_report_card(self):
        self.client.force_login(self.t_user)
        self._post('/teaching/gradebook/', {'school_class': self.jss1.pk, 'subject': self.maths.pk})
        self.again()
        gb = Gradebook.objects.get()
        self.assertEqual(gb.report_columns.get('TEST'), 'ca1')
        self._post(f'/teaching/gradebook/{gb.pk}/', {'action': 'item', 'title': 'Test 1', 'category': 'TEST',
                                                     'max_score': '20'})
        self.again()
        test = GradeItem.objects.get(title='Test 1')
        self._post(f'/teaching/gradebook/item/{test.pk}/', {f'm_{self.ada.pk}': '25', f'm_{self.chi.pk}': 'x'})
        self.again()
        self.assertFalse(test.marks.filter(student=self.ada).exists())     # 25 out of 20 refused
        self._post(f'/teaching/gradebook/item/{test.pk}/', {f'm_{self.ada.pk}': '15', f'm_{self.chi.pk}': 'x'})
        self.again()
        hw = svc.add_item(gb, 'Homework 1', 'HOMEWORK', '10', None)
        svc.save_marks(hw, {self.ada.pk: '8', self.chi.pk: '6'})
        _items, rows = svc.standings(gb)
        ada = next(r for r in rows if r['student'] == self.ada)
        chi = next(r for r in rows if r['student'] == self.chi)
        self.assertEqual((ada['cats']['TEST'], ada['cats']['HOMEWORK']), (75.0, 80.0))
        self.assertAlmostEqual(ada['overall'], (75 * 60 + 80 * 20) / 80, delta=0.1)
        self.assertIsNone(chi['cats']['TEST'])                               # excused: not counted
        self.assertEqual(chi['overall'], 60.0)
        self._post(f'/teaching/gradebook/{gb.pk}/', {'action': 'push'})
        self.again()
        r = TermResult.objects.get(student=self.ada, subject=self.maths, term=self.term)
        self.assertEqual((r.ca1_score, r.ca2_score), (7.5, 8.0))
        self.assertEqual(TermResult.objects.get(student=self.chi).ca1_score, 0)
        self.assertContains(self._get(f'/teaching/gradebook/{gb.pk}/'), 'Ada Obi')

    def test_only_the_subject_teacher(self):
        gb = svc.gradebook_for(self.jss1, self.maths, self.term, self.teacher)
        self.client.force_login(self.o_user)
        self.assertEqual(self._get(f'/teaching/gradebook/{gb.pk}/').status_code, 302)
        with self.assertRaises(svc.TeachError):
            svc.set_weights(gb, {'w_TEST': '0'})


class BehaviourAndCommentTests(Base):

    def test_points_houses_and_digest(self):
        blue = House.objects.create(tenant=self.school, name='Blue')
        self.ada.house = blue
        self.ada.save()
        self.client.force_login(self.t_user)
        self._post('/teaching/behaviour/', {'class_id': self.jss1.pk, 'students': [self.ada.pk],
                                            'category': 'KIND', 'note': 'Helped a new student'})
        self.again()
        svc.give_points(self.teacher, [self.chi], 'LATE')
        self.assertEqual(HousePoint.objects.get().student, self.ada)
        summary = {r['student'].pk: r for r in svc.behaviour_summary([self.ada, self.chi])}
        self.assertEqual((summary[self.ada.pk]['plus'], summary[self.chi.pk]['minus']), (1, 1))
        html = family.digest_html(self.school, family.digest([self.ada], timezone.localdate()))
        self.assertIn('Kindness', html)
        self.assertEqual(BehaviourEntry.objects.count(), 2)

    def test_comment_bank_to_report_remarks(self):
        svc.ensure_comments(self.school)
        tpl = CommentTemplate.objects.filter(band='GOOD').first()
        self.assertEqual(svc.render_comment('{first} did well.', self.ada), 'Ada did well.')
        self.client.force_login(self.t_user)
        page = self._get(f'/teaching/comments/remarks/?class={self.jss1.pk}&subject={self.maths.pk}')
        self.assertContains(page, 'Chi Eze')
        self._post('/teaching/comments/remarks/', {'class': self.jss1.pk, 'subject': self.maths.pk,
                                                   f'r_{self.ada.pk}': tpl.text, f't_{self.ada.pk}': tpl.pk})
        self.again()
        remark = SubjectTeacherRemark.objects.get(student=self.ada)
        self.assertIn('Ada', remark.remark)
        self.assertNotIn('{first}', remark.remark)
        tpl.refresh_from_db()
        self.assertEqual(tpl.uses, 1)
        self._post('/teaching/comments/remarks/', {'class': self.jss1.pk, f'r_{self.chi.pk}': 'Polite and helpful.'})
        self.again()
        self.assertEqual(TeacherRemark.objects.get(student=self.chi).remark, 'Polite and helpful.')
        with self.assertRaises(svc.TeachError):          # Mr Okon is not the form teacher
            svc.apply_remarks(self.other, self.jss1, None, self.term, {f'r_{self.ada.pk}': 'x'})
        self.assertEqual(svc.band_for(80), 'EXCELLENT')
        self.assertEqual(svc.band_for(40), 'WEAK')


class QueueTaskResourceTests(Base):

    def test_marking_queue_and_tasks(self):
        course = Course.objects.create(tenant=self.school, name='Maths JSS1', teacher=self.t_user)
        section = Section.objects.create(tenant=self.school, course=course, title='Week 1', order=1)
        a = Assignment.objects.create(tenant=self.school, section=section, title='Fractions worksheet',
                                      description='Do all', due_date=timezone.now())
        Submission.objects.create(tenant=self.school, assignment=a, student=self.ada.user, submitted_at=timezone.now())
        gb = svc.gradebook_for(self.jss1, self.maths, self.term, self.teacher)
        it = svc.add_item(gb, 'Quiz', 'CLASSWORK', '5', None)
        svc.save_marks(it, {self.ada.pk: '4'})
        mail.outbox.clear()
        svc.add_task(self.teacher, 'Submit scheme of work', timezone.localdate() + timedelta(days=2),
                     assigned_by=self.admin)
        self.assertEqual(mail.outbox[0].to, ['ade@example.com'])
        q = svc.marking_queue(self.teacher, self.t_user, self.school)
        self.assertEqual(q['assignments'][0]['assignment'], a)
        self.assertEqual(q['blanks'][0]['missing'], 1)
        self.assertEqual(len(q['tasks']), 1)
        self.client.force_login(self.t_user)
        self.assertContains(self._get('/teaching/'), 'Fractions worksheet')
        t = TeacherTask.objects.get()
        self._post('/teaching/tasks/', {'action': 'toggle', 'id': t.pk})
        self._post('/teaching/tasks/', {'action': 'delete', 'id': t.pk})       # set by the head: kept
        self.again()
        self.assertTrue(TeacherTask.objects.get().done)

    def test_resources_shared_or_private(self):
        f = SimpleUploadedFile('ws.pdf', b'%PDF-1.4 x', content_type='application/pdf')
        svc.add_resource(self.teacher, {'title': 'Fractions worksheet', 'kind': 'WORKSHEET', 'shared': '1',
                                        'subject': self.maths.pk, 'level': 'JSS 1'}, f)
        svc.add_resource(self.teacher, {'title': 'My draft', 'kind': 'NOTES', 'link': 'https://example.com'})
        self.assertEqual([r.title for r in svc.find_resources(self.other)], ['Fractions worksheet'])
        self.assertEqual(svc.find_resources(self.teacher).count(), 2)
        r = Resource.objects.get(title='Fractions worksheet')
        self.client.force_login(self.o_user)
        self.assertEqual(self._get(f'/teaching/resources/{r.pk}/file/').status_code, 200)
        draft = Resource.objects.get(title='My draft')
        self.assertEqual(self._get(f'/teaching/resources/{draft.pk}/file/').status_code, 404)
        with self.assertRaises(svc.TeachError):
            svc.add_resource(self.teacher, {'title': 'Nothing', 'kind': 'OTHER'})


class DutyTests(Base):

    def test_fair_rota_skips_leave_weekends_and_overlaps(self):
        gate = DutyPost.objects.create(tenant=self.school, name='Gate', start_time=time(7, 0), end_time=time(7, 45))
        DutyPost.objects.create(tenant=self.school, name='Morning field', start_time=time(7, 15),
                                end_time=time(7, 45))
        monday = date(2026, 10, 5)
        StaffLeave.objects.create(tenant=self.school, staff=self.other, start_date=monday, end_date=monday,
                                  status=StaffLeave.APPROVED)
        third = Staff.objects.get(user=make_teacher(self.school, username='alphaz', full_name='Ms Zara'))
        self.again()
        n = svc.generate_rota(self.school, monday, monday + timedelta(days=6), [self.teacher, self.other, third])
        self.assertEqual(n, 10)                                   # 5 school days × 2 posts
        mon = DutyAssignment.objects.filter(date=monday)
        self.assertNotIn(self.other, [a.staff for a in mon])      # on leave
        self.assertEqual(len({a.staff_id for a in mon}), 2)       # overlapping posts: two different people
        counts = sorted(DutyAssignment.objects.values_list('staff_id', flat=True))
        per = [counts.count(s.pk) for s in (self.teacher, self.other, third)]
        self.assertLessEqual(max(per) - min(per), 2)
        self.assertFalse(DutyAssignment.objects.filter(date=monday + timedelta(days=5)).exists())   # Saturday
        mail.outbox.clear()
        self.assertEqual(svc.send_duty_reminders(self.school, monday), 2)
        self.assertEqual(svc.send_duty_reminders(self.school, monday), 0)
        self.assertEqual(svc.generate_rota(self.school, monday, monday, [self.teacher]), 0)      # already full
        self.assertTrue(gate.assignments.exists())


class HandoverAndDashboardTests(Base):

    def test_handover_read_receipt(self):
        mail.outbox.clear()
        self.client.force_login(self.t_user)
        self._post('/teaching/handovers/', {'to_staff': self.other.pk, 'kind': 'COVER', 'school_class': self.jss1.pk,
                                            'subject': self.maths.pk, 'covered': 'Fractions up to ex. 4',
                                            'next_steps': 'Decimals'})
        self.again()
        h = HandoverNote.objects.get()
        self.assertEqual(mail.outbox[0].to, ['okon@example.com'])
        self.client.force_login(self.o_user)
        self.assertContains(self._get(f'/teaching/handovers/{h.pk}/'), 'Fractions up to ex. 4')
        self._post(f'/teaching/handovers/{h.pk}/')
        self.again()
        h.refresh_from_db()
        self.assertIsNotNone(h.acknowledged_at)
        with self.assertRaises(svc.TeachError):
            svc.create_handover(self.teacher, {'to_staff': self.other.pk, 'kind': 'COVER'})

    def test_class_dashboard_and_picker(self):
        from users.models import Attendance
        Attendance.objects.create(tenant=self.school, student=self.ada, school_class=self.jss1,
                                  date=timezone.localdate(), status='A')
        d = svc.class_dashboard(self.jss1)
        self.assertEqual(d['absent'], [self.ada])
        self.client.force_login(self.t_user)
        self.assertContains(self._get('/teaching/class/'), 'JSS 1A')
        self.assertContains(self._get(f'/teaching/picker/?class={self.jss1.pk}'), 'Chi Eze')
        self.client.force_login(self.o_user)
        self.assertEqual(self._get('/teaching/class/').status_code, 302)      # not a form teacher
