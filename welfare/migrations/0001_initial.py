import django.db.models.deletion
import welfare.models
from django.conf import settings
from django.db import migrations, models

CASCADE, SET_NULL = django.db.models.deletion.CASCADE, django.db.models.deletion.SET_NULL


def _id():
    return ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID"))


def _tenant():
    return ("tenant", models.ForeignKey(blank=True, null=True, on_delete=CASCADE, to="tenants.tenant"))


def _user(name, blank=False):
    return (name, models.ForeignKey(blank=blank, null=True, on_delete=SET_NULL, related_name="+",
                                    to=settings.AUTH_USER_MODEL))


def _student(related, null=False):
    if null:
        return ("student", models.ForeignKey(blank=True, null=True, on_delete=CASCADE, related_name=related,
                                             to="users.student"))
    return ("student", models.ForeignKey(on_delete=CASCADE, related_name=related, to="users.student"))


def _staff(name):
    return (name, models.ForeignKey(blank=True, null=True, on_delete=SET_NULL, related_name="+", to="users.staff"))


def _staff_m2m(name):
    return (name, models.ManyToManyField(blank=True, related_name="+", to="users.staff"))


def _term():
    return ("term", models.ForeignKey(blank=True, null=True, on_delete=SET_NULL, related_name="+",
                                      to="academics.term"))


INCIDENT_CATEGORIES = [("FIGHT", "Fighting"), ("BULLY", "Bullying"), ("TRUANCY", "Truancy / leaving school"),
                       ("CHEAT", "Exam malpractice / cheating"), ("VANDAL", "Damage to property"),
                       ("THEFT", "Theft"), ("DISRESPECT", "Disrespect to staff"), ("PHONE", "Phone / device"),
                       ("SUBSTANCE", "Drugs, alcohol, smoking"), ("DRESS", "Uniform / appearance"),
                       ("LATE", "Persistent lateness"), ("CULT", "Cultism / gang activity"), ("OTHER", "Other")]
INCIDENT_ACTIONS = [("WARNING", "Warning"), ("DETENTION", "Detention"), ("LINES", "Extra work / manual labour"),
                    ("PARENT", "Parent meeting"), ("RESTORATIVE", "Restorative conversation"),
                    ("SUSPENSION", "Suspension"), ("REFER", "Referred to counsellor"), ("NONE", "No further action")]
VACCINES = [("BCG", "BCG"), ("OPV", "Polio (OPV/IPV)"), ("PENTA", "Pentavalent (DPT-HepB-Hib)"),
            ("PCV", "Pneumococcal (PCV)"), ("ROTA", "Rotavirus"), ("MEASLES", "Measles / MR"),
            ("YELLOW", "Yellow fever"), ("MENA", "Meningitis A"), ("HEPB", "Hepatitis B"), ("HPV", "HPV"),
            ("TD", "Tetanus-diphtheria (Td)"), ("TYPHOID", "Typhoid"), ("COVID", "COVID-19"), ("OTHER", "Other")]
CONCERN_CATEGORIES = [("PHYSICAL", "Physical harm"), ("EMOTIONAL", "Emotional harm"), ("SEXUAL", "Sexual harm"),
                      ("NEGLECT", "Neglect"), ("SELFHARM", "Self-harm or suicide risk"), ("ONLINE", "Online safety"),
                      ("EXPLOIT", "Exploitation / child labour"), ("BULLYING", "Serious bullying"),
                      ("DISCLOSURE", "Something the child told me"), ("OTHER", "Other")]


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
            name="WelfareSettings",
            fields=[
                _id(),
                _staff_m2m("nurses"), _staff_m2m("counsellors"), _staff_m2m("safeguarding_leads"),
                _staff_m2m("discipline_team"),
                ("tell_parents_incidents", models.BooleanField(default=True,
                                                               help_text="Email families about discipline incidents")),
                ("immunisation_reminder_days", models.PositiveSmallIntegerField(default=14)),
                _tenant(),
            ],
        ),
        migrations.CreateModel(
            name="HealthPlan",
            fields=[
                _id(),
                ("name", models.CharField(help_text="e.g. School health plan 2026/27", max_length=120)),
                ("provider", models.CharField(help_text="HMO or insurer", max_length=120)),
                ("phone", models.CharField(blank=True, max_length=30)),
                ("hospital", models.CharField(blank=True, help_text="Main hospital or clinic on the plan",
                                              max_length=200)),
                ("covers", models.TextField(blank=True)),
                ("starts", models.DateField(blank=True, null=True)),
                ("ends", models.DateField(blank=True, null=True)),
                ("active", models.BooleanField(default=True)),
                _tenant(),
            ],
            options={"ordering": ["-active", "name"]},
        ),
        migrations.CreateModel(
            name="MedicalProfile",
            fields=[
                _id(),
                ("student", models.OneToOneField(on_delete=CASCADE, related_name="medical", to="users.student")),
                ("blood_group", models.CharField(blank=True, choices=[
                    ("", "—"), ("A+", "A+"), ("A-", "A−"), ("B+", "B+"), ("B-", "B−"), ("AB+", "AB+"),
                    ("AB-", "AB−"), ("O+", "O+"), ("O-", "O−")], max_length=3)),
                ("genotype", models.CharField(blank=True, choices=[
                    ("", "—"), ("AA", "AA"), ("AS", "AS"), ("AC", "AC"), ("SS", "SS"), ("SC", "SC")], max_length=2)),
                ("allergies", models.CharField(blank=True, help_text="Food, drugs, insects…", max_length=255)),
                ("conditions", models.TextField(blank=True, help_text="Asthma, epilepsy, diabetes, sickle cell…")),
                ("emergency_medication", models.CharField(
                    blank=True, help_text="e.g. Inhaler in school bag; EpiPen at the sick bay", max_length=255)),
                ("diet", models.CharField(blank=True, max_length=160)),
                ("doctor_name", models.CharField(blank=True, max_length=160)),
                ("doctor_phone", models.CharField(blank=True, max_length=20)),
                ("hospital", models.CharField(blank=True, max_length=200)),
                ("consent_first_aid", models.BooleanField(
                    default=True, help_text="Parent agrees to first aid and paracetamol")),
                ("consent_hospital", models.BooleanField(
                    default=True, help_text="Parent agrees to emergency hospital treatment")),
                ("plan", models.ForeignKey(blank=True, null=True, on_delete=SET_NULL, related_name="members",
                                           to="welfare.healthplan")),
                ("member_number", models.CharField(blank=True, max_length=60)),
                ("cover_expires", models.DateField(blank=True, null=True)),
                ("other_insurance", models.CharField(blank=True, help_text="Family's own HMO / policy",
                                                     max_length=160)),
                ("notes", models.TextField(blank=True, help_text="Nurses only")),
                _user("updated_by", blank=True),
                ("updated_at", models.DateTimeField(auto_now=True)),
                _tenant(),
            ],
        ),
        migrations.CreateModel(
            name="Immunisation",
            fields=[
                _id(),
                _student("immunisations"),
                ("vaccine", models.CharField(choices=VACCINES, max_length=8)),
                ("other_name", models.CharField(blank=True, max_length=80)),
                ("dose", models.CharField(blank=True, help_text="e.g. Dose 2, booster", max_length=30)),
                ("given_on", models.DateField(blank=True, null=True)),
                ("next_due", models.DateField(blank=True, null=True)),
                ("proof", models.FileField(blank=True, storage=welfare.models._private,
                                           upload_to=welfare.models._upload)),
                ("verified", models.BooleanField(default=False)),
                ("reminded_at", models.DateTimeField(blank=True, null=True)),
                _user("recorded_by"),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                _tenant(),
            ],
            options={"ordering": ["student", "-given_on"]},
        ),
        migrations.CreateModel(
            name="MedicationPlan",
            fields=[
                _id(),
                _student("medication_plans"),
                ("medicine", models.CharField(max_length=120)),
                ("dose", models.CharField(max_length=60)),
                ("times", models.CharField(help_text='e.g. 12:00, or "when needed"', max_length=60)),
                ("starts", models.DateField()),
                ("ends", models.DateField(blank=True, null=True)),
                ("reason", models.CharField(blank=True, max_length=160)),
                ("parent_consent", models.BooleanField(default=False)),
                ("active", models.BooleanField(default=True)),
                _user("added_by"),
                _tenant(),
            ],
            options={"ordering": ["student__full_name", "medicine"]},
        ),
        migrations.CreateModel(
            name="MedicationDose",
            fields=[
                _id(),
                _student("medication_doses"),
                ("plan", models.ForeignKey(blank=True, null=True, on_delete=SET_NULL, related_name="doses",
                                           to="welfare.medicationplan")),
                ("medicine", models.CharField(max_length=120)),
                ("dose", models.CharField(max_length=60)),
                ("given_at", models.DateTimeField()),
                ("reason", models.CharField(blank=True, max_length=160)),
                ("refused", models.BooleanField(default=False)),
                ("note", models.CharField(blank=True, max_length=255)),
                _user("given_by"),
                _tenant(),
            ],
            options={"ordering": ["-given_at"]},
        ),
        migrations.CreateModel(
            name="Screening",
            fields=[
                _id(),
                _student("screenings"),
                ("date", models.DateField()),
                _term(),
                ("height_cm", models.DecimalField(blank=True, decimal_places=1, max_digits=5, null=True)),
                ("weight_kg", models.DecimalField(blank=True, decimal_places=1, max_digits=5, null=True)),
                ("vision_left", models.CharField(blank=True, help_text="e.g. 6/6", max_length=10)),
                ("vision_right", models.CharField(blank=True, max_length=10)),
                ("hearing_ok", models.BooleanField(blank=True, null=True)),
                ("dental_ok", models.BooleanField(blank=True, null=True)),
                ("notes", models.CharField(blank=True, max_length=255)),
                ("refer", models.BooleanField(default=False, help_text="Family advised to see a doctor")),
                _user("by"),
                _tenant(),
            ],
            options={"ordering": ["-date"]},
        ),
        migrations.CreateModel(
            name="Accident",
            fields=[
                _id(),
                _student("accidents", null=True),
                ("other_person", models.CharField(blank=True, help_text="Staff member or visitor hurt",
                                                  max_length=160)),
                ("happened_at", models.DateTimeField()),
                ("place", models.CharField(choices=[
                    ("PLAYGROUND", "Playground"), ("SPORTS", "Sports / PE"), ("CLASS", "Classroom"),
                    ("LAB", "Laboratory"), ("STAIRS", "Stairs / corridor"), ("HOSTEL", "Hostel"),
                    ("TRIP", "School trip"), ("BUS", "School bus"), ("OTHER", "Other")], max_length=10)),
                ("activity", models.CharField(blank=True, max_length=160)),
                ("injury", models.CharField(max_length=255)),
                ("body_part", models.CharField(blank=True, max_length=80)),
                ("first_aid", models.TextField(blank=True)),
                ("severity", models.CharField(choices=[
                    ("MINOR", "Minor — first aid only"), ("MODERATE", "Needs a doctor"),
                    ("SERIOUS", "Serious — hospital")], default="MINOR", max_length=8)),
                ("hospital", models.BooleanField(default=False)),
                ("witnesses", models.CharField(blank=True, max_length=255)),
                ("parent_told", models.BooleanField(default=False)),
                ("follow_up", models.TextField(blank=True,
                                               help_text="Checks, what changed to stop it happening again")),
                ("closed", models.BooleanField(default=False)),
                _user("reported_by"),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                _tenant(),
            ],
            options={"ordering": ["-happened_at"]},
        ),
        migrations.CreateModel(
            name="Incident",
            fields=[
                _id(),
                _student("incidents"),
                ("group", models.CharField(blank=True, help_text="Shared by everyone in the same incident",
                                           max_length=12)),
                ("date", models.DateField()),
                ("place", models.CharField(blank=True, max_length=120)),
                ("category", models.CharField(choices=INCIDENT_CATEGORIES, max_length=10)),
                ("severity", models.PositiveSmallIntegerField(
                    choices=[(1, "Minor"), (2, "Moderate"), (3, "Serious"), (4, "Very serious")], default=1)),
                ("description", models.TextField()),
                ("action", models.CharField(blank=True, choices=INCIDENT_ACTIONS, max_length=11)),
                ("action_note", models.CharField(blank=True, max_length=255)),
                ("demerits", models.PositiveSmallIntegerField(default=0)),
                ("parent_visible", models.BooleanField(default=True)),
                ("parents_told_at", models.DateTimeField(blank=True, null=True)),
                ("status", models.CharField(choices=[("OPEN", "Open"), ("CLOSED", "Closed")], default="OPEN",
                                            max_length=6)),
                _user("reported_by"),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                _tenant(),
            ],
            options={"ordering": ["-date", "-id"]},
        ),
        migrations.CreateModel(
            name="Threshold",
            fields=[
                _id(),
                ("name", models.CharField(help_text="e.g. Head's commendation, Report to the vice principal",
                                          max_length=80)),
                ("kind", models.CharField(choices=[("MERIT", "Merit points reach"),
                                                   ("DEMERIT", "Demerit points reach")], max_length=7)),
                ("points", models.PositiveSmallIntegerField()),
                ("action", models.CharField(help_text="What happens next", max_length=255)),
                ("tell_parents", models.BooleanField(default=True)),
                _tenant(),
            ],
            options={"ordering": ["kind", "points"]},
        ),
        migrations.CreateModel(
            name="ThresholdHit",
            fields=[
                _id(),
                ("threshold", models.ForeignKey(on_delete=CASCADE, related_name="hits", to="welfare.threshold")),
                _student("threshold_hits"),
                ("term", models.ForeignKey(blank=True, null=True, on_delete=CASCADE, related_name="+",
                                           to="academics.term")),
                ("total", models.IntegerField()),
                ("reached_at", models.DateTimeField(auto_now_add=True)),
                ("done", models.BooleanField(default=False)),
                _tenant(),
            ],
            options={"ordering": ["-reached_at"], "unique_together": {("threshold", "student", "term")}},
        ),
        migrations.CreateModel(
            name="DetentionSession",
            fields=[
                _id(),
                ("date", models.DateField()),
                ("starts", models.TimeField()),
                ("ends", models.TimeField()),
                ("room", models.CharField(max_length=60)),
                _staff("supervisor"),
                ("capacity", models.PositiveSmallIntegerField(default=30)),
                _tenant(),
            ],
            options={"ordering": ["-date", "starts"]},
        ),
        migrations.CreateModel(
            name="Detention",
            fields=[
                _id(),
                ("session", models.ForeignKey(on_delete=CASCADE, related_name="detentions",
                                              to="welfare.detentionsession")),
                _student("detentions"),
                ("incident", models.ForeignKey(blank=True, null=True, on_delete=SET_NULL, related_name="+",
                                               to="welfare.incident")),
                ("reason", models.CharField(max_length=255)),
                ("status", models.CharField(choices=[("SET", "Set"), ("ATTENDED", "Attended"), ("MISSED", "Missed"),
                                                     ("EXCUSED", "Excused")], default="SET", max_length=8)),
                _user("set_by"),
                _tenant(),
            ],
            options={"unique_together": {("session", "student")}},
        ),
        migrations.CreateModel(
            name="SuspensionCase",
            fields=[
                _id(),
                _student("suspension_cases"),
                ("incident", models.ForeignKey(blank=True, null=True, on_delete=SET_NULL, related_name="+",
                                               to="welfare.incident")),
                ("reason", models.TextField()),
                ("starts", models.DateField()),
                ("ends", models.DateField()),
                ("status", models.CharField(choices=[("PROPOSED", "Waiting for approval"),
                                                     ("APPROVED", "Approved — student away"),
                                                     ("DECLINED", "Declined"),
                                                     ("RETURNED", "Returned after meeting")],
                                            default="PROPOSED", max_length=8)),
                _user("proposed_by"),
                _user("decided_by", blank=True),
                ("decision_note", models.CharField(blank=True, max_length=255)),
                ("meeting_at", models.DateTimeField(blank=True, null=True,
                                                    help_text="Return meeting with the family")),
                ("meeting_notes", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                _tenant(),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="RestorativeMeeting",
            fields=[
                _id(),
                ("incident", models.ForeignKey(blank=True, null=True, on_delete=SET_NULL, related_name="+",
                                               to="welfare.incident")),
                ("date", models.DateField()),
                ("students", models.ManyToManyField(related_name="restorative_meetings", to="users.student")),
                ("others", models.CharField(blank=True, help_text="Staff or parents present", max_length=255)),
                ("what_happened", models.TextField()),
                ("who_was_affected", models.TextField(blank=True)),
                ("agreement", models.TextField(help_text="What everyone agreed to do")),
                ("outcome", models.CharField(choices=[("RESOLVED", "Resolved"), ("PARTLY", "Partly resolved"),
                                                      ("NOT", "Not resolved")], default="RESOLVED", max_length=8)),
                ("follow_up_on", models.DateField(blank=True, null=True)),
                ("followed_up", models.BooleanField(default=False)),
                ("follow_up_note", models.CharField(blank=True, max_length=255)),
                _user("facilitator"),
                _tenant(),
            ],
            options={"ordering": ["-date", "-id"]},
        ),
        migrations.CreateModel(
            name="CounsellingCase",
            fields=[
                _id(),
                _student("counselling_cases"),
                ("reason", models.CharField(choices=[
                    ("ACADEMIC", "Academic worries"), ("FAMILY", "Family"), ("FRIENDS", "Friendships / bullying"),
                    ("MOOD", "Mood / anxiety"), ("GRIEF", "Bereavement"), ("BEHAVIOUR", "Behaviour"),
                    ("CAREER", "Career guidance"), ("OTHER", "Other")], default="OTHER", max_length=9)),
                ("summary", models.CharField(blank=True, help_text="Only counsellors see this", max_length=255)),
                ("status", models.CharField(choices=[("REQUESTED", "Asked for help"), ("OPEN", "Open"),
                                                     ("CLOSED", "Closed")], default="OPEN", max_length=9)),
                ("risk", models.CharField(choices=[("LOW", "Low"), ("MEDIUM", "Medium"), ("HIGH", "High")],
                                          default="LOW", max_length=6)),
                _staff("counsellor"),
                ("self_referred", models.BooleanField(default=False)),
                _user("opened_by"),
                ("opened_at", models.DateTimeField(auto_now_add=True)),
                ("closed_at", models.DateTimeField(blank=True, null=True)),
                _tenant(),
            ],
            options={"ordering": ["-opened_at"]},
        ),
        migrations.CreateModel(
            name="CounsellingNote",
            fields=[
                _id(),
                ("case", models.ForeignKey(on_delete=CASCADE, related_name="notes", to="welfare.counsellingcase")),
                ("date", models.DateField()),
                ("text", models.TextField()),
                ("next_session", models.DateField(blank=True, null=True)),
                _user("author"),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                _tenant(),
            ],
            options={"ordering": ["-date", "-id"]},
        ),
        migrations.CreateModel(
            name="Concern",
            fields=[
                _id(),
                _student("concerns"),
                ("category", models.CharField(choices=CONCERN_CATEGORIES, max_length=10)),
                ("description", models.TextField(
                    help_text="What you saw or heard — the child's own words where you can")),
                ("immediate_danger", models.BooleanField(default=False)),
                ("status", models.CharField(choices=[("NEW", "New"), ("REVIEW", "Being looked at"),
                                                     ("REFERRED", "Referred outside school"),
                                                     ("MONITOR", "Monitoring"), ("CLOSED", "Closed")],
                                            default="NEW", max_length=8)),
                _staff("lead"),
                ("referred_to", models.CharField(blank=True, max_length=160,
                                                 help_text="e.g. Ministry of Women Affairs, police, hospital")),
                _user("raised_by"),
                ("raised_at", models.DateTimeField(auto_now_add=True)),
                _tenant(),
            ],
            options={"ordering": ["-raised_at"]},
        ),
        migrations.CreateModel(
            name="ConcernAction",
            fields=[
                _id(),
                ("concern", models.ForeignKey(on_delete=CASCADE, related_name="actions", to="welfare.concern")),
                ("text", models.TextField()),
                ("status", models.CharField(blank=True, max_length=8)),
                _user("by"),
                ("at", models.DateTimeField(auto_now_add=True)),
                _tenant(),
            ],
            options={"ordering": ["at"]},
        ),
        migrations.CreateModel(
            name="SupportPlan",
            fields=[
                _id(),
                _student("support_plans"),
                ("title", models.CharField(default="Individual support plan", max_length=160)),
                ("needs", models.TextField(help_text="What the student finds hard")),
                ("strengths", models.TextField(blank=True)),
                ("goals", models.TextField(help_text="One per line — what we want to see by the review")),
                ("strategies", models.TextField(help_text="What teachers and parents will do")),
                _staff("owner"),
                ("starts", models.DateField()),
                ("review_on", models.DateField()),
                ("parent_agreed", models.BooleanField(default=False)),
                ("status", models.CharField(choices=[("ACTIVE", "Active"), ("CLOSED", "Closed")], default="ACTIVE",
                                            max_length=6)),
                ("reminded_at", models.DateTimeField(blank=True, null=True)),
                _user("created_by"),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                _tenant(),
            ],
            options={"ordering": ["review_on"]},
        ),
        migrations.CreateModel(
            name="PlanReview",
            fields=[
                _id(),
                ("plan", models.ForeignKey(on_delete=CASCADE, related_name="reviews", to="welfare.supportplan")),
                ("date", models.DateField()),
                ("progress", models.TextField()),
                ("rating", models.CharField(choices=[("BEHIND", "Not yet"), ("SOME", "Some progress"),
                                                     ("MET", "Goals met")], default="SOME", max_length=8)),
                ("next_steps", models.TextField(blank=True)),
                _user("by"),
                _tenant(),
            ],
            options={"ordering": ["-date"]},
        ),
        migrations.CreateModel(
            name="Team",
            fields=[
                _id(),
                ("name", models.CharField(help_text="e.g. U-15 Boys Football", max_length=80)),
                ("sport", models.CharField(max_length=60)),
                _staff("coach"),
                ("season", models.CharField(blank=True, max_length=40)),
                ("active", models.BooleanField(default=True)),
                _tenant(),
            ],
            options={"ordering": ["sport", "name"]},
        ),
        migrations.CreateModel(
            name="TeamMember",
            fields=[
                _id(),
                ("team", models.ForeignKey(on_delete=CASCADE, related_name="members", to="welfare.team")),
                _student("teams"),
                ("position", models.CharField(blank=True, max_length=40)),
                ("captain", models.BooleanField(default=False)),
                ("since", models.DateField(auto_now_add=True)),
                _tenant(),
            ],
            options={"ordering": ["-captain", "student__full_name"], "unique_together": {("team", "student")}},
        ),
        migrations.CreateModel(
            name="FitnessTest",
            fields=[
                _id(),
                _student("fitness_tests"),
                ("test", models.CharField(choices=[
                    ("SPRINT", "100 m sprint (seconds)"), ("BEEP", "Bleep test (level)"),
                    ("RUN", "1.5 km run (minutes)"), ("PUSHUP", "Push-ups in 1 min"), ("SITUP", "Sit-ups in 1 min"),
                    ("JUMP", "Standing long jump (cm)"), ("REACH", "Sit and reach (cm)")], max_length=6)),
                ("value", models.DecimalField(decimal_places=2, max_digits=7)),
                ("date", models.DateField()),
                _user("by"),
                _tenant(),
            ],
            options={"ordering": ["-date"]},
        ),
        migrations.CreateModel(
            name="Resource",
            fields=[
                _id(),
                ("title", models.CharField(max_length=160)),
                ("topic", models.CharField(choices=[
                    ("STRESS", "Exam and school stress"), ("MOOD", "Feeling low or anxious"),
                    ("FRIENDS", "Friends and bullying"), ("ONLINE", "Staying safe online"),
                    ("SLEEP", "Sleep, food and exercise"), ("GRIEF", "Loss and grief"),
                    ("PARENTING", "For parents"), ("HELP", "Getting help")], max_length=9)),
                ("audience", models.CharField(choices=[("BOTH", "Students and parents"), ("STUDENTS", "Students"),
                                                       ("PARENTS", "Parents")], default="BOTH", max_length=8)),
                ("body", models.TextField()),
                ("link", models.URLField(blank=True)),
                ("contact", models.CharField(blank=True, help_text="Phone line or person to speak to",
                                             max_length=160)),
                ("published", models.BooleanField(default=True)),
                ("order", models.PositiveSmallIntegerField(default=0)),
                _tenant(),
            ],
            options={"ordering": ["topic", "order", "title"]},
        ),
    ]
