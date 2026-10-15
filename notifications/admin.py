from django.contrib import admin

from .models import Notification


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ('title', 'kind', 'audience', 'tenant', 'created_at')
    list_filter = ('kind', 'audience')
    search_fields = ('title', 'body')
    raw_id_fields = ('recipient', 'student', 'school_class', 'actor')
