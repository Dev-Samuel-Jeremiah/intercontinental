import security.media
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("admissions", "0002_application_scheduling_and_offers"),
    ]

    operations = [
        migrations.AlterField(
            model_name="application",
            name="passport",
            field=models.ImageField(blank=True, null=True, storage=security.media.signed_storage, upload_to='admissions/passports/'),
        ),
        migrations.AlterField(
            model_name="applicationdocument",
            name="file",
            field=models.FileField(storage=security.media.signed_storage, upload_to='admissions/documents/'),
        ),
    ]
