"""
tenants/admin.py
The school record. (Plans, subscriptions and registrations are legacy SaaS
tables kept only for migrations; they are not shown.)
"""
from django.contrib import admin

from .models import Tenant


@admin.register(Tenant)
class SchoolAdmin(admin.ModelAdmin):
    list_display = ('name', 'subdomain', 'email', 'phone', 'state')
    fieldsets = (
        ('School', {'fields': ('name', 'subdomain', 'school_type', 'motto', 'logo')}),
        ('Contact', {'fields': ('email', 'phone', 'address', 'state', 'country')}),
        ('Settings', {'fields': ('timezone', 'currency', 'term_system')}),
    )

    def has_add_permission(self, request):
        return not Tenant.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False
