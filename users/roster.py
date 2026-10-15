"""
users/roster.py
───────────────
Organising students: class arms, sports houses, changing many students at
once, spotting duplicate records, and preparing passport photos.
"""
import io
import re
from collections import defaultdict
from datetime import timedelta

from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.utils import timezone

from users.audit import audit_source
from users.models import (ENROLLED_STATUSES, Class, DuplicateDismissal, House, HousePoint,
                          Student)


class RosterError(ValueError):
    pass


# ─────────────────────────────────────────────────────────────────────────────
# Class arms
# ─────────────────────────────────────────────────────────────────────────────

_ARMED = re.compile(r'^(?P<level>.*?\d+)\s*(?P<arm>[A-Za-z]{1,2}|\([A-Za-z]+\)|[A-Z][a-z]+)$')


def split_class_name(name):
    """'JSS 1A' → ('JSS 1', 'A'); 'SS2 Gold' → ('SS2', 'Gold'); 'Grade 10' → ('Grade 10', '')."""
    name = (name or '').strip()
    m = _ARMED.match(name)
    if m and m.group('level').strip():
        return m.group('level').strip(), m.group('arm').strip('()')
    return name, ''


def arm_class_name(level, arm):
    level, arm = level.strip(), arm.strip()
    return f'{level}{arm}' if len(arm) <= 2 and level[-1:].isdigit() else f'{level} {arm}'


def create_arms(tenant, level, arms):
    """Create one class per arm of a level. Returns (created, already_there)."""
    level = (level or '').strip()
    names = [a.strip() for a in re.split(r'[,\s]+', arms or '') if a.strip()]
    if not level:
        raise RosterError('Give the level, e.g. "JSS 1".')
    if not names:
        raise RosterError('List the arms, e.g. "A, B, C".')
    if len(names) > 26:
        raise RosterError('That is more arms than a level usually has — check the list.')
    created, existing = [], []
    for arm in names:
        name = arm_class_name(level, arm)
        cls = Class.objects.filter(tenant=tenant, name__iexact=name).first()
        if cls:
            if not cls.level:
                cls.level, cls.arm = level, arm
                cls.save(update_fields=['level', 'arm'])
            existing.append(cls)
        else:
            created.append(Class.objects.create(tenant=tenant, name=name, level=level, arm=arm))
    return created, existing


def fill_levels(tenant):
    """Work out level and arm for classes that do not have them yet, from their names."""
    n = 0
    for cls in Class.objects.filter(tenant=tenant, level=''):
        cls.level, cls.arm = split_class_name(cls.name)
        cls.save(update_fields=['level', 'arm'])
        n += 1
    return n


def classes_by_level(tenant):
    """[(level, [classes])] with each level's student count, for the class page."""
    groups = defaultdict(list)
    for cls in (Class.objects.filter(tenant=tenant)
                .annotate(enrolled=Count('student', filter=Q(student__status__in=ENROLLED_STATUSES)))
                .select_related('form_teacher').order_by('level', 'arm', 'name')):
        groups[cls.level or cls.name].append(cls)
    return sorted(groups.items(), key=lambda kv: _natural(kv[0]))


def _natural(text):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r'(\d+)', text or '')]


# ─────────────────────────────────────────────────────────────────────────────
# Houses
# ─────────────────────────────────────────────────────────────────────────────

def standings(tenant, start=None, end=None):
    """Houses ranked by points in the period, with members and the period's top earners."""
    points = HousePoint.objects.filter(tenant=tenant)
    if start:
        points = points.filter(awarded_on__gte=start)
    if end:
        points = points.filter(awarded_on__lte=end)
    totals = dict(points.values_list('house').annotate(t=Sum('points')))
    rows = []
    for house in (House.objects.filter(tenant=tenant, is_active=True)
                  .annotate(members_count=Count('members', filter=Q(members__status__in=ENROLLED_STATUSES)))
                  .select_related('house_master', 'captain')):
        rows.append({'house': house, 'points': totals.get(house.id, 0) or 0,
                     'members': house.members_count})
    rows.sort(key=lambda r: (-r['points'], r['house'].name))
    top = max((r['points'] for r in rows), default=0) or 1
    for i, r in enumerate(rows, start=1):
        r['position'] = i
        r['share'] = max(0, round(100 * r['points'] / top)) if top > 0 else 0
    return rows


def award(house, points, reason, user=None, student=None, awarded_on=None):
    try:
        points = int(points)
    except (TypeError, ValueError):
        raise RosterError('Points must be a whole number.')
    if points == 0:
        raise RosterError('Award at least one point (or take some away with a minus).')
    if abs(points) > 1000:
        raise RosterError('That is a lot of points at once — check the number.')
    if not (reason or '').strip():
        raise RosterError('Say what the points are for.')
    if student and student.house_id and student.house_id != house.id:
        raise RosterError(f'{student.full_name} is in {student.house}, not {house}.')
    return HousePoint.objects.create(tenant=house.tenant, house=house, points=points,
                                     reason=reason.strip()[:200], student=student,
                                     awarded_by=user, awarded_on=awarded_on or timezone.localdate())


@transaction.atomic
def share_into_houses(tenant, user=None):
    """
    Put every enrolled student without a house into one, keeping houses even
    in size and in boys and girls within each class. Returns how many moved.
    """
    houses = list(House.objects.filter(tenant=tenant, is_active=True))
    if len(houses) < 2:
        raise RosterError('Create at least two houses first.')
    size = {h.id: h.members.filter(status__in=ENROLLED_STATUSES).count() for h in houses}
    waiting = list(Student.objects.filter(tenant=tenant, status__in=ENROLLED_STATUSES,
                                          house__isnull=True)
                   .order_by('class_assigned__name', 'gender', 'full_name'))
    by_id = {h.id: h for h in houses}
    with audit_source('House allocation', user):
        for student in waiting:
            target = min(houses, key=lambda h: (size[h.id], h.name))
            student.house = by_id[target.id]
            student.save(update_fields=['house'])
            size[target.id] += 1
    return len(waiting)


# ─────────────────────────────────────────────────────────────────────────────
# Changing many students at once
# ─────────────────────────────────────────────────────────────────────────────

BULK_ACTIONS = {'class': 'Move to class', 'house': 'Put in house', 'term': 'Set term',
                'status': 'Change status'}


@transaction.atomic
def bulk_apply(tenant, student_ids, action, value, user=None, reason=''):
    """
    Apply one change to many students. Each student is saved individually so
    the change history records every one. Returns (changed, skipped messages).
    """
    from users.lifecycle import StatusError, change_status
    from users.models import STATUS_OPTIONS, TERM_OPTIONS

    if action not in BULK_ACTIONS:
        raise RosterError('Choose what to change.')
    students = list(Student.objects.filter(tenant=tenant, pk__in=student_ids)
                    .select_related('class_assigned', 'house', 'user'))
    if not students:
        raise RosterError('Tick the students to change.')

    target = None
    if action == 'class':
        target = Class.objects.filter(tenant=tenant, pk=value or 0).first()
        if not target:
            raise RosterError('Choose the class to move them to.')
    elif action == 'house':
        if value == 'none':
            target = None
        else:
            target = House.objects.filter(tenant=tenant, pk=value or 0).first()
            if not target:
                raise RosterError('Choose the house.')
    elif action == 'term':
        if value not in dict(TERM_OPTIONS):
            raise RosterError('Choose the term.')
        target = value
    elif action == 'status' and value not in dict(STATUS_OPTIONS):
        raise RosterError('Choose the status.')

    changed, skipped = 0, []
    with audit_source('Bulk edit', user):
        for s in students:
            if action == 'class':
                if s.class_assigned_id == target.id:
                    continue
                s.class_assigned = target
                s.save(update_fields=['class_assigned'])
            elif action == 'house':
                if s.house_id == (target.id if target else None):
                    continue
                s.house = target
                s.save(update_fields=['house'])
            elif action == 'term':
                if s.current_term == target:
                    continue
                s.current_term = target
                s.save(update_fields=['current_term'])
            else:
                if s.status == value:
                    continue
                try:
                    change_status(s, value, reason=reason, user=user)
                except StatusError as problem:
                    skipped.append(f'{s.full_name}: {problem}')
                    continue
            changed += 1
    return changed, skipped


# ─────────────────────────────────────────────────────────────────────────────
# Duplicate records
# ─────────────────────────────────────────────────────────────────────────────

_TITLES = {'mr', 'mrs', 'miss', 'master', 'dr'}


def _name_tokens(name):
    tokens = re.findall(r"[a-z]+", (name or '').lower())
    return frozenset(t for t in tokens if t not in _TITLES and len(t) > 1)


def _phone(phone):
    digits = re.sub(r'\D', '', phone or '')
    return digits[-10:] if len(digits) >= 10 else ''


def find_duplicates(tenant, include_left=False):
    """
    Pairs of records that are probably the same child, strongest first:

      • the same names in any order ("Ada Bello" / "Bello Ada");
      • the same date of birth and a shared name;
      • the same parent phone and the same first name.

    Pairs an administrator has already marked as different are left out.
    """
    qs = Student.objects.filter(tenant=tenant).select_related('class_assigned')
    if not include_left:
        qs = qs.filter(status__in=ENROLLED_STATUSES)
    students = list(qs)
    dismissed = {(a, b) for a, b in DuplicateDismissal.objects.filter(tenant=tenant)
                 .values_list('first_id', 'second_id')}

    found = {}

    def flag(a, b, reason, score):
        if a.pk == b.pk:
            return
        a, b = (a, b) if a.pk < b.pk else (b, a)
        if (a.pk, b.pk) in dismissed:
            return
        entry = found.setdefault((a.pk, b.pk), {'a': a, 'b': b, 'reasons': [], 'score': 0})
        if reason not in entry['reasons']:
            entry['reasons'].append(reason)
            entry['score'] += score

    by_names = defaultdict(list)
    by_dob = defaultdict(list)
    by_phone = defaultdict(list)
    for s in students:
        tokens = _name_tokens(s.full_name)
        if len(tokens) >= 2:
            by_names[tokens].append(s)
        if s.date_of_birth:
            by_dob[s.date_of_birth].append(s)
        if _phone(s.parent_phone):
            by_phone[_phone(s.parent_phone)].append(s)

    for group in by_names.values():
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                flag(a, b, 'Same name', 3)
    for group in by_dob.values():
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                if _name_tokens(a.full_name) & _name_tokens(b.full_name):
                    flag(a, b, 'Same date of birth and a shared name', 2)
    for group in by_phone.values():
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                first_a = (a.full_name or '').split()[:1]
                first_b = (b.full_name or '').split()[:1]
                if first_a and first_a[0].lower() == (first_b[0].lower() if first_b else None):
                    flag(a, b, 'Same parent phone and first name', 2)

    return sorted(found.values(), key=lambda e: (-e['score'], e['a'].full_name))


def dismiss_duplicate(tenant, first, second, user=None):
    a, b = (first, second) if first.pk < second.pk else (second, first)
    DuplicateDismissal.objects.get_or_create(tenant=tenant, first=a, second=b,
                                             defaults={'dismissed_by': user})


# ─────────────────────────────────────────────────────────────────────────────
# Passport photos
# ─────────────────────────────────────────────────────────────────────────────

PHOTO_SIZE = (600, 800)          # 3:4, matches the ID card frame
MAX_PHOTO_BYTES = 8 * 1024 * 1024


def prepare_passport(upload, crop=None):
    """
    Open an uploaded photo, crop it to 3:4 (centred unless a crop box is
    given), resize to 600×800 and return (ContentFile JPEG, warnings).
    Warnings flag photos that are too dark, too bright or blurred — they are
    still saved, so a school is never blocked, but they are told.
    """
    from PIL import Image, ImageFilter, ImageOps, ImageStat, UnidentifiedImageError

    if upload.size > MAX_PHOTO_BYTES:
        raise RosterError('The photo is larger than 8 MB.')
    try:
        img = Image.open(upload)
        img.load()
    except (UnidentifiedImageError, OSError):
        raise RosterError("That file isn't a photo we can read.")
    img = ImageOps.exif_transpose(img).convert('RGB')
    if min(img.size) < 120:
        raise RosterError('The photo is too small — use one at least 300 pixels wide.')

    if crop:
        x, y, w, h = (max(0, int(float(v))) for v in crop)
        if w < 50 or h < 50:
            raise RosterError('The crop box is too small.')
        img = img.crop((x, y, min(img.width, x + w), min(img.height, y + h)))
    # Whatever came in, the result is exactly 3:4.
    img = ImageOps.fit(img, PHOTO_SIZE, Image.LANCZOS, centering=(0.5, 0.4))

    warnings = []
    grey = img.convert('L')
    brightness = ImageStat.Stat(grey).mean[0]
    if brightness < 60:
        warnings.append('looks too dark')
    elif brightness > 215:
        warnings.append('looks washed out')
    edges = ImageStat.Stat(grey.filter(ImageFilter.FIND_EDGES)).var[0]
    if edges < 60:
        warnings.append('looks blurred')

    out = io.BytesIO()
    img.save(out, 'JPEG', quality=88, optimize=True)
    return ContentFile(out.getvalue()), warnings


def save_passport(student, upload, crop=None, user=None):
    content, warnings = prepare_passport(upload, crop)
    name = f'{re.sub(r"[^A-Za-z0-9_-]", "-", student.admission_number)}.jpg'
    old = student.passport.name if student.passport else None
    with audit_source('Photo', user):
        student.passport.save(name, content, save=True)
    if old and old != student.passport.name:
        student.passport.storage.delete(old)
    return warnings


def match_photos(tenant, files):
    """
    For a bulk upload: pair each file with a student by its name, which should
    be the admission number ('ICS-2024-015.jpg'). Returns (pairs, unmatched).
    """
    import os
    lookup = {}
    for s in Student.objects.filter(tenant=tenant).only('id', 'admission_number', 'full_name',
                                                        'passport'):
        key = re.sub(r'[^a-z0-9]', '', s.admission_number.lower())
        lookup[key] = s
    pairs, unmatched = [], []
    for f in files:
        stem = os.path.splitext(os.path.basename(f.name))[0]
        key = re.sub(r'[^a-z0-9]', '', stem.lower())
        student = lookup.get(key)
        if student:
            pairs.append((student, f))
        else:
            unmatched.append(f.name)
    return pairs, unmatched
