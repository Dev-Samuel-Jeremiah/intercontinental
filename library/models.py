"""
library/models.py
─────────────────
The school library: branches, books and their copies, loans (including
termly textbook rentals), reservations, fines and charges, e-books and past
questions, reading logs and challenges, stock-taking, purchase requests,
donations and visits.

A borrower is either a student or a member of staff — every borrower-shaped
row carries both foreign keys and exactly one is filled.
"""
from decimal import Decimal

from django.conf import settings
from django.db import models

from tenants.managers import TenantManager, TenantModelMixin

USER = settings.AUTH_USER_MODEL
ZERO = Decimal('0')

LEVELS = [('ALL', 'All ages'), ('NURSERY', 'Nursery'), ('LOWER', 'Primary 1–3'), ('UPPER', 'Primary 4–6'),
          ('JSS', 'Junior secondary'), ('SSS', 'Senior secondary'), ('STAFF', 'Staff / reference')]


def _private():
    from users.models import PrivateStorage
    return PrivateStorage()


def _upload(instance, filename):
    import os
    import uuid
    ext = os.path.splitext(filename)[1].lower()[:8]
    return f"library/{instance.tenant_id}/{instance.__class__.__name__.lower()}/{uuid.uuid4().hex}{ext}"


class LibrarySettings(TenantModelMixin, models.Model):
    loan_days = models.PositiveSmallIntegerField(default=14)
    student_max_loans = models.PositiveSmallIntegerField(default=2)
    staff_max_loans = models.PositiveSmallIntegerField(default=5)
    max_renewals = models.PositiveSmallIntegerField(default=1)
    fine_per_day = models.DecimalField(max_digits=8, decimal_places=2, default=Decimal('20'))
    fine_cap = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('1000'),
                                   help_text='Most a single late book can be fined (0 = no limit)')
    grace_days = models.PositiveSmallIntegerField(default=0)
    remind_days_before = models.PositiveSmallIntegerField(default=2)
    hold_days = models.PositiveSmallIntegerField(default=3, help_text='Days a reserved book is kept at the desk')
    block_when_owing = models.BooleanField(default=True, help_text='No new loans while charges are unpaid')
    opening_hours = models.TextField(blank=True, help_text='Shown on the catalogue page')
    managers = models.ManyToManyField('users.Staff', blank=True, related_name='+')

    objects = TenantManager()


class Branch(TenantModelMixin, models.Model):
    name = models.CharField(max_length=80, help_text='e.g. Main library, Junior library, Campus B')
    location = models.CharField(max_length=160, blank=True)
    active = models.BooleanField(default=True)

    objects = TenantManager()

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class Donation(TenantModelMixin, models.Model):
    donor_name = models.CharField(max_length=160)
    donor_email = models.EmailField(blank=True)
    donor_phone = models.CharField(max_length=20, blank=True)
    date = models.DateField()
    note = models.CharField(max_length=255, blank=True)
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    recorded_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, related_name='+')

    objects = TenantManager()

    class Meta:
        ordering = ['-date', '-id']

    def __str__(self):
        return f'{self.donor_name} ({self.date:%d %b %Y})'


class Book(TenantModelMixin, models.Model):
    title = models.CharField(max_length=255)
    authors = models.CharField(max_length=255, blank=True)
    isbn = models.CharField(max_length=17, blank=True, db_index=True)
    publisher = models.CharField(max_length=160, blank=True)
    year = models.PositiveSmallIntegerField(null=True, blank=True)
    edition = models.CharField(max_length=40, blank=True)
    subject = models.CharField(max_length=80, blank=True, help_text='e.g. Mathematics, Fiction, Biography')
    level = models.CharField(max_length=8, choices=LEVELS, default='ALL')
    keywords = models.CharField(max_length=255, blank=True)
    description = models.TextField(blank=True)
    cover_url = models.URLField(blank=True)
    is_textbook = models.BooleanField(default=False, help_text='Can be rented for a term')
    rental_fee = models.DecimalField(max_digits=10, decimal_places=2, default=ZERO)
    price = models.DecimalField(max_digits=10, decimal_places=2, default=ZERO, help_text='Replacement cost')
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        ordering = ['title']

    def __str__(self):
        return self.title


class Copy(TenantModelMixin, models.Model):
    CONDITIONS = [('NEW', 'New'), ('GOOD', 'Good'), ('FAIR', 'Fair'), ('POOR', 'Poor'), ('DAMAGED', 'Damaged')]
    STATUSES = [('IN', 'On the shelf'), ('OUT', 'On loan'), ('HELD', 'Held for a reservation'),
                ('REPAIR', 'Being repaired'), ('LOST', 'Lost'), ('MISSING', 'Missing at stock-take'),
                ('GONE', 'Withdrawn')]
    book = models.ForeignKey(Book, on_delete=models.CASCADE, related_name='copies')
    branch = models.ForeignKey(Branch, on_delete=models.SET_NULL, null=True, blank=True, related_name='copies')
    barcode = models.CharField(max_length=40)
    shelf = models.CharField(max_length=40, blank=True, help_text='e.g. F-12 or 510 MAT')
    condition = models.CharField(max_length=7, choices=CONDITIONS, default='GOOD')
    status = models.CharField(max_length=7, choices=STATUSES, default='IN')
    price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True,
                                help_text="Leave blank to use the book's replacement cost")
    acquired_on = models.DateField(null=True, blank=True)
    donation = models.ForeignKey(Donation, on_delete=models.SET_NULL, null=True, blank=True, related_name='copies')
    note = models.CharField(max_length=160, blank=True)

    objects = TenantManager()

    class Meta:
        ordering = ['barcode']
        unique_together = ('tenant', 'barcode')

    def __str__(self):
        return f'{self.book.title} [{self.barcode}]'

    @property
    def cost(self):
        return self.price if self.price is not None else self.book.price


class Loan(TenantModelMixin, models.Model):
    copy = models.ForeignKey(Copy, on_delete=models.CASCADE, related_name='loans')
    student = models.ForeignKey('users.Student', on_delete=models.CASCADE, null=True, blank=True,
                                related_name='library_loans')
    staff = models.ForeignKey('users.Staff', on_delete=models.CASCADE, null=True, blank=True,
                              related_name='library_loans')
    issued_at = models.DateTimeField(auto_now_add=True)
    due_on = models.DateField()
    returned_at = models.DateTimeField(null=True, blank=True)
    renewals = models.PositiveSmallIntegerField(default=0)
    rental_term = models.ForeignKey('academics.Term', on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name='+', help_text='Set for a termly textbook rental')
    condition_out = models.CharField(max_length=7, blank=True)
    condition_in = models.CharField(max_length=7, blank=True)
    lost = models.BooleanField(default=False)
    reminded_at = models.DateTimeField(null=True, blank=True)
    overdue_notice_at = models.DateTimeField(null=True, blank=True)
    issued_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, related_name='+')
    returned_to = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')

    objects = TenantManager()

    class Meta:
        ordering = ['-issued_at']

    @property
    def borrower(self):
        return self.student or self.staff

    @property
    def borrower_name(self):
        return self.student.full_name if self.student_id else (self.staff.full_name if self.staff_id else '')


class Charge(TenantModelMixin, models.Model):
    KINDS = [('FINE', 'Late fine'), ('LOST', 'Lost book'), ('DAMAGE', 'Damage'), ('RENTAL', 'Textbook rental')]
    STATUSES = [('UNPAID', 'Unpaid'), ('PAID', 'Paid'), ('WAIVED', 'Waived')]
    student = models.ForeignKey('users.Student', on_delete=models.CASCADE, null=True, blank=True,
                                related_name='library_charges')
    staff = models.ForeignKey('users.Staff', on_delete=models.CASCADE, null=True, blank=True, related_name='+')
    loan = models.ForeignKey(Loan, on_delete=models.SET_NULL, null=True, blank=True, related_name='charges')
    kind = models.CharField(max_length=6, choices=KINDS)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=6, choices=STATUSES, default='UNPAID')
    transaction = models.ForeignKey('finance.Transaction', on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name='+')
    note = models.CharField(max_length=160, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    settled_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')

    objects = TenantManager()

    class Meta:
        ordering = ['-created_at']


class Reservation(TenantModelMixin, models.Model):
    STATUSES = [('WAITING', 'Waiting'), ('READY', 'Ready to collect'), ('DONE', 'Collected'),
                ('CANCELLED', 'Cancelled'), ('EXPIRED', 'Not collected')]
    book = models.ForeignKey(Book, on_delete=models.CASCADE, related_name='reservations')
    student = models.ForeignKey('users.Student', on_delete=models.CASCADE, null=True, blank=True,
                                related_name='library_reservations')
    staff = models.ForeignKey('users.Staff', on_delete=models.CASCADE, null=True, blank=True, related_name='+')
    status = models.CharField(max_length=9, choices=STATUSES, default='WAITING')
    copy = models.ForeignKey(Copy, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    hold_until = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        ordering = ['created_at']

    @property
    def borrower_name(self):
        return self.student.full_name if self.student_id else (self.staff.full_name if self.staff_id else '')


class EResource(TenantModelMixin, models.Model):
    KINDS = [('EBOOK', 'E-book'), ('PASTQ', 'Past questions'), ('ANSWERS', 'Marking scheme / answers'),
             ('OTHER', 'Other')]
    EXAMS = [('', '—'), ('WAEC', 'WAEC'), ('NECO', 'NECO'), ('BECE', 'BECE / Junior WAEC'), ('JAMB', 'JAMB UTME'),
             ('NABTEB', 'NABTEB'), ('COMMON', 'Common Entrance'), ('SCHOOL', 'School exams')]
    title = models.CharField(max_length=200)
    kind = models.CharField(max_length=7, choices=KINDS, default='EBOOK')
    authors = models.CharField(max_length=200, blank=True)
    subject = models.CharField(max_length=80, blank=True)
    level = models.CharField(max_length=8, choices=LEVELS, default='ALL')
    exam = models.CharField(max_length=6, choices=EXAMS, blank=True)
    year = models.PositiveSmallIntegerField(null=True, blank=True)
    paper = models.CharField(max_length=60, blank=True, help_text='e.g. Paper 2 (Essay), Objective')
    file = models.FileField(upload_to=_upload, storage=_private)
    licence_note = models.CharField(max_length=200, blank=True,
                                    help_text='Who owns it and how the school may share it')
    for_students = models.BooleanField(default=True)
    downloads = models.PositiveIntegerField(default=0)
    uploaded_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        ordering = ['subject', '-year', 'title']


class ReadingLog(TenantModelMixin, models.Model):
    student = models.ForeignKey('users.Student', on_delete=models.CASCADE, related_name='reading_logs')
    book = models.ForeignKey(Book, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    title = models.CharField(max_length=255)
    author = models.CharField(max_length=160, blank=True)
    pages = models.PositiveIntegerField(null=True, blank=True)
    finished_on = models.DateField()
    rating = models.PositiveSmallIntegerField(null=True, blank=True)
    review = models.TextField(blank=True)
    approved = models.BooleanField(default=False)
    approved_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        ordering = ['-finished_on', '-id']


class ReadingChallenge(TenantModelMixin, models.Model):
    title = models.CharField(max_length=160)
    description = models.TextField(blank=True)
    starts = models.DateField()
    ends = models.DateField()
    target = models.PositiveSmallIntegerField(default=5, help_text='Approved books to finish')
    classes = models.ManyToManyField('users.Class', blank=True, related_name='+', help_text='Empty = everyone')
    badge = models.CharField(max_length=80, default='Reading Champion')
    created_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, related_name='+')

    objects = TenantManager()

    class Meta:
        ordering = ['-starts']


class ChallengeBadge(TenantModelMixin, models.Model):
    challenge = models.ForeignKey(ReadingChallenge, on_delete=models.CASCADE, related_name='badges')
    student = models.ForeignKey('users.Student', on_delete=models.CASCADE, related_name='reading_badges')
    awarded_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        unique_together = ('challenge', 'student')


class StockTake(TenantModelMixin, models.Model):
    branch = models.ForeignKey(Branch, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    started_at = models.DateTimeField(auto_now_add=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    started_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, related_name='+')
    note = models.CharField(max_length=255, blank=True)
    missing_count = models.PositiveIntegerField(default=0)

    objects = TenantManager()

    class Meta:
        ordering = ['-started_at']


class StockScan(TenantModelMixin, models.Model):
    stocktake = models.ForeignKey(StockTake, on_delete=models.CASCADE, related_name='scans')
    copy = models.ForeignKey(Copy, on_delete=models.CASCADE, related_name='+')
    at = models.DateTimeField(auto_now_add=True)
    by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, related_name='+')

    objects = TenantManager()

    class Meta:
        unique_together = ('stocktake', 'copy')


class PurchaseRequest(TenantModelMixin, models.Model):
    STATUSES = [('PENDING', 'Waiting'), ('APPROVED', 'Approved'), ('ORDERED', 'Ordered'), ('RECEIVED', 'Received'),
                ('DECLINED', 'Declined')]
    title = models.CharField(max_length=255)
    authors = models.CharField(max_length=255, blank=True)
    isbn = models.CharField(max_length=17, blank=True)
    copies = models.PositiveSmallIntegerField(default=1)
    reason = models.CharField(max_length=255)
    est_price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    status = models.CharField(max_length=8, choices=STATUSES, default='PENDING')
    note = models.CharField(max_length=255, blank=True)
    requested_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, related_name='+')
    decided_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        ordering = ['-created_at']


class Visit(TenantModelMixin, models.Model):
    # Own reverse name: another app has a model with the same name, and both
    # can't be Tenant.visit_set.
    tenant = models.ForeignKey('tenants.Tenant', on_delete=models.CASCADE, db_index=True,
                               null=True, blank=True, related_name='library_visit_log')
    PURPOSES = [('READ', 'Reading / study'), ('BORROW', 'Borrowing'), ('LESSON', 'Library lesson'),
                ('RESEARCH', 'Research'), ('OTHER', 'Other')]
    branch = models.ForeignKey(Branch, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    student = models.ForeignKey('users.Student', on_delete=models.CASCADE, null=True, blank=True,
                                related_name='library_visits')
    school_class = models.ForeignKey('users.Class', on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name='+', help_text='For a whole-class visit')
    headcount = models.PositiveSmallIntegerField(default=1)
    purpose = models.CharField(max_length=8, choices=PURPOSES, default='READ')
    at = models.DateTimeField()
    recorded_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, related_name='+')

    objects = TenantManager()

    class Meta:
        ordering = ['-at']
