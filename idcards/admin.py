from django.contrib import admin
from .models import IDCard, GateScanLog, StaffClockLog, ReceptionStation


@admin.register(IDCard)
class IDCardAdmin(admin.ModelAdmin):
    list_display = ('holder_name', 'card_type', 'card_number', 'is_active', 'issued_at', 'tenant')
    list_filter = ('card_type', 'is_active', 'tenant')
    search_fields = ('card_number', 'student__full_name', 'staff__full_name')


@admin.register(GateScanLog)
class GateScanLogAdmin(admin.ModelAdmin):
    list_display = ('student', 'direction', 'scanned_at', 'parent_notified', 'tenant')
    list_filter = ('direction', 'parent_notified', 'tenant')
    search_fields = ('student__full_name',)


@admin.register(StaffClockLog)
class StaffClockLogAdmin(admin.ModelAdmin):
    list_display = ('staff', 'direction', 'scanned_at', 'tenant')
    list_filter = ('direction', 'tenant')
    search_fields = ('staff__full_name',)


@admin.register(ReceptionStation)
class ReceptionStationAdmin(admin.ModelAdmin):
    list_display = ('name', 'station_uid', 'is_active', 'tenant')
