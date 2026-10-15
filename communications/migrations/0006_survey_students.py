from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("communications", "0005_chat_languages_preferences_units_inbox"),
    ]

    operations = [
        migrations.AlterField(
            model_name="survey",
            name="audience",
            field=models.CharField(choices=[("PARENTS", "Parents"), ("STAFF", "Staff"), ("BOTH", "Parents and staff"),
                                            ("STUDENTS", "Students")], default="PARENTS", max_length=8),
        ),
    ]
