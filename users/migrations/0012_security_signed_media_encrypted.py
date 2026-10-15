import security.crypto
import security.media
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0011_alter_attendancesettings_school_starts_at"),
    ]

    operations = [
        migrations.AlterField(
            model_name="student",
            name="passport",
            field=models.ImageField(blank=True, null=True, storage=security.media.signed_storage, upload_to='student_passports/'),
        ),
        migrations.AlterField(
            model_name="guardian",
            name="photo",
            field=models.ImageField(blank=True, help_text='Shown to gate staff when this person collects the child.', null=True, storage=security.media.signed_storage, upload_to='guardian_photos/'),
        ),
        migrations.AlterField(
            model_name="supportneeds",
            name="private_notes",
            field=security.crypto.EncryptedTextField(blank=True, help_text='Diagnosis or background — administrators only.'),
        ),
    ]
