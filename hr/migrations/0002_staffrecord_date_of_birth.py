from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("hr", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="staffrecord",
            name="date_of_birth",
            field=models.DateField(blank=True, help_text="Used for birthday greetings", null=True),
        ),
    ]
