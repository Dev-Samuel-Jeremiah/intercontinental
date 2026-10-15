"""
Login passwords: the shared 'master' passwords must be off by default, and
each student / parent signs in with their own password.
"""
from django.test import override_settings

from users.passwords import generate_term_password, term_password_matches

from .factories import TenantTestCase, make_school, make_student


@override_settings(ROOT_DOMAIN='localhost', ALLOWED_HOSTS=['*'],
                   MASTER_STUDENT_PASSWORD='master-student', MASTER_PARENT_PASSWORD='2026')
class MasterPasswordTests(TenantTestCase):

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha')
        self.student = make_student(self.school, 'STU001',
                                    student_password='own-stud-1', parent_password='own-par-1')

    def _student_login(self, password):
        self.client.post('/student/login/',
                         {'admission_number': 'STU001', 'password': password},
                         HTTP_HOST=self.host(self.school))
        return '_auth_user_id' in self.client.session

    def _parent_login(self, password):
        self.client.post('/parent/login/',
                         {'admission_number': 'STU001', 'password': password},
                         HTTP_HOST=self.host(self.school))
        return '_auth_user_id' in self.client.session

    @override_settings(ALLOW_MASTER_PASSWORDS=False)
    def test_student_master_password_is_refused_by_default(self):
        self.assertFalse(self._student_login('master-student'))

    @override_settings(ALLOW_MASTER_PASSWORDS=False)
    def test_parent_master_password_is_refused_by_default(self):
        self.assertFalse(self._parent_login('2026'))

    @override_settings(ALLOW_MASTER_PASSWORDS=False)
    def test_student_own_password_still_works(self):
        self.assertTrue(self._student_login('own-stud-1'))

    @override_settings(ALLOW_MASTER_PASSWORDS=False)
    def test_parent_own_password_still_works(self):
        self.assertTrue(self._parent_login('own-par-1'))

    @override_settings(ALLOW_MASTER_PASSWORDS=True)
    def test_master_password_can_be_switched_back_on(self):
        self.assertTrue(self._student_login('master-student'))

    @override_settings(ALLOW_MASTER_PASSWORDS=False)
    def test_wrong_password_is_refused(self):
        self.assertFalse(self._student_login('not-the-password'))


class TypedPasswordTests(TenantTestCase):
    """Passwords are read off a screen and typed by hand."""

    def test_generated_passwords_avoid_look_alike_characters(self):
        for _ in range(60):
            pwd = generate_term_password()
            self.assertEqual(len(pwd), 8)
            self.assertFalse(set(pwd) & set('01loiIO'), f'{pwd} has look-alike characters')

    def test_matching_forgives_case_spaces_and_look_alikes(self):
        self.assertTrue(term_password_matches(' LiooAqlz ', 'lI0OaQ1z'))
        self.assertTrue(term_password_matches('p49w786k', 'p49w786k'))

    def test_matching_still_rejects_a_different_password(self):
        self.assertFalse(term_password_matches('abcdefgh', 'p49w786k'))
        self.assertFalse(term_password_matches('', 'p49w786k'))
