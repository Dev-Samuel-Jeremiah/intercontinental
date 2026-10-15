from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("timetable", "0007_extralessongroup_extralessonenrolment"),
    ]

    operations = [
        migrations.AddField(
            model_name="extralessongroup", name="teacher_pay",
            field=models.DecimalField(decimal_places=2, default=0, max_digits=12,
                                      help_text="Paid to the teacher for each session (added to their salary)"),
        ),
    ]
