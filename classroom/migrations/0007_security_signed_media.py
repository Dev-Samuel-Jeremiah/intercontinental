import security.media
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("classroom", "0006_post_held_thread_held"),
    ]

    operations = [
        migrations.AlterField(
            model_name="lesson",
            name="video",
            field=models.FileField(storage=security.media.signed_storage, upload_to='course_videos/'),
        ),
        migrations.AlterField(
            model_name="material",
            name="file",
            field=models.FileField(storage=security.media.signed_storage, upload_to='section_materials/'),
        ),
        migrations.AlterField(
            model_name="submission",
            name="file",
            field=models.FileField(blank=True, null=True, storage=security.media.signed_storage, upload_to='submissions/'),
        ),
    ]
