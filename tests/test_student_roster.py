"""
Organising students: class arms, sports houses and points, bulk edits,
duplicate records, printable registers and passport photos.
"""
import io
import shutil
import tempfile
from datetime import date

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings

from tenants.middleware import set_current_tenant
from users import roster
from users.lifecycle import change_status
from users.models import (Class, DuplicateDismissal, House, HousePoint, Student, StudentChange,
                          User)

from .factories import (TenantTestCase, make_admin, make_class, make_school, make_student,
                        make_teacher)


def _photo(size=(900, 1200), colour=(150, 120, 100), fmt='JPEG', name='p.jpg', pattern=True):
    from PIL import Image, ImageDraw
    img = Image.new('RGB', size, colour)
    if pattern:      # some detail, so it does not count as blurred
        d = ImageDraw.Draw(img)
        for x in range(0, size[0], 24):
            d.line([(x, 0), (x, size[1])], fill=(20, 20, 20), width=3)
    buf = io.BytesIO()
    img.save(buf, fmt)
    return SimpleUploadedFile(name, buf.getvalue(), content_type='image/jpeg')


class RosterBase(TenantTestCase):

    def setUp(self):
        super().setUp()
        media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media, ignore_errors=True)
        override = override_settings(MEDIA_ROOT=media)
        override.enable()
        self.addCleanup(override.disable)
        self.school = make_school('alpha')
        self.admin = make_admin(self.school)
        self.jss1 = make_class(self.school, 'JSS 1A')
        set_current_tenant(self.school)
        self.ada = make_student(self.school, 'STU001', full_name='Ada Bello', school_class=self.jss1)


class ArmTests(RosterBase):

    def test_reading_level_and_arm_from_a_name(self):
        for name, expected in (('JSS 1A', ('JSS 1', 'A')), ('SS2 Gold', ('SS2', 'Gold')),
                               ('Primary 3B', ('Primary 3', 'B')), ('Grade 10', ('Grade 10', '')),
                               ('Nursery', ('Nursery', '')), ('JSS1 (Diamond)', ('JSS1', 'Diamond'))):
            self.assertEqual(roster.split_class_name(name), expected, name)

    def test_creating_arms(self):
        created, existing = roster.create_arms(self.school, 'JSS 2', 'A, B, C')
        self.assertEqual([c.name for c in created], ['JSS 2A', 'JSS 2B', 'JSS 2C'])
        self.assertEqual(created[0].level, 'JSS 2')
        created, existing = roster.create_arms(self.school, 'JSS 2', 'C D')
        self.assertEqual(([c.name for c in created], [c.name for c in existing]),
                         (['JSS 2D'], ['JSS 2C']))

    def test_word_arms_get_a_space(self):
        created, _ = roster.create_arms(self.school, 'SS 1', 'Gold, Diamond')
        self.assertEqual([c.name for c in created], ['SS 1 Gold', 'SS 1 Diamond'])

    def test_bad_input_is_refused(self):
        for level, arms in (('', 'A'), ('JSS 1', '')):
            with self.assertRaises(roster.RosterError):
                roster.create_arms(self.school, level, arms)

    def test_filling_levels_for_existing_classes(self):
        self.assertEqual(roster.fill_levels(self.school), 1)
        self.jss1.refresh_from_db()
        self.assertEqual((self.jss1.level, self.jss1.arm), ('JSS 1', 'A'))

    def test_classes_are_grouped_by_level_with_counts(self):
        roster.create_arms(self.school, 'JSS 1', 'A, B')     # A already exists
        roster.create_arms(self.school, 'JSS 10', 'A')
        levels = roster.classes_by_level(self.school)
        self.assertEqual([lv for lv, _ in levels], ['JSS 1', 'JSS 10'])   # natural order
        self.assertEqual({c.name: c.enrolled for c in levels[0][1]}, {'JSS 1A': 1, 'JSS 1B': 0})


class HouseTests(RosterBase):

    def setUp(self):
        super().setUp()
        self.blue = House.objects.create(tenant=self.school, name='Blue', colour='#1d4ed8')
        self.red = House.objects.create(tenant=self.school, name='Red', colour='#dc2626')

    def test_awarding_and_standings(self):
        roster.award(self.blue, 10, 'Relay', self.admin)
        roster.award(self.red, 15, 'Quiz', self.admin)
        roster.award(self.red, -3, 'Late to assembly', self.admin)
        rows = roster.standings(self.school)
        self.assertEqual([(r['house'].name, r['points'], r['position']) for r in rows],
                         [('Red', 12, 1), ('Blue', 10, 2)])
        self.assertEqual(rows[0]['share'], 100)

    def test_standings_for_a_period(self):
        HousePoint.objects.create(tenant=self.school, house=self.blue, points=50, reason='Old',
                                  awarded_on=date(2025, 1, 1))
        roster.award(self.red, 5, 'New')
        rows = roster.standings(self.school, start=date(2026, 1, 1))
        self.assertEqual(rows[0]['house'], self.red)

    def test_points_must_be_sensible(self):
        for points, reason in ((0, 'x'), ('lots', 'x'), (5, ''), (5000, 'x')):
            with self.assertRaises(roster.RosterError):
                roster.award(self.blue, points, reason)

    def test_a_student_earns_points_only_for_their_own_house(self):
        self.ada.house = self.red
        self.ada.save()
        with self.assertRaisesMessage(roster.RosterError, 'is in Red'):
            roster.award(self.blue, 5, 'Sprint', student=self.ada)

    def test_sharing_students_evenly(self):
        for i in range(2, 7):
            make_student(self.school, f'STU00{i}', full_name=f'Pupil {i}', school_class=self.jss1)
        gone = make_student(self.school, 'STU009', school_class=self.jss1)
        change_status(gone, 'Withdrawn', reason='Moved')
        moved = roster.share_into_houses(self.school, self.admin)
        self.assertEqual(moved, 6)
        sizes = sorted(h.members.count() for h in (self.blue, self.red))
        self.assertEqual(sizes, [3, 3])
        self.assertIsNone(Student.objects.get(pk=gone.pk).house)
        self.assertTrue(StudentChange.objects.filter(field='house', source='House allocation').exists())

    def test_sharing_needs_two_houses(self):
        self.red.delete()
        with self.assertRaises(roster.RosterError):
            roster.share_into_houses(self.school)


class BulkEditTests(RosterBase):

    def setUp(self):
        super().setUp()
        self.tobi = make_student(self.school, 'STU002', full_name='Tobi Okafor', school_class=self.jss1)
        self.jss2 = make_class(self.school, 'JSS 2A')
        set_current_tenant(self.school)

    def test_moving_many_students_records_each(self):
        changed, skipped = roster.bulk_apply(self.school, [self.ada.id, self.tobi.id], 'class',
                                             self.jss2.id, self.admin)
        self.assertEqual((changed, skipped), (2, []))
        self.assertEqual(StudentChange.objects.filter(field='class_assigned', source='Bulk edit').count(), 2)

    def test_students_already_there_are_not_counted(self):
        self.ada.class_assigned = self.jss2
        self.ada.save()
        changed, _ = roster.bulk_apply(self.school, [self.ada.id, self.tobi.id], 'class', self.jss2.id)
        self.assertEqual(changed, 1)

    def test_bulk_status_uses_the_lifecycle(self):
        changed, skipped = roster.bulk_apply(self.school, [self.ada.id, self.tobi.id], 'status',
                                             'Graduated', self.admin)
        self.assertEqual(changed, 2)
        self.ada.refresh_from_db()
        self.assertEqual(self.ada.left_on is not None, True)
        self.assertFalse(User.objects.get(pk=self.ada.user_id).is_active)

    def test_bulk_status_needing_a_reason_is_skipped_with_a_note(self):
        changed, skipped = roster.bulk_apply(self.school, [self.ada.id], 'status', 'Withdrawn')
        self.assertEqual(changed, 0)
        self.assertIn('Give a reason', skipped[0])

    def test_another_schools_students_are_ignored(self):
        other = make_school('beta')
        theirs = make_student(other, 'STU001')
        set_current_tenant(None)
        with self.assertRaisesMessage(roster.RosterError, 'Tick the students'):
            roster.bulk_apply(self.school, [theirs.id], 'term', '2nd Term')

    def test_a_bad_target_is_refused(self):
        with self.assertRaises(roster.RosterError):
            roster.bulk_apply(self.school, [self.ada.id], 'class', 99999)
        with self.assertRaises(roster.RosterError):
            roster.bulk_apply(self.school, [self.ada.id], 'term', '9th Term')


class DuplicateTests(RosterBase):

    def test_the_same_names_in_another_order(self):
        twin = make_student(self.school, 'STU002', full_name='BELLO Ada')
        pairs = roster.find_duplicates(self.school)
        self.assertEqual(len(pairs), 1)
        self.assertEqual({pairs[0]['a'], pairs[0]['b']}, {self.ada, twin})
        self.assertIn('Same name', pairs[0]['reasons'])

    def test_same_birthday_and_a_shared_name(self):
        self.ada.date_of_birth = date(2014, 3, 2)
        self.ada.save()
        other = make_student(self.school, 'STU002', full_name='Ada Bello-Okon')
        other.date_of_birth = date(2014, 3, 2)
        other.save()
        reasons = roster.find_duplicates(self.school)[0]['reasons']
        self.assertIn('Same date of birth and a shared name', reasons)

    def test_twins_are_not_flagged(self):
        for s, name in ((self.ada, 'Kehinde Bello'),):
            s.full_name = name
        self.ada.date_of_birth = date(2014, 3, 2)
        self.ada.parent_phone = '08031234567'
        self.ada.save()
        twin = make_student(self.school, 'STU002', full_name='Taiwo Bello')
        twin.date_of_birth, twin.parent_phone = date(2014, 3, 2), '0803 123 4567'
        twin.save()
        # They share a surname and a birthday, which flags them for a look — but
        # not on the stronger "same names" or "same phone and first name" checks.
        pairs = roster.find_duplicates(self.school)
        reasons = pairs[0]['reasons'] if pairs else []
        self.assertNotIn('Same name', reasons)
        self.assertNotIn('Same parent phone and first name', reasons)

    def test_same_phone_and_first_name(self):
        self.ada.parent_phone = '+234 803 123 4567'
        self.ada.save()
        other = make_student(self.school, 'STU002', full_name='Ada Okafor')
        other.parent_phone = '08031234567'
        other.save()
        self.assertIn('Same parent phone and first name', roster.find_duplicates(self.school)[0]['reasons'])

    def test_a_dismissed_pair_stays_dismissed(self):
        twin = make_student(self.school, 'STU002', full_name='Ada Bello')
        roster.dismiss_duplicate(self.school, twin, self.ada, self.admin)
        self.assertEqual(roster.find_duplicates(self.school), [])
        self.assertEqual(DuplicateDismissal.objects.count(), 1)

    def test_students_who_left_are_skipped_unless_asked(self):
        twin = make_student(self.school, 'STU002', full_name='Ada Bello')
        change_status(twin, 'Withdrawn', reason='Entered twice')
        self.assertEqual(roster.find_duplicates(self.school), [])
        self.assertEqual(len(roster.find_duplicates(self.school, include_left=True)), 1)


class PhotoTests(RosterBase):

    def test_a_photo_is_cropped_to_the_id_card_shape(self):
        from PIL import Image
        warnings = roster.save_passport(self.ada, _photo(size=(1600, 900)))
        self.ada.refresh_from_db()
        with Image.open(self.ada.passport.path) as img:
            self.assertEqual(img.size, (600, 800))
        self.assertEqual(warnings, [])

    def test_a_browser_crop_box_is_used(self):
        from PIL import Image
        roster.save_passport(self.ada, _photo(), crop=['100', '100', '300', '400'])
        with Image.open(self.ada.passport.path) as img:
            self.assertEqual(img.size, (600, 800))

    def test_dark_and_blurred_photos_are_flagged_not_refused(self):
        warnings = roster.save_passport(self.ada, _photo(colour=(10, 10, 10), pattern=False))
        self.assertIn('looks too dark', warnings)
        self.assertIn('looks blurred', warnings)
        self.ada.refresh_from_db()
        self.assertTrue(self.ada.passport)

    def test_not_a_photo(self):
        with self.assertRaisesMessage(roster.RosterError, "isn't a photo"):
            roster.save_passport(self.ada, SimpleUploadedFile('x.jpg', b'not an image'))

    def test_too_small(self):
        with self.assertRaisesMessage(roster.RosterError, 'too small'):
            roster.save_passport(self.ada, _photo(size=(80, 100)))

    def test_the_old_photo_is_removed(self):
        import os
        roster.save_passport(self.ada, _photo())
        first = self.ada.passport.path
        roster.save_passport(self.ada, _photo())
        self.assertFalse(os.path.exists(first))
        self.assertTrue(os.path.exists(self.ada.passport.path))

    def test_matching_files_to_students_by_admission_number(self):
        other = make_student(self.school, 'WDA/2024/015', full_name='Tobi')
        pairs, unmatched = roster.match_photos(self.school, [
            _photo(name='stu001.JPG'), _photo(name='WDA-2024-015.jpg'), _photo(name='nobody.jpg')])
        self.assertEqual([s for s, _ in pairs], [self.ada, other])
        self.assertEqual(unmatched, ['nobody.jpg'])


@override_settings(ROOT_DOMAIN='localhost', ALLOWED_HOSTS=['*'])
class PageTests(RosterBase):

    def _post(self, path, data, **kw):
        return self.client.post(path, data, HTTP_HOST=self.host(self.school), **kw)

    def _get(self, path):
        return self.client.get(path, HTTP_HOST=self.host(self.school))

    def test_the_pages_load_for_an_admin(self):
        self.client.force_login(self.admin)
        for path in ('/classes/arms/', '/students/houses/', '/students/duplicates/',
                     f'/students/registers/?class={self.jss1.id}', '/students/photos/',
                     '/students/all/'):
            self.assertEqual(self._get(path).status_code, 200, path)

    def test_the_register_lists_enrolled_students_only(self):
        gone = make_student(self.school, 'STU009', full_name='Gone', school_class=self.jss1)
        change_status(gone, 'Withdrawn', reason='Moved')
        self.client.force_login(self.admin)
        page = self._get(f'/students/registers/?class={self.jss1.id}&columns=6&photos=0')
        self.assertEqual([s.full_name for s in page.context['students']], ['Ada Bello'])
        self.assertEqual(len(page.context['column_range']), 6)
        self.assertFalse(page.context['opts']['photos'])

    def test_bulk_edit_from_all_students(self):
        jss2 = make_class(self.school, 'JSS 2A')
        set_current_tenant(self.school)
        self.client.force_login(self.admin)
        response = self._post('/students/bulk/', {'student_ids': [self.ada.id], 'bulk_action': 'class',
                                                  'bulk_value': jss2.id, 'next': '/students/all/?page=2'})
        self.assertRedirects(response, '/students/all/?page=2', fetch_redirect_response=False)
        self.ada.refresh_from_db()
        self.assertEqual(self.ada.class_assigned, jss2)

    def test_bulk_edit_will_not_redirect_off_site(self):
        self.client.force_login(self.admin)
        response = self._post('/students/bulk/', {'student_ids': [self.ada.id], 'bulk_action': 'term',
                                                  'bulk_value': '2nd Term', 'next': '//evil.example'})
        self.assertEqual(response['Location'], '/students/all/')

    def test_uploading_a_cropped_photo(self):
        self.client.force_login(self.admin)
        response = self._post(f'/students/{self.ada.id}/photo/', {'photo': _photo()})
        self.assertTrue(response.json()['ok'])
        self.ada.refresh_from_db()
        self.assertTrue(self.ada.passport)

    def test_bulk_photo_upload_keeps_existing_unless_asked(self):
        self.client.force_login(self.admin)
        roster.save_passport(self.ada, _photo())
        page = self._post('/students/photos/', {'photos': [_photo(name='STU001.jpg')]})
        self.assertEqual(len(page.context['report']['kept']), 1)
        page = self._post('/students/photos/', {'photos': [_photo(name='STU001.jpg')], 'replace': '1'})
        self.assertEqual(len(page.context['report']['saved']), 1)

    def test_awarding_points_as_a_teacher(self):
        house = House.objects.create(tenant=self.school, name='Blue')
        teacher = make_teacher(self.school)
        set_current_tenant(self.school)
        self.client.force_login(teacher)
        self._post('/students/houses/', {'action': 'award', 'house_id': house.id, 'points': '5',
                                         'reason': 'Tidy classroom'})
        self.assertEqual(HousePoint.objects.get().awarded_by, teacher)
        self._post('/students/houses/', {'action': 'add', 'name': 'Sneaky'})
        self.assertFalse(House.objects.filter(name='Sneaky').exists())

    def test_teachers_cannot_bulk_edit_or_change_photos(self):
        teacher = make_teacher(self.school)
        set_current_tenant(self.school)
        self.client.force_login(teacher)
        self._post('/students/bulk/', {'student_ids': [self.ada.id], 'bulk_action': 'term',
                                       'bulk_value': '3rd Term'})
        self.ada.refresh_from_db()
        self.assertEqual(self.ada.current_term, '1st Term')
        self.assertEqual(self._post(f'/students/{self.ada.id}/photo/', {'photo': _photo()}).status_code, 403)
        self.assertNotEqual(self._get('/students/duplicates/').status_code, 200)

    def test_a_parent_cannot_reach_these_pages(self):
        parent = User.objects.create_user(username='par1', password='x', tenant=self.school, is_parent=True)
        self.client.force_login(parent)
        for path in ('/classes/arms/', '/students/houses/', '/students/registers/', '/students/photos/'):
            self.assertNotEqual(self._get(path).status_code, 200, path)
