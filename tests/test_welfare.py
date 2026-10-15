"""
Health, welfare and discipline: sick bay (F381), medical profiles (F382),
immunisations (F383), medication (F384), screening (F385), incidents (F386),
merits and demerits (F387), detentions (F388), suspensions (F389),
counselling (F390), safeguarding (F391), conduct reports (F392), accidents
(F393), emergency contacts (F394), wellbeing survey (F395), sports (F396),
support plans (F397), resources (F398), restorative meetings (F399) and
health plans (F400).
"""
from datetime import timedelta
from decimal import Decimal as D
from unittest import mock

from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.utils import timezone

from communications.models import Survey
from hostel.models import SickBayVisit
from teaching.models import BehaviourEntry
from tenants.middleware import set_current_tenant
from users.models import Guardian, Staff, User
from welfare import services as svc
from welfare.models import (Concern, CounsellingCase, Detention, DetentionSession, FitnessTest, HealthPlan,
                            Immunisation, Incident, MedicationDose, MedicationPlan, Resource, SupportPlan,
                            SuspensionCase, Team, Threshold, ThresholdHit)

from .factories import TenantTestCase, make_admin, make_class, make_school, make_student, make_teacher


@override_settings(RUN_TASKS_EAGERLY=True)
class Base(TenantTestCase):

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha')
        self.admin = make_admin(self.school)
        self.admin.email = 'head@example.com'
        self.admin.save()
        self.nurse_user = make_teacher(self.school, username='alphanurse', full_name='Nurse Joy')
        self.nurse_user.email = 'nurse@example.com'
        self.nurse_user.save()
        self.teacher = make_teacher(self.school, username='alphatch', full_name='Mr Teach')
        self.teacher.email = 'teach@example.com'
        self.teacher.save()
        self.couns_user = make_teacher(self.school, username='alphacoun', full_name='Mrs Listen')
        self.couns_user.email = 'couns@example.com'
        self.couns_user.save()
        set_current_tenant(self.school)
        self.nurse = Staff.objects.get(user=self.nurse_user)
        self.tstaff = Staff.objects.get(user=self.teacher)
        self.counsellor = Staff.objects.get(user=self.couns_user)
        cfg = svc.settings_for(self.school)
        cfg.nurses.add(self.nurse)
        cfg.counsellors.add(self.counsellor)
        self.jss1 = make_class(self.school, 'JSS 1A')
        self.jss1.form_teacher = self.tstaff
        self.jss1.save()
        self.ada = make_student(self.school, 'A/001', full_name='Ada Obi', school_class=self.jss1,
                                parent_email='mum@example.com')
        self.chi = make_student(self.school, 'A/003', full_name='Chi Eze', school_class=self.jss1,
                                parent_email='eze@example.com')
        set_current_tenant(self.school)
        self.parent = User.objects.create_user(username=f'parent_{self.ada.id}', password='x', tenant=self.school,
                                               is_parent=True, email='mum@example.com')

    def _get(self, path, **kw):
        return self.client.get(path, HTTP_HOST=self.host(self.school), **kw)

    def _post(self, path, data=None, **kw):
        return self.client.post(path, data or {}, HTTP_HOST=self.host(self.school), **kw)

    def again(self):
        set_current_tenant(self.school)


class HealthTests(Base):

    def test_medical_record_for_nurses_only(self):
        self.client.force_login(self.teacher)
        self.assertEqual(self._get(f'/welfare/medical/{self.ada.pk}/').status_code, 302)
        self.client.force_login(self.nurse_user)
        self._post(f'/welfare/medical/{self.ada.pk}/', {'action': 'profile', 'blood_group': 'O+', 'genotype': 'AS',
                                                        'allergies': 'Groundnuts', 'notes': 'Private',
                                                        'consent_first_aid': 'on'})
        self.again()
        p = svc.profile_for(self.ada)
        self.assertEqual((p.blood_group, p.allergies, p.notes), ('O+', 'Groundnuts', 'Private'))
        self.assertIn('Allergies: Groundnuts', p.alerts)

    def test_parent_updates_profile_and_nurse_told(self):
        self.client.force_login(self.parent)
        mail.outbox.clear()
        self._post('/welfare/family/', {'student': self.ada.pk, 'action': 'profile', 'genotype': 'SS',
                                        'allergies': 'Penicillin', 'notes': 'hack', 'consent_hospital': 'on'})
        self.again()
        p = svc.profile_for(self.ada)
        self.assertEqual(p.genotype, 'SS')
        self.assertEqual(p.notes, '')                       # parents cannot write nurse notes
        self.assertFalse(p.consent_first_aid)
        self.assertTrue(any(m.to == ['nurse@example.com'] for m in mail.outbox))
        self.assertIn('Genotype SS', p.alerts)

    def test_school_wide_sick_bay(self):
        self.client.force_login(self.nurse_user)
        with mock.patch('communications.services.send_sms', return_value=(True, 'ok')):
            self._post('/welfare/sick-bay/', {'action': 'admit', 'student': self.chi.pk, 'complaint': 'Headache',
                                              'tell_parent': 'on'})
        self.again()
        v = SickBayVisit.objects.get(student=self.chi)
        self.assertContains(self._get('/welfare/sick-bay/'), 'Headache')
        self._post('/welfare/sick-bay/', {'action': 'discharge', 'id': v.pk, 'outcome': 'DORM'})
        self.again()
        v.refresh_from_db()
        self.assertIsNotNone(v.discharged_at)

    def test_immunisations_and_reminders(self):
        soon = timezone.localdate() + timedelta(days=5)
        svc.add_immunisation(self.ada, {'vaccine': 'HPV', 'dose': 'Dose 2', 'next_due': soon.isoformat()},
                             self.nurse_user)
        with self.assertRaises(svc.WelfareError):
            svc.add_immunisation(self.ada, {'vaccine': 'BCG'}, self.nurse_user)     # no dates
        mail.outbox.clear()
        self.assertEqual(svc.send_immunisation_reminders(self.school), 1)
        self.assertTrue(any('Vaccination reminder' in m.subject for m in mail.outbox))
        self.assertEqual(svc.send_immunisation_reminders(self.school), 0)

    def test_parent_adds_vaccination_with_card(self):
        self.client.force_login(self.parent)
        card = SimpleUploadedFile('card.pdf', b'%PDF-1.4 card', content_type='application/pdf')
        self._post('/welfare/family/', {'student': self.ada.pk, 'action': 'jab', 'vaccine': 'YELLOW',
                                        'given_on': '2020-01-10', 'proof': card})
        self.again()
        imm = Immunisation.objects.get(student=self.ada)
        self.assertFalse(imm.verified)
        self.client.force_login(self.nurse_user)
        self.assertContains(self._get('/welfare/immunisations/'), 'Ada Obi')

    def test_medication_consent_and_log(self):
        plan = svc.add_plan(self.ada, {'medicine': 'Salbutamol', 'dose': '2 puffs', 'times': '12:00'},
                            self.nurse_user)
        with self.assertRaises(svc.WelfareError):
            svc.give_dose(self.ada, self.nurse_user, {}, plan)                       # no consent
        MedicationPlan.objects.filter(pk=plan.pk).update(parent_consent=True)
        plan.refresh_from_db()
        self.assertFalse(svc.plans_today(self.school)[0].given_today)
        svc.give_dose(self.ada, self.nurse_user, {}, plan)
        self.assertTrue(svc.plans_today(self.school)[0].given_today)
        p = svc.profile_for(self.ada)
        p.consent_first_aid = False
        p.save()
        with self.assertRaises(svc.WelfareError):
            svc.give_dose(self.ada, self.nurse_user, {'medicine': 'Paracetamol', 'dose': '500 mg'})
        mail.outbox.clear()
        svc.give_dose(self.chi, self.nurse_user, {'medicine': 'Paracetamol', 'dose': '500 mg', 'reason': 'Fever'})
        self.assertTrue(any('Medicine given' in m.subject for m in mail.outbox))
        self.assertEqual(MedicationDose.objects.count(), 2)

    def test_screening(self):
        mail.outbox.clear()
        s = svc.save_screening(self.ada, {'height_cm': '150', 'weight_kg': '45', 'refer': 'on',
                                          'notes': 'Squints at the board'}, self.nurse_user)
        self.assertEqual(s.bmi, 20.0)
        self.assertTrue(any('Health check' in m.subject for m in mail.outbox))
        with self.assertRaises(svc.WelfareError):
            svc.save_screening(self.ada, {'height_cm': '1.5'}, self.nurse_user)
        self.client.force_login(self.nurse_user)
        self.assertContains(self._get(f'/welfare/screening/?class={self.jss1.pk}'), 'Ada Obi')

    def test_accident_report(self):
        mail.outbox.clear()
        with mock.patch('communications.services.send_sms', return_value=(True, 'ok')):
            a = svc.report_accident(self.school, self.teacher, {'place': 'SPORTS', 'injury': 'Twisted ankle',
                                                                'severity': 'MODERATE'}, self.ada)
        self.assertTrue(a.parent_told)
        self.assertTrue(any('Accident report' in m.subject for m in mail.outbox))
        self.assertTrue(any(m.to == ['nurse@example.com'] for m in mail.outbox))
        with self.assertRaises(svc.WelfareError):
            svc.report_accident(self.school, self.teacher, {'place': 'SPORTS', 'injury': 'x'})

    def test_emergency_card(self):
        self.ada.parent_phone = '08030000001'
        self.ada.save()
        Guardian.objects.create(tenant=self.school, student=self.ada, full_name='Uncle Obi', phone='08030000002',
                                emergency_contact=True)
        card = svc.emergency_card(self.ada)
        self.assertEqual([p[2] for p in card['phones']], ['08030000001', '08030000002'])
        self.client.force_login(self.teacher)
        self.assertContains(self._get(f'/welfare/emergency/?class={self.jss1.pk}'), '08030000002')

    def test_health_plan(self):
        hp = HealthPlan.objects.create(tenant=self.school, name='School plan', provider='Good HMO')
        p = svc.profile_for(self.ada)
        p.plan, p.member_number = hp, 'GH-1'
        p.save()
        self.assertEqual(list(svc.plan_members(hp))[0].student, self.ada)
        self.client.force_login(self.nurse_user)
        self.assertContains(self._get('/welfare/health-plans/'), 'GH-1')


class DisciplineTests(Base):

    def test_incident_for_two_students(self):
        mail.outbox.clear()
        made = svc.report_incident(self.school, self.teacher, [self.ada, self.chi],
                                   {'category': 'FIGHT', 'severity': '3', 'description': 'Fight at break',
                                    'demerits': '5'})
        self.assertEqual(len(made), 2)
        self.assertEqual(made[0].group, made[1].group)
        self.assertTrue(made[0].parents_told_at)
        self.assertTrue(any('Serious incident' in m.subject for m in mail.outbox))    # admin alerted
        hidden = svc.report_incident(self.school, self.teacher, [self.ada],
                                     {'category': 'PHONE', 'description': 'Phone', 'parent_visible': '0'})[0]
        self.assertIsNone(hidden.parents_told_at)
        self.assertEqual(svc.points(self.ada)['demerits'], 5)

    def test_teacher_sees_only_own_incidents(self):
        svc.report_incident(self.school, self.admin, [self.chi], {'category': 'LATE', 'description': 'Late again'})
        self.client.force_login(self.teacher)
        self.assertNotContains(self._get('/welfare/incidents/'), 'Late again')
        self._post('/welfare/incidents/', {'students': [self.ada.pk], 'category': 'DRESS', 'description': 'No tie'})
        self.again()
        self.assertTrue(Incident.objects.filter(student=self.ada, reported_by=self.teacher).exists())

    def test_thresholds_from_points_and_signal(self):
        Threshold.objects.create(tenant=self.school, name='Certificate of merit', kind='MERIT', points=2,
                                 action='Assembly')
        Threshold.objects.create(tenant=self.school, name='Report card', kind='DEMERIT', points=3,
                                 action='See the VP')
        from teaching.services import give_points
        mail.outbox.clear()
        give_points(self.tstaff, [self.ada], 'KIND')
        self.assertFalse(ThresholdHit.objects.exists())
        give_points(self.tstaff, [self.ada], 'HELP')
        hit = ThresholdHit.objects.get(student=self.ada)
        self.assertEqual(hit.threshold.name, 'Certificate of merit')
        self.assertTrue(any('Certificate of merit' in m.subject for m in mail.outbox))
        give_points(self.tstaff, [self.ada], 'EFFORT')
        self.assertEqual(ThresholdHit.objects.filter(student=self.ada).count(), 1)     # once a term
        svc.report_incident(self.school, self.teacher, [self.chi], {'category': 'DISRESPECT',
                                                                   'description': 'Rude', 'demerits': '3'})
        self.assertTrue(ThresholdHit.objects.filter(student=self.chi, threshold__kind='DEMERIT').exists())
        self.assertEqual(BehaviourEntry.objects.filter(student=self.ada).count(), 3)

    def test_detentions(self):
        tomorrow = timezone.localdate() + timedelta(days=1)
        s = DetentionSession.objects.create(tenant=self.school, date=tomorrow, starts='14:30', ends='15:30',
                                            room='Room 4', capacity=1)
        s.refresh_from_db()
        mail.outbox.clear()
        with mock.patch('communications.services.send_sms', return_value=(True, 'ok')):
            d = svc.set_detention(s, self.ada, 'Homework', self.teacher)
            with self.assertRaises(svc.WelfareError):
                svc.set_detention(s, self.chi, 'Homework', self.teacher)                 # full
        self.assertTrue(any('Detention' in m.subject for m in mail.outbox))
        mail.outbox.clear()
        svc.mark_detentions(s, {str(d.pk): 'MISSED'})
        d.refresh_from_db()
        self.assertEqual(d.status, 'MISSED')
        self.assertTrue(any('missed' in m.subject for m in mail.outbox))

    def test_suspension_flow(self):
        start = timezone.localdate()
        case = svc.propose_suspension(self.ada, self.teacher, {'starts': start.isoformat(),
                                                                'ends': (start + timedelta(days=2)).isoformat(),
                                                                'reason': 'Fighting'})
        with self.assertRaises(svc.WelfareError):
            svc.propose_suspension(self.ada, self.teacher, {'starts': start.isoformat(), 'ends': start.isoformat(),
                                                            'reason': 'Again'})
        self.client.force_login(self.admin)
        mail.outbox.clear()
        self._post('/welfare/suspensions/', {'id': case.pk, 'action': 'approve'})
        self.again()
        case.refresh_from_db()
        self.ada.refresh_from_db()
        self.assertEqual((case.status, self.ada.status), ('APPROVED', 'Suspended'))
        self.assertTrue(any('Suspension letter' in m.subject for m in mail.outbox))
        self.assertContains(self._get(f'/welfare/suspensions/{case.pk}/letter/'), 'Fighting')
        svc.record_return(case, self.admin, 'Agreed behaviour contract')
        self.ada.refresh_from_db()
        self.assertEqual(self.ada.status, 'Active')

    def test_only_admin_approves(self):
        cfg = svc.settings_for(self.school)
        cfg.discipline_team.add(self.tstaff)
        case = SuspensionCase.objects.create(tenant=self.school, student=self.ada, reason='x',
                                             starts=timezone.localdate(), ends=timezone.localdate())
        self.client.force_login(self.teacher)
        self._post('/welfare/suspensions/', {'id': case.pk, 'action': 'approve'})
        self.again()
        case.refresh_from_db()
        self.assertEqual(case.status, 'PROPOSED')

    def test_restorative_and_conduct_report(self):
        svc.save_restorative(self.school, self.teacher, [self.ada, self.chi],
                             {'what_happened': 'Argument', 'agreement': 'Apologise and sit apart'})
        svc.report_incident(self.school, self.teacher, [self.ada], {'category': 'LATE', 'description': 'Late'})
        r = svc.conduct_report(self.ada)
        self.assertEqual(r['restorative'], 1)
        self.assertEqual(len(r['incidents']), 1)
        self.client.force_login(self.parent)
        self.assertContains(self._get('/welfare/family/'), 'Persistent lateness')


class CareTests(Base):

    def test_counselling_is_counsellors_only(self):
        mail.outbox.clear()
        self.client.force_login(self.ada.user)
        self._post('/welfare/me/', {'reason': 'MOOD', 'text': 'Feeling low'})
        self.again()
        case = CounsellingCase.objects.get(student=self.ada)
        self.assertTrue(case.self_referred)
        self.assertEqual(mail.outbox[0].to, ['couns@example.com'])
        for user in (self.teacher, self.admin):
            self.client.force_login(user)
            self.assertEqual(self._get(f'/welfare/counselling/{case.pk}/').status_code, 302)
        self.client.force_login(self.couns_user)
        self._post(f'/welfare/counselling/{case.pk}/', {'action': 'note', 'text': 'First session', 'risk': 'MEDIUM'})
        self.again()
        case.refresh_from_db()
        self.assertEqual((case.status, case.risk, case.notes.count()), ('OPEN', 'MEDIUM', 1))

    def test_safeguarding(self):
        mail.outbox.clear()
        self.client.force_login(self.teacher)
        self._post('/welfare/safeguarding/', {'student': self.ada.pk, 'category': 'NEGLECT',
                                              'description': 'Hungry every day', 'immediate_danger': 'on'})
        self.again()
        c = Concern.objects.get()
        self.assertTrue(any('URGENT' in m.subject and m.to == ['head@example.com'] for m in mail.outbox))
        self.assertTrue(all('Hungry' not in m.body for m in mail.outbox))           # details stay in the system
        self.assertEqual(self._get(f'/welfare/safeguarding/{c.pk}/').status_code, 302)
        self.client.force_login(self.admin)
        lead = Staff.objects.get(user=self.couns_user)
        svc.settings_for(self.school).safeguarding_leads.add(lead)
        self._post(f'/welfare/safeguarding/{c.pk}/', {'text': 'Spoke to the family', 'status': 'MONITOR',
                                                      'lead': lead.pk})
        self.again()
        c.refresh_from_db()
        self.assertEqual((c.status, c.lead), ('MONITOR', lead))
        self.assertEqual(c.actions.count(), 1)

    def test_support_plan(self):
        start = timezone.localdate()
        plan = svc.save_plan(self.ada, self.admin, {'needs': 'Reading', 'goals': 'Read 20 words\nUse a ruler',
                                                    'strategies': 'Large print', 'owner': self.tstaff.pk,
                                                    'starts': start.isoformat(),
                                                    'review_on': (start + timedelta(days=3)).isoformat()})
        self.assertEqual(plan.goal_list, ['Read 20 words', 'Use a ruler'])
        mail.outbox.clear()
        self.assertEqual(svc.send_reminders(self.school)['plans'], 1)
        self.assertEqual(mail.outbox[0].to, ['teach@example.com'])
        svc.review_plan(plan, self.tstaff.user, {'progress': 'Better', 'rating': 'SOME',
                                                 'next_review': (start + timedelta(days=60)).isoformat()})
        plan.refresh_from_db()
        self.assertIsNone(plan.reminded_at)
        self.client.force_login(self.teacher)
        self.assertContains(self._get(f'/welfare/support-plans/{plan.pk}/'), 'Large print')
        self.assertEqual(SupportPlan.objects.count(), 1)


class OtherTests(Base):

    def test_sports_and_fitness(self):
        team = Team.objects.create(tenant=self.school, name='U-15 Football', sport='Football')
        svc.add_member(team, self.ada, 'Striker', True)
        with self.assertRaises(svc.WelfareError):
            svc.add_member(team, self.ada)
        svc.record_fitness(self.ada, 'SPRINT', '14.2', self.teacher)
        svc.record_fitness(self.ada, 'SPRINT', '13.8', self.teacher)
        svc.record_fitness(self.chi, 'SPRINT', '15.0', self.teacher)
        board = svc.fitness_board(self.school, 'SPRINT')
        self.assertEqual([(f.student, f.value) for f in board], [(self.ada, D('13.80')), (self.chi, D('15.00'))])
        self.assertEqual(FitnessTest.objects.count(), 3)
        self.client.force_login(self.ada.user)
        r = self._get('/welfare/me/')
        self.assertContains(r, 'U-15 Football')
        self.assertContains(r, 'Feeling stressed about exams')                   # starter resources

    def test_resources_and_survey(self):
        self.assertEqual(svc.ensure_starters(self.school), len(svc.STARTERS))
        self.assertEqual(svc.ensure_starters(self.school), 0)
        self.assertFalse(svc.resources_for(self.school, 'STUDENTS').filter(audience='PARENTS').exists())
        self.client.force_login(self.admin)
        self._post('/welfare/settings/', {'action': 'survey',
                                          'closes': (timezone.localdate() + timedelta(days=7)).isoformat()})
        self.again()
        s = Survey.objects.get()
        self.assertEqual((s.audience, s.anonymous), ('STUDENTS', True))
        self.assertEqual(len(s.questions), 7)
        self.assertTrue(Resource.objects.exists())

    def test_settings_admin_only(self):
        self.client.force_login(self.teacher)
        self.assertEqual(self._get('/welfare/settings/').status_code, 302)
        self.client.force_login(self.admin)
        self._post('/welfare/settings/', {'nurses': [self.tstaff.pk], 'immunisation_reminder_days': '21'})
        self.again()
        cfg = svc.settings_for(self.school)
        self.assertEqual(list(cfg.nurses.all()), [self.tstaff])
        self.assertEqual(cfg.immunisation_reminder_days, 21)

    def test_overview_pages(self):
        self.client.force_login(self.nurse_user)
        self.assertEqual(self._get('/welfare/').status_code, 200)
        self.client.force_login(self.ada.user)
        self.assertEqual(self._get('/welfare/').status_code, 302)            # students are not staff
        self.assertEqual(Detention.objects.count(), 0)
