from django.contrib import admin
from .models import AcademicSession, Term

@admin.register(AcademicSession)
class AcademicSessionAdmin(admin.ModelAdmin):
    list_display = ('name', 'is_active')
    list_editable = ('is_active',)
    ordering = ('-name',)

@admin.register(Term)
class TermAdmin(admin.ModelAdmin):
    list_display = ('session', 'name', 'is_active')
    list_filter = ('session',)
    list_editable = ('is_active',)
