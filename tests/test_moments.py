"""
School Moments: staff share photos and videos of class activities and events;
staff, students and parents see what is meant for them, and like, save,
comment and report. Comments are safety-checked; photos lose hidden location
data; files are only served to people who can see the post.
"""
import io
import json
import os
import shutil
import subprocess
import tempfile
from unittest import skipUnless

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from PIL import Image

from moments import feed
from moments import media as media_mod
from moments.models import Comment, Media, Moment, MomentReport
from notifications.models import Notification
from tenants.middleware import set_current_tenant
from users.models import Staff, User

from .factories import TenantTestCase, make_admin, make_class, make_school, make_student, make_teacher


def photo(name='class.jpg', size=(2400, 1800), gps=True):
    img = Image.new('RGB', size, (30, 120, 200))
    exif = Image.Exif()
    exif[0x010F] = 'PhoneMaker'
    if gps:
        exif[0x8825] = {1: 'N', 2: (12.0, 0.0, 0.0), 3: 'E', 4: (8.0, 30.0, 0.0)}   # Kano
    out = io.BytesIO()
    img.save(out, 'JPEG', exif=exif)
    return SimpleUploadedFile(name, out.getvalue(), content_type='image/jpeg')


class Base(TenantTestCase):

    def setUp(self):
        super().setUp()
        self.private = tempfile.mkdtemp(prefix='moments-test-')
        self.override = override_settings(PRIVATE_MEDIA_ROOT=self.private)
        self.override.enable()
        self.school = make_school('alpha')
        self.admin = make_admin(self.school)
        self.teacher = make_teacher(self.school, username='tolu', full_name='Tolu Ade')
        self.jss1 = make_class(self.school, 'JSS 1')
        self.jss2 = make_class(self.school, 'JSS 2')
        self.amina = make_student(self.school, 'A1', 'Amina Bello', school_class=self.jss1)
        self.musa = make_student(self.school, 'B1', 'Musa Kano', school_class=self.jss2)
        self.mum = User.objects.create_user(username=f'parent_{self.amina.id}', password='x-pass-12345',
                                            tenant=self.school, is_parent=True)
        self.dad = User.objects.create_user(username=f'parent_{self.musa.id}', password='x-pass-12345',
                                            tenant=self.school, is_parent=True)
        set_current_tenant(self.school)

    def tearDown(self):
        self.override.disable()
        shutil.rmtree(self.private, ignore_errors=True)
        super().tearDown()

    def v(self, user):
        return feed.Viewer(user, self.school)

    def make(self, user=None, caption='Science week 🔬 #ScienceWeek', school_class=None, files=None, **kw):
        return feed.create_post(self.v(user or self.teacher), caption, kw.get('category', 'CLASSROOM'),
                                school_class.id if school_class else None, files or [photo()],
                                kw.get('comments_on', True))

    def get(self, user, path, **kw):
        self.client.force_login(user)
        return self.client.get(path, HTTP_HOST=self.host(self.school), **kw)

    def post(self, user, path, data=None):
        self.client.force_login(user)
        r = self.client.post(path, json.dumps(data or {}), content_type='application/json', HTTP_HOST=self.host(self.school))
        return r.status_code, (json.loads(r.content) if r['Content-Type'].startswith('application/json') else {})


class VisibilityTests(Base):

    def test_who_sees_what(self):
        everyone = self.make(caption='Sports day')
        jss1 = self.make(caption='JSS 1 lab', school_class=self.jss1)
        self.assertEqual(set(self.v(self.admin).posts()), {everyone, jss1})
        self.assertEqual(set(self.v(self.amina.user).posts()), {everyone, jss1})
        self.assertEqual(set(self.v(self.mum).posts()), {everyone, jss1})
        self.assertEqual(set(self.v(self.musa.user).posts()), {everyone})
        self.assertEqual(set(self.v(self.dad).posts()), {everyone})
        media = jss1.media.first()
        self.assertEqual(self.get(self.dad, f'/moments/media/{media.id}/').status_code, 404)
        self.assertEqual(self.get(self.mum, f'/moments/media/{media.id}/').status_code, 200)
        self.assertRedirects(self.get(self.dad, f'/moments/{jss1.id}/'), '/moments/', fetch_redirect_response=False)

    def test_only_staff_post(self):
        self.assertRedirects(self.get(self.mum, '/moments/new/'), '/moments/', fetch_redirect_response=False)
        with self.assertRaises(feed.FeedError):
            feed.create_post(self.v(self.amina.user), 'Hi', 'EVENT', None, [photo()])
        self.client.force_login(self.teacher)
        r = self.client.post('/moments/new/', {'caption': 'Our garden 🌱', 'category': 'CLASSROOM', 'school_class': self.jss1.id,
                                               'comments_on': 'on', 'files': [photo('a.jpg'), photo('b.jpg')]},
                             HTTP_HOST=self.host(self.school), HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertTrue(json.loads(r.content)['ok'])
        post = Moment.objects.get(caption='Our garden 🌱')
        self.assertEqual((post.school_class, post.media.count()), (self.jss1, 2))

    def test_pages(self):
        p = self.make(caption='Prize giving #Awards', category='ACHIEVEMENT')
        page = self.get(self.mum, '/moments/')
        self.assertContains(page, 'School Moments')
        self.assertContains(page, 'Prize giving')
        self.assertContains(page, 'href="/moments/?tag=Awards"')
        self.assertNotContains(page, 'New post')                      # parents don't post
        self.assertContains(self.get(self.teacher, '/moments/'), 'New post')
        self.assertContains(self.get(self.mum, '/moments/?tag=Awards'), 'Prize giving')
        self.assertNotContains(self.get(self.mum, '/moments/?tag=Sports'), 'Prize giving')
        r = json.loads(self.get(self.mum, '/moments/?view=grid&partial=1').content)
        self.assertIn(f'/moments/{p.id}/', r['html'])
        self.assertContains(self.get(self.mum, f'/moments/{p.id}/'), 'Add a comment')
        self.assertContains(self.get(self.mum, '/parent/dashboard/'), '/moments/')            # parent menu link


class MediaTests(Base):

    def test_photos_are_resized_and_lose_location(self):
        post = self.make(files=[photo(size=(4000, 3000))])
        m = post.media.get()
        self.assertEqual((m.kind, m.width, m.height), (Media.IMAGE, 1600, 1200))
        with Image.open(m.file.path) as img:
            self.assertEqual(img.size, (1600, 1200))
            self.assertNotIn(0x8825, img.getexif())                    # no GPS
        with Image.open(m.thumb.path) as img:
            self.assertEqual(max(img.size), 640)
        self.assertFalse(m.file.path.startswith(os.path.abspath('mediafiles')))

    def test_bad_uploads(self):
        with self.assertRaisesMessage(media_mod.MediaError, 'not a photo or video'):
            self.make(files=[SimpleUploadedFile('notes.pdf', b'%PDF', content_type='application/pdf')])
        with self.assertRaisesMessage(media_mod.MediaError, 'up to 10'):
            self.make(files=[photo(f'{i}.jpg', size=(10, 10)) for i in range(11)])
        with self.assertRaisesMessage(media_mod.MediaError, 'could not be opened'):
            self.make(files=[SimpleUploadedFile('fake.jpg', b'not a picture', content_type='image/jpeg')])
        self.assertFalse(Moment.objects.exists())

    def test_byte_ranges_for_video_players(self):
        m = self.make().media.get()
        r = self.get(self.mum, f'/moments/media/{m.id}/', HTTP_RANGE='bytes=0-9')
        self.assertEqual((r.status_code, len(r.content)), (206, 10))
        self.assertTrue(r['Content-Range'].startswith('bytes 0-9/'))
        self.assertEqual(r['Accept-Ranges'], 'bytes')

    @skipUnless(shutil.which('ffmpeg'), 'ffmpeg is not installed')
    def test_videos_become_web_mp4_with_a_cover(self):
        src = os.path.join(self.private, 'clip.mov')
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'testsrc=duration=2:size=320x240:rate=15',
                        '-c:v', 'mpeg4', src], check=True, timeout=60)
        with open(src, 'rb') as f:
            clip = SimpleUploadedFile('clip.mov', f.read(), content_type='video/quicktime')
        m = self.make(files=[clip]).media.get()
        self.assertEqual(m.status, Media.PROCESSING)
        media_mod.process_video(m.id)
        m.refresh_from_db()
        self.assertEqual((m.status, m.width, m.height), (Media.READY, 320, 240))
        self.assertTrue(m.file.name.endswith('.mp4'))
        self.assertTrue(m.thumb)
        self.assertEqual(json.loads(self.get(self.mum, f'/moments/{m.post_id}/status/').content)['processing'], False)


class EngagementTests(Base):

    def test_likes_and_saves(self):
        post = self.make()
        code, r = self.post(self.mum, f'/moments/{post.id}/like/')
        self.assertEqual((r['liked'], r['count']), (True, 1))
        self.post(self.amina.user, f'/moments/{post.id}/like/', {'like': True})
        self.post(self.amina.user, f'/moments/{post.id}/like/', {'like': True})            # double tap twice: still one
        self.assertEqual(Moment.objects.get(pk=post.pk).like_count, 2)
        _, r = self.post(self.mum, f'/moments/{post.id}/like/')
        self.assertEqual((r['liked'], r['count']), (False, 1))
        people = json.loads(self.get(self.teacher, f'/moments/{post.id}/likes/').content)['people']
        self.assertEqual([p['name'] for p in people], ['Amina Bello'])
        _, r = self.post(self.mum, f'/moments/{post.id}/save/')
        self.assertTrue(r['saved'])
        self.assertContains(self.get(self.mum, '/moments/?saved=1'), f'id="p{post.id}"')
        self.assertNotContains(self.get(self.dad, '/moments/?saved=1'), f'id="p{post.id}"')

    def test_comments_replies_and_notifications(self):
        post = self.make()
        _, r = self.post(self.mum, f'/moments/{post.id}/comment/', {'body': 'Lovely to see! 😍'})
        self.assertEqual((r['count'], r['comment']['name']), (1, 'Parent of Amina'))
        self.assertTrue(Notification.objects.filter(recipient=self.teacher, title='Parent of Amina commented on your post').exists())
        _, r2 = self.post(self.teacher, f'/moments/{post.id}/comment/', {'body': 'Thank you!', 'reply_to': r['comment']['id']})
        self.assertEqual(r2['comment']['reply_to'], r['comment']['id'])
        self.assertTrue(Notification.objects.filter(recipient=self.mum, title='Tolu Ade replied to your comment').exists())
        page = self.get(self.dad, f'/moments/{post.id}/')
        self.assertContains(page, 'Lovely to see!')
        self.assertContains(page, 'Thank you!')
        code, _ = self.post(self.dad, f'/moments/comments/{r["comment"]["id"]}/delete/')
        self.assertEqual(code, 400)                                    # not his
        code, _ = self.post(self.mum, f'/moments/comments/{r["comment"]["id"]}/delete/')
        self.assertEqual(Moment.objects.get(pk=post.pk).comment_count, 0)   # the reply went with it

    def test_unkind_comments_are_held_for_staff(self):
        post = self.make()
        _, r = self.post(self.musa.user, f'/moments/{post.id}/comment/', {'body': 'you are so stupid lol'})
        self.assertTrue(r['held'])
        self.assertEqual(Moment.objects.get(pk=post.pk).comment_count, 0)
        self.assertNotContains(self.get(self.mum, f'/moments/{post.id}/'), 'so stupid')
        self.assertContains(self.get(self.musa.user, f'/moments/{post.id}/'), 'so stupid')
        self.assertContains(self.get(self.teacher, '/moments/review/'), 'so stupid')
        self.assertTrue(Notification.objects.filter(title='A School Moments comment is waiting for review').exists())
        c = Comment.objects.get()
        self.client.force_login(self.teacher)
        self.client.post(f'/moments/comments/{c.id}/review/', {'allow': '0'}, HTTP_HOST=self.host(self.school))
        c.refresh_from_db()
        self.assertEqual(c.status, Comment.HIDDEN)
        self.assertNotContains(self.get(self.musa.user, f'/moments/{post.id}/'), 'so stupid')

    def test_comments_can_be_turned_off(self):
        post = self.make(comments_on=False)
        code, r = self.post(self.mum, f'/moments/{post.id}/comment/', {'body': 'Hi'})
        self.assertEqual((code, r['error']), (400, 'Comments are turned off for this post.'))

    def test_new_class_post_tells_that_class(self):
        with self.captureOnCommitCallbacks(execute=True):              # sent once the post is saved
            self.make(school_class=self.jss1)
        n = Notification.objects.filter(title__startswith='Tolu Ade shared classroom activity photos')
        self.assertEqual(set(n.values_list('audience', flat=True)), {Notification.CLASS_PARENTS, Notification.CLASS_STUDENTS})
        self.assertEqual(set(n.values_list('school_class', flat=True)), {self.jss1.id})


class ModerationTests(Base):

    def test_who_can_change_a_post(self):
        mine = self.make()
        other = make_teacher(self.school, username='bisi', full_name='Bisi Obi')
        code, _ = self.post(other, f'/moments/{mine.id}/delete/')
        self.assertEqual(code, 400)
        code, _ = self.post(self.teacher, f'/moments/{mine.id}/edit/', {'pinned': True})
        self.assertEqual(code, 400)                                    # only administrators pin
        _, r = self.post(self.teacher, f'/moments/{mine.id}/edit/', {'caption': 'Edited #News', 'comments_on': False})
        self.assertIn('#News', r['caption_html'])
        self.post(self.admin, f'/moments/{mine.id}/edit/', {'pinned': True})
        self.assertTrue(Moment.objects.get(pk=mine.pk).pinned)
        path = mine.media.first().file.path
        self.post(self.admin, f'/moments/{mine.id}/delete/')
        self.assertFalse(Moment.objects.exists())
        self.assertFalse(os.path.exists(path))                         # files go with it

    def test_reports_reach_administrators(self):
        post = self.make()
        self.post(self.dad, f'/moments/{post.id}/report/', {'reason': 'My son is in this photo and we said no photos.'})
        self.assertEqual(MomentReport.objects.get().reason, 'My son is in this photo and we said no photos.')
        self.assertTrue(Notification.objects.filter(title__contains='reported a post in School Moments').exists())
        self.assertContains(self.get(self.admin, '/moments/review/'), 'we said no photos')

    def test_photo_consent_warning(self):
        from security import services as sec
        purposes = sec.ensure_purposes(self.school)
        photos = purposes.get(name=feed.PHOTO_PURPOSE)
        sec.set_consent(self.musa, photos, False, self.dad)
        sec.set_consent(self.amina, photos, True, self.mum)
        rows = feed.photo_consent(self.school)
        self.assertEqual(rows[self.jss2.id]['refused'], ['Musa Kano'])
        self.assertEqual((rows[self.jss1.id]['refused'], rows[self.jss1.id]['unanswered']), ([], 0))
        self.assertEqual(rows['']['total'], 2)
        self.assertContains(self.get(self.teacher, '/moments/new/'), 'Musa Kano')
