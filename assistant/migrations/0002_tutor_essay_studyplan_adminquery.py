# Course tutor, essay feedback, study plans and admin questions (AI part 2).

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
        ("assistant", "0001_initial"),
        ("classroom", "0006_post_held_thread_held"),
        ("tenants", "0006_alter_tenant_owner"),
        ("users", "0011_alter_attendancesettings_school_starts_at"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="TutorSettings",
            fields=[
                _id(),
                ("enabled", models.BooleanField(default=False)),
                ("daily_limit", models.PositiveSmallIntegerField(default=20, help_text="Questions per student per day")),
                ("instructions", models.TextField(blank=True,
                                                  help_text="e.g. \"Don't solve Assignment 3; give hints only.\"")),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("course", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE,
                                                related_name="tutor_settings", to="classroom.course")),
                _tenant(),
            ],
        ),
        migrations.CreateModel(
            name="TutorMessage",
            fields=[
                _id(),
                ("role", models.CharField(choices=[("user", "Student"), ("assistant", "Tutor")], max_length=9)),
                ("text", models.TextField()),
                ("held", models.BooleanField(default=False,
                                             help_text="Stopped by the safety check; the teacher was told")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("course", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                             related_name="tutor_messages", to="classroom.course")),
                ("student", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                              related_name="tutor_messages", to=settings.AUTH_USER_MODEL)),
                _tenant(),
            ],
            options={"ordering": ["created_at", "id"]},
        ),
        migrations.CreateModel(
            name="EssayFeedback",
            fields=[
                _id(),
                ("title", models.CharField(blank=True, max_length=200)),
                ("text", models.TextField()),
                ("feedback", models.JSONField(default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("assignment", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                                 related_name="+", to="classroom.assignment")),
                ("student", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                              related_name="essay_feedback", to=settings.AUTH_USER_MODEL)),
                _tenant(),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="StudyPlan",
            fields=[
                _id(),
                ("plan", models.JSONField(default=dict)),
                ("focus", models.JSONField(default=list, help_text="The weak topics and subjects it was built from")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("requested_by", models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL,
                                                   related_name="+", to=settings.AUTH_USER_MODEL)),
                ("student", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                              related_name="study_plans", to="users.student")),
                _tenant(),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="AdminQuery",
            fields=[
                _id(),
                ("question", models.CharField(max_length=500)),
                ("tool", models.CharField(blank=True, max_length=40)),
                ("params", models.JSONField(default=dict)),
                ("rows", models.PositiveIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                _tenant(),
                ("user", models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL,
                                           related_name="+", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["-created_at"]},
        ),
    ]
