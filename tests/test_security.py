"""
Security, privacy & compliance: two-step sign-in (F442), roles (F443), audit
trail (F444), new-device alerts (F445), lockout (F446), password rules (F447),
consent (F448), privacy notice (F449), retention (F450), data requests (F451),
breaches (F452), field encryption (F453), tenant isolation (F455), sessions
(F456), security headers (F457), compliance report (F458), signed media links
(F459) and the disclosure programme (F460).
"""
import io
import re
import time
from datetime import timedelta
from unittest import mock

from django.apps import apps
from django.contrib.auth.password_validation import validate_password
from django.core import mail
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.db import connection
from django.test import Client, override_settings
from django.utils import timezone

from security import services as svc
from security import totp
from security.models import (AuditEvent, Breach, Consent, DataPurpose, DataRequest, KnownDevice, PasswordHistory,
                             RetentionRule, Role, TwoFactor, UserSession, VulnerabilityReport)
from tenants.managers import TenantManager, TenantModelMixin
from tenants.middleware import clear_current_tenant, set_current_tenant
from users.models import Staff, Student, StudentChange, User

from .factories import TenantTestCase, make_admin, make_class, make_school, make_student, make_teacher

PNG = (b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89'
       b'\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82')


@override_settings(RUN_TASKS_EAGERLY=True)
class Base(TenantTestCase):

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha')
        self.admin = make_admin(self.school)
        self.admin.email = 'head@example.com'
        self.admin.save()
        self.teacher = make_teacher(self.school, username='alphatch', full_name='Mr Teach')
        self.teacher.email = 'teach@example.com'
        self.teacher.save()
        set_current_tenant(self.school)
        self.tstaff = Staff.objects.get(user=self.teacher)
        self.cls = make_class(self.school, 'JSS 1A')
        self.ada = make_student(self.school, 'A/001', full_name='Ada Obi', school_class=self.cls,
                                parent_email='mum@example.com')
        self.parent = User.objects.create_user(username=f'parent_{self.ada.id}', password='x', tenant=self.school,
                                               is_parent=True, email='mum@example.com')
        clear_current_tenant()

    def _get(self, path, client=None, host=None, **kw):
        return (client or self.client).get(path, HTTP_HOST=host or self.host(self.school), **kw)

    def _post(self, path, data=None, client=None, host=None, **kw):
        return (client or self.client).post(path, data or {}, HTTP_HOST=host or self.host(self.school), **kw)

    def login_post(self, username='alphaadmin', password='admin-pass-1', client=None):
        return self._post('/staff/login/', {'username': username, 'password': password}, client=client)


class TwoFactorTests(Base):

    def setup_totp(self):
        self.client.force_login(self.admin)
        self._post('/security/2fa/setup/', {'op': 'start', 'method': 'TOTP'})
        tf = TwoFactor.objects.get(user=self.admin)
        r = self._post('/security/2fa/setup/', {'op': 'confirm', 'code': totp.now_code(tf.secret)})
        self.assertRedirects(r, '/security/2fa/backup-codes/', fetch_redirect_response=False)
        codes = self.client.session['sec_backup']
        tf.refresh_from_db()
        return tf, codes

    def test_totp_setup_and_sign_in(self):
        tf, codes = self.setup_totp()
        self.assertTrue(tf.active)
        self.assertEqual(len(codes), 10)
        self.assertTrue(AuditEvent.objects.filter(action='2FA_ON', user=self.admin).exists())
        self.client.logout()
        self.login_post()
        r = self._get('/dashboard/')
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r['Location'].startswith('/security/2fa/verify/'))
        r = self._post('/security/2fa/verify/', {'code': '000000', 'next': '/dashboard/'})
        self.assertEqual(self.client.session['sec_2fa'], 'pending')
        r = self._post('/security/2fa/verify/', {'code': totp.code_at(tf.secret, tf.last_step + 1), 'next': '/dashboard/'})
        self.assertRedirects(r, '/dashboard/', fetch_redirect_response=False)
        self.assertEqual(self.client.session['sec_2fa'], 'ok')

    def test_backup_code_works_once(self):
        tf, codes = self.setup_totp()
        self.client.logout()
        self.login_post()
        self._post('/security/2fa/verify/', {'code': codes[0]})
        self.assertEqual(self.client.session['sec_2fa'], 'ok')
        self.client.logout()
        self.login_post()
        self._post('/security/2fa/verify/', {'code': codes[0]})
        self.assertEqual(self.client.session['sec_2fa'], 'pending')

    def test_email_codes(self):
        self.client.force_login(self.admin)
        mail.outbox.clear()
        self._post('/security/2fa/setup/', {'op': 'start', 'method': 'EMAIL'})
        code = re.search(r'(\d{6})', mail.outbox[-1].subject).group(1)
        self._post('/security/2fa/setup/', {'op': 'confirm', 'code': code})
        self.assertTrue(svc.two_factor_for(self.admin))

    def test_five_wrong_codes_sign_out(self):
        self.setup_totp()
        self.client.logout()
        self.login_post()
        for _ in range(5):
            self._post('/security/2fa/verify/', {'code': '123456'})
        self.assertNotIn('_auth_user_id', self.client.session)

    @override_settings(TWO_FACTOR_ENFORCE=True)
    def test_policy_requires_setup(self):
        self.client.force_login(self.admin)
        r = self._get('/dashboard/')
        self.assertEqual(r['Location'], '/security/2fa/setup/?required=1')
        self.client.force_login(self.teacher)
        self.assertNotEqual(self._get('/dashboard/').get('Location'), '/security/2fa/setup/?required=1')
        cfg = svc.settings_for(self.school)
        cfg.require_2fa = 'STAFF'
        cfg.save()
        c = Client()
        c.force_login(self.teacher)
        self.assertEqual(self._get('/dashboard/', client=c)['Location'], '/security/2fa/setup/?required=1')

    def test_admin_resets_lost_phone(self):
        TwoFactor.objects.create(user=self.teacher, secret=totp.new_secret(), confirmed_at=timezone.now())
        self.client.force_login(self.admin)
        self._post('/security/people/', {'op': 'reset_2fa', 'user': self.teacher.pk})
        self.assertFalse(TwoFactor.objects.filter(user=self.teacher).exists())
        self.assertTrue(AuditEvent.objects.filter(action='2FA_OFF').exists())


class SignInProtectionTests(Base):

    def test_lockout_after_failures(self):
        mail.outbox.clear()
        for _ in range(5):
            self.assertEqual(self.login_post(password='wrong').status_code, 200)
        r = self.login_post()                                   # right password, but locked
        self.assertEqual(r.status_code, 429)
        self.assertNotIn('_auth_user_id', self.client.session)
        self.assertTrue(AuditEvent.objects.filter(action='LOCKED', tenant=self.school).exists())
        self.assertTrue(any('locked' in m.subject.lower() for m in mail.outbox))
        other = Client()
        other.force_login(self.admin)
        self._post('/security/people/', {'op': 'unlock', 'identity': 'alphaadmin'}, client=other)
        self.assertEqual(self.login_post().status_code, 302)

    def test_success_clears_failures(self):
        for _ in range(3):
            self.login_post(password='wrong')
        self.login_post()
        self.client.logout()
        for _ in range(3):
            self.login_post(password='wrong')
        self.assertEqual(self.login_post().status_code, 302)

    def test_new_device_alert(self):
        self.login_post()
        self.assertEqual(KnownDevice.objects.filter(user=self.admin).count(), 1)
        device = self.client.cookies['tm_dev'].value
        self.client.logout()                                    # (the test client forgets all cookies here)
        self.client.cookies['tm_dev'] = device
        mail.outbox.clear()
        self.login_post()                                       # same browser: no alert
        self.assertFalse(any('New sign-in' in m.subject for m in mail.outbox))
        self.client.logout()
        self.login_post()                                       # a browser without the cookie
        self.assertTrue(any('New sign-in' in m.subject for m in mail.outbox))
        self.assertTrue(AuditEvent.objects.filter(action='NEW_DEVICE').exists())

    def test_sessions_and_sign_out_elsewhere(self):
        a, b = Client(), Client()
        a.force_login(self.admin)
        b.force_login(self.admin)
        self._get('/dashboard/', client=a)
        self._get('/dashboard/', client=b)
        self.assertEqual(len(svc.active_sessions(user=self.admin)), 2)
        self._post('/security/sessions/', {'op': 'others'}, client=a)
        self.assertNotIn('_auth_user_id', b.session)
        self.assertEqual(len(svc.active_sessions(user=self.admin)), 1)
        self.assertTrue(UserSession.objects.filter(ended_reason__icontains='everywhere').exists())

    def test_password_rules_and_history(self):
        cfg = svc.settings_for(self.school)
        cfg.require_mix, cfg.require_symbol, cfg.history, cfg.min_length = True, True, 3, 10
        cfg.save()
        with self.assertRaises(ValidationError) as e:
            validate_password('onlyletters', self.admin)
        self.assertTrue(any('letters and numbers' in m for m in e.exception.messages))
        with self.assertRaises(ValidationError) as e:
            validate_password('admin-pass-1', self.admin)       # current password
        self.assertTrue(any('not used before' in m for m in e.exception.messages))
        validate_password('Gr3en-Mango-77!', self.admin)
        self.client.force_login(self.admin)
        self._post('/change-password/', {'current_password': 'admin-pass-1', 'new_password': 'Gr3en-Mango-77!',
                                         'confirm_password': 'Gr3en-Mango-77!'})
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.check_password('Gr3en-Mango-77!'))
        self.assertGreaterEqual(PasswordHistory.objects.filter(user=self.admin).count(), 2)

    def test_expired_password_redirects(self):
        cfg = svc.settings_for(self.school)
        cfg.expiry_days = 30
        cfg.save()
        PasswordHistory.objects.filter(user=self.admin).update(created_at=timezone.now() - timedelta(days=60))
        User.objects.filter(pk=self.admin.pk).update(date_joined=timezone.now() - timedelta(days=60))
        self.client.force_login(self.admin)
        self.assertEqual(self._get('/dashboard/')['Location'], '/change-password/')

    def test_idle_sign_out(self):
        cfg = svc.settings_for(self.school)
        cfg.idle_minutes = 10
        cfg.save()
        self.client.force_login(self.admin)
        self._get('/dashboard/')
        s = self.client.session
        s['sec_seen'] = time.time() - 3600
        s.save()
        self._get('/dashboard/')
        self.assertNotIn('_auth_user_id', self.client.session)


class AccessTests(Base):

    def test_roles_set_permissions(self):
        self.assertEqual(svc.create_starter_roles(self.school, self.admin), 6)
        bursar = Role.objects.get(tenant=self.school, name='Bursar')
        self.client.force_login(self.admin)
        self._post('/security/roles/', {'op': 'assign', 'id': bursar.pk, 'staff': [self.tstaff.pk]})
        self.tstaff.refresh_from_db()
        self.assertTrue(self.tstaff.can_access_finance and self.tstaff.can_approve_payments)
        self.assertEqual(self.tstaff.role, 'ACCOUNT')
        self._post('/security/roles/', {'op': 'save', 'id': bursar.pk, 'name': 'Bursar', 'base_role': 'ACCOUNT',
                                        'permissions': ['can_access_finance']})
        self.tstaff.refresh_from_db()
        self.assertFalse(self.tstaff.can_approve_payments)
        self.assertTrue(AuditEvent.objects.filter(action='PERMISSIONS', tenant=self.school).exists())
        self.client.force_login(self.teacher)
        self.assertEqual(self._get('/security/roles/').status_code, 302)       # admins only

    def test_deleting_a_school_with_audited_rows(self):
        # The delete audit used to look up the school after its row was gone and crash the admin.
        from tenants.models import Tenant
        Tenant.objects.filter(pk=self.school.pk).delete()
        self.assertFalse(Student.objects.filter(pk=self.ada.pk).exists())
        self.assertFalse(AuditEvent.objects.filter(action='DELETED', summary__icontains='Ada Obi').exists())

    def test_audit_trail(self):
        set_current_tenant(self.school)
        Student.objects.get(pk=self.ada.pk).delete()
        clear_current_tenant()
        self.assertTrue(AuditEvent.objects.filter(action='DELETED', summary__icontains='Ada Obi').exists())
        self.client.force_login(self.admin)
        self.assertContains(self._get('/security/audit/?q=Ada'), 'Ada Obi')
        r = self._get('/security/audit/?csv=1')
        self.assertEqual(r['Content-Type'], 'text/csv')
        self.assertIn('Deleted', r.content.decode())

    def test_tenant_isolation(self):
        for model in apps.get_models():
            if issubclass(model, TenantModelMixin):
                self.assertIsInstance(model.objects, TenantManager, model._meta.label)
        beta = make_school('beta')
        beta_admin = make_admin(beta)
        clear_current_tenant()
        dr = svc.submit_request(self.school, {'requester_name': 'X', 'requester_email': 'x@example.com',
                                              'details': 'Copy please'})
        c = Client()
        c.force_login(beta_admin)
        self.assertEqual(self._get(f'/security/requests/{dr.pk}/', client=c, host=self.host(beta)).status_code, 404)
        self.client.force_login(self.admin)
        self.assertEqual(self._get(f'/security/requests/{dr.pk}/').status_code, 200)


class PrivacyTests(Base):

    def test_public_notice_and_request(self):
        n = svc.notice_for(self.school)
        n.dpo_name, n.dpo_email = 'Mrs Data', 'dpo@alpha.example.com'
        n.save()
        r = self._get('/privacy/')
        self.assertContains(r, 'Mrs Data')
        self.assertContains(r, 'Nigeria Data Protection Act')
        self.assertIn("object-src 'none'", r['Content-Security-Policy'])
        mail.outbox.clear()
        r = self._post('/privacy/request/', {'kind': 'ACCESS', 'requester_name': 'Former Pupil',
                                             'requester_email': 'old@example.com', 'details': 'Send my records'})
        self.assertContains(r, 'DSR-')
        dr = DataRequest.objects.get()
        self.assertEqual(dr.due_on, timezone.localdate() + timedelta(days=30))
        self.assertTrue(any('dpo@alpha.example.com' in m.to for m in mail.outbox))
        self._post('/privacy/request/', {'website': 'spam', 'requester_name': 'Bot', 'details': 'x'})
        self.assertEqual(DataRequest.objects.count(), 1)

    def test_parent_consent(self):
        self.client.force_login(self.parent)
        self.assertEqual(self._get('/security/family/').status_code, 200)
        photos = DataPurpose.objects.get(tenant=self.school, name__startswith='School photos')
        self._post('/security/family/', {'op': 'consent', 'student': self.ada.pk, 'purpose': photos.pk, 'granted': '1'})
        self.assertTrue(svc.consent_allowed(self.ada, photos.name))
        self._post('/security/family/', {'op': 'consent', 'student': self.ada.pk, 'purpose': photos.pk, 'granted': '0'})
        c = Consent.objects.get(student=self.ada, purpose=photos)
        self.assertFalse(c.granted)
        self.assertEqual(len(c.history), 2)
        contract = DataPurpose.objects.get(tenant=self.school, lawful_basis='CONTRACT')
        self.assertTrue(svc.consent_allowed(self.ada, contract.name))           # no consent needed

    def test_parent_access_request_to_download(self):
        self.client.force_login(self.parent)
        self._post('/security/family/', {'op': 'request', 'student': self.ada.pk, 'kind': 'ACCESS',
                                         'details': 'Everything you hold, please'})
        dr = DataRequest.objects.get()
        self.assertTrue(dr.identity_checked)
        admin = Client()
        admin.force_login(self.admin)
        self._post(f'/security/requests/{dr.pk}/', {'op': 'export'}, client=admin)
        dr.refresh_from_db()
        self.assertTrue(dr.export)
        mail.outbox.clear()
        self._post(f'/security/requests/{dr.pk}/', {'op': 'close', 'status': 'DONE', 'response': 'Attached.'},
                   client=admin)
        self.assertTrue(any('mum@example.com' in m.to for m in mail.outbox))
        r = self._get(f'/security/requests/{dr.pk}/file/')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r['Content-Type'], 'application/zip')

    def test_anonymise_leaver(self):
        with self.assertRaises(svc.SecurityError):
            svc.anonymise_student(self.ada, self.admin)
        Student._base_manager.filter(pk=self.ada.pk).update(status='Withdrawn', left_on=timezone.localdate())
        st = Student._base_manager.get(pk=self.ada.pk)
        svc.anonymise_student(st, self.admin)
        st.refresh_from_db()
        self.assertEqual(st.full_name, f'Former student {st.pk}')
        self.assertIsNone(st.parent_email)
        self.assertFalse(StudentChange._base_manager.filter(student=st).exists())
        self.assertFalse(User.objects.get(pk=st.user_id).is_active)

    def test_breach_workflow(self):
        self.client.force_login(self.admin)
        mail.outbox.clear()
        self._post('/security/breaches/', {'title': 'Results emailed to wrong class', 'description': 'Oops',
                                           'people_affected': '30', 'severity': 'HIGH', 'children_involved': 'on'})
        b = Breach.objects.get()
        self.assertTrue(any('URGENT' in m.subject for m in mail.outbox))
        self.assertAlmostEqual(b.hours_left, 72, delta=0.2)
        with self.assertRaises(svc.SecurityError):
            svc.update_breach(b, 'close', self.admin, {})
        self.assertContains(self._get(f'/security/breaches/{b.pk}/'), 'Nigeria Data Protection Commission')
        svc.update_breach(b, 'reported', self.admin, {'ndpc_reference': 'NDPC/1'})
        svc.update_breach(b, 'close', self.admin, {})
        self.assertEqual(b.status, 'CLOSED')
        self.assertEqual(b.actions.count(), 3)

    def test_retention(self):
        e = svc.log_event('SETTINGS', 'old', tenant=self.school)
        AuditEvent.objects.filter(pk=e.pk).update(at=timezone.now() - timedelta(days=800))
        Student._base_manager.filter(pk=self.ada.pk).update(status='Graduated',
                                                            left_on=timezone.localdate() - timedelta(days=3000))
        rules = {r.kind: r for r in svc.rules_for(self.school)}
        with self.assertRaises(svc.SecurityError):
            svc.save_rule(rules['audit'], True, 10, self.admin)
        svc.save_rule(rules['audit'], True, 730, self.admin)
        svc.save_rule(rules['leavers'], True, 2555, self.admin)
        self.assertEqual(svc.run_rule(rules['leavers'], dry=True), 1)
        out = svc.run_retention(self.school)
        self.assertEqual(out['leavers'], 1)
        self.assertFalse(AuditEvent.objects.filter(pk=e.pk).exists())
        self.assertTrue(Student._base_manager.get(pk=self.ada.pk).full_name.startswith('Former student'))
        self.assertTrue(RetentionRule.objects.get(tenant=self.school, kind='leavers').last_run)

    def test_compliance_report(self):
        self.client.force_login(self.admin)
        r = self._get('/security/compliance/')
        self.assertContains(r, 'Accountability')
        self.assertContains(r, 'Security of processing')


class ProtectionTests(Base):

    def test_encrypted_fields(self):
        from welfare.models import MedicalProfile
        set_current_tenant(self.school)
        MedicalProfile.objects.create(tenant=self.school, student=self.ada, conditions='Asthma', notes='Inhaler')
        with connection.cursor() as cur:
            cur.execute('SELECT conditions FROM welfare_medicalprofile')
            raw = cur.fetchone()[0]
        self.assertTrue(raw.startswith('enc1:'))
        self.assertNotIn('Asthma', raw)
        self.assertEqual(MedicalProfile.objects.get(student=self.ada).conditions, 'Asthma')
        with connection.cursor() as cur:
            cur.execute("UPDATE welfare_medicalprofile SET notes = 'legacy text'")
        self.assertEqual(MedicalProfile.objects.get(student=self.ada).notes, 'legacy text')
        call_command('encrypt_fields', stdout=io.StringIO())
        with connection.cursor() as cur:
            cur.execute('SELECT notes FROM welfare_medicalprofile')
            self.assertTrue(cur.fetchone()[0].startswith('enc1:'))
        self.assertEqual(MedicalProfile.objects.get(student=self.ada).notes, 'legacy text')

    def test_signed_media_links(self):
        set_current_tenant(self.school)
        st = Student.objects.get(pk=self.ada.pk)
        st.passport.save('ada.png', ContentFile(PNG), save=True)
        url = st.passport.url
        clear_current_tenant()
        self.assertTrue(url.startswith('/m/'))
        r = self._get(url)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(b''.join(r.streaming_content), PNG)
        beta = make_school('beta')
        clear_current_tenant()
        self.assertEqual(self._get(url, host=self.host(beta)).status_code, 404)    # another school's address
        self.assertEqual(self._get(url.replace('/m/', '/m/x'), ).status_code, 404)  # tampered
        with mock.patch('security.media.time.time', return_value=time.time() + 8 * 3600):
            self.assertEqual(self._get(url).status_code, 404)                    # expired

    def test_legacy_public_files_still_work_and_move(self):
        import os
        from django.conf import settings
        os.makedirs(os.path.join(settings.MEDIA_ROOT, 'student_passports'), exist_ok=True)
        with open(os.path.join(settings.MEDIA_ROOT, 'student_passports', 'old.png'), 'wb') as fh:
            fh.write(PNG)
        Student._base_manager.filter(pk=self.ada.pk).update(passport='student_passports/old.png')
        set_current_tenant(self.school)
        url = Student.objects.get(pk=self.ada.pk).passport.url
        clear_current_tenant()
        self.assertEqual(self._get(url).status_code, 200)
        call_command('move_signed_media', stdout=io.StringIO())
        self.assertFalse(os.path.exists(os.path.join(settings.MEDIA_ROOT, 'student_passports', 'old.png')))
        self.assertEqual(self._get(url).status_code, 200)

    def test_headers_and_security_txt(self):
        r = self._get('/privacy/')
        self.assertIn('Permissions-Policy', r)
        self.assertEqual(r['Referrer-Policy'], 'strict-origin-when-cross-origin')
        r = self.client.get('/.well-known/security.txt', HTTP_HOST='localhost')
        self.assertContains(r, 'Contact:')
        self.assertContains(r, 'Expires:')

    def test_disclosure(self):
        mail.outbox.clear()
        ops = User.objects.create_user(username='ops', password='ops-pass-123', email='ops@school.example',
                                       is_superuser=True, is_staff=True)
        r = self.client.post('/security/disclosure/', {'reporter_name': 'Res Searcher', 'email': 'r@example.com',
                                                       'title': 'IDOR on report cards',
                                                       'details': 'Changing the id in the URL shows another pupil.'},
                             HTTP_HOST='localhost')
        self.assertContains(r, 'VR-')
        self.assertEqual(VulnerabilityReport.objects.count(), 1)
        self.assertTrue(any('ops@school.example' in m.to for m in mail.outbox))
        self.assertEqual(self.client.get('/console/security/', HTTP_HOST='localhost').status_code, 404)
        self.assertTrue(any(level in ('warn', 'bad') for level, _ in svc.security_check()))
