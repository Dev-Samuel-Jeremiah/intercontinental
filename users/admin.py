from django.contrib import admin
from .models import (
    User, Staff, Student, Class, Subject,
    ClassSubject, StudentSubject, StaffSubjectClass
)

# ----------------- User -----------------
@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = ('username', 'email', 'is_staff_user','is_parent', 'is_student')
    search_fields = ('username', 'email')
    list_filter = ('is_staff_user', 'is_student')

# ----------------- Staff -----------------
@admin.register(Staff)
class StaffAdmin(admin.ModelAdmin):
    list_display = ('full_name', 'role', 'user')
    search_fields = ('full_name', 'user__username')
    list_filter = ('role',)

# ----------------- Student -----------------
@admin.register(Student)
class StudentAdmin(admin.ModelAdmin):
    list_display = ('full_name', 'admission_number', 'class_assigned', 'status', 'current_term')
    search_fields = ('full_name', 'admission_number')
    list_filter = ('status', 'current_term', 'class_assigned')
    autocomplete_fields = ('class_assigned',)

# ----------------- Class -----------------
@admin.register(Class)
class ClassAdmin(admin.ModelAdmin):
    list_display = ('name', 'description')
    search_fields = ('name',)

# ----------------- Subject -----------------
@admin.register(Subject)
class SubjectAdmin(admin.ModelAdmin):
    list_display = ('name', 'code')
    search_fields = ('name', 'code')

# ----------------- ClassSubject -----------------
@admin.register(ClassSubject)
class ClassSubjectAdmin(admin.ModelAdmin):
    list_display = ('school_class', 'subject')
    list_filter = ('school_class', 'subject')
    autocomplete_fields = ('school_class', 'subject')

# ----------------- StudentSubject -----------------
@admin.register(StudentSubject)
class StudentSubjectAdmin(admin.ModelAdmin):
    list_display = ('student', 'subject')
    list_filter = ('subject',)
    autocomplete_fields = ('student', 'subject')

# ----------------- StaffSubjectClass -----------------
@admin.register(StaffSubjectClass)
class StaffSubjectClassAdmin(admin.ModelAdmin):
    list_display = ('staff', 'subject', 'school_class')
    list_filter = ('staff', 'subject', 'school_class')
    autocomplete_fields = ('staff', 'subject', 'school_class')



