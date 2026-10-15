"""
finance/models.py  —  TIT Finance ERP
Full production models: Wallet, Transaction, FeeStructure, SchoolItem,
Invoice, Payment, ParentBilling, ChildBilling, ChildPaymentAllocation,
Installment, FeePayment, Purchase, TopUpRequest, AuditLog
"""

from decimal import Decimal
import uuid

from django.conf import settings
from django.db import models
from tenants.managers import TenantModelMixin, TenantManager
from django.db import models, transaction as db_transaction
from django.utils import timezone

from users.models import Student
from academics.models import AcademicSession, Term
from security.media import signed_storage


def _gen_ref(prefix="TIT"):
    return prefix + uuid.uuid4().hex[:10].upper()


# ── Wallet ────────────────────────────────────────────────────────────────────
class Wallet(TenantModelMixin, models.Model):
    student    = models.OneToOneField(Student, on_delete=models.CASCADE,
                                      related_name='wallet')
    balance    = models.DecimalField(max_digits=14, decimal_places=2,
                                     default=Decimal('0.00'))
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Parent Wallet"

    def __str__(self):
        return f"Wallet – {self.student.full_name}  ₦{self.balance:,.2f}"

    def credit(self, amount, description, ref=None, performed_by=None,
               category='TOPUP', related_payment=None):
        amount = Decimal(str(amount))
        if amount <= 0:
            raise ValueError("Credit amount must be positive.")
        with db_transaction.atomic():
            self.balance += amount
            self.save(update_fields=['balance', 'updated_at'])
            return Transaction.objects.create(
                wallet=self, txn_type=Transaction.CREDIT, amount=amount,
                description=description, reference=ref or _gen_ref("CRD"),
                category=category, performed_by=performed_by,
                balance_after=self.balance, related_payment=related_payment,
            )

    def debit(self, amount, description, ref=None, performed_by=None,
              category='FEE', related_payment=None):
        amount = Decimal(str(amount))
        if amount <= 0:
            raise ValueError("Debit amount must be positive.")
        if self.balance < amount:
            raise ValueError(
                f"Insufficient balance. Available: ₦{self.balance:,.2f}, Required: ₦{amount:,.2f}")
        with db_transaction.atomic():
            self.balance -= amount
            self.save(update_fields=['balance', 'updated_at'])
            return Transaction.objects.create(
                wallet=self, txn_type=Transaction.DEBIT, amount=amount,
                description=description, reference=ref or _gen_ref("DBT"),
                category=category, performed_by=performed_by,
                balance_after=self.balance, related_payment=related_payment,
            )

    @classmethod
    def get_or_create_for_student(cls, student):
        wallet, _ = cls.objects.get_or_create(student=student)
        return wallet

    @staticmethod
    def _gen_ref():
        return _gen_ref("TIT")


# ── Transaction ───────────────────────────────────────────────────────────────
class Transaction(TenantModelMixin, models.Model):
    CREDIT = 'CREDIT'
    DEBIT  = 'DEBIT'
    TYPE_CHOICES = [(CREDIT, 'Credit'), (DEBIT, 'Debit')]
    CATEGORY_CHOICES = [
        ('TOPUP','Wallet Top-Up'),('FEE','School Fee'),
        ('TEXTBOOK','Textbook'),('UNIFORM','Uniform'),
        ('SUPPLY','School Supply'),('SHOP','School Shop'),('CANTEEN','Canteen'),
        ('PAYSTACK','Paystack Payment'),
        ('REFUND','Refund'),('REVERSAL','Reversal'),
        ('SALARY','Salary'),('OTHER','Other'),
    ]
    STATUS_CHOICES = [
        ('PENDING','Pending'),('COMPLETED','Completed'),
        ('FAILED','Failed'),('REVERSED','Reversed'),
    ]

    wallet          = models.ForeignKey(Wallet, on_delete=models.PROTECT,
                                        related_name='transactions')
    txn_type        = models.CharField(max_length=6, choices=TYPE_CHOICES)
    category        = models.CharField(max_length=20, choices=CATEGORY_CHOICES,
                                       default='OTHER')
    amount          = models.DecimalField(max_digits=14, decimal_places=2)
    balance_after   = models.DecimalField(max_digits=14, decimal_places=2)
    description     = models.CharField(max_length=300)
    reference       = models.CharField(max_length=80, unique=True)
    status          = models.CharField(max_length=10, choices=STATUS_CHOICES,
                                       default='COMPLETED')
    performed_by    = models.ForeignKey(settings.AUTH_USER_MODEL,
                                        on_delete=models.SET_NULL,
                                        null=True, blank=True,
                                        related_name='finance_transactions')
    related_payment = models.ForeignKey('Payment', on_delete=models.SET_NULL,
                                        null=True, blank=True,
                                        related_name='linked_transactions')
    note            = models.TextField(blank=True)
    created_at      = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.txn_type} ₦{self.amount:,.2f}  {self.reference}"


# ── Fee Structure ─────────────────────────────────────────────────────────────
class FeeStructure(TenantModelMixin, models.Model):
    name         = models.CharField(max_length=200)
    description  = models.TextField(blank=True)
    amount       = models.DecimalField(max_digits=14, decimal_places=2)
    category     = models.CharField(max_length=20,
                                    choices=Transaction.CATEGORY_CHOICES,
                                    default='FEE')
    term         = models.ForeignKey(Term, on_delete=models.SET_NULL,
                                     null=True, blank=True,
                                     related_name='fee_structures')
    school_class = models.ForeignKey('users.Class', on_delete=models.SET_NULL,
                                     null=True, blank=True)
    session      = models.ForeignKey(AcademicSession, on_delete=models.SET_NULL,
                                     null=True, blank=True)
    is_active    = models.BooleanField(default=True)
    is_compulsory = models.BooleanField(default=False)
    due_date     = models.DateField(null=True, blank=True)
    created_at   = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['category', 'name']

    def __str__(self):
        return f"{self.name}  ₦{self.amount:,.2f}"


# ── School Item ───────────────────────────────────────────────────────────────
class SchoolItem(TenantModelMixin, models.Model):
    TYPE_CHOICES = [
        ('TEXTBOOK','Textbook'),('UNIFORM','Uniform'),
        ('SUPPLY','School Supply'),('FOOD','Canteen food & drink'),('OTHER','Other'),
    ]
    name            = models.CharField(max_length=200)
    sku             = models.CharField('Code', max_length=40, blank=True,
                                       help_text='Short code or barcode for finding it at the till.')
    cost_price      = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'),
                                          help_text='What the school paid for one — for profit.')
    reorder_level   = models.PositiveIntegerField(default=5,
                                                  help_text='Warn when stock falls to this.')
    description     = models.TextField(blank=True)
    item_type       = models.CharField(max_length=10, choices=TYPE_CHOICES)
    price           = models.DecimalField(max_digits=14, decimal_places=2)
    stock_qty       = models.PositiveIntegerField(default=0)
    school_class     = models.ForeignKey('users.Class', on_delete=models.SET_NULL,
                                         null=True, blank=True,
                                         help_text='Leave blank = available to all classes')
    assigned_student = models.ForeignKey('users.Student', on_delete=models.SET_NULL,
                                         null=True, blank=True,
                                         related_name='assigned_items',
                                         help_text='Assign this item to ONE specific student only')
    image            = models.ImageField(upload_to='school_items/', blank=True, null=True)
    is_active        = models.BooleanField(default=True)
    inventory_asset  = models.ForeignKey('inventory.Asset', on_delete=models.SET_NULL,
                                         null=True, blank=True,
                                         related_name='school_items')
    created_at       = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['item_type', 'name']

    def __str__(self):
        return f"{self.name}  ₦{self.price:,.2f}"

    @property
    def is_low(self):
        return self.stock_qty <= self.reorder_level

    @property
    def is_canteen(self):
        return self.item_type == 'FOOD'


# ── Invoice ───────────────────────────────────────────────────────────────────
class Invoice(TenantModelMixin, models.Model):
    STATUS_CHOICES = [
        ('DRAFT','Draft'),('SENT','Sent'),('PARTIAL','Partially Paid'),
        ('PAID','Fully Paid'),('CANCELLED','Cancelled'),('OVERDUE','Overdue'),
    ]
    student      = models.ForeignKey(Student, on_delete=models.PROTECT,
                                     related_name='invoices')
    session      = models.ForeignKey(AcademicSession, on_delete=models.PROTECT,
                                     null=True, blank=True)
    term         = models.ForeignKey(Term, on_delete=models.SET_NULL,
                                     null=True, blank=True)
    invoice_no   = models.CharField(max_length=30, unique=True)
    title        = models.CharField(max_length=200, default='School Fee Invoice')
    total        = models.DecimalField(max_digits=14, decimal_places=2,
                                       default=Decimal('0.00'))
    amount_paid  = models.DecimalField(max_digits=14, decimal_places=2,
                                       default=Decimal('0.00'))
    balance_due  = models.DecimalField(max_digits=14, decimal_places=2,
                                       default=Decimal('0.00'))
    status       = models.CharField(max_length=10, choices=STATUS_CHOICES,
                                    default='DRAFT')
    due_date     = models.DateField(null=True, blank=True)
    note         = models.TextField(blank=True)
    created_by   = models.ForeignKey(settings.AUTH_USER_MODEL,
                                     on_delete=models.SET_NULL,
                                     null=True, blank=True)
    created_at   = models.DateTimeField(auto_now_add=True)
    updated_at   = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.invoice_no}  {self.student.full_name}"

    def recalculate(self):
        self.total      = sum(i.amount for i in self.items.all())
        self.balance_due = max(Decimal('0'), self.total - self.amount_paid)
        if self.balance_due == 0 and self.total > 0:
            self.status = 'PAID'
        elif self.amount_paid > 0:
            self.status = 'PARTIAL'
        self.save(update_fields=['total','balance_due','status','updated_at'])

    @classmethod
    def generate_for_student(cls, student, term, session, created_by=None):
        from django.db.models import Q
        fees = FeeStructure.objects.filter(is_active=True).filter(
            Q(school_class=student.class_assigned) | Q(school_class__isnull=True)
        ).filter(
            Q(term=term) | Q(term__isnull=True)
        ).filter(
            Q(session=session) | Q(session__isnull=True)
        )
        inv = cls.objects.create(
            student=student, session=session, term=term,
            invoice_no=_gen_ref("INV"),
            title=f"Fee Invoice — {term} / {session}",
            due_date=getattr(term, 'closing_date', None),
            created_by=created_by,
        )
        for fee in fees:
            InvoiceItem.objects.create(
                invoice=inv, description=fee.name,
                amount=fee.amount, fee_structure=fee, student=student,
            )
        inv.recalculate()
        return inv


class InvoiceItem(TenantModelMixin, models.Model):
    invoice       = models.ForeignKey(Invoice, on_delete=models.CASCADE,
                                      related_name='items')
    description   = models.CharField(max_length=300)
    amount        = models.DecimalField(max_digits=14, decimal_places=2)
    fee_structure = models.ForeignKey(FeeStructure, on_delete=models.SET_NULL,
                                      null=True, blank=True)
    school_item   = models.ForeignKey(SchoolItem, on_delete=models.SET_NULL,
                                      null=True, blank=True)
    student       = models.ForeignKey(Student, on_delete=models.SET_NULL,
                                      null=True, blank=True)

    def __str__(self):
        return f"{self.description}  ₦{self.amount:,.2f}"


# ── Payment ───────────────────────────────────────────────────────────────────
class Payment(TenantModelMixin, models.Model):
    PENDING = 'PENDING'; APPROVED = 'APPROVED'; REJECTED = 'REJECTED'
    STATUS_CHOICES = [
        (PENDING,'Pending'),(APPROVED,'Approved'),(REJECTED,'Rejected'),
    ]
    METHOD_CHOICES = [
        ('CASH','Cash'),('BANK_TRANSFER','Bank Transfer'),
        ('PAYSTACK','Paystack (Online)'),('POS','POS / Card'),('CHEQUE','Cheque'),
    ]

    wallet         = models.ForeignKey(Wallet, on_delete=models.PROTECT,
                                       related_name='payments')
    invoice        = models.ForeignKey(Invoice, on_delete=models.SET_NULL,
                                       null=True, blank=True,
                                       related_name='payments')
    amount         = models.DecimalField(max_digits=14, decimal_places=2)
    reference      = models.CharField(max_length=100, unique=True)
    method         = models.CharField(max_length=20, choices=METHOD_CHOICES,
                                      default='CASH')
    status         = models.CharField(max_length=10, choices=STATUS_CHOICES,
                                      default=PENDING)
    proof          = models.ImageField(upload_to='payment_proofs/', storage=signed_storage, blank=True, null=True)
    paystack_ref   = models.CharField(max_length=100, blank=True)
    description    = models.CharField(max_length=300, blank=True)
    rejection_note = models.TextField(blank=True)
    approved_by    = models.ForeignKey(settings.AUTH_USER_MODEL,
                                       on_delete=models.SET_NULL,
                                       null=True, blank=True,
                                       related_name='approved_payments')
    approved_at    = models.DateTimeField(null=True, blank=True)
    created_by     = models.ForeignKey(settings.AUTH_USER_MODEL,
                                       on_delete=models.SET_NULL,
                                       null=True, blank=True,
                                       related_name='created_payments')
    created_at     = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"PMT {self.reference}  ₦{self.amount:,.2f}  [{self.status}]"


# ── Parent Billing ────────────────────────────────────────────────────────────
class ParentBilling(TenantModelMixin, models.Model):
    wallet         = models.ForeignKey(Wallet, on_delete=models.PROTECT,
                                       related_name='billings')
    session        = models.ForeignKey(AcademicSession, on_delete=models.PROTECT)
    term           = models.ForeignKey(Term, on_delete=models.PROTECT)
    total_expected = models.DecimalField(max_digits=14, decimal_places=2,
                                         default=Decimal('0.00'))
    total_paid     = models.DecimalField(max_digits=14, decimal_places=2,
                                         default=Decimal('0.00'))

    class Meta:
        unique_together = ('wallet','session','term')
        ordering = ['-session__name','term__name']

    @property
    def balance(self):
        return max(Decimal('0'), self.total_expected - self.total_paid)

    def refresh_from_children(self):
        children = ChildBilling.objects.filter(parent_billing=self)
        self.total_expected = sum(c.amount_due for c in children)
        self.total_paid     = sum(c.paid for c in children)
        self.save(update_fields=['total_expected','total_paid'])

    def __str__(self):
        return (f"Billing {self.wallet.student.full_name} — "
                f"{self.term} {self.session}  due ₦{self.balance:,.2f}")


# ── Child Billing ─────────────────────────────────────────────────────────────
class ChildBilling(TenantModelMixin, models.Model):
    student        = models.ForeignKey(Student, on_delete=models.PROTECT,
                                       related_name='child_billings')
    parent_billing = models.ForeignKey(ParentBilling, on_delete=models.CASCADE,
                                       related_name='children')
    total_fee      = models.DecimalField(max_digits=14, decimal_places=2,
                                         default=Decimal('0.00'),
                                         help_text="What the family owes, after discounts.")
    gross_fee      = models.DecimalField(max_digits=14, decimal_places=2,
                                         default=Decimal('0.00'),
                                         help_text="Before discounts.")
    discount       = models.DecimalField(max_digits=14, decimal_places=2,
                                         default=Decimal('0.00'))
    discount_note  = models.CharField(max_length=300, blank=True)
    late_fee       = models.DecimalField(max_digits=14, decimal_places=2,
                                         default=Decimal('0.00'),
                                         help_text="Penalties raised for paying late, "
                                                   "not counting any that were waived.")
    paid           = models.DecimalField(max_digits=14, decimal_places=2,
                                         default=Decimal('0.00'))

    class Meta:
        unique_together = ('student','parent_billing')
        ordering = ['student__full_name']

    @property
    def amount_due(self):
        """The bill plus any late payment penalty still standing."""
        return self.total_fee + self.late_fee

    @property
    def balance(self):
        return max(Decimal('0'), self.amount_due - self.paid)

    @property
    def is_fully_paid(self):
        return self.paid >= self.amount_due

    def __str__(self):
        return (f"{self.student.full_name}  "
                f"fee ₦{self.total_fee:,.2f}  paid ₦{self.paid:,.2f}")


# ── Child Payment Allocation ──────────────────────────────────────────────────
class ChildPaymentAllocation(TenantModelMixin, models.Model):
    payment          = models.ForeignKey(Payment, on_delete=models.PROTECT,
                                         related_name='allocations')
    child_billing    = models.ForeignKey(ChildBilling, on_delete=models.PROTECT,
                                         related_name='allocations')
    amount_allocated = models.DecimalField(max_digits=14, decimal_places=2)
    created_at       = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return (f"Alloc ₦{self.amount_allocated:,.2f} → "
                f"{self.child_billing.student.full_name}")


# ── Installment ───────────────────────────────────────────────────────────────
class Installment(TenantModelMixin, models.Model):
    payment     = models.ForeignKey(Payment, on_delete=models.PROTECT,
                                    related_name='installments')
    invoice     = models.ForeignKey(Invoice, on_delete=models.PROTECT,
                                    related_name='installments',
                                    null=True, blank=True)
    wallet      = models.ForeignKey(Wallet, on_delete=models.PROTECT,
                                    related_name='installments')
    amount      = models.DecimalField(max_digits=14, decimal_places=2)
    description = models.CharField(max_length=300, blank=True)
    paid_at     = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-paid_at']

    def __str__(self):
        return f"Installment ₦{self.amount:,.2f}  {self.paid_at:%d %b %Y}"


# ── Fee Payment ───────────────────────────────────────────────────────────────
class FeePayment(TenantModelMixin, models.Model):
    STATUS_CHOICES = [
        ('PAID','Paid'),('PARTIAL','Partial'),
        ('UNPAID','Unpaid'),('WAIVED','Waived'),
    ]
    student       = models.ForeignKey(Student, on_delete=models.PROTECT,
                                      related_name='fee_payments')
    fee_structure = models.ForeignKey(FeeStructure, on_delete=models.PROTECT)
    amount_paid   = models.DecimalField(max_digits=14, decimal_places=2,
                                        default=Decimal('0.00'))
    balance_due   = models.DecimalField(max_digits=14, decimal_places=2,
                                        default=Decimal('0.00'))
    status        = models.CharField(max_length=10, choices=STATUS_CHOICES,
                                     default='UNPAID')
    transaction   = models.ForeignKey(Transaction, on_delete=models.SET_NULL,
                                      null=True, blank=True,
                                      related_name='fee_payments')
    paid_at       = models.DateTimeField(null=True, blank=True)
    waived_by     = models.ForeignKey(settings.AUTH_USER_MODEL,
                                      on_delete=models.SET_NULL,
                                      null=True, blank=True)
    note          = models.TextField(blank=True)

    class Meta:
        ordering = ['-paid_at']
        unique_together = ('student','fee_structure')

    def __str__(self):
        return f"{self.student.full_name} — {self.fee_structure.name}  [{self.status}]"


# ── Purchase ──────────────────────────────────────────────────────────────────
class Purchase(TenantModelMixin, models.Model):
    STATUS_CHOICES = [
        ('PAID','Paid'),('CANCELLED','Cancelled'),('REFUNDED','Refunded'),
    ]
    student     = models.ForeignKey(Student, on_delete=models.PROTECT,
                                    related_name='purchases')
    item        = models.ForeignKey(SchoolItem, on_delete=models.PROTECT)
    quantity    = models.PositiveIntegerField(default=1)
    unit_price  = models.DecimalField(max_digits=14, decimal_places=2)
    total_price = models.DecimalField(max_digits=14, decimal_places=2)
    status      = models.CharField(max_length=10, choices=STATUS_CHOICES,
                                   default='PAID')
    transaction = models.OneToOneField(Transaction, on_delete=models.SET_NULL,
                                       null=True, blank=True,
                                       related_name='purchase')
    created_at  = models.DateTimeField(default=timezone.now)
    note        = models.TextField(blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.student.full_name} – {self.item.name} x{self.quantity}"


# ── Top-Up Request ────────────────────────────────────────────────────────────
class TopUpRequest(TenantModelMixin, models.Model):
    STATUS_CHOICES = [
        ('PENDING','Pending'),('APPROVED','Approved'),('REJECTED','Rejected'),
    ]
    METHOD_CHOICES = [
        ('CASH','Cash'),('BANK_TRANSFER','Bank Transfer'),
        ('PAYSTACK','Paystack'),('POS','POS / Card'),
    ]
    wallet        = models.ForeignKey(Wallet, on_delete=models.PROTECT,
                                      related_name='topup_requests')
    amount        = models.DecimalField(max_digits=14, decimal_places=2)
    method        = models.CharField(max_length=20, choices=METHOD_CHOICES,
                                     default='CASH')
    payment_proof = models.ImageField(upload_to='topup_proofs/', storage=signed_storage, blank=True, null=True)
    reference     = models.CharField(max_length=100, blank=True)
    status        = models.CharField(max_length=10, choices=STATUS_CHOICES,
                                     default='PENDING')
    requested_at  = models.DateTimeField(default=timezone.now)
    reviewed_by   = models.ForeignKey(settings.AUTH_USER_MODEL,
                                      on_delete=models.SET_NULL,
                                      null=True, blank=True,
                                      related_name='reviewed_topups')
    reviewed_at   = models.DateTimeField(null=True, blank=True)
    note          = models.TextField(blank=True)
    transaction   = models.OneToOneField(Transaction, on_delete=models.SET_NULL,
                                         null=True, blank=True,
                                         related_name='topup')

    class Meta:
        ordering = ['-requested_at']

    def __str__(self):
        return f"TopUp ₦{self.amount:,.2f}  {self.wallet.student.full_name}  [{self.status}]"


# ── Audit Log ─────────────────────────────────────────────────────────────────
class AuditLog(TenantModelMixin, models.Model):
    ACTION_CHOICES = [
        ('PAYMENT_APPROVE','Payment Approved'),('PAYMENT_REJECT','Payment Rejected'),
        ('TOPUP_APPROVE','Top-Up Approved'),('TOPUP_REJECT','Top-Up Rejected'),
        ('WALLET_CREDIT','Wallet Credited'),('WALLET_DEBIT','Wallet Debited'),
        ('INVOICE_CREATE','Invoice Created'),('FEE_WAIVE','Fee Waived'),
        ('SALARY_PAY','Salary Paid'),('OTHER','Other'),
    ]
    performed_by = models.ForeignKey(settings.AUTH_USER_MODEL,
                                     on_delete=models.SET_NULL,
                                     null=True, blank=True)
    action       = models.CharField(max_length=20, choices=ACTION_CHOICES)
    target_model = models.CharField(max_length=50, blank=True)
    target_id    = models.PositiveIntegerField(null=True, blank=True)
    description  = models.TextField()
    ip_address   = models.GenericIPAddressField(null=True, blank=True)
    created_at   = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.action}  by {self.performed_by}  {self.created_at:%d %b %Y %H:%M}"

# ── Fee Discounts, Rebates & Scholarships ────────────────────────────────────
class FeeDiscount(TenantModelMixin, models.Model):
    """Money off a student's fees.

    Two kinds:
      * a rule the school sets once — "every third child pays 10% less",
        "staff children pay half", "JSS 1 gets ₦5,000 off this term";
      * an award to one student — a scholarship or a bursary.

    Discounts are applied when a term's bill is worked out, so the parent and
    the bursar always see the same figure.
    """
    PERCENT = 'PERCENT'
    AMOUNT = 'AMOUNT'
    VALUE_CHOICES = [(PERCENT, 'Percentage off'), (AMOUNT, 'Fixed amount off')]

    ALL = 'ALL'
    SIBLING = 'SIBLING'
    STAFF_CHILD = 'STAFF_CHILD'
    CLASS = 'CLASS'
    STUDENT = 'STUDENT'
    APPLIES_CHOICES = [
        (STUDENT, 'One student (scholarship / bursary)'),
        (SIBLING, 'Siblings — from the nth child'),
        (STAFF_CHILD, 'Children of staff'),
        (CLASS, 'Everyone in a class'),
        (ALL, 'Every student'),
    ]

    name = models.CharField(max_length=160)
    applies_to = models.CharField(max_length=12, choices=APPLIES_CHOICES, default=STUDENT)
    value_type = models.CharField(max_length=8, choices=VALUE_CHOICES, default=PERCENT)
    value = models.DecimalField(max_digits=10, decimal_places=2,
                                help_text="10 means 10% (percentage) or ₦10 (fixed amount).")

    # Who it applies to
    student = models.ForeignKey('users.Student', on_delete=models.CASCADE, null=True,
                                blank=True, related_name='fee_discounts',
                                help_text="For a scholarship or bursary.")
    school_class = models.ForeignKey('users.Class', on_delete=models.CASCADE, null=True,
                                     blank=True, related_name='fee_discounts')
    sibling_from = models.PositiveSmallIntegerField(
        default=3, help_text="Siblings rule: the child's position in the family, "
                             "counting from the oldest. 3 means the third child onward.")

    # What it applies to
    category = models.CharField(max_length=20, blank=True,
                                help_text="Limit to one fee category. Blank means all fees.")
    session = models.ForeignKey(AcademicSession, on_delete=models.CASCADE, null=True,
                                blank=True, related_name='fee_discounts')
    term = models.ForeignKey(Term, on_delete=models.CASCADE, null=True, blank=True,
                             related_name='fee_discounts')

    reason = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                    null=True, blank=True, related_name='approved_discounts')
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        amount = f"{self.value}%" if self.value_type == self.PERCENT else f"₦{self.value:,.2f}"
        return f"{self.name} — {amount}"

    @property
    def label(self):
        return f"{self.value:g}% off" if self.value_type == self.PERCENT \
            else f"₦{self.value:,.0f} off"

    def amount_off(self, base_amount):
        """How much this discount takes off a given amount."""
        base_amount = Decimal(str(base_amount or 0))
        if base_amount <= 0:
            return Decimal('0.00')
        if self.value_type == self.PERCENT:
            off = base_amount * (Decimal(str(self.value)) / Decimal('100'))
        else:
            off = Decimal(str(self.value))
        return min(off, base_amount).quantize(Decimal('0.01'))


# ── Payment Plans (instalments) ───────────────────────────────────────────────
class PaymentPlan(TenantModelMixin, models.Model):
    """An agreement to pay a term's fees in instalments."""
    ACTIVE = 'ACTIVE'
    COMPLETED = 'COMPLETED'
    CANCELLED = 'CANCELLED'
    STATUS_CHOICES = [(ACTIVE, 'Active'), (COMPLETED, 'Completed'), (CANCELLED, 'Cancelled')]

    student = models.ForeignKey('users.Student', on_delete=models.CASCADE,
                                related_name='payment_plans')
    session = models.ForeignKey(AcademicSession, on_delete=models.SET_NULL, null=True, blank=True)
    term = models.ForeignKey(Term, on_delete=models.SET_NULL, null=True, blank=True)
    total_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=ACTIVE)
    note = models.TextField(blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                   null=True, blank=True, related_name='payment_plans_created')
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.student.full_name} — ₦{self.total_amount:,.2f} in instalments"

    @property
    def paid(self):
        return sum((i.amount_paid for i in self.installments.all()), Decimal('0.00'))

    @property
    def balance(self):
        return max(Decimal('0'), self.total_amount - self.paid)

    @property
    def next_due(self):
        return self.installments.filter(amount_paid__lt=models.F('amount')).order_by('due_date').first()


class PaymentPlanInstallment(TenantModelMixin, models.Model):
    plan = models.ForeignKey(PaymentPlan, on_delete=models.CASCADE, related_name='installments')
    due_date = models.DateField()
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    amount_paid = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    reminder_sent_at = models.DateTimeField(null=True, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)

    objects = TenantManager()

    class Meta:
        ordering = ['due_date']

    def __str__(self):
        return f"₦{self.amount:,.2f} due {self.due_date}"

    @property
    def balance(self):
        return max(Decimal('0'), self.amount - self.amount_paid)

    @property
    def is_paid(self):
        return self.amount_paid >= self.amount

    @property
    def is_overdue(self):
        from django.utils import timezone as _tz
        return (not self.is_paid) and self.due_date < _tz.now().date()


# ── Debtor follow-up log ─────────────────────────────────────────────────────
class DebtorContact(TenantModelMixin, models.Model):
    """A record of chasing an unpaid fee: who was called, and what they said."""
    CALL = 'CALL'
    SMS = 'SMS'
    WHATSAPP = 'WHATSAPP'
    EMAIL = 'EMAIL'
    VISIT = 'VISIT'
    METHOD_CHOICES = [(CALL, 'Phone call'), (SMS, 'SMS'), (WHATSAPP, 'WhatsApp'),
                      (EMAIL, 'Email'), (VISIT, 'Visit to school')]

    PROMISED = 'PROMISED'
    NO_ANSWER = 'NO_ANSWER'
    DISPUTED = 'DISPUTED'
    PAID = 'PAID'
    OTHER = 'OTHER'
    OUTCOME_CHOICES = [(PROMISED, 'Promised to pay'), (NO_ANSWER, 'No answer'),
                       (DISPUTED, 'Disputed the amount'), (PAID, 'Paid'),
                       (OTHER, 'Something else')]

    student = models.ForeignKey('users.Student', on_delete=models.CASCADE,
                                related_name='debtor_contacts')
    method = models.CharField(max_length=10, choices=METHOD_CHOICES, default=CALL)
    outcome = models.CharField(max_length=10, choices=OUTCOME_CHOICES, default=PROMISED)
    promise_date = models.DateField(null=True, blank=True,
                                    help_text="When the parent said they would pay.")
    amount_promised = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    note = models.TextField(blank=True)
    contacted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                     null=True, blank=True, related_name='debtor_contacts')
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.student.full_name} — {self.get_outcome_display()} ({self.created_at:%d %b})"


# ── Money going out: vendors, expenses, petty cash, budgets ──────────────────
class Vendor(TenantModelMixin, models.Model):
    """A supplier the school pays: the diesel seller, the printer, the cleaner."""
    name = models.CharField(max_length=160)
    phone = models.CharField(max_length=30, blank=True)
    email = models.EmailField(blank=True)
    address = models.TextField(blank=True)
    service = models.CharField(max_length=120, blank=True,
                               help_text="What they supply — diesel, stationery, catering…")
    bank_name = models.CharField(max_length=80, blank=True)
    account_number = models.CharField(max_length=20, blank=True)
    account_name = models.CharField(max_length=160, blank=True)
    payee_type = models.CharField(
        max_length=10, choices=[('COMPANY', 'Company or business name'),
                                ('INDIVIDUAL', 'Individual')], default='COMPANY',
        help_text="Decides the withholding tax rate and which tax authority it goes to.")
    tin = models.CharField('TIN', max_length=30, blank=True,
                           help_text="Tax identification number, for the WHT schedule.")
    wht_exempt = models.BooleanField(
        default=False, help_text="Never deduct withholding tax from this supplier.")
    is_active = models.BooleanField(default=True)
    note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        ordering = ['name']
        unique_together = ('tenant', 'name')

    def __str__(self):
        return self.name

    @property
    def total_paid(self):
        return self.expenses.filter(status=Expense.PAID).aggregate(
            total=models.Sum('amount'))['total'] or Decimal('0.00')


class ExpenseCategory(TenantModelMixin, models.Model):
    """Diesel, salaries, repairs, utilities — whatever the school spends on."""
    name = models.CharField(max_length=120)
    description = models.CharField(max_length=255, blank=True)
    account_code = models.CharField(max_length=20, blank=True,
                                    help_text="The expense account in the school's accounting "
                                              "software. Blank uses the general expenses code.")
    is_active = models.BooleanField(default=True)

    objects = TenantManager()

    class Meta:
        ordering = ['name']
        unique_together = ('tenant', 'name')
        verbose_name_plural = 'Expense categories'

    def __str__(self):
        return self.name


class Expense(TenantModelMixin, models.Model):
    """One payment out of the school's money, with its receipt."""
    CASH = 'CASH'
    TRANSFER = 'TRANSFER'
    PETTY_CASH = 'PETTY_CASH'
    CHEQUE = 'CHEQUE'
    METHOD_CHOICES = [(CASH, 'Cash'), (TRANSFER, 'Bank transfer'),
                      (PETTY_CASH, 'Petty cash'), (CHEQUE, 'Cheque')]

    PENDING = 'PENDING'
    APPROVED = 'APPROVED'
    PAID = 'PAID'
    REJECTED = 'REJECTED'
    STATUS_CHOICES = [(PENDING, 'Awaiting approval'), (APPROVED, 'Approved'),
                      (PAID, 'Paid'), (REJECTED, 'Rejected')]

    COMPANY = 'COMPANY'
    INDIVIDUAL = 'INDIVIDUAL'
    PAYEE_TYPE_CHOICES = [(COMPANY, 'Company'), (INDIVIDUAL, 'Individual')]

    FEDERAL = 'FEDERAL'
    STATE = 'STATE'
    AUTHORITY_CHOICES = [(FEDERAL, 'NRS (formerly FIRS)'), (STATE, 'State Internal Revenue')]

    spent_on = models.DateField(default=timezone.now)
    category = models.ForeignKey(ExpenseCategory, on_delete=models.PROTECT,
                                 related_name='expenses')
    payee_type = models.CharField(max_length=10, choices=PAYEE_TYPE_CHOICES, default=COMPANY)
    wht_rate = models.ForeignKey('WhtRate', on_delete=models.SET_NULL, null=True, blank=True,
                                 related_name='expenses')
    wht_percent = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0.00'),
                                      help_text="The rate used, kept even if the table changes.")
    wht_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'),
                                     help_text="Withheld from the supplier and owed to the "
                                               "tax authority.")
    wht_authority = models.CharField(max_length=10, choices=AUTHORITY_CHOICES, blank=True)
    wht_remittance = models.ForeignKey('WhtRemittance', on_delete=models.SET_NULL, null=True,
                                       blank=True, related_name='expenses')
    vat_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'),
                                     help_text="VAT included in the amount, from the invoice.")
    vendor = models.ForeignKey(Vendor, on_delete=models.SET_NULL, null=True, blank=True,
                               related_name='expenses')
    payee = models.CharField(max_length=160, blank=True,
                             help_text="Used when the payment isn't to a saved vendor.")
    description = models.CharField(max_length=300)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    method = models.CharField(max_length=12, choices=METHOD_CHOICES, default=CASH)
    reference = models.CharField(max_length=80, blank=True,
                                 help_text="Teller, cheque or transfer reference.")
    receipt = models.FileField(upload_to='expense_receipts/', storage=signed_storage, blank=True, null=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=PENDING)
    session = models.ForeignKey(AcademicSession, on_delete=models.SET_NULL, null=True, blank=True)
    term = models.ForeignKey(Term, on_delete=models.SET_NULL, null=True, blank=True)
    note = models.TextField(blank=True)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                     null=True, blank=True, related_name='expenses_requested')
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                    null=True, blank=True, related_name='expenses_approved')
    approved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        ordering = ['-spent_on', '-created_at']
        indexes = [models.Index(fields=['tenant', 'status'])]

    def __str__(self):
        return f"{self.description} — NGN {self.amount:,.2f}"

    @property
    def paid_to(self):
        return self.vendor.name if self.vendor else (self.payee or '—')

    @property
    def net_paid(self):
        """What actually reaches the supplier: the bill less tax withheld."""
        return self.amount - self.wht_amount

    @property
    def counts_as_spent(self):
        """Only money actually paid out belongs in the expenditure total."""
        return self.status == self.PAID


class PettyCashTopUp(TenantModelMixin, models.Model):
    """Money moved into the petty cash tin."""
    topped_up_on = models.DateField(default=timezone.now)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    note = models.CharField(max_length=255, blank=True)
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                    null=True, blank=True, related_name='petty_cash_topups')
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        ordering = ['-topped_up_on', '-created_at']

    def __str__(self):
        return f"Petty cash top-up NGN {self.amount:,.2f}"


class Budget(TenantModelMixin, models.Model):
    """What the school planned to spend on a category this term or session."""
    category = models.ForeignKey(ExpenseCategory, on_delete=models.CASCADE,
                                 related_name='budgets')
    session = models.ForeignKey(AcademicSession, on_delete=models.CASCADE, null=True, blank=True,
                                related_name='budgets')
    term = models.ForeignKey(Term, on_delete=models.CASCADE, null=True, blank=True,
                             related_name='budgets')
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    note = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        ordering = ['category__name']
        unique_together = ('tenant', 'category', 'session', 'term')

    def __str__(self):
        return f"{self.category} budget NGN {self.amount:,.2f}"


# ── Late payment penalties ────────────────────────────────────────────────────
class LateFeeRule(TenantModelMixin, models.Model):
    """
    What a school charges a family that pays late.

    The rule names a due date for a term. Anything still unpaid after the
    grace period is charged — either a flat amount or a percentage of what is
    still owed — once, or again every week or month that it stays unpaid.
    A cap stops the penalty growing past what the school thinks is fair.
    """
    PERCENT = 'PERCENT'
    AMOUNT = 'AMOUNT'
    CHARGE_CHOICES = [(PERCENT, 'Percentage of the balance'), (AMOUNT, 'Fixed amount')]

    ONCE = 'ONCE'
    WEEKLY = 'WEEKLY'
    MONTHLY = 'MONTHLY'
    REPEAT_CHOICES = [(ONCE, 'Once only'), (WEEKLY, 'Every week late'),
                      (MONTHLY, 'Every month late')]

    name = models.CharField(max_length=160, default='Late payment charge')
    session = models.ForeignKey(AcademicSession, on_delete=models.CASCADE,
                                related_name='late_fee_rules')
    term = models.ForeignKey(Term, on_delete=models.CASCADE, related_name='late_fee_rules')
    due_date = models.DateField(help_text="Fees are late after this date.")
    grace_days = models.PositiveIntegerField(
        default=0, help_text="Days after the due date before anything is charged.")
    charge_type = models.CharField(max_length=10, choices=CHARGE_CHOICES, default=AMOUNT)
    value = models.DecimalField(max_digits=10, decimal_places=2,
                                help_text="Naira, or a percentage when charging a percentage.")
    repeat = models.CharField(max_length=10, choices=REPEAT_CHOICES, default=ONCE)
    max_total = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True,
                                    help_text="Never charge one family more than this in total.")
    minimum_balance = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'),
        help_text="Skip families owing less than this.")
    is_active = models.BooleanField(default=True)
    note = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        ordering = ['-due_date']
        unique_together = ('tenant', 'session', 'term', 'name')

    def __str__(self):
        return f"{self.name} — after {self.due_date:%d %b %Y}"

    @property
    def starts_on(self):
        """The first day a charge may be raised."""
        from datetime import timedelta
        return self.due_date + timedelta(days=self.grace_days)

    def charge_for(self, balance):
        """What this rule charges on an outstanding balance, per period."""
        if self.charge_type == self.PERCENT:
            return (balance * self.value / Decimal('100')).quantize(Decimal('0.01'))
        return Decimal(self.value).quantize(Decimal('0.01'))


class LateFeeCharge(TenantModelMixin, models.Model):
    """One penalty raised against one child's bill, and whether it was waived."""
    rule = models.ForeignKey(LateFeeRule, on_delete=models.CASCADE, related_name='charges')
    child_billing = models.ForeignKey(ChildBilling, on_delete=models.CASCADE,
                                      related_name='late_fees')
    period = models.PositiveIntegerField(
        default=1, help_text="1 for the first charge, 2 for the next week or month, and so on.")
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    balance_then = models.DecimalField(max_digits=14, decimal_places=2,
                                       default=Decimal('0.00'))
    charged_on = models.DateField(default=timezone.now)
    waived = models.BooleanField(default=False)
    waived_reason = models.CharField(max_length=255, blank=True)
    waived_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                  null=True, blank=True, related_name='late_fees_waived')
    waived_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        ordering = ['-charged_on', '-id']
        unique_together = ('tenant', 'rule', 'child_billing', 'period')

    def __str__(self):
        return (f"Late fee NGN {self.amount:,.2f} — "
                f"{self.child_billing.student.full_name}")

    @property
    def counts(self):
        return not self.waived


# ── Refunds ───────────────────────────────────────────────────────────────────
class Refund(TenantModelMixin, models.Model):
    """
    Money given back to a family — a withdrawal, an overpayment, a cancelled
    trip. It is requested, approved, then paid, and only a paid refund comes
    off the school's income.
    """
    REQUESTED = 'REQUESTED'
    APPROVED = 'APPROVED'
    PAID = 'PAID'
    REJECTED = 'REJECTED'
    STATUS_CHOICES = [(REQUESTED, 'Awaiting approval'), (APPROVED, 'Approved'),
                      (PAID, 'Paid'), (REJECTED, 'Rejected')]

    TRANSFER = 'TRANSFER'
    CASH = 'CASH'
    WALLET = 'WALLET'
    CHEQUE = 'CHEQUE'
    METHOD_CHOICES = [(TRANSFER, 'Bank transfer'), (CASH, 'Cash'),
                      (WALLET, 'Credited to the wallet'), (CHEQUE, 'Cheque')]

    OVERPAYMENT = 'OVERPAYMENT'
    WITHDRAWAL = 'WITHDRAWAL'
    CANCELLED = 'CANCELLED'
    DUPLICATE = 'DUPLICATE'
    OTHER = 'OTHER'
    REASON_CHOICES = [(OVERPAYMENT, 'Paid more than the bill'),
                      (WITHDRAWAL, 'Student withdrew'),
                      (CANCELLED, 'Cancelled trip or activity'),
                      (DUPLICATE, 'Duplicate payment'),
                      (OTHER, 'Other')]

    student = models.ForeignKey(Student, on_delete=models.PROTECT, related_name='refunds')
    payment = models.ForeignKey(Payment, on_delete=models.SET_NULL, null=True, blank=True,
                                related_name='refunds',
                                help_text="The payment being refunded, where there is one.")
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    reason = models.CharField(max_length=20, choices=REASON_CHOICES, default=OVERPAYMENT)
    detail = models.CharField(max_length=300, blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=REQUESTED)
    method = models.CharField(max_length=10, choices=METHOD_CHOICES, default=TRANSFER)
    reference = models.CharField(max_length=60, blank=True,
                                 help_text="Teller or transfer reference.")
    bank_name = models.CharField(max_length=80, blank=True)
    account_number = models.CharField(max_length=20, blank=True)
    account_name = models.CharField(max_length=160, blank=True)
    session = models.ForeignKey(AcademicSession, on_delete=models.SET_NULL,
                                null=True, blank=True, related_name='refunds')
    term = models.ForeignKey(Term, on_delete=models.SET_NULL, null=True, blank=True,
                             related_name='refunds')
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                     null=True, blank=True, related_name='refunds_requested')
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                    null=True, blank=True, related_name='refunds_approved')
    approved_at = models.DateTimeField(null=True, blank=True)
    paid_on = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Refund NGN {self.amount:,.2f} — {self.student.full_name}"


# ── Bank reconciliation ───────────────────────────────────────────────────────
class BankAccount(TenantModelMixin, models.Model):
    """One of the school's bank accounts, as the bursar sees it on a statement."""
    name = models.CharField(max_length=120, help_text="What the school calls it — "
                                                      "\"Fees account\", \"Main account\".")
    bank_name = models.CharField(max_length=80)
    account_number = models.CharField(max_length=20)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        ordering = ['name']
        unique_together = ('tenant', 'bank_name', 'account_number')

    def __str__(self):
        return f"{self.name} — {self.bank_name} {self.account_number}"


class BankStatement(TenantModelMixin, models.Model):
    """A statement file the bursar imported from the bank."""
    account = models.ForeignKey(BankAccount, on_delete=models.CASCADE,
                                related_name='statements')
    file_name = models.CharField(max_length=200, blank=True)
    period_start = models.DateField(null=True, blank=True)
    period_end = models.DateField(null=True, blank=True)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                    null=True, blank=True, related_name='bank_statements')
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.account.name} statement {self.period_start} – {self.period_end}"


class BankLine(TenantModelMixin, models.Model):
    """
    One line on a bank statement. Money in is positive, money out negative.
    A line is matched to the payment, expense or refund it represents, set
    aside as nothing to do with fees (bank charges, transfers between the
    school's own accounts), or left unmatched for someone to look at.
    """
    UNMATCHED = 'UNMATCHED'
    MATCHED = 'MATCHED'
    IGNORED = 'IGNORED'
    STATUS_CHOICES = [(UNMATCHED, 'Not matched'), (MATCHED, 'Matched'),
                      (IGNORED, 'Set aside')]

    statement = models.ForeignKey(BankStatement, on_delete=models.CASCADE,
                                  related_name='lines')
    date = models.DateField()
    description = models.CharField(max_length=400, blank=True)
    reference = models.CharField(max_length=120, blank=True)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    balance = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=UNMATCHED)
    payment = models.ForeignKey(Payment, on_delete=models.SET_NULL, null=True, blank=True,
                                related_name='bank_lines')
    expense = models.ForeignKey('Expense', on_delete=models.SET_NULL, null=True, blank=True,
                                related_name='bank_lines')
    refund = models.ForeignKey('Refund', on_delete=models.SET_NULL, null=True, blank=True,
                               related_name='bank_lines')
    wht_remittance = models.ForeignKey('WhtRemittance', on_delete=models.SET_NULL, null=True,
                                       blank=True, related_name='bank_lines')
    matched_automatically = models.BooleanField(default=False)
    matched_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                   null=True, blank=True, related_name='bank_lines_matched')
    matched_at = models.DateTimeField(null=True, blank=True)
    note = models.CharField(max_length=255, blank=True)

    objects = TenantManager()

    class Meta:
        ordering = ['date', 'id']

    def __str__(self):
        return f"{self.date} {self.amount:,.2f} {self.description[:40]}"

    @property
    def is_credit(self):
        return self.amount > 0

    @property
    def matched_record(self):
        return self.payment or self.expense or self.refund or self.wht_remittance


# ── Chart of accounts (for the accounting export) ─────────────────────────────
class AccountingCodes(TenantModelMixin, models.Model):
    """
    The account codes a school's accountant uses, so the export lands in the
    right accounts in QuickBooks, Xero, Sage or Zoho Books. Every code has a
    sensible default; most schools only change them to match an existing
    chart of accounts.
    """
    bank = models.CharField(max_length=20, default='1000')
    bank_name = models.CharField(max_length=80, default='Bank')
    cash = models.CharField(max_length=20, default='1010')
    cash_name = models.CharField(max_length=80, default='Cash on hand')
    online = models.CharField(max_length=20, default='1020')
    online_name = models.CharField(max_length=80, default='Paystack clearing')
    petty_cash = models.CharField(max_length=20, default='1030')
    petty_cash_name = models.CharField(max_length=80, default='Petty cash')
    parent_wallets = models.CharField(max_length=20, default='2100')
    parent_wallets_name = models.CharField(max_length=80, default='Parent wallet balances')
    wht_payable = models.CharField(max_length=20, default='2200')
    wht_payable_name = models.CharField(max_length=80, default='Withholding tax payable')
    fee_income = models.CharField(max_length=20, default='4000')
    fee_income_name = models.CharField(max_length=80, default='School fees income')
    shop_sales = models.CharField(max_length=20, default='4200')
    shop_sales_name = models.CharField(max_length=80, default='Shop and canteen sales')
    refunds = models.CharField(max_length=20, default='4900')
    refunds_name = models.CharField(max_length=80, default='Fee refunds')
    expenses = models.CharField(max_length=20, default='6000')
    expenses_name = models.CharField(max_length=80, default='General expenses')
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantManager()

    class Meta:
        verbose_name_plural = 'Accounting codes'
        constraints = [models.UniqueConstraint(fields=['tenant'],
                                               name='one_accounting_codes_per_school')]

    FIELDS = ['bank', 'cash', 'online', 'petty_cash', 'parent_wallets', 'wht_payable',
              'fee_income', 'shop_sales', 'refunds', 'expenses']

    def __str__(self):
        return f"Accounting codes — {self.tenant}"

    @classmethod
    def for_school(cls, tenant):
        codes, _ = cls.objects.get_or_create(tenant=tenant)
        return codes

    def account(self, key):
        """(code, name) for one of the fixed accounts."""
        return getattr(self, key), getattr(self, f'{key}_name')


# ── Withholding tax ───────────────────────────────────────────────────────────
class WhtRate(TenantModelMixin, models.Model):
    """
    One line of the school's withholding tax table: a kind of transaction and
    the rate deducted from a company or from an individual. Every school keeps
    its own table, because rates change and advisers differ.
    """
    name = models.CharField(max_length=120)
    company_rate = models.DecimalField(max_digits=5, decimal_places=2)
    individual_rate = models.DecimalField(max_digits=5, decimal_places=2)
    is_active = models.BooleanField(default=True)
    note = models.CharField(max_length=255, blank=True)

    objects = TenantManager()

    class Meta:
        ordering = ['name']
        unique_together = ('tenant', 'name')

    def __str__(self):
        return f"{self.name} ({self.company_rate}% / {self.individual_rate}%)"

    def rate_for(self, payee_type):
        return self.individual_rate if payee_type == 'INDIVIDUAL' else self.company_rate


class WhtRemittance(TenantModelMixin, models.Model):
    """Withholding tax the school paid over to a tax authority, and what it covered."""
    authority = models.CharField(max_length=10, choices=Expense.AUTHORITY_CHOICES)
    period_start = models.DateField()
    period_end = models.DateField()
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    paid_on = models.DateField(default=timezone.now)
    method = models.CharField(max_length=10, choices=Expense.METHOD_CHOICES,
                              default=Expense.TRANSFER)
    reference = models.CharField(max_length=80, blank=True,
                                 help_text="Payment reference or receipt number.")
    receipt = models.FileField(upload_to='wht_receipts/', storage=signed_storage, blank=True, null=True)
    note = models.CharField(max_length=255, blank=True)
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                    null=True, blank=True, related_name='wht_remittances')
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        ordering = ['-paid_on', '-id']

    def __str__(self):
        return (f"WHT to {self.get_authority_display()} NGN {self.amount:,.2f} "
                f"({self.period_start:%b %Y})")


# ── Shop and canteen till ─────────────────────────────────────────────────────
class Sale(TenantModelMixin, models.Model):
    """
    One sale at the school shop or canteen till: a basket of items, paid from
    a family's wallet or in cash, by transfer or by card.
    """
    SHOP = 'SHOP'
    CANTEEN = 'CANTEEN'
    OUTLET_CHOICES = [(SHOP, 'School shop'), (CANTEEN, 'Canteen')]

    WALLET = 'WALLET'
    CASH = 'CASH'
    TRANSFER = 'TRANSFER'
    POS = 'POS'
    METHOD_CHOICES = [(WALLET, 'Family wallet'), (CASH, 'Cash'),
                      (TRANSFER, 'Bank transfer'), (POS, 'POS / card')]

    COMPLETED = 'COMPLETED'
    VOIDED = 'VOIDED'
    STATUS_CHOICES = [(COMPLETED, 'Completed'), (VOIDED, 'Voided')]

    number = models.CharField(max_length=30, help_text="Receipt number.")
    outlet = models.CharField(max_length=10, choices=OUTLET_CHOICES, default=SHOP)
    student = models.ForeignKey(Student, on_delete=models.PROTECT, null=True, blank=True,
                                related_name='sales', help_text="Blank for a walk-in customer.")
    customer_name = models.CharField(max_length=120, blank=True)
    method = models.CharField(max_length=10, choices=METHOD_CHOICES, default=CASH)
    total = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    cost = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    amount_tendered = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=COMPLETED)
    transaction = models.ForeignKey(Transaction, on_delete=models.SET_NULL, null=True,
                                    blank=True, related_name='sales')
    sold_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                null=True, blank=True, related_name='sales_made')
    voided_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                  null=True, blank=True, related_name='sales_voided')
    voided_at = models.DateTimeField(null=True, blank=True)
    void_reason = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    objects = TenantManager()

    class Meta:
        ordering = ['-created_at', '-id']
        unique_together = ('tenant', 'number')

    def __str__(self):
        return f"Sale {self.number} NGN {self.total:,.2f}"

    @property
    def change_due(self):
        if self.amount_tendered is None:
            return None
        return max(Decimal('0.00'), self.amount_tendered - self.total)

    @property
    def buyer(self):
        if self.student:
            return self.student.full_name
        return self.customer_name or 'Walk-in customer'


class SaleLine(TenantModelMixin, models.Model):
    sale = models.ForeignKey(Sale, on_delete=models.CASCADE, related_name='lines')
    item = models.ForeignKey(SchoolItem, on_delete=models.PROTECT, related_name='sale_lines')
    name = models.CharField(max_length=200, help_text="The item's name when sold.")
    quantity = models.PositiveIntegerField()
    unit_price = models.DecimalField(max_digits=14, decimal_places=2)
    unit_cost = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    total = models.DecimalField(max_digits=14, decimal_places=2)

    objects = TenantManager()

    class Meta:
        ordering = ['id']

    def __str__(self):
        return f"{self.name} x{self.quantity}"


class SpendingLimit(TenantModelMixin, models.Model):
    """How much a child may spend at the canteen in a day, set by the parent."""
    student = models.OneToOneField(Student, on_delete=models.CASCADE,
                                   related_name='spending_limit')
    daily_canteen = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True,
                                        help_text="Blank means no limit.")
    shop_allowed = models.BooleanField(default=True,
                                       help_text="May buy from the school shop on the wallet.")
    set_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                               null=True, blank=True, related_name='spending_limits_set')
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantManager()

    def __str__(self):
        return f"Limits for {self.student.full_name}"
