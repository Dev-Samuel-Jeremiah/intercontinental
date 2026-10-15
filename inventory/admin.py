# inventory/admin.py
from django.contrib import admin
from .models import (
    Category,
    Location,
    Asset,
    StockMovement,
    AssetAssignment,
    MaintenanceLog
)


# Register your models here.

@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ('name',)
    search_fields = ('name',)


@admin.register(Location)
class LocationAdmin(admin.ModelAdmin):
    list_display = ('name', 'location_type')
    list_filter = ('location_type',)
    search_fields = ('name',)


@admin.register(Asset)
class AssetAdmin(admin.ModelAdmin):
    list_display = (
        'name',
        'asset_code',
        'category',
        'asset_type',
        'current_stock_display',
        'reorder_level',
        'status',
    )
    list_filter = ('asset_type', 'category', 'status')
    search_fields = ('name', 'asset_code')
    readonly_fields = ('created_at',)

    def current_stock_display(self, obj):
        return obj.current_stock()
    current_stock_display.short_description = 'Current Stock'


@admin.register(StockMovement)
class StockMovementAdmin(admin.ModelAdmin):
    list_display = (
        'asset',
        'movement_type',
        'quantity',
        'performed_by',
        'date',
    )
    list_filter = ('movement_type', 'date')
    search_fields = ('asset__name', 'asset__asset_code')
    readonly_fields = (
        'asset',
        'movement_type',
        'quantity',
        'performed_by',
        'reason',
        'date',
    )

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
    
    def has_add_permission(self, request):
        return request.user.is_superuser



@admin.register(AssetAssignment)
class AssetAssignmentAdmin(admin.ModelAdmin):
    list_display = (
        'asset',
        'assigned_to_user',
        'location',
        'quantity',
        'issued_at',
        'returned',
    )
    list_filter = ('returned', 'issued_at')
    search_fields = ('asset__name', 'assigned_to_user__username')
    readonly_fields = ('issued_at',)

    def get_readonly_fields(self, request, obj=None):
        if obj:
            return self.readonly_fields + ('asset', 'quantity')
        return self.readonly_fields



@admin.register(MaintenanceLog)
class MaintenanceLogAdmin(admin.ModelAdmin):
    list_display = ('asset', 'date', 'cost')
    list_filter = ('date',)
    search_fields = ('asset__name',)



