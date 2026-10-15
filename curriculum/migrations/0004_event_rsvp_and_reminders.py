# Event times, venue, RSVPs and reminders (F151).

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("curriculum", "0003_club_clubmeeting_lessonobservation_and_more"),
        ("tenants", "0006_alter_tenant_owner"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(model_name="calendarevent", name="start_time",
                            field=models.TimeField(blank=True, null=True)),
        migrations.AddField(model_name="calendarevent", name="end_time",
                            field=models.TimeField(blank=True, null=True)),
        migrations.AddField(model_name="calendarevent", name="venue",
                            field=models.CharField(blank=True, max_length=120)),
        migrations.AddField(model_name="calendarevent", name="rsvp",
                            field=models.BooleanField(default=False, help_text="Ask people whether they are coming")),
        migrations.AddField(model_name="calendarevent", name="rsvp_deadline",
                            field=models.DateField(blank=True, null=True)),
        migrations.AddField(model_name="calendarevent", name="capacity",
                            field=models.PositiveIntegerField(
                                blank=True, null=True, help_text="Most people who can come, guests included")),
        migrations.AddField(model_name="calendarevent", name="reminder_days",
                            field=models.PositiveSmallIntegerField(
                                default=0, help_text="Email a reminder this many days before (0 = none)")),
        migrations.AddField(model_name="calendarevent", name="reminded_at",
                            field=models.DateTimeField(blank=True, null=True)),
        migrations.CreateModel(
            name="EventRSVP",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("answer", models.CharField(choices=[("YES", "Coming"), ("MAYBE", "Maybe"), ("NO", "Not coming")],
                                            max_length=5)),
                ("guests", models.PositiveSmallIntegerField(default=0, help_text="People coming with them")),
                ("note", models.CharField(blank=True, max_length=255)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("event", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="rsvps",
                                            to="curriculum.calendarevent")),
                ("tenant", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE,
                                             to="tenants.tenant")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="event_rsvps",
                                           to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["-updated_at"], "unique_together": {("event", "user")}},
        ),
    ]
