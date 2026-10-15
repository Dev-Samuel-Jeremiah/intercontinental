# After-school lessons and coaching classes with separate billing (F156).

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

DAYS = [("MON", "Monday"), ("TUE", "Tuesday"), ("WED", "Wednesday"), ("THU", "Thursday"), ("FRI", "Friday"),
        ("SAT", "Saturday")]


class Migration(migrations.Migration):

    dependencies = [
        ("academics", "0002_initial"),
        ("finance", "0011_alter_feestructure_category_and_more"),
        ("tenants", "0006_alter_tenant_owner"),
        ("timetable", "0006_roombooking_meetings_versions_feeds"),
        ("users", "0011_alter_attendancesettings_school_starts_at"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ExtraLessonGroup",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(help_text='e.g. "SS3 WAEC Maths coaching"', max_length=120)),
                ("day", models.CharField(choices=DAYS, max_length=3)),
                ("start_time", models.TimeField()),
                ("end_time", models.TimeField()),
                ("fee", models.DecimalField(decimal_places=2, default=0, help_text="Per child, for the term",
                                            max_digits=12)),
                ("capacity", models.PositiveSmallIntegerField(blank=True, null=True)),
                ("open_to_parents", models.BooleanField(default=True, help_text="Parents can sign their child up")),
                ("active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("classes", models.ManyToManyField(blank=True, help_text="Who can join (empty = anyone)",
                                                   related_name="+", to="users.class")),
                ("room", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                           related_name="+", to="timetable.teachingroom")),
                ("subject", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                              related_name="+", to="users.subject")),
                ("teacher", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                              related_name="extra_lesson_groups", to="users.staff")),
                ("tenant", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE,
                                             to="tenants.tenant")),
                ("term", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                           related_name="+", to="academics.term")),
            ],
            options={"ordering": ["day", "start_time", "name"]},
        ),
        migrations.CreateModel(
            name="ExtraLessonEnrolment",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("fee", models.DecimalField(decimal_places=2, default=0, max_digits=12)),
                ("status", models.CharField(choices=[("DUE", "To pay"), ("PAID", "Paid"), ("WAIVED", "Free / waived")],
                                            default="DUE", max_length=6)),
                ("paid_at", models.DateTimeField(blank=True, null=True)),
                ("payment_note", models.CharField(blank=True, max_length=160)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("enrolled_by", models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL,
                                                  related_name="+", to=settings.AUTH_USER_MODEL)),
                ("group", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="enrolments",
                                            to="timetable.extralessongroup")),
                ("student", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                              related_name="extra_lessons", to="users.student")),
                ("tenant", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE,
                                             to="tenants.tenant")),
                ("transaction", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                                  related_name="+", to="finance.transaction")),
            ],
            options={"ordering": ["student__full_name"], "unique_together": {("group", "student")}},
        ),
    ]
