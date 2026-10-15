from django.contrib import admin

from .models import (Appraisal, AppraisalCycle, Contract, DisciplinaryCase, ExitCase, JobApplication,
                     OnboardingTask, StaffRecord, TrainingRecord, Vacancy)


@admin.register(StaffRecord)
class StaffRecordAdmin(admin.ModelAdmin):
    list_display = ('staff', 'job_title', 'department', 'phone', 'date_joined', 'left_on')
    search_fields = ('staff__full_name', 'job_title', 'phone')


@admin.register(Contract)
class ContractAdmin(admin.ModelAdmin):
    list_display = ('staff', 'kind', 'start', 'end')
    list_filter = ('kind',)


@admin.register(AppraisalCycle)
class AppraisalCycleAdmin(admin.ModelAdmin):
    list_display = ('title', 'opens', 'closes', 'open')


@admin.register(Appraisal)
class AppraisalAdmin(admin.ModelAdmin):
    list_display = ('staff', 'cycle', 'reviewer', 'status', 'overall')
    list_filter = ('status',)


@admin.register(DisciplinaryCase)
class DisciplinaryCaseAdmin(admin.ModelAdmin):
    list_display = ('staff', 'kind', 'date', 'outcome', 'closed_at')


@admin.register(ExitCase)
class ExitCaseAdmin(admin.ModelAdmin):
    list_display = ('staff', 'kind', 'last_day', 'completed_at')


@admin.register(Vacancy)
class VacancyAdmin(admin.ModelAdmin):
    list_display = ('title', 'closing_date', 'published')


@admin.register(JobApplication)
class JobApplicationAdmin(admin.ModelAdmin):
    list_display = ('full_name', 'vacancy', 'status', 'created_at')
    list_filter = ('status',)


admin.site.register(OnboardingTask)
admin.site.register(TrainingRecord)
