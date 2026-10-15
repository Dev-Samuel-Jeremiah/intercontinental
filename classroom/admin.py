from django.contrib import admin
from .models import (
    Course, Section, Lesson, Material, Assignment, AssignmentQuestion,
    Quiz, Question, Submission, StudentSectionProgress, LessonProgress,  SubmissionAnswer
)

# ----------------- INLINES -----------------
class LessonInline(admin.TabularInline):
    model = Lesson
    extra = 1
    fields = ('title', 'video', 'duration_minutes', 'order')
    ordering = ('order',)

class MaterialInline(admin.TabularInline):
    model = Material
    extra = 1
    fields = ('title', 'file', 'description')

class AssignmentQuestionInline(admin.TabularInline):
    model = AssignmentQuestion
    extra = 1
    fields = ('question_text',)
    show_change_link = True

class AssignmentInline(admin.TabularInline):
    model = Assignment
    extra = 1
    fields = ('title', 'description', 'due_date')
    inlines = [AssignmentQuestionInline]

class QuestionInline(admin.TabularInline):
    model = Question
    extra = 1
    fields = ('question_text', 'option_a', 'option_b', 'option_c', 'option_d', 'correct_option')
    show_change_link = True

class QuizInline(admin.TabularInline):
    model = Quiz
    extra = 1
    fields = ('title', 'description', 'duration_minutes', 'pass_mark')
    inlines = [QuestionInline]

class SubmissionInline(admin.TabularInline):
    model = Submission
    extra = 0
    readonly_fields = ('submitted_at',)

# ----------------- ADMIN MODELS -----------------
@admin.register(Course)
class CourseAdmin(admin.ModelAdmin):
    list_display = ('name', 'teacher', 'class_assigned', 'created_at')
    search_fields = ('name', 'teacher__username')
    list_filter = ('created_at', 'class_assigned')

@admin.register(Section)
class SectionAdmin(admin.ModelAdmin):
    list_display = ('title', 'course', 'order')
    search_fields = ('title', 'course__name')
    list_filter = ('course',)
    inlines = [LessonInline, MaterialInline, AssignmentInline, QuizInline]
    ordering = ('course', 'order')

@admin.register(Lesson)
class LessonAdmin(admin.ModelAdmin):
    list_display = ('title', 'section', 'order', 'duration_minutes')
    search_fields = ('title', 'section__title', 'section__course__name')
    list_filter = ('section',)
    ordering = ('section', 'order')

@admin.register(Material)
class MaterialAdmin(admin.ModelAdmin):
    list_display = ('title', 'section', 'uploaded_at')
    search_fields = ('title', 'section__title', 'section__course__name')
    list_filter = ('uploaded_at', 'section__course')

@admin.register(Assignment)
class AssignmentAdmin(admin.ModelAdmin):
    list_display = ('title', 'section', 'due_date', 'created_at')
    search_fields = ('title', 'section__title', 'section__course__name')
    list_filter = ('due_date', 'created_at')
    inlines = [AssignmentQuestionInline, SubmissionInline]

@admin.register(Submission)
class SubmissionAdmin(admin.ModelAdmin):
    list_display = ('assignment', 'student', 'submitted_at', 'grade')
    search_fields = ('assignment__title', 'student__username')
    list_filter = ('submitted_at', 'grade')

@admin.register(Quiz)
class QuizAdmin(admin.ModelAdmin):
    list_display = ('title', 'section', 'duration_minutes', 'pass_mark', 'created_at')
    search_fields = ('title', 'section__title', 'section__course__name')
    list_filter = ('created_at',)
    inlines = [QuestionInline]

@admin.register(Question)
class QuestionAdmin(admin.ModelAdmin):
    list_display = ('quiz', 'question_text', 'correct_option')
    search_fields = ('quiz__title', 'question_text')

@admin.register(StudentSectionProgress)
class StudentSectionProgressAdmin(admin.ModelAdmin):
    list_display = ('student', 'section', 'completed', 'completed_at')
    search_fields = ('student__username', 'section__title', 'section__course__name')
    list_filter = ('completed', 'section__course')

@admin.register(LessonProgress)
class LessonProgressAdmin(admin.ModelAdmin):
    list_display = ('student', 'lesson', 'watched')
    search_fields = ('student__username', 'lesson__title', 'lesson__section__course__name')
    list_filter = ('watched', 'lesson__section__course')

admin.site.register( SubmissionAnswer)