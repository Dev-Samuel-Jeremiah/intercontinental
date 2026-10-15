"""
users/forms.py
All ModelChoiceField querysets are tenant-scoped via __init__ overrides.
Never use Class.objects.all() / Student.objects.all() as static querysets —
they evaluate at import time before any tenant context is set.
"""
from django import forms
from .models import User, Staff, Student, Class, Subject, ClassSubject, StudentSubject
from tenants.middleware import get_current_tenant


# ─── Helper ──────────────────────────────────────────────────────────────────
def _tenant_classes():
    """Return tenant-scoped Class queryset (safe to call at request time)."""
    return Class.objects.all()   # TenantManager auto-scopes to current tenant

def _tenant_subjects():
    return Subject.objects.all()

def _tenant_students():
    return Student.objects.all()


# ─── User / Auth Forms ────────────────────────────────────────────────────────
class UserForm(forms.ModelForm):
    class Meta:
        model  = User
        fields = ['username', 'email', 'password', 'is_staff_user']
        widgets = {
            'password': forms.PasswordInput(attrs={'placeholder': 'Enter password'}),
        }

    def clean_username(self):
        # Usernames are unique per school, so check within the current school.
        username = self.cleaned_data['username']
        if User.objects.filter(username=username, tenant=get_current_tenant()).exists():
            raise forms.ValidationError("A user with that username already exists.")
        return username


# ─── Staff Form ───────────────────────────────────────────────────────────────
class StaffForm(forms.ModelForm):
    class Meta:
        model  = Staff
        fields = ['full_name', 'role', 'can_create_staff', 'can_generate_password',
                  'can_manage_exams', 'can_manage_students']
        widgets = {
            'role':                 forms.Select(attrs={'class': 'form-select'}),
            'can_create_staff':     forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'can_generate_password':forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'can_manage_exams':     forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'can_manage_students':  forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }


# ─── Student Registration Form ────────────────────────────────────────────────
class StudentForm(forms.ModelForm):
    parent_email = forms.EmailField(
        required=False,
        label="Parent's Email",
        widget=forms.EmailInput(attrs={'class': 'form-control'})
    )

    class Meta:
        model  = Student
        fields = [
            'full_name', 'gender', 'date_of_birth', 'passport',
            'admission_number', 'class_assigned', 'status',
            'parent_name', 'parent_phone', 'parent_email', 'address',
        ]
        widgets = {
            'gender':       forms.Select(attrs={'class': 'form-control'}),
            'class_assigned': forms.Select(attrs={'class': 'form-control'}),
            'status':       forms.Select(attrs={'class': 'form-control'}),
            'date_of_birth':forms.DateInput(attrs={'type': 'date', 'class': 'form-control'}),
            'address':      forms.Textarea(attrs={'class': 'form-control', 'rows': 2}),
            'passport':     forms.ClearableFileInput(attrs={'class': 'form-control'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Scope class_assigned to the current tenant only
        self.fields['class_assigned'].queryset = _tenant_classes()


# ─── Promote Student Form ─────────────────────────────────────────────────────
class PromoteStudentForm(forms.ModelForm):
    new_class = forms.ModelChoiceField(
        queryset=Class.objects.none(),   # overridden in __init__
        required=True,
        empty_label="Select Class",
    )

    class Meta:
        model  = Student
        fields = ['new_class']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['new_class'].queryset = _tenant_classes()


# ─── Class Form ───────────────────────────────────────────────────────────────
GRADE_CHOICES = [
    ('Grade 1',  'Grade 1'),  ('Grade 2',  'Grade 2'),  ('Grade 3',  'Grade 3'),
    ('Grade 4',  'Grade 4'),  ('Grade 5',  'Grade 5'),  ('Grade 6',  'Grade 6'),
    ('Grade 7',  'Grade 7'),  ('Grade 8',  'Grade 8'),  ('Grade 9',  'Grade 9'),
    ('Grade 10', 'Grade 10'), ('Grade 11', 'Grade 11'), ('Grade 12', 'Grade 12'),
    ('JSS 1', 'JSS 1'), ('JSS 2', 'JSS 2'), ('JSS 3', 'JSS 3'),
    ('SSS 1', 'SSS 1'), ('SSS 2', 'SSS 2'), ('SSS 3', 'SSS 3'),
    ('Primary 1', 'Primary 1'), ('Primary 2', 'Primary 2'), ('Primary 3', 'Primary 3'),
    ('Primary 4', 'Primary 4'), ('Primary 5', 'Primary 5'), ('Primary 6', 'Primary 6'),
]

class ClassForm(forms.ModelForm):
    name = forms.ChoiceField(
        choices=GRADE_CHOICES,
        widget=forms.Select(attrs={'class': 'form-control'})
    )

    class Meta:
        model  = Class
        fields = ['name']


# ─── Subject Assignment Forms ─────────────────────────────────────────────────
class AssignSubjectToClassForm(forms.ModelForm):
    school_class = forms.ModelChoiceField(queryset=Class.objects.none())
    subject      = forms.ModelChoiceField(queryset=Subject.objects.none())

    class Meta:
        model  = ClassSubject
        fields = ['school_class', 'subject']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['school_class'].queryset = _tenant_classes()
        self.fields['subject'].queryset      = _tenant_subjects()


class AssignSubjectToStudentForm(forms.ModelForm):
    student = forms.ModelChoiceField(queryset=Student.objects.none())
    subject = forms.ModelChoiceField(queryset=Subject.objects.none())

    class Meta:
        model  = StudentSubject
        fields = ['student', 'subject']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['student'].queryset = _tenant_students()
        self.fields['subject'].queryset = _tenant_subjects()