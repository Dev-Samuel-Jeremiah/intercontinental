"""
My profile: everyone signed in manages their picture, contact details and password. Pictures are
re-drawn as a 400×400 JPEG; a new password works with the sign-in each kind of user actually uses
(students' term password, parents' password for their child).
"""
import io
import shutil
import tempfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from PIL import Image

from security.models import PasswordHistory
from tenants.middleware import set_current_tenant
from users.models import User

from .factories import TenantTestCase, make_admin, make_school, make_student, make_teacher

URL = '/my-profile/'


def picture(fmt='PNG', size=(900, 600)):
    out = io.BytesIO()
    Image.new('RGB', size, (200, 30, 30)).save(out, fmt)
    return SimpleUploadedFile(f'me.{fmt.lower()}', out.getvalue(), content_type=f'image/{fmt.lower()}')


class MyProfileTests(TenantTestCase):

    def setUp(self):
        super().setUp()
        for setting in ('MEDIA_ROOT', 'PRIVATE_MEDIA_ROOT'):
            folder = tempfile.mkdtemp()
            self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
            o = override_settings(**{setting: folder})
            o.enable()
            self.addCleanup(o.disable)
        self.school = make_school('alpha')
        self.admin = make_admin(self.school)
        self.teacher = make_teacher(self.school, password='Teach-pass-1', full_name='Grace Okon')
        set_current_tenant(self.school)
        self.ada = make_student(self.school, 'A1', 'Ada Obi', student_password='Stud-pass-1', parent_password='Par-pass-1')
        self.parent = User.objects.create_user(username=f'parent_{self.ada.pk}', password='Par-pass-1',
                                               tenant=self.school, is_parent=True)

    def post(self, user, data, **kw):
        self.client.force_login(user)
        return self.client.post(URL, data, HTTP_HOST=self.host(self.school), follow=True, **kw)

    def test_everyone_has_a_profile_page(self):
        for user, name in ((self.teacher, 'Grace Okon'), (self.ada.user, 'Ada Obi'), (self.parent, ''),
                           (self.admin, '')):
            self.client.force_login(user)
            page = self.client.get(URL, HTTP_HOST=self.host(self.school))
            self.assertEqual(page.status_code, 200, user.username)
            self.assertContains(page, 'Change password')
            if name:
                self.assertContains(page, name)
        self.assertContains(self.client.get('/dashboard/', HTTP_HOST=self.host(self.school), follow=True), 'My profile')

    def test_a_picture_is_squared_and_shown(self):
        page = self.post(self.teacher, {'action': 'photo', 'photo': picture()})
        self.assertContains(page, 'Your picture is updated.')
        self.teacher = User.objects.get(pk=self.teacher.pk)
        with self.teacher.photo.open('rb') as fh:
            img = Image.open(fh)
            self.assertEqual((img.format, img.size), ('JPEG', (400, 400)))
        self.assertContains(page, self.teacher.photo.name.rsplit('/', 1)[-1])          # in the top bar
        old = self.teacher.photo.name
        self.post(self.teacher, {'action': 'photo', 'photo': picture('JPEG')})
        self.teacher = User.objects.get(pk=self.teacher.pk)
        self.assertFalse(self.teacher.photo.storage.exists(old))                        # the old file is gone
        self.post(self.teacher, {'action': 'remove_photo'})
        self.teacher = User.objects.get(pk=self.teacher.pk)
        self.assertFalse(self.teacher.photo)

    def test_files_that_are_not_pictures_are_refused(self):
        fake = SimpleUploadedFile('me.png', b'<script>alert(1)</script>', content_type='image/png')
        self.assertContains(self.post(self.teacher, {'action': 'photo', 'photo': fake}), "isn&#x27;t a picture")
        self.teacher = User.objects.get(pk=self.teacher.pk)
        self.assertFalse(self.teacher.photo)

    def test_contact_details(self):
        page = self.post(self.teacher, {'action': 'details', 'email': 'Grace@Example.com', 'phone': '0803 123 4567'})
        self.assertContains(page, 'Your details are saved.')
        self.teacher = User.objects.get(pk=self.teacher.pk)
        self.assertEqual((self.teacher.email, self.teacher.phone), ('grace@example.com', '0803 123 4567'))
        self.admin.email = 'head@example.com'
        self.admin.save()
        self.assertContains(self.post(self.teacher, {'action': 'details', 'email': 'head@example.com'}),
                            'already uses that email')
        self.assertContains(self.post(self.teacher, {'action': 'details', 'email': 'nope'}), 'does not look right')
        self.assertContains(self.post(self.teacher, {'action': 'details', 'phone': 'call me'}), 'digits only')

    def change(self, user, current, new, confirm=None):
        return self.post(user, {'action': 'password', 'current_password': current, 'new_password': new,
                                'confirm_password': confirm or new})

    def test_staff_password(self):
        self.assertContains(self.change(self.teacher, 'wrong', 'Fresh-pass-22'), 'current password is not right')
        self.assertContains(self.change(self.teacher, 'Teach-pass-1', 'Fresh-pass-22', 'Other-22'), 'are different')
        page = self.change(self.teacher, 'Teach-pass-1', 'Fresh-pass-22')
        self.assertContains(page, 'Your password is changed.')
        self.teacher = User.objects.get(pk=self.teacher.pk)
        self.assertTrue(self.teacher.check_password('Fresh-pass-22'))
        self.assertTrue(PasswordHistory.objects.filter(user=self.teacher).exists())
        self.assertContains(self.client.get(URL, HTTP_HOST=self.host(self.school)), 'Change password')   # still signed in

    def test_student_password_works_at_the_student_sign_in(self):
        self.change(self.ada.user, 'Stud-pass-1', 'Newer-pass-33')
        self.client.logout()
        r = self.client.post('/student/login/', {'admission_number': 'A1', 'password': 'Newer-pass-33'},
                             HTTP_HOST=self.host(self.school))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(int(self.client.session['_auth_user_id']), self.ada.user.pk)

    def test_parent_password_becomes_the_parent_password(self):
        self.change(self.parent, 'Par-pass-1', 'Parent-pass-44')
        self.ada.refresh_from_db()
        self.assertEqual(self.ada.parent_term_password, 'Parent-pass-44')
