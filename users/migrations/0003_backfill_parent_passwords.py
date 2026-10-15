"""Give every student without a parent password its own one.

Previously a student whose parent email was already in use (e.g. a sibling)
was saved with no parent password, so the parent couldn't log in with that
child's admission number. Existing passwords are left untouched.
"""
from django.db import migrations
from django.db.models import Q
from django.utils.crypto import get_random_string


def backfill(apps, schema_editor):
    Student = apps.get_model('users', 'Student')
    missing = Student.objects.filter(
        Q(parent_term_password__isnull=True) | Q(parent_term_password='')
    )
    for student in missing.iterator():
        # Same readable alphabet as users.passwords.generate_term_password
        student.parent_term_password = get_random_string(8, 'abcdefghjkmnpqrstuvwxyz23456789')
        student.save(update_fields=['parent_term_password'])


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0002_username_unique_per_tenant'),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
