# Discussion posts held by the AI safety check (F179).

from django.db import migrations, models


def _held():
    return models.BooleanField(default=False, help_text="Held by the safety check until a teacher looks at it")


class Migration(migrations.Migration):

    dependencies = [
        ("classroom", "0005_submission_feedback_audio_rubriccriterion_and_more"),
    ]

    operations = [
        migrations.AddField(model_name="thread", name="held", field=_held()),
        migrations.AddField(model_name="thread", name="held_reason", field=models.CharField(blank=True, max_length=160)),
        migrations.AddField(model_name="post", name="held", field=_held()),
        migrations.AddField(model_name="post", name="held_reason", field=models.CharField(blank=True, max_length=160)),
    ]
