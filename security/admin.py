from django.contrib import admin

from .models import (AuditEvent, Breach, Consent, DataPurpose, DataRequest, KnownDevice, PenTest, PrivacyNotice,
                     RetentionRule, Role, SecuritySettings, TwoFactor, UserSession, VulnerabilityReport)


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    list_display = ('at', 'tenant', 'username', 'category', 'action', 'summary', 'ip')
    list_filter = ('category', 'action')
    search_fields = ('summary', 'username')

    def has_change_permission(self, request, obj=None):
        return False

    def has_add_permission(self, request):
        return False


@admin.register(TwoFactor)
class TwoFactorAdmin(admin.ModelAdmin):
    list_display = ('user', 'method', 'confirmed_at', 'last_used_at')
    exclude = ('secret', 'backup_codes')
    readonly_fields = ('user', 'method', 'confirmed_at', 'last_used_at')


@admin.register(UserSession)
class UserSessionAdmin(admin.ModelAdmin):
    list_display = ('user', 'tenant', 'ip', 'created_at', 'last_seen', 'ended_at')
    exclude = ('session_key',)


@admin.register(DataRequest)
class DataRequestAdmin(admin.ModelAdmin):
    list_display = ('reference', 'tenant', 'kind', 'requester_name', 'status', 'due_on')
    list_filter = ('status', 'kind')


@admin.register(Breach)
class BreachAdmin(admin.ModelAdmin):
    list_display = ('reference', 'tenant', 'title', 'severity', 'status', 'discovered_at', 'reported_at')


@admin.register(VulnerabilityReport)
class VulnerabilityReportAdmin(admin.ModelAdmin):
    list_display = ('reference', 'title', 'severity', 'status', 'created_at')


for m in (SecuritySettings, Role, PrivacyNotice, DataPurpose, Consent, RetentionRule, KnownDevice, PenTest):
    admin.site.register(m)
