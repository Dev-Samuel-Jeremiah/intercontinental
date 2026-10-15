# Room booking, parent–teacher meeting slots, timetable version history and calendar feeds.

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def _id():
    return ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID"))


def _tenant():
    return ("tenant", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE,
                                        to="tenants.tenant"))


class Migration(migrations.Migration):

    dependencies = [
        ("tenants", "0006_alter_tenant_owner"),
        ("timetable", "0005_teachersubjectclass_double_periods_and_more"),
        ("users", "0011_alter_attendancesettings_school_starts_at"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="RoomBooking",
            fields=[
                _id(),
                ("date", models.DateField()),
                ("start_time", models.TimeField()),
                ("end_time", models.TimeField()),
                ("purpose", models.CharField(max_length=160)),
                ("cancelled", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("booked_by", models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL,
                                                related_name="+", to=settings.AUTH_USER_MODEL)),
                ("room", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="bookings",
                                           to="timetable.teachingroom")),
                _tenant(),
            ],
            options={"ordering": ["date", "start_time"]},
        ),
        migrations.CreateModel(
            name="MeetingEvening",
            fields=[
                _id(),
                ("title", models.CharField(help_text='e.g. "First term PTA consultation"', max_length=120)),
                ("date", models.DateField()),
                ("start_time", models.TimeField()),
                ("end_time", models.TimeField()),
                ("slot_minutes", models.PositiveSmallIntegerField(default=10)),
                ("location", models.CharField(blank=True, max_length=120)),
                ("notes", models.CharField(blank=True, max_length=255)),
                ("is_open", models.BooleanField(default=True, help_text="Parents can book and cancel")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("classes", models.ManyToManyField(blank=True, help_text="Leave empty for every class",
                                                   related_name="+", to="users.class")),
                ("created_by", models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL,
                                                 related_name="+", to=settings.AUTH_USER_MODEL)),
                ("teachers", models.ManyToManyField(
                    blank=True, help_text="Leave empty for every teacher who teaches the class",
                    related_name="meeting_evenings", to="users.staff")),
                _tenant(),
            ],
            options={"ordering": ["-date", "start_time"]},
        ),
        migrations.CreateModel(
            name="MeetingBooking",
            fields=[
                _id(),
                ("start_time", models.TimeField()),
                ("note", models.CharField(blank=True, max_length=255)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("booked_by", models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL,
                                                related_name="+", to=settings.AUTH_USER_MODEL)),
                ("evening", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="bookings",
                                              to="timetable.meetingevening")),
                ("student", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                              related_name="meeting_bookings", to="users.student")),
                ("teacher", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                              related_name="meeting_bookings", to="users.staff")),
                _tenant(),
            ],
            options={
                "ordering": ["start_time"],
                "unique_together": {("evening", "teacher", "start_time"), ("evening", "teacher", "student")},
            },
        ),
        migrations.CreateModel(
            name="TimetableVersion",
            fields=[
                _id(),
                ("number", models.PositiveIntegerField()),
                ("label", models.CharField(blank=True, max_length=120)),
                ("entries", models.JSONField(default=list)),
                ("entry_count", models.PositiveIntegerField(default=0)),
                ("saved_at", models.DateTimeField(auto_now_add=True)),
                ("config", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="versions",
                                             to="timetable.timetableconfiguration")),
                ("saved_by", models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL,
                                               related_name="+", to=settings.AUTH_USER_MODEL)),
                ("source", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                             related_name="versions", to="timetable.generatedtimetable")),
                _tenant(),
            ],
            options={"ordering": ["-number"], "unique_together": {("config", "number")}},
        ),
        migrations.CreateModel(
            name="CalendarFeed",
            fields=[
                _id(),
                ("token", models.CharField(max_length=64, unique=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("last_used_at", models.DateTimeField(blank=True, null=True)),
                _tenant(),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                           related_name="calendar_feeds", to=settings.AUTH_USER_MODEL)),
            ],
            options={"unique_together": {("tenant", "user")}},
        ),
    ]
