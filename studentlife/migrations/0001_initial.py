import django.db.models.deletion
import studentlife.models
from django.conf import settings
from django.db import migrations, models

CASCADE, SET_NULL = django.db.models.deletion.CASCADE, django.db.models.deletion.SET_NULL


def _id():
    return ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID"))


def _tenant():
    return ("tenant", models.ForeignKey(blank=True, null=True, on_delete=CASCADE, to="tenants.tenant"))


def _file(name):
    return (name, models.FileField(blank=True, null=True, storage=studentlife.models._private,
                                   upload_to=studentlife.models._upload))


def _student(related, name="student"):
    return (name, models.ForeignKey(on_delete=CASCADE, related_name=related, to="users.student"))


def _staff(name, blank=True):
    return (name, models.ForeignKey(blank=blank, null=True, on_delete=SET_NULL, related_name="+", to="users.staff"))


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("tenants", "0006_alter_tenant_owner"),
        ("users", "0011_alter_attendancesettings_school_starts_at"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="LifeSettings",
            fields=[
                _id(),
                ("leaderboards", models.BooleanField(default=True)),
                ("notes_need_approval", models.BooleanField(default=True,
                                                            help_text="A teacher checks shared notes first")),
                ("tutoring", models.BooleanField(default=True)),
                ("handbook_version", models.PositiveIntegerField(default=1)),
                ("safeguarding_leads", models.ManyToManyField(
                    blank=True, help_text="Receive anonymous reports and wellbeing alerts", related_name="+",
                    to="users.staff")),
                _tenant(),
            ],
        ),
        migrations.CreateModel(
            name="LeaderboardEntry",
            fields=[
                _id(),
                ("nickname", models.CharField(blank=True, help_text="Shown instead of your name if set",
                                              max_length=40)),
                ("joined_at", models.DateTimeField(auto_now_add=True)),
                ("student", models.OneToOneField(on_delete=CASCADE, related_name="leaderboard", to="users.student")),
                _tenant(),
            ],
        ),
        migrations.CreateModel(
            name="PortfolioItem",
            fields=[
                _id(),
                ("title", models.CharField(max_length=160)),
                ("kind", models.CharField(choices=[("PROJECT", "Project"), ("ESSAY", "Essay or writing"),
                                                   ("ART", "Art or design"), ("CERT", "Certificate"),
                                                   ("COMPETITION", "Competition"), ("SERVICE", "Community service"),
                                                   ("OTHER", "Other")], default="PROJECT", max_length=11)),
                ("description", models.TextField(blank=True)),
                ("date", models.DateField()),
                _file("file"),
                ("link", models.URLField(blank=True)),
                ("share_with_family", models.BooleanField(default=True)),
                ("featured", models.BooleanField(default=False)),
                ("teacher_comment", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                _staff("commented_by"),
                _student("portfolio"),
                _tenant(),
            ],
            options={"ordering": ["-featured", "-date"]},
        ),
        migrations.CreateModel(
            name="CareerProfile",
            fields=[
                _id(),
                ("answers", models.JSONField(default=dict, help_text="Interest quiz answers")),
                ("clusters", models.JSONField(default=list, help_text="Top interest areas from the quiz")),
                ("chosen", models.JSONField(default=list, help_text="Careers the student is considering")),
                ("notes", models.TextField(blank=True, help_text="The student's own thoughts")),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("student", models.OneToOneField(on_delete=CASCADE, related_name="career", to="users.student")),
                _tenant(),
            ],
        ),
        migrations.CreateModel(
            name="CareerNote",
            fields=[
                _id(),
                ("note", models.TextField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                _staff("staff", blank=False),
                _student("career_notes"),
                _tenant(),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="HandbookSection",
            fields=[
                _id(),
                ("title", models.CharField(max_length=160)),
                ("body", models.TextField()),
                ("order", models.PositiveSmallIntegerField(default=0)),
                ("published", models.BooleanField(default=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                _tenant(),
            ],
            options={"ordering": ["order", "id"]},
        ),
        migrations.CreateModel(
            name="HandbookAck",
            fields=[
                _id(),
                ("version", models.PositiveIntegerField()),
                ("acknowledged_at", models.DateTimeField(auto_now_add=True)),
                ("user", models.ForeignKey(on_delete=CASCADE, related_name="+", to=settings.AUTH_USER_MODEL)),
                _tenant(),
            ],
            options={"unique_together": {("tenant", "user", "version")}},
        ),
        migrations.CreateModel(
            name="SafetyReport",
            fields=[
                _id(),
                ("reference", models.CharField(max_length=12)),
                ("code_hash", models.CharField(help_text="Lets an anonymous reporter check back", max_length=64)),
                ("kind", models.CharField(choices=[("BULLYING", "Bullying"), ("CYBER", "Online bullying"),
                                                   ("HARASSMENT", "Harassment or abuse"),
                                                   ("SAFETY", "Something unsafe"),
                                                   ("WELLBEING", "Worried about a friend"),
                                                   ("OTHER", "Something else")], default="BULLYING", max_length=10)),
                ("where", models.CharField(blank=True, max_length=160)),
                ("when", models.CharField(blank=True, max_length=120)),
                ("who", models.CharField(blank=True, help_text="Who was involved, if known", max_length=255)),
                ("description", models.TextField()),
                ("status", models.CharField(choices=[("NEW", "Received"), ("REVIEWING", "Being looked into"),
                                                     ("ACTIONED", "Action taken"), ("CLOSED", "Closed")],
                                            default="NEW", max_length=9)),
                ("urgent", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("reporter", models.ForeignKey(blank=True, help_text="Empty when the report is anonymous", null=True,
                                               on_delete=SET_NULL, related_name="+", to=settings.AUTH_USER_MODEL)),
                _tenant(),
            ],
            options={"ordering": ["-created_at"], "unique_together": {("tenant", "reference")}},
        ),
        migrations.CreateModel(
            name="ReportMessage",
            fields=[
                _id(),
                ("from_school", models.BooleanField(default=True)),
                ("body", models.TextField()),
                ("internal", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("author", models.ForeignKey(blank=True, null=True, on_delete=SET_NULL, related_name="+",
                                             to=settings.AUTH_USER_MODEL)),
                ("report", models.ForeignKey(on_delete=CASCADE, related_name="messages",
                                             to="studentlife.safetyreport")),
                _tenant(),
            ],
            options={"ordering": ["created_at"]},
        ),
        migrations.CreateModel(
            name="Checkin",
            fields=[
                _id(),
                ("date", models.DateField()),
                ("mood", models.PositiveSmallIntegerField(choices=[(5, "Great"), (4, "Good"), (3, "Okay"), (2, "Low"),
                                                                   (1, "Very low")])),
                ("sleep", models.PositiveSmallIntegerField(blank=True, help_text="Hours of sleep", null=True)),
                ("feelings", models.JSONField(default=list)),
                ("note", models.CharField(blank=True, max_length=500)),
                ("wants_talk", models.BooleanField(default=False)),
                ("flagged", models.BooleanField(default=False)),
                ("followed_up_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                _staff("followed_up_by"),
                _student("checkins"),
                _tenant(),
            ],
            options={"ordering": ["-date"], "unique_together": {("student", "date")}},
        ),
        migrations.CreateModel(
            name="TutorOffer",
            fields=[
                _id(),
                ("about", models.CharField(blank=True, help_text="Topics you can help with", max_length=255)),
                ("when", models.CharField(blank=True, help_text="e.g. Break time, Tuesdays after school",
                                          max_length=120)),
                ("active", models.BooleanField(default=True)),
                ("approved", models.BooleanField(default=False, help_text="A teacher has approved this tutor")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                _student("tutor_offers"),
                ("subject", models.ForeignKey(on_delete=CASCADE, related_name="+", to="users.subject")),
                _tenant(),
            ],
            options={"unique_together": {("student", "subject")}},
        ),
        migrations.CreateModel(
            name="TutorSession",
            fields=[
                _id(),
                ("topic", models.CharField(max_length=160)),
                ("proposed", models.CharField(blank=True, help_text="When and where (at school)", max_length=120)),
                ("status", models.CharField(choices=[("REQUESTED", "Requested"), ("ACCEPTED", "Agreed"),
                                                     ("DONE", "Held"), ("DECLINED", "Declined"),
                                                     ("CANCELLED", "Cancelled")], default="REQUESTED",
                                            max_length=9)),
                ("rating", models.PositiveSmallIntegerField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                _student("tutoring_received", name="learner"),
                ("offer", models.ForeignKey(on_delete=CASCADE, related_name="sessions", to="studentlife.tutoroffer")),
                _tenant(),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="Goal",
            fields=[
                _id(),
                ("title", models.CharField(max_length=160)),
                ("category", models.CharField(choices=[("ACADEMIC", "Studies"), ("SKILL", "A new skill"),
                                                       ("SPORT", "Sport and health"),
                                                       ("CHARACTER", "Character and habits"), ("OTHER", "Other")],
                                              default="ACADEMIC", max_length=9)),
                ("measure", models.CharField(blank=True, help_text="How you will know you got there",
                                             max_length=255)),
                ("target_date", models.DateField(blank=True, null=True)),
                ("progress", models.PositiveSmallIntegerField(default=0)),
                ("status", models.CharField(choices=[("ACTIVE", "Working on it"), ("ACHIEVED", "Achieved"),
                                                     ("DROPPED", "Dropped")], default="ACTIVE", max_length=8)),
                ("teacher_comment", models.CharField(blank=True, max_length=500)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("achieved_at", models.DateTimeField(blank=True, null=True)),
                _student("goals"),
                _tenant(),
            ],
            options={"ordering": ["status", "target_date", "-created_at"]},
        ),
        migrations.CreateModel(
            name="GoalUpdate",
            fields=[
                _id(),
                ("progress", models.PositiveSmallIntegerField()),
                ("note", models.CharField(blank=True, max_length=500)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("goal", models.ForeignKey(on_delete=CASCADE, related_name="updates", to="studentlife.goal")),
                _tenant(),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="SharedNote",
            fields=[
                _id(),
                ("level", models.CharField(help_text="Year group the notes are for, e.g. JSS 2", max_length=40)),
                ("title", models.CharField(max_length=160)),
                ("body", models.TextField(blank=True)),
                _file("file"),
                ("status", models.CharField(choices=[("PENDING", "Waiting for a teacher"), ("APPROVED", "Shared"),
                                                     ("HIDDEN", "Hidden")], default="PENDING", max_length=8)),
                ("reports", models.PositiveSmallIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                _student("shared_notes", name="author"),
                _staff("reviewed_by"),
                ("subject", models.ForeignKey(on_delete=CASCADE, related_name="+", to="users.subject")),
                _tenant(),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="NoteLike",
            fields=[
                _id(),
                ("note", models.ForeignKey(on_delete=CASCADE, related_name="likes", to="studentlife.sharednote")),
                _student("+"),
                _tenant(),
            ],
            options={"unique_together": {("note", "student")}},
        ),
    ]
