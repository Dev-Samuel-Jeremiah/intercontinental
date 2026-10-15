from decimal import Decimal

import django.db.models.deletion
import teaching.models
from django.conf import settings
from django.db import migrations, models

CASCADE, SET_NULL = django.db.models.deletion.CASCADE, django.db.models.deletion.SET_NULL
CATEGORIES = [("CLASSWORK", "Classwork"), ("HOMEWORK", "Homework"), ("TEST", "Tests"), ("PROJECT", "Projects"),
              ("PRACTICAL", "Practicals")]
BEHAVIOUR = [("KIND", "Kindness"), ("EFFORT", "Great effort"), ("HELP", "Helping others"), ("LEAD", "Leadership"),
             ("PROGRESS", "Big improvement"), ("HOMEWORK", "Excellent homework"), ("LATE", "Late to class"),
             ("NOHW", "Homework not done"), ("DISRUPT", "Disrupting the class"), ("UNIFORM", "Uniform"),
             ("PHONE", "Phone in class"), ("RUDE", "Disrespect")]


def _id():
    return ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID"))


def _tenant():
    return ("tenant", models.ForeignKey(blank=True, null=True, on_delete=CASCADE, to="tenants.tenant"))


def _file():
    return ("file", models.FileField(blank=True, null=True, storage=teaching.models._private,
                                     upload_to=teaching.models._upload))


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("academics", "0002_initial"),
        ("tenants", "0006_alter_tenant_owner"),
        ("users", "0011_alter_attendancesettings_school_starts_at"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="Gradebook",
            fields=[
                _id(),
                ("weights", models.JSONField(default=dict, help_text='{"TEST": 50, "HOMEWORK": 30, ...}')),
                ("report_columns", models.JSONField(default=dict, help_text='{"TEST": "ca1", "HOMEWORK": "ca2"}')),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("school_class", models.ForeignKey(on_delete=CASCADE, related_name="+", to="users.class")),
                ("subject", models.ForeignKey(on_delete=CASCADE, related_name="+", to="users.subject")),
                ("teacher", models.ForeignKey(null=True, on_delete=SET_NULL, related_name="gradebooks",
                                              to="users.staff")),
                ("term", models.ForeignKey(on_delete=CASCADE, related_name="+", to="academics.term")),
                _tenant(),
            ],
            options={"unique_together": {("school_class", "subject", "term")}},
        ),
        migrations.CreateModel(
            name="GradeItem",
            fields=[
                _id(),
                ("title", models.CharField(max_length=120)),
                ("category", models.CharField(choices=CATEGORIES, default="CLASSWORK", max_length=9)),
                ("max_score", models.DecimalField(decimal_places=1, default=Decimal("10"), max_digits=6)),
                ("date", models.DateField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("gradebook", models.ForeignKey(on_delete=CASCADE, related_name="items", to="teaching.gradebook")),
                _tenant(),
            ],
            options={"ordering": ["date", "id"]},
        ),
        migrations.CreateModel(
            name="GradeMark",
            fields=[
                _id(),
                ("score", models.DecimalField(blank=True, decimal_places=1, max_digits=6, null=True)),
                ("excused", models.BooleanField(default=False, help_text="Not counted (absent with a reason)")),
                ("item", models.ForeignKey(on_delete=CASCADE, related_name="marks", to="teaching.gradeitem")),
                ("student", models.ForeignKey(on_delete=CASCADE, related_name="+", to="users.student")),
                _tenant(),
            ],
            options={"unique_together": {("item", "student")}},
        ),
        migrations.CreateModel(
            name="BehaviourEntry",
            fields=[
                _id(),
                ("category", models.CharField(choices=BEHAVIOUR, max_length=10)),
                ("points", models.SmallIntegerField()),
                ("note", models.CharField(blank=True, max_length=255)),
                ("date", models.DateField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("staff", models.ForeignKey(null=True, on_delete=SET_NULL, related_name="+", to="users.staff")),
                ("student", models.ForeignKey(on_delete=CASCADE, related_name="behaviour", to="users.student")),
                _tenant(),
            ],
            options={"ordering": ["-date", "-id"]},
        ),
        migrations.CreateModel(
            name="CommentTemplate",
            fields=[
                _id(),
                ("band", models.CharField(choices=[("EXCELLENT", "Excellent"), ("GOOD", "Good"), ("FAIR", "Fair"),
                                                   ("WEAK", "Needs to work harder"), ("CONDUCT", "Conduct")],
                                          default="GOOD", max_length=9)),
                ("text", models.CharField(help_text="Use {first} for the first name", max_length=255)),
                ("uses", models.PositiveIntegerField(default=0)),
                ("staff", models.ForeignKey(blank=True, help_text="Empty = shared with the whole school", null=True,
                                            on_delete=CASCADE, related_name="+", to="users.staff")),
                _tenant(),
            ],
            options={"ordering": ["band", "-uses", "id"]},
        ),
        migrations.CreateModel(
            name="Resource",
            fields=[
                _id(),
                ("title", models.CharField(max_length=160)),
                ("level", models.CharField(blank=True, help_text="e.g. JSS 1, SS 2", max_length=40)),
                ("kind", models.CharField(choices=[("WORKSHEET", "Worksheet"), ("SLIDES", "Slides"),
                                                   ("NOTES", "Lesson notes"), ("PAST", "Past questions"),
                                                   ("VIDEO", "Video"), ("LINK", "Website"), ("OTHER", "Other")],
                                          default="WORKSHEET", max_length=9)),
                ("description", models.TextField(blank=True)),
                _file(),
                ("link", models.URLField(blank=True)),
                ("shared", models.BooleanField(default=True, help_text="Other teachers can find it")),
                ("downloads", models.PositiveIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("saved_by", models.ManyToManyField(blank=True, related_name="saved_resources", to="users.staff")),
                ("subject", models.ForeignKey(blank=True, null=True, on_delete=SET_NULL, related_name="+",
                                              to="users.subject")),
                ("uploaded_by", models.ForeignKey(null=True, on_delete=SET_NULL, related_name="+", to="users.staff")),
                _tenant(),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="TeacherTask",
            fields=[
                _id(),
                ("title", models.CharField(max_length=200)),
                ("notes", models.TextField(blank=True)),
                ("due_date", models.DateField(blank=True, null=True)),
                ("priority", models.CharField(choices=[("HIGH", "High"), ("NORMAL", "Normal"), ("LOW", "Low")],
                                              default="NORMAL", max_length=6)),
                ("done", models.BooleanField(default=False)),
                ("done_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("assigned_by", models.ForeignKey(blank=True, null=True, on_delete=SET_NULL, related_name="+",
                                                  to=settings.AUTH_USER_MODEL)),
                ("staff", models.ForeignKey(on_delete=CASCADE, related_name="tasks", to="users.staff")),
                _tenant(),
            ],
            options={"ordering": ["done", "due_date", "-created_at"]},
        ),
        migrations.CreateModel(
            name="DutyPost",
            fields=[
                _id(),
                ("name", models.CharField(help_text="e.g. Gate (morning), Dining hall, Break — field", max_length=100)),
                ("location", models.CharField(blank=True, max_length=100)),
                ("start_time", models.TimeField()),
                ("end_time", models.TimeField()),
                ("staff_needed", models.PositiveSmallIntegerField(default=1)),
                ("active", models.BooleanField(default=True)),
                _tenant(),
            ],
            options={"ordering": ["start_time", "name"]},
        ),
        migrations.CreateModel(
            name="DutyAssignment",
            fields=[
                _id(),
                ("date", models.DateField()),
                ("reminded", models.BooleanField(default=False)),
                ("post", models.ForeignKey(on_delete=CASCADE, related_name="assignments", to="teaching.dutypost")),
                ("staff", models.ForeignKey(on_delete=CASCADE, related_name="duties", to="users.staff")),
                _tenant(),
            ],
            options={"ordering": ["date", "post__start_time"], "unique_together": {("post", "staff", "date")}},
        ),
        migrations.CreateModel(
            name="HandoverNote",
            fields=[
                _id(),
                ("kind", models.CharField(choices=[("COVER", "Cover while I am away"),
                                                   ("CLASS", "New form teacher / end of year"),
                                                   ("SUBJECT", "Taking over a subject"), ("OTHER", "Other")],
                                          default="COVER", max_length=7)),
                ("start", models.DateField(blank=True, null=True)),
                ("end", models.DateField(blank=True, null=True)),
                ("covered", models.TextField(blank=True, help_text="What has been taught or done")),
                ("next_steps", models.TextField(blank=True, help_text="What comes next")),
                ("students", models.TextField(blank=True, help_text="Students to watch, needs, behaviour")),
                ("other", models.TextField(blank=True)),
                _file(),
                ("acknowledged_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("from_staff", models.ForeignKey(on_delete=CASCADE, related_name="handovers_given", to="users.staff")),
                ("school_class", models.ForeignKey(blank=True, null=True, on_delete=SET_NULL, related_name="+",
                                                   to="users.class")),
                ("subject", models.ForeignKey(blank=True, null=True, on_delete=SET_NULL, related_name="+",
                                              to="users.subject")),
                ("to_staff", models.ForeignKey(on_delete=CASCADE, related_name="handovers_received",
                                               to="users.staff")),
                _tenant(),
            ],
            options={"ordering": ["-created_at"]},
        ),
    ]
