from django import forms

from tenants.forms import TenantScopedFormMixin
from .models import Course, Section, Lesson, Material, Assignment, Quiz

class CourseForm(TenantScopedFormMixin, forms.ModelForm):
    class Meta:
        model = Course
        fields = ['name', 'description', 'class_assigned']

class SectionForm(TenantScopedFormMixin, forms.ModelForm):
    class Meta:
        model = Section
        fields = ['title', 'order', 'course']

class LessonForm(forms.ModelForm):
    class Meta:
        model = Lesson
        fields = ['title', 'video', 'duration_minutes']

class MaterialForm(forms.ModelForm):
    class Meta:
        model = Material
        fields = ['title', 'file', 'description']

class AssignmentForm(forms.ModelForm):
    class Meta:
        model = Assignment
        fields = ['title', 'description', 'due_date', 'allow_late', 'grace_minutes', 'late_penalty_per_day',
                  'max_late_days']

class QuizForm(forms.ModelForm):
    class Meta:
        model = Quiz
        fields = ['title', 'description', 'duration_minutes']



# classroom/forms.py
from django import forms
from .models import Question, AssignmentQuestion

class QuestionForm(forms.ModelForm):
    class Meta:
        model = Question
        fields = ['question_text', 'option_a', 'option_b', 'option_c', 'option_d', 'correct_option']

class AssignmentQuestionForm(forms.ModelForm):
    class Meta:
        model = AssignmentQuestion
        fields = ['question_text']
