from django import forms

from tenants.forms import TenantScopedFormMixin
from users.models import Class

from .models import AdmissionSettings, Application


class ApplicationForm(TenantScopedFormMixin, forms.ModelForm):
    """The public form a parent fills in. Deliberately short."""

    class Meta:
        model = Application
        fields = [
            'full_name', 'gender', 'date_of_birth', 'applying_for', 'previous_school',
            'passport', 'parent_name', 'relationship', 'parent_phone', 'parent_email',
            'address', 'source', 'source_detail',
        ]
        widgets = {
            'date_of_birth': forms.DateInput(attrs={'type': 'date'}),
            'address': forms.Textarea(attrs={'rows': 2}),
            'source_detail': forms.TextInput(attrs={'placeholder': 'Optional — who or where?'}),
        }
        labels = {
            'full_name': "Child's full name",
            'applying_for': 'Class applying for',
            'previous_school': 'Previous school (if any)',
            'passport': 'Passport photograph',
            'parent_name': 'Parent / guardian name',
            'relationship': 'Relationship to the child',
            'parent_phone': 'Phone number',
            'parent_email': 'Email address',
            'address': 'Home address',
            'source': 'How did you hear about us?',
            'source_detail': 'Any detail',
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['applying_for'].queryset = self.fields['applying_for'].queryset.order_by('name')
        self.fields['applying_for'].required = True
        self.fields['parent_name'].required = True
        self.fields['parent_phone'].required = True
        for name, field in self.fields.items():
            field.widget.attrs.setdefault('class', 'f-input')
            if name in ('full_name', 'parent_name', 'parent_phone'):
                field.widget.attrs['required'] = 'required'

    def clean_parent_phone(self):
        phone = (self.cleaned_data.get('parent_phone') or '').strip()
        digits = ''.join(ch for ch in phone if ch.isdigit())
        if len(digits) < 10:
            raise forms.ValidationError('Enter a complete phone number so the school can reach you.')
        return phone


class DecisionForm(forms.ModelForm):
    """Staff-side scoring and notes."""

    class Meta:
        model = Application
        fields = ['exam_score', 'interview_score', 'offer_expires_on', 'decision_note',
                  'applying_for']
        widgets = {
            'offer_expires_on': forms.DateInput(attrs={'type': 'date'}),
            'decision_note': forms.Textarea(attrs={'rows': 3}),
        }


class AdmissionSettingsForm(TenantScopedFormMixin, forms.ModelForm):
    class Meta:
        model = AdmissionSettings
        fields = ['applications_open', 'session', 'application_fee', 'number_format',
                  'number_prefix', 'next_number', 'require_documents', 'instructions']
        widgets = {'instructions': forms.Textarea(attrs={'rows': 3})}
        labels = {
            'applications_open': 'Accept applications now',
            'session': 'Applicants are applying for',
            'application_fee': 'Application fee (₦)',
            'number_format': 'Admission number format',
            'number_prefix': 'Prefix',
            'next_number': 'Next number',
            'require_documents': 'Require at least one document',
            'instructions': 'Notes shown on the form',
        }
