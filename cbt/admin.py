from django.contrib import admin
from .models import Exam, ExamPart, Question, Option, ExamAttempt, StudentAnswer, EssayScore
from users.models import Staff, Class, Subject

# ----------------- Option Inline -----------------
class OptionInline(admin.TabularInline):
    model = Option
    extra = 1
    autocomplete_fields = ('question',)

# ----------------- Question Inline -----------------
class QuestionInline(admin.TabularInline):
    model = Question
    extra = 1
    autocomplete_fields = ('exam_part',)
    show_change_link = True

# ----------------- ExamPart Inline -----------------
class ExamPartInline(admin.TabularInline):
    model = ExamPart
    extra = 1
    show_change_link = True

# ----------------- Exam Admin -----------------
@admin.register(Exam)
class ExamAdmin(admin.ModelAdmin):
    list_display = ('title', 'subject', 'exam_type', 'created_by', 'start_time', 'end_time', 'published', 'total_marks')
    list_filter = ('subject', 'classes', 'published', 'exam_type', 'created_by')
    search_fields = ('title',)
    autocomplete_fields = ('subject', 'classes', 'created_by')
    inlines = [ExamPartInline]

# ----------------- ExamPart Admin -----------------
@admin.register(ExamPart)
class ExamPartAdmin(admin.ModelAdmin):
    list_display = ('exam', 'part_type', 'total_marks', 'duration_minutes')
    list_filter = ('exam', 'part_type')
    search_fields = ('exam__title',)
    autocomplete_fields = ('exam',)
    inlines = [QuestionInline]  # Inline questions directly under ExamPart

# ----------------- Question Admin -----------------
@admin.register(Question)
class QuestionAdmin(admin.ModelAdmin):
    list_display = ('question_text', 'exam_part', 'question_type', 'marks')
    list_filter = ('exam_part', 'question_type')
    search_fields = ('question_text',)
    autocomplete_fields = ('exam_part',)
    inlines = [OptionInline]  # Inline options under each question

# ----------------- Option Admin -----------------
@admin.register(Option)
class OptionAdmin(admin.ModelAdmin):
    list_display = ('text', 'question', 'is_correct')
    list_filter = ('question', 'is_correct')
    search_fields = ('text',)
    autocomplete_fields = ('question',)

# ----------------- ExamAttempt Admin -----------------
@admin.register(ExamAttempt)
class ExamAttemptAdmin(admin.ModelAdmin):
    list_display = ('student', 'exam_part', 'score', 'completed', 'retake_number', 'started_at', 'submitted_at')
    list_filter = ('exam_part', 'completed', 'retake_number')
    search_fields = ('student__full_name', 'exam_part__exam__title')
    autocomplete_fields = ('student', 'exam_part')

# ----------------- StudentAnswer Admin -----------------
@admin.register(StudentAnswer)
class StudentAnswerAdmin(admin.ModelAdmin):
    list_display = ('attempt', 'question', 'selected_option', 'text_answer')
    list_filter = ('question',)
    search_fields = ('attempt__student__full_name', 'question__question_text')
    autocomplete_fields = ('attempt', 'question', 'selected_option')

# ----------------- EssayScore Admin -----------------
@admin.register(EssayScore)
class EssayScoreAdmin(admin.ModelAdmin):
    list_display = ('student', 'exam_part', 'score')
    list_filter = ('exam_part',)
    search_fields = ('student__full_name', 'exam_part__exam__title')
    autocomplete_fields = ('student', 'exam_part')
