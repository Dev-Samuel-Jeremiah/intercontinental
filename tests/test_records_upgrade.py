"""
Student records upgrade (see also test_student_records.py): class history across sections (written automatically
on admission, class changes, promotion and leaving, and rebuilt from the change
log for older records), guardian changes in the history, health on the profile,
record completeness, the records-health report and the printable record card.
"""
import io
from datetime import timedelta

from django.core.management import call_command
from django.utils import timezone

from saas_admin.models import Campus
from tenants.middleware import set_current_tenant
from users import completeness, history
from users.audit import audit_source
from users.models import ClassHistory, Guardian, Student, StudentChange
from welfare.models import MedicalProfile

from .factories import TenantTestCase, make_admin, make_class, make_school, make_student, make_teacher


class Base(TenantTestCase):

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha')
        self.admin = make_admin(self.school)
        self.teacher = make_teacher(self.school, username='tolu', full_name='Tolu Ade')
        self.p6 = make_class(self.school, 'Primary 6')
        self.jss1 = make_class(self.school, 'JSS 1A')
        self.jss1b = make_class(self.school, 'JSS 1B')
        self.primary = Campus.objects.create(tenant=self.school, name='Nursery & Primary')
        self.secondary = Campus.objects.create(tenant=self.school, name='Secondary')
        self.primary.classes.add(self.p6)
        self.secondary.classes.add(self.jss1, self.jss1b)
        self.ada = make_student(self.school, 'A1', 'Ada Obi', school_class=self.p6)
        set_current_tenant(self.school)

    def get(self, user, path, **kw):
        self.client.force_login(user)
        return self.client.get(path, HTTP_HOST=self.host(self.school), **kw)

    def post(self, user, path, data):
        self.client.force_login(user)
        return self.client.post(path, data, HTTP_HOST=self.host(self.school))


class ClassHistoryTests(Base):

    def test_admission_move_and_section_change(self):
        rows = history.history(self.ada)
        self.assertEqual([(r.class_name, r.section_name, r.how, r.ended_on) for r in rows],
                         [('Primary 6', 'Nursery & Primary', 'ADMITTED', None)])
        self.ada.class_assigned = self.jss1
        self.ada.save()
        rows = history.history(self.ada)
        self.assertEqual([(r.class_name, r.how) for r in rows], [('Primary 6', 'ADMITTED'), ('JSS 1A', 'MOVED')])
        self.assertIsNotNone(rows[0].ended_on)
        self.assertTrue(rows[1].new_section)
        self.assertEqual(history.sections_attended(rows), ['Nursery & Primary', 'Secondary'])
        self.ada.save()                                                           # nothing changed
        self.assertEqual(ClassHistory.objects.filter(student=self.ada).count(), 2)

    def test_promotion_and_repeating(self):
        with audit_source('Promotion', self.admin):
            self.ada.class_assigned = self.jss1
            self.ada.save()
        self.assertEqual(history.open_row(self.ada).how, 'PROMOTED')
        self.client.force_login(self.admin)
        bola = make_student(self.school, 'B1', 'Bola Ade', school_class=self.jss1)
        self.client.post('/students/promote/', {'student_ids': [self.ada.id, bola.id], 'new_class': self.jss1.id,
                                                'new_term': '1st Term'}, HTTP_HOST=self.host(self.school))
        self.assertEqual(history.open_row(self.ada).how, 'REPEATED')
        self.assertEqual(ClassHistory.objects.filter(student=bola).count(), 2)

    def test_leaving_closes_and_returning_reopens(self):
        from users.lifecycle import change_status
        change_status(self.ada, 'Withdrawn', reason='Relocated', user=self.admin)
        self.assertIsNone(history.open_row(self.ada))
        self.ada.refresh_from_db()
        change_status(self.ada, 'Active', reason='Came back', user=self.admin)
        self.assertEqual(history.open_row(self.ada).class_name, 'Primary 6')

    def test_older_records_are_rebuilt_from_the_change_log(self):
        ClassHistory.objects.all().delete()
        when = timezone.now() - timedelta(days=200)
        StudentChange.objects.create(tenant=self.school, student=self.ada, field='class_assigned', label='Class',
                                     old_value='Primary 6', new_value='JSS 1A', source='Promotion', changed_at=when)
        Student.objects.filter(pk=self.ada.pk).update(class_assigned=self.jss1)
        self.ada.refresh_from_db()
        out = io.StringIO()
        call_command('build_class_history', stdout=out)
        self.assertIn('1 student', out.getvalue())
        rows = list(ClassHistory.objects.filter(student=self.ada).order_by('started_on'))
        self.assertEqual([(r.class_name, r.how, r.section_name) for r in rows],
                         [('Primary 6', 'ADMITTED', 'Nursery & Primary'), ('JSS 1A', 'PROMOTED', 'Secondary')])
        self.assertEqual(rows[1].started_on, timezone.localdate(when))


class ProfileTests(Base):

    def test_profile_shows_history_section_and_completeness(self):
        page = self.get(self.admin, f'/students/{self.ada.id}/')
        self.assertContains(page, 'Class history')
        self.assertContains(page, 'Nursery &amp; Primary')
        self.assertContains(page, 'complete')

    def test_health_alert_for_all_staff_conditions_for_nurse(self):
        MedicalProfile.objects.create(tenant=self.school, student=self.ada, genotype='SS', allergies='Peanuts',
                                      conditions='Sickle cell — crisis plan on file')
        page = self.get(self.teacher, f'/students/{self.ada.id}/')
        self.assertContains(page, 'Allergies: Peanuts')
        self.assertContains(page, 'Genotype SS')
        self.assertNotContains(page, 'crisis plan on file')
        self.assertContains(self.get(self.admin, f'/students/{self.ada.id}/'), 'crisis plan on file')

    def test_guardian_changes_are_in_the_history(self):
        self.post(self.admin, f'/students/{self.ada.id}/guardians/',
                  {'action': 'add', 'full_name': 'Ngozi Obi', 'relationship': 'MOTHER', 'phone': '08030000000',
                   'emergency_contact': 'on', 'can_pick_up': 'on'})
        g = Guardian.objects.get()
        self.post(self.admin, f'/students/{self.ada.id}/guardians/',
                  {'action': 'delete', 'guardian_id': g.id})
        log = list(StudentChange.objects.filter(student=self.ada, field='guardian').order_by('id')
                   .values_list('old_value', 'new_value', 'changed_by'))
        self.assertEqual(log[0][1].split(' · ')[0], 'Ngozi Obi (Mother)')
        self.assertEqual((log[1][1], log[1][2]), ('', self.admin.id))
        self.assertContains(self.get(self.admin, f'/students/{self.ada.id}/'), 'Guardian added: Ngozi Obi (Mother)')

    def test_record_card(self):
        self.ada.class_assigned = self.jss1
        self.ada.save()
        page = self.get(self.teacher, f'/students/{self.ada.id}/record-card/')
        self.assertContains(page, 'Cumulative record card')
        self.assertContains(page, 'Primary 6')
        self.assertContains(page, 'JSS 1A')
        pupil = make_student(self.school, 'Z1', 'Zara', school_class=self.p6)
        self.assertRedirects(self.get(pupil.user, f'/students/{self.ada.id}/record-card/'), '/dashboard/',
                             fetch_redirect_response=False)


class CompletenessTests(Base):

    def test_one_student(self):
        c = completeness.check(self.ada)
        keys = {m['key'] for m in c['missing']}
        self.assertIn('dob', keys)
        self.assertIn('emergency', keys)
        self.assertNotIn('class', keys)
        self.assertNotIn('parent_email', keys)                 # the factory gives every student one
        before = c['percent']
        Guardian.objects.create(tenant=self.school, student=self.ada, full_name='Ngozi', emergency_contact=True)
        MedicalProfile.objects.create(tenant=self.school, student=self.ada, blood_group='O+')
        after = completeness.check(self.ada)
        self.assertGreater(after['percent'], before)
        self.assertNotIn('emergency', {m['key'] for m in after['missing']})
        self.assertNotIn('medical', {m['key'] for m in after['missing']})

    def test_records_health_page(self):
        make_student(self.school, 'B1', 'Bola Ade', school_class=self.jss1)
        page = self.get(self.teacher, '/students/records/')
        self.assertContains(page, 'Records')
        self.assertContains(page, 'Ada Obi')
        page = self.get(self.teacher, f'/students/records/?section={self.secondary.id}')
        self.assertContains(page, 'Bola Ade')
        self.assertNotContains(page, 'Ada Obi')
        csv = self.get(self.admin, '/students/records/?export=csv&missing=dob').content.decode()
        self.assertIn('Ada Obi', csv)
        self.assertIn('Date of birth', csv)
