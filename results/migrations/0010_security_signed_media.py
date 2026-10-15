import security.media
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("results", "0009_reportbundle_resultquery"),
    ]

    operations = [
        migrations.AlterField(
            model_name="headteachersignature",
            name="signature",
            field=models.ImageField(storage=security.media.signed_storage, upload_to='head_teacher_signature/'),
        ),
    ]
