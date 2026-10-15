from django.contrib import admin
from .models import (
    TimeSlot, TimetableConfiguration, TeacherSubjectClass,
    GeneratedTimetable, TimetableSlot
)


@admin.register(TimeSlot)
class TimeSlotAdmin(admin.ModelAdmin):
    list_display = ('name', 'start_time', 'end_time', 'order', 'is_break')
    list_editable = ('order', 'is_break')
    ordering = ('order',)


class TeacherSubjectClassInline(admin.TabularInline):
    model = TeacherSubjectClass
    extra = 1
    autocomplete_fields = []


@admin.register(TimetableConfiguration)
class TimetableConfigurationAdmin(admin.ModelAdmin):
    list_display = ('name', 'is_active', 'created_at')
    list_editable = ('is_active',)
    inlines = [TeacherSubjectClassInline]
    filter_horizontal = ('time_slots',)


@admin.register(TeacherSubjectClass)
class TeacherSubjectClassAdmin(admin.ModelAdmin):
    list_display = ('config', 'staff', 'subject', 'school_class', 'periods_per_week')
    list_filter = ('config', 'school_class')
    search_fields = ('staff__full_name', 'subject__name', 'school_class__name')


class TimetableSlotInline(admin.TabularInline):
    model = TimetableSlot
    extra = 0
    readonly_fields = ('school_class', 'day', 'time_slot', 'subject', 'teacher')


@admin.register(GeneratedTimetable)
class GeneratedTimetableAdmin(admin.ModelAdmin):
    list_display = ('config', 'generated_at', 'is_published')
    list_editable = ('is_published',)
    inlines = [TimetableSlotInline]


from .models import CalendarFeed, MeetingBooking, MeetingEvening, RoomBooking, TimetableVersion  # noqa: E402

admin.site.register(RoomBooking)
admin.site.register(MeetingEvening)
admin.site.register(MeetingBooking)
admin.site.register(TimetableVersion)
admin.site.register(CalendarFeed)

from .models import ExtraLessonEnrolment, ExtraLessonGroup  # noqa: E402

admin.site.register(ExtraLessonGroup)
admin.site.register(ExtraLessonEnrolment)
