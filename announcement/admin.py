from django.contrib import admin
from .models import Announcement

@admin.register(Announcement)
class AnnouncementAdmin(admin.ModelAdmin):
    list_display = (
        'title',
        'audience',
        'school_class',
        'published',
        'published_at',
        'expires_at'
    )

    list_filter = (
        'audience',
        'published',
        'school_class',
        'term',
        'session'
    )

    search_fields = ('title', 'message')
    ordering = ('-created_at',)
    readonly_fields = ('created_at', 'published_at')

    actions = ['publish_announcements', 'unpublish_announcements']

    def publish_announcements(self, request, queryset):
        for announcement in queryset:
            announcement.publish()
        self.message_user(request, "Selected announcements have been published.")

    publish_announcements.short_description = "Publish selected announcements"

    def unpublish_announcements(self, request, queryset):
        queryset.update(published=False, published_at=None)
        self.message_user(request, "Selected announcements have been unpublished.")
