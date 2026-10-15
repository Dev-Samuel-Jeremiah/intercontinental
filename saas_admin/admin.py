from django.contrib import admin

from .models import Article, Backup, Branding, Campus, Notice, Tour


@admin.register(Campus)
class SectionAdmin(admin.ModelAdmin):
    """The school's sections (Nursery & Primary, Secondary, Darun)."""
    list_display = ('name', 'code', 'head', 'active')
    filter_horizontal = ('classes', 'staff')


@admin.register(Backup)
class BackupAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'kind', 'rows', 'size', 'restored_at')
    readonly_fields = ('tenant', 'kind', 'file', 'size', 'rows', 'created_at', 'restored_at', 'restore_note')


admin.site.register([Article, Branding, Notice, Tour])
