from django.contrib import admin
from django.utils.html import format_html
from .models import (
    TermResult,
    SessionResult,
    TeacherRemark,
    HeadTeacherSignature,
    SubjectTeacherRemark,
    Grading,
    Skill,
    SkillAssessment,
    AIStudentComment,
)


# ============================
# TERM RESULTS (WITH PUBLISH CONTROL)
# ============================

@admin.register(TermResult)
class TermResultAdmin(admin.ModelAdmin):
    list_display = (
        'student', 'class_assigned', 'subject',
        'term', 'session',
        'ca1_score', 'ca2_score', 'ca3_score',
        'essay_score', 'exam_score',
        'total_score', 'grade', 'remark',
        'published'
    )
    list_filter   = ('term', 'session', 'subject', 'class_assigned', 'published')
    search_fields = ('student__full_name', 'subject__name', 'remark')
    ordering      = ('student', 'subject', 'term')
    list_editable = ('published',)
    actions       = ['publish_results', 'unpublish_results']

    def publish_results(self, request, queryset):
        updated = queryset.update(published=True)
        self.message_user(request, f"{updated} result(s) published.")
    publish_results.short_description = "Publish selected Term Results"

    def unpublish_results(self, request, queryset):
        updated = queryset.update(published=False)
        self.message_user(request, f"{updated} result(s) unpublished.")
    unpublish_results.short_description = "Unpublish selected Term Results"


# ============================
# SESSION RESULTS
# ============================

@admin.register(SessionResult)
class SessionResultAdmin(admin.ModelAdmin):
    list_display  = (
        'student', 'class_assigned', 'subject', 'session',
        'first_term', 'second_term', 'third_term',
        'total_score', 'average_score'
    )
    list_filter   = ('session', 'subject', 'class_assigned')
    search_fields = ('student__full_name', 'subject__name')
    ordering      = ('student', 'subject', 'session')


# ============================
# CLASS TEACHER REMARK
# ============================

@admin.register(TeacherRemark)
class TeacherRemarkAdmin(admin.ModelAdmin):
    list_display  = ('student', 'term', 'session', 'remark')
    list_filter   = ('term', 'session')
    search_fields = ('student__full_name', 'remark')
    ordering      = ('student', 'term', 'session')


# ============================
# SUBJECT TEACHER REMARK
# ============================

@admin.register(SubjectTeacherRemark)
class SubjectTeacherRemarkAdmin(admin.ModelAdmin):
    list_display  = ('student', 'subject', 'term', 'session', 'remark', 'updated_at')
    list_filter   = ('term', 'session', 'subject')
    search_fields = ('student__full_name', 'subject__name', 'remark')
    ordering      = ('student', 'subject', 'term', 'session')
    readonly_fields = ('updated_at',)


# ============================
# HEAD TEACHER SIGNATURE
# ============================

@admin.register(HeadTeacherSignature)
class HeadTeacherSignatureAdmin(admin.ModelAdmin):
    list_display = ('id', 'signature')


# ============================
# GRADING SYSTEM
# ============================

@admin.register(Grading)
class GradingAdmin(admin.ModelAdmin):
    list_display  = ('grade', 'min_score', 'description')
    search_fields = ('grade', 'description')
    ordering      = ('-min_score',)


# ============================
# SKILLS & BEHAVIOUR
# ============================

@admin.register(Skill)
class SkillAdmin(admin.ModelAdmin):
    list_display  = ('name', 'category')
    list_filter   = ('category',)
    search_fields = ('name',)
    ordering      = ('category', 'name')


@admin.register(SkillAssessment)
class SkillAssessmentAdmin(admin.ModelAdmin):
    list_display  = ('student', 'skill', 'score', 'term', 'session')
    list_filter   = ('skill__category', 'term', 'session')
    search_fields = ('student__full_name', 'skill__name')
    ordering      = ('student', 'skill__category', 'skill__name')


# ============================
# HOS REMARK
# ============================

from results.models import HosRemark

@admin.register(HosRemark)
class HosRemarkAdmin(admin.ModelAdmin):
    list_display  = ('student', 'term', 'session', 'updated_at')
    search_fields = ('student__full_name',)


# ============================
# AI STUDENT COMMENTS
# ============================

@admin.register(AIStudentComment)
class AIStudentCommentAdmin(admin.ModelAdmin):
    list_display  = (
        'student', 'term', 'session',
        'overall_percentage_display',
        'total_subjects',
        'status_badge',
        'model_used',
        'generated_at',
    )
    list_filter   = ('generation_ok', 'term', 'session', 'model_used')
    search_fields = ('student__full_name',)
    readonly_fields = (
        'student', 'term', 'session',
        'overall_percentage', 'total_subjects',
        'generated_at', 'model_used', 'generation_ok', 'error_message',
        'teacher_comment', 'hos_comment', 'overall_summary',
    )
    ordering = ('-generated_at',)

    fieldsets = (
        ('Student', {'fields': ('student', 'term', 'session')}),
        ('Generated Comments', {'fields': (
            'teacher_comment', 'hos_comment', 'overall_summary',
        )}),
        ('Performance Snapshot', {'fields': (
            'overall_percentage', 'total_subjects',
        )}),
        ('Generation Metadata', {'fields': (
            'generated_at', 'model_used', 'generation_ok', 'error_message',
        )}),
    )

    def overall_percentage_display(self, obj):
        color = (
            '#22c55e' if obj.overall_percentage >= 70
            else '#f59e0b' if obj.overall_percentage >= 50
            else '#ef4444'
        )
        return format_html(
            '<span style="color:{};font-weight:600;">{:.1f}%</span>',
            color, obj.overall_percentage
        )
    overall_percentage_display.short_description = 'Overall %'

    def status_badge(self, obj):
        if obj.generation_ok:
            return format_html(
                '<span style="background:#22c55e;color:#fff;padding:2px 8px;'
                'border-radius:12px;font-size:.75rem;font-weight:600;">✓ OK</span>'
            )
        return format_html(
            '<span style="background:#ef4444;color:#fff;padding:2px 8px;'
            'border-radius:12px;font-size:.75rem;font-weight:600;">✗ Failed</span>'
        )
    status_badge.short_description = 'Status'