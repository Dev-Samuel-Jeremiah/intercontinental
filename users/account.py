"""
users/account.py
────────────────
"My profile": every signed-in person (staff, students, parents) manages their
picture, contact details and password.

Pictures are re-drawn as a 400×400 JPEG, which squares them, keeps the file
small and strips anything hidden in the original (camera details, location).
Names are the school's record and are changed by the office, not here.

Passwords: staff have an ordinary password. Students sign in with their term
password and parents with their child's parent password, so a change here
updates those too — otherwise they could not sign in with the new one.
"""
import io
import re
import uuid

from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.validators import validate_email

MAX_PHOTO_BYTES = 5 * 1024 * 1024
PHOTO_SIZE = 400
PHONE = re.compile(r'^\+?[0-9][0-9 \-()]{6,18}$')


class ProfileError(ValueError):
    pass


def _profile(user):
    from users.models import AccountProfile
    p, _ = AccountProfile.objects.get_or_create(user=user)
    user._profile_cache = p
    return p


# ─────────────────────────────────────────────────────────────────────────────
# Who this is
# ─────────────────────────────────────────────────────────────────────────────

def kind_of(user):
    if user.is_superuser:
        return 'admin'
    if user.is_student:
        return 'student'
    if user.is_parent:
        return 'parent'
    return 'staff'


def child_of_parent(user):
    """A parent account belongs to one child (username parent_<student id>)."""
    from users.models import Student
    m = re.fullmatch(r'parent_(\d+)', user.username or '')
    if not m:
        return None
    return Student._default_manager.filter(pk=int(m.group(1)), tenant_id=user.tenant_id).first()


def facts(user):
    """What the school records about them, for the read-only part of the page."""
    rows = [('Username', user.username)]
    kind = kind_of(user)
    if kind == 'student' and user.student:
        s = user.student
        rows += [('Admission number', s.admission_number), ('Class', s.class_assigned.name if s.class_assigned else '—'),
                 ('Status', s.status)]
    elif kind == 'parent':
        child = child_of_parent(user)
        if child:
            rows += [('Child', f'{child.full_name} ({child.admission_number})'),
                     ('Class', child.class_assigned.name if child.class_assigned else '—')]
    else:
        staff = user.staff
        if staff:
            rows += [('Role', staff.get_role_display())]
            forms = list(staff.form_classes.values_list('name', flat=True))
            if forms:
                rows.append(('Form teacher of', ', '.join(forms)))
        elif user.is_superuser:
            rows.append(('Role', 'School administrator'))
    if user.last_login:
        from django.utils import timezone
        rows.append(('Last signed in', timezone.localtime(user.last_login).strftime('%a %d %b %Y, %-I:%M %p')))
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# Picture
# ─────────────────────────────────────────────────────────────────────────────

def save_photo(user, upload):
    from PIL import Image, ImageOps
    if upload is None:
        raise ProfileError('Choose a picture first.')
    if upload.size > MAX_PHOTO_BYTES:
        raise ProfileError('That picture is too big. Use one under 5 MB.')
    try:
        Image.open(upload).verify()                       # a real image, not something renamed
        upload.seek(0)
        img = Image.open(upload)
        if img.format not in ('JPEG', 'PNG', 'WEBP', 'GIF', 'MPO'):
            raise ProfileError('Use a JPG, PNG or WebP picture.')
        img = ImageOps.exif_transpose(img)                # phones store photos sideways
        img = ImageOps.fit(img.convert('RGB'), (PHOTO_SIZE, PHOTO_SIZE), Image.LANCZOS, centering=(0.5, 0.4))
        out = io.BytesIO()
        img.save(out, 'JPEG', quality=86, optimize=True)  # a fresh file: nothing hidden carried over
    except ProfileError:
        raise
    except Exception:
        raise ProfileError("That file isn't a picture we can read. Try a JPG or PNG.")
    p = _profile(user)
    old = p.photo.name if p.photo else ''
    p.photo.save(f'{uuid.uuid4().hex}.jpg', ContentFile(out.getvalue()), save=False)
    p.save(update_fields=['photo', 'updated_at'])
    if old:
        p.photo.storage.delete(old)


def remove_photo(user):
    p = _profile(user)
    if p.photo:
        name = p.photo.name
        p.photo = None
        p.save(update_fields=['photo', 'updated_at'])
        p.photo.storage.delete(name)


# ─────────────────────────────────────────────────────────────────────────────
# Contact details
# ─────────────────────────────────────────────────────────────────────────────

def save_details(user, data):
    """Email and phone. Returns the names of what changed."""
    from users.models import User
    email = (data.get('email') or '').strip().lower()
    phone = re.sub(r'\s+', ' ', (data.get('phone') or '').strip())
    if email:
        try:
            validate_email(email)
        except ValidationError:
            raise ProfileError('That email address does not look right.')
        if User._default_manager.filter(tenant_id=user.tenant_id, email__iexact=email).exclude(pk=user.pk).exists():
            raise ProfileError('Another account at this school already uses that email address.')
    if phone and not PHONE.match(phone):
        raise ProfileError('Enter the phone number with digits only, e.g. 0803 123 4567.')
    changed = [name for name, old, new in (('email', user.email or '', email), ('phone', user.phone or '', phone))
               if old != new]
    if 'email' in changed:
        user.email = email
        user.save(update_fields=['email'])
    if 'phone' in changed:
        p = _profile(user)
        p.phone = phone
        p.save(update_fields=['phone', 'updated_at'])
    return changed


# ─────────────────────────────────────────────────────────────────────────────
# Password
# ─────────────────────────────────────────────────────────────────────────────

def _current_ok(user, typed):
    from users.passwords import term_password_matches
    if user.check_password(typed):
        return True
    kind = kind_of(user)
    if kind == 'student':
        return term_password_matches(typed, user.term_password)
    if kind == 'parent':
        child = child_of_parent(user)
        return bool(child and term_password_matches(typed, child.parent_term_password))
    return False


def change_password(request, current, new, confirm):
    user = request.user
    if not _current_ok(user, current):
        raise ProfileError('Your current password is not right.')
    if new != confirm:
        raise ProfileError('The new password and its confirmation are different.')
    if new == current:
        raise ProfileError('Choose a new password that is different from the current one.')
    try:
        validate_password(new, user)                      # the school's password rules
    except ValidationError as problem:
        raise ProfileError(' '.join(problem.messages))
    kind = kind_of(user)
    user.set_password(new)
    user.must_change_password = False
    fields = ['password', 'must_change_password']
    if kind == 'student':
        user.term_password = new                          # what the student sign-in checks
        fields.append('term_password')
    user.save(update_fields=fields)
    if kind == 'parent':
        child = child_of_parent(user)
        if child:
            child.parent_term_password = new              # what the parent sign-in checks
            child.save(update_fields=['parent_term_password'])
    from security import services as svc
    svc.remember_password(user, user.password)
    svc.log_event('PASSWORD', 'Changed their own password from My profile', request=request)
    update_session_auth_hash(request, user)               # stay signed in here; other devices are signed out
