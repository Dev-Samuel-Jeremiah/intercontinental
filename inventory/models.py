# inventory/models.py
from django.db import models
from tenants.managers import TenantModelMixin, TenantManager

from django.conf import settings
from django.utils import timezone
from django.core.exceptions import ValidationError


class Category(TenantModelMixin, models.Model):
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)

    class Meta:
        verbose_name_plural = "Categories"
        unique_together = ('tenant', 'name')

    def __str__(self):
        return self.name


class Location(TenantModelMixin, models.Model):
    LOCATION_TYPES = (
        ('LAB', 'Laboratory'),
        ('CLASS', 'Classroom'),
        ('OFFICE', 'Office'),
        ('STORE', 'Store'),
    )

    name = models.CharField(max_length=100)
    location_type = models.CharField(max_length=20, choices=LOCATION_TYPES)
    description = models.TextField(blank=True)

    def __str__(self):
        return self.name


class AssetCategory(TenantModelMixin, models.Model):
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)

    class Meta:
        unique_together = ('tenant', 'name')

    def __str__(self):
        return self.name


class Asset(TenantModelMixin, models.Model):
    ASSET_TYPE = (
        ('FIXED', 'Fixed Asset'),
        ('CONSUMABLE', 'Consumable'),
    )

    STATUS = (
        ('ACTIVE', 'Active'),
        ('RETIRED', 'Retired'),
    )

    name = models.CharField(max_length=200)
    asset_code = models.CharField(max_length=50)
    category = models.ForeignKey(Category, on_delete=models.CASCADE)
    asset_type = models.CharField(max_length=20, choices=ASSET_TYPE)
    unit = models.CharField(max_length=20, default='pcs')
    reorder_level = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=20, choices=STATUS, default='ACTIVE')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('tenant', 'asset_code')
        permissions = [
            ("can_view_inventory", "Can view inventory"),
            ("can_issue_asset", "Can issue and return assets"),
            ("can_adjust_stock", "Can adjust stock"),
            ("can_manage_inventory", "Can manage inventory (add/edit assets)"),
        ]

    def __str__(self):
        return f"{self.name} ({self.asset_code})"

    def current_stock(self):
        total = 0
        for m in self.movements.all():
            if m.movement_type == 'IN':
                total += m.quantity
            elif m.movement_type == 'OUT':
                total -= m.quantity
            elif m.movement_type == 'ADJUST':
                total += m.quantity
        return total

    def total_stock_in(self):
        return self.movements.filter(movement_type='IN').aggregate(
            total=models.Sum('quantity'))['total'] or 0

    def total_stock_out(self):
        return self.movements.filter(movement_type='OUT').aggregate(
            total=models.Sum('quantity'))['total'] or 0

    def is_low_stock(self):
        if self.asset_type == 'CONSUMABLE':
            return self.current_stock() <= self.reorder_level
        return False


class StockMovement(TenantModelMixin, models.Model):
    MOVEMENT_TYPE = (
        ('IN', 'Stock In'),
        ('OUT', 'Stock Out'),
        ('ADJUST', 'Adjustment'),
    )

    asset = models.ForeignKey(Asset, on_delete=models.CASCADE, related_name='movements')
    movement_type = models.CharField(max_length=10, choices=MOVEMENT_TYPE)
    quantity = models.IntegerField()
    performed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                      null=True, blank=True)
    reason = models.TextField()
    date = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-date']

    def __str__(self):
        return f"{self.asset.name} - {self.movement_type} ({self.quantity})"


class AssetAssignment(TenantModelMixin, models.Model):
    CONDITION = (
        ('NEW', 'New'),
        ('GOOD', 'Good'),
        ('FAIR', 'Fair'),
        ('DAMAGED', 'Damaged'),
    )

    asset = models.ForeignKey(Asset, on_delete=models.CASCADE)
    assigned_to_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    location = models.ForeignKey(Location, on_delete=models.CASCADE, null=True, blank=True)
    quantity = models.PositiveIntegerField()
    condition_at_issue = models.CharField(max_length=20, choices=CONDITION)
    issued_at = models.DateTimeField(auto_now_add=True)
    expected_return_date = models.DateField(null=True, blank=True)
    returned = models.BooleanField(default=False)
    returned_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.asset.name} assignment"


class MaintenanceLog(TenantModelMixin, models.Model):
    asset = models.ForeignKey(Asset, on_delete=models.CASCADE)
    issue_reported = models.TextField()
    action_taken = models.TextField()
    cost = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    date = models.DateField(default=timezone.now)

    def __str__(self):
        return f"Maintenance - {self.asset.name}"


# ─────────────────────────────────────────────────────────────────────────────
# INVENTORY ACCESS CONTROL  (email-based whitelist)
# ─────────────────────────────────────────────────────────────────────────────

class InventoryAllowedEmail(TenantModelMixin, models.Model):
    """
    Whitelist of email addresses allowed to access the inventory module.
    If this table is empty, all staff users can access inventory (default behaviour).
    If ANY record exists, ONLY those emails (plus superusers) can access it.
    """
    email       = models.EmailField()
    full_name   = models.CharField(max_length=200, blank=True,
                                   help_text='Optional — for display purposes')
    note        = models.TextField(blank=True,
                                   help_text='e.g. Proprietress, Head of Stores')
    added_by    = models.ForeignKey(settings.AUTH_USER_MODEL,
                                    on_delete=models.SET_NULL,
                                    null=True, blank=True)
    is_active   = models.BooleanField(default=True)
    created_at  = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name        = 'Inventory Allowed Email'
        unique_together     = ('tenant', 'email')
        verbose_name_plural = 'Inventory Allowed Emails'
        ordering            = ['email']

    def __str__(self):
        return f"{self.email} ({self.full_name or 'Unnamed'})"