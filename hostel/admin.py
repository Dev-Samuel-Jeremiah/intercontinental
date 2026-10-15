from django.contrib import admin
from .models import (
    Hostel, Floor, Room, Bed, BoarderProfile,
    HostelTermBilling, HostelFeeStructure,
    CheckInOut, MealPlan, MealAttendance,
    VisitorLog, IncidentReport, HostelNotice,
)


@admin.register(Hostel)
class HostelAdmin(admin.ModelAdmin):
    list_display  = ['name', 'gender', 'total_beds', 'occupied_beds', 'occupancy_rate', 'is_active']
    list_filter   = ['gender', 'is_active']
    search_fields = ['name']


class BedInline(admin.TabularInline):
    model  = Bed
    extra  = 4
    fields = ['bed_number', 'status', 'note']


@admin.register(Room)
class RoomAdmin(admin.ModelAdmin):
    list_display  = ['room_number', 'hostel', 'floor', 'room_type', 'capacity', 'occupied_count', 'status']
    list_filter   = ['hostel', 'room_type', 'status']
    search_fields = ['room_number', 'hostel__name']
    inlines       = [BedInline]


@admin.register(BoarderProfile)
class BoarderProfileAdmin(admin.ModelAdmin):
    list_display  = ['student', 'student_type', 'hostel', 'room', 'bed', 'status']
    list_filter   = ['student_type', 'status']
    search_fields = ['student__full_name', 'student__admission_number']
    autocomplete_fields = ['student']


@admin.register(HostelTermBilling)
class HostelTermBillingAdmin(admin.ModelAdmin):
    list_display  = ['boarder', 'term', 'session', 'total_fee', 'amount_paid', 'balance_due', 'status']
    list_filter   = ['status', 'session', 'term']
    search_fields = ['boarder__student__full_name']


@admin.register(HostelFeeStructure)
class HostelFeeStructureAdmin(admin.ModelAdmin):
    list_display  = ['hostel', 'term', 'session', 'boarding_fee', 'meal_fee', 'total', 'is_active']
    list_filter   = ['is_active', 'hostel']


@admin.register(CheckInOut)
class CheckInOutAdmin(admin.ModelAdmin):
    list_display  = ['boarder', 'movement_type', 'datetime', 'authorized_by', 'is_overdue']
    list_filter   = ['movement_type']
    search_fields = ['boarder__student__full_name']


@admin.register(VisitorLog)
class VisitorLogAdmin(admin.ModelAdmin):
    list_display  = ['visitor_name', 'boarder', 'relationship', 'visit_date', 'status']
    list_filter   = ['status', 'relationship', 'visit_date']
    search_fields = ['visitor_name', 'boarder__student__full_name']


@admin.register(IncidentReport)
class IncidentReportAdmin(admin.ModelAdmin):
    list_display  = ['title', 'boarder', 'incident_type', 'severity', 'status', 'incident_date']
    list_filter   = ['severity', 'status', 'incident_type']
    search_fields = ['title', 'boarder__student__full_name']


@admin.register(HostelNotice)
class HostelNoticeAdmin(admin.ModelAdmin):
    list_display = ['title', 'hostel', 'priority', 'is_active', 'expires_on', 'created_at']
    list_filter  = ['priority', 'is_active', 'hostel']


admin.site.register(Floor)
admin.site.register(Bed)
admin.site.register(MealPlan)
admin.site.register(MealAttendance)
