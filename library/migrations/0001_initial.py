from decimal import Decimal

import django.db.models.deletion
import library.models
from django.conf import settings
from django.db import migrations, models

CASCADE, SET_NULL = django.db.models.deletion.CASCADE, django.db.models.deletion.SET_NULL
ZERO = Decimal("0")
LEVELS = [("ALL", "All ages"), ("NURSERY", "Nursery"), ("LOWER", "Primary 1–3"), ("UPPER", "Primary 4–6"),
          ("JSS", "Junior secondary"), ("SSS", "Senior secondary"), ("STAFF", "Staff / reference")]


def _id():
    return ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID"))


def _tenant():
    return ("tenant", models.ForeignKey(blank=True, null=True, on_delete=CASCADE, to="tenants.tenant"))


def _user(name, blank=False):
    return (name, models.ForeignKey(blank=blank, null=True, on_delete=SET_NULL, related_name="+",
                                    to=settings.AUTH_USER_MODEL))


def _student(related):
    return ("student", models.ForeignKey(blank=True, null=True, on_delete=CASCADE, related_name=related,
                                         to="users.student"))


def _staff(related="+"):
    return ("staff", models.ForeignKey(blank=True, null=True, on_delete=CASCADE, related_name=related,
                                       to="users.staff"))


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("academics", "0002_initial"),
        ("finance", "0011_alter_feestructure_category_and_more"),
        ("tenants", "0006_alter_tenant_owner"),
        ("users", "0011_alter_attendancesettings_school_starts_at"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="LibrarySettings",
            fields=[
                _id(),
                ("loan_days", models.PositiveSmallIntegerField(default=14)),
                ("student_max_loans", models.PositiveSmallIntegerField(default=2)),
                ("staff_max_loans", models.PositiveSmallIntegerField(default=5)),
                ("max_renewals", models.PositiveSmallIntegerField(default=1)),
                ("fine_per_day", models.DecimalField(decimal_places=2, default=Decimal("20"), max_digits=8)),
                ("fine_cap", models.DecimalField(decimal_places=2, default=Decimal("1000"), max_digits=10,
                                                 help_text="Most a single late book can be fined (0 = no limit)")),
                ("grace_days", models.PositiveSmallIntegerField(default=0)),
                ("remind_days_before", models.PositiveSmallIntegerField(default=2)),
                ("hold_days", models.PositiveSmallIntegerField(default=3,
                                                               help_text="Days a reserved book is kept at the desk")),
                ("block_when_owing", models.BooleanField(default=True,
                                                         help_text="No new loans while charges are unpaid")),
                ("opening_hours", models.TextField(blank=True, help_text="Shown on the catalogue page")),
                ("managers", models.ManyToManyField(blank=True, related_name="+", to="users.staff")),
                _tenant(),
            ],
        ),
        migrations.CreateModel(
            name="Branch",
            fields=[
                _id(),
                ("name", models.CharField(help_text="e.g. Main library, Junior library, Campus B", max_length=80)),
                ("location", models.CharField(blank=True, max_length=160)),
                ("active", models.BooleanField(default=True)),
                _tenant(),
            ],
            options={"ordering": ["name"]},
        ),
        migrations.CreateModel(
            name="Donation",
            fields=[
                _id(),
                ("donor_name", models.CharField(max_length=160)),
                ("donor_email", models.EmailField(blank=True, max_length=254)),
                ("donor_phone", models.CharField(blank=True, max_length=20)),
                ("date", models.DateField()),
                ("note", models.CharField(blank=True, max_length=255)),
                ("acknowledged_at", models.DateTimeField(blank=True, null=True)),
                _user("recorded_by"),
                _tenant(),
            ],
            options={"ordering": ["-date", "-id"]},
        ),
        migrations.CreateModel(
            name="Book",
            fields=[
                _id(),
                ("title", models.CharField(max_length=255)),
                ("authors", models.CharField(blank=True, max_length=255)),
                ("isbn", models.CharField(blank=True, db_index=True, max_length=17)),
                ("publisher", models.CharField(blank=True, max_length=160)),
                ("year", models.PositiveSmallIntegerField(blank=True, null=True)),
                ("edition", models.CharField(blank=True, max_length=40)),
                ("subject", models.CharField(blank=True, help_text="e.g. Mathematics, Fiction, Biography",
                                             max_length=80)),
                ("level", models.CharField(choices=LEVELS, default="ALL", max_length=8)),
                ("keywords", models.CharField(blank=True, max_length=255)),
                ("description", models.TextField(blank=True)),
                ("cover_url", models.URLField(blank=True)),
                ("is_textbook", models.BooleanField(default=False, help_text="Can be rented for a term")),
                ("rental_fee", models.DecimalField(decimal_places=2, default=ZERO, max_digits=10)),
                ("price", models.DecimalField(decimal_places=2, default=ZERO, help_text="Replacement cost",
                                              max_digits=10)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                _tenant(),
            ],
            options={"ordering": ["title"]},
        ),
        migrations.CreateModel(
            name="Copy",
            fields=[
                _id(),
                ("book", models.ForeignKey(on_delete=CASCADE, related_name="copies", to="library.book")),
                ("branch", models.ForeignKey(blank=True, null=True, on_delete=SET_NULL, related_name="copies",
                                             to="library.branch")),
                ("barcode", models.CharField(max_length=40)),
                ("shelf", models.CharField(blank=True, help_text="e.g. F-12 or 510 MAT", max_length=40)),
                ("condition", models.CharField(choices=[("NEW", "New"), ("GOOD", "Good"), ("FAIR", "Fair"),
                                                        ("POOR", "Poor"), ("DAMAGED", "Damaged")],
                                               default="GOOD", max_length=7)),
                ("status", models.CharField(choices=[("IN", "On the shelf"), ("OUT", "On loan"),
                                                     ("HELD", "Held for a reservation"), ("REPAIR", "Being repaired"),
                                                     ("LOST", "Lost"), ("MISSING", "Missing at stock-take"),
                                                     ("GONE", "Withdrawn")], default="IN", max_length=7)),
                ("price", models.DecimalField(blank=True, decimal_places=2, max_digits=10, null=True,
                                              help_text="Leave blank to use the book's replacement cost")),
                ("acquired_on", models.DateField(blank=True, null=True)),
                ("donation", models.ForeignKey(blank=True, null=True, on_delete=SET_NULL, related_name="copies",
                                               to="library.donation")),
                ("note", models.CharField(blank=True, max_length=160)),
                _tenant(),
            ],
            options={"ordering": ["barcode"], "unique_together": {("tenant", "barcode")}},
        ),
        migrations.CreateModel(
            name="Loan",
            fields=[
                _id(),
                ("copy", models.ForeignKey(on_delete=CASCADE, related_name="loans", to="library.copy")),
                _student("library_loans"),
                _staff("library_loans"),
                ("issued_at", models.DateTimeField(auto_now_add=True)),
                ("due_on", models.DateField()),
                ("returned_at", models.DateTimeField(blank=True, null=True)),
                ("renewals", models.PositiveSmallIntegerField(default=0)),
                ("rental_term", models.ForeignKey(blank=True, null=True, on_delete=SET_NULL, related_name="+",
                                                  to="academics.term",
                                                  help_text="Set for a termly textbook rental")),
                ("condition_out", models.CharField(blank=True, max_length=7)),
                ("condition_in", models.CharField(blank=True, max_length=7)),
                ("lost", models.BooleanField(default=False)),
                ("reminded_at", models.DateTimeField(blank=True, null=True)),
                ("overdue_notice_at", models.DateTimeField(blank=True, null=True)),
                _user("issued_by"),
                _user("returned_to", blank=True),
                _tenant(),
            ],
            options={"ordering": ["-issued_at"]},
        ),
        migrations.CreateModel(
            name="Charge",
            fields=[
                _id(),
                _student("library_charges"),
                _staff(),
                ("loan", models.ForeignKey(blank=True, null=True, on_delete=SET_NULL, related_name="charges",
                                           to="library.loan")),
                ("kind", models.CharField(choices=[("FINE", "Late fine"), ("LOST", "Lost book"),
                                                   ("DAMAGE", "Damage"), ("RENTAL", "Textbook rental")],
                                          max_length=6)),
                ("amount", models.DecimalField(decimal_places=2, max_digits=10)),
                ("status", models.CharField(choices=[("UNPAID", "Unpaid"), ("PAID", "Paid"), ("WAIVED", "Waived")],
                                            default="UNPAID", max_length=6)),
                ("transaction", models.ForeignKey(blank=True, null=True, on_delete=SET_NULL, related_name="+",
                                                  to="finance.transaction")),
                ("note", models.CharField(blank=True, max_length=160)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                _user("settled_by", blank=True),
                _tenant(),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="Reservation",
            fields=[
                _id(),
                ("book", models.ForeignKey(on_delete=CASCADE, related_name="reservations", to="library.book")),
                _student("library_reservations"),
                _staff(),
                ("status", models.CharField(choices=[("WAITING", "Waiting"), ("READY", "Ready to collect"),
                                                     ("DONE", "Collected"), ("CANCELLED", "Cancelled"),
                                                     ("EXPIRED", "Not collected")], default="WAITING",
                                            max_length=9)),
                ("copy", models.ForeignKey(blank=True, null=True, on_delete=SET_NULL, related_name="+",
                                           to="library.copy")),
                ("hold_until", models.DateField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                _tenant(),
            ],
            options={"ordering": ["created_at"]},
        ),
        migrations.CreateModel(
            name="EResource",
            fields=[
                _id(),
                ("title", models.CharField(max_length=200)),
                ("kind", models.CharField(choices=[("EBOOK", "E-book"), ("PASTQ", "Past questions"),
                                                   ("ANSWERS", "Marking scheme / answers"), ("OTHER", "Other")],
                                          default="EBOOK", max_length=7)),
                ("authors", models.CharField(blank=True, max_length=200)),
                ("subject", models.CharField(blank=True, max_length=80)),
                ("level", models.CharField(choices=LEVELS, default="ALL", max_length=8)),
                ("exam", models.CharField(blank=True, choices=[("", "—"), ("WAEC", "WAEC"), ("NECO", "NECO"),
                                                               ("BECE", "BECE / Junior WAEC"),
                                                               ("JAMB", "JAMB UTME"), ("NABTEB", "NABTEB"),
                                                               ("COMMON", "Common Entrance"),
                                                               ("SCHOOL", "School exams")], max_length=6)),
                ("year", models.PositiveSmallIntegerField(blank=True, null=True)),
                ("paper", models.CharField(blank=True, help_text="e.g. Paper 2 (Essay), Objective", max_length=60)),
                ("file", models.FileField(storage=library.models._private, upload_to=library.models._upload)),
                ("licence_note", models.CharField(blank=True, max_length=200,
                                                  help_text="Who owns it and how the school may share it")),
                ("for_students", models.BooleanField(default=True)),
                ("downloads", models.PositiveIntegerField(default=0)),
                _user("uploaded_by"),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                _tenant(),
            ],
            options={"ordering": ["subject", "-year", "title"]},
        ),
        migrations.CreateModel(
            name="ReadingLog",
            fields=[
                _id(),
                ("student", models.ForeignKey(on_delete=CASCADE, related_name="reading_logs", to="users.student")),
                ("book", models.ForeignKey(blank=True, null=True, on_delete=SET_NULL, related_name="+",
                                           to="library.book")),
                ("title", models.CharField(max_length=255)),
                ("author", models.CharField(blank=True, max_length=160)),
                ("pages", models.PositiveIntegerField(blank=True, null=True)),
                ("finished_on", models.DateField()),
                ("rating", models.PositiveSmallIntegerField(blank=True, null=True)),
                ("review", models.TextField(blank=True)),
                ("approved", models.BooleanField(default=False)),
                _user("approved_by", blank=True),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                _tenant(),
            ],
            options={"ordering": ["-finished_on", "-id"]},
        ),
        migrations.CreateModel(
            name="ReadingChallenge",
            fields=[
                _id(),
                ("title", models.CharField(max_length=160)),
                ("description", models.TextField(blank=True)),
                ("starts", models.DateField()),
                ("ends", models.DateField()),
                ("target", models.PositiveSmallIntegerField(default=5, help_text="Approved books to finish")),
                ("classes", models.ManyToManyField(blank=True, help_text="Empty = everyone", related_name="+",
                                                   to="users.class")),
                ("badge", models.CharField(default="Reading Champion", max_length=80)),
                _user("created_by"),
                _tenant(),
            ],
            options={"ordering": ["-starts"]},
        ),
        migrations.CreateModel(
            name="ChallengeBadge",
            fields=[
                _id(),
                ("challenge", models.ForeignKey(on_delete=CASCADE, related_name="badges",
                                                to="library.readingchallenge")),
                ("student", models.ForeignKey(on_delete=CASCADE, related_name="reading_badges",
                                              to="users.student")),
                ("awarded_at", models.DateTimeField(auto_now_add=True)),
                _tenant(),
            ],
            options={"unique_together": {("challenge", "student")}},
        ),
        migrations.CreateModel(
            name="StockTake",
            fields=[
                _id(),
                ("branch", models.ForeignKey(blank=True, null=True, on_delete=SET_NULL, related_name="+",
                                             to="library.branch")),
                ("started_at", models.DateTimeField(auto_now_add=True)),
                ("closed_at", models.DateTimeField(blank=True, null=True)),
                _user("started_by"),
                ("note", models.CharField(blank=True, max_length=255)),
                ("missing_count", models.PositiveIntegerField(default=0)),
                _tenant(),
            ],
            options={"ordering": ["-started_at"]},
        ),
        migrations.CreateModel(
            name="StockScan",
            fields=[
                _id(),
                ("stocktake", models.ForeignKey(on_delete=CASCADE, related_name="scans", to="library.stocktake")),
                ("copy", models.ForeignKey(on_delete=CASCADE, related_name="+", to="library.copy")),
                ("at", models.DateTimeField(auto_now_add=True)),
                _user("by"),
                _tenant(),
            ],
            options={"unique_together": {("stocktake", "copy")}},
        ),
        migrations.CreateModel(
            name="PurchaseRequest",
            fields=[
                _id(),
                ("title", models.CharField(max_length=255)),
                ("authors", models.CharField(blank=True, max_length=255)),
                ("isbn", models.CharField(blank=True, max_length=17)),
                ("copies", models.PositiveSmallIntegerField(default=1)),
                ("reason", models.CharField(max_length=255)),
                ("est_price", models.DecimalField(blank=True, decimal_places=2, max_digits=10, null=True)),
                ("status", models.CharField(choices=[("PENDING", "Waiting"), ("APPROVED", "Approved"),
                                                     ("ORDERED", "Ordered"), ("RECEIVED", "Received"),
                                                     ("DECLINED", "Declined")], default="PENDING", max_length=8)),
                ("note", models.CharField(blank=True, max_length=255)),
                _user("requested_by"),
                _user("decided_by", blank=True),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                _tenant(),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="Visit",
            fields=[
                _id(),
                ("branch", models.ForeignKey(blank=True, null=True, on_delete=SET_NULL, related_name="+",
                                             to="library.branch")),
                _student("library_visits"),
                ("school_class", models.ForeignKey(blank=True, null=True, on_delete=SET_NULL, related_name="+",
                                                   to="users.class", help_text="For a whole-class visit")),
                ("headcount", models.PositiveSmallIntegerField(default=1)),
                ("purpose", models.CharField(choices=[("READ", "Reading / study"), ("BORROW", "Borrowing"),
                                                      ("LESSON", "Library lesson"), ("RESEARCH", "Research"),
                                                      ("OTHER", "Other")], default="READ", max_length=8)),
                ("at", models.DateTimeField()),
                _user("recorded_by"),
                _tenant(),
            ],
            options={"ordering": ["-at"]},
        ),
    ]
