from django import forms

from tenants.forms import TenantScopedFormMixin
from .models import TimetableConfiguration, TimeSlot, TeacherSubjectClass
from users.models import Staff, Subject, Class


class TimetableConfigForm(TenantScopedFormMixin, forms.ModelForm):
    DAY_CHOICES = [
        ('MON', 'Monday'),
        ('TUE', 'Tuesday'),
        ('WED', 'Wednesday'),
        ('THU', 'Thursday'),
        ('FRI', 'Friday'),
        ('SAT', 'Saturday'),
    ]

    active_days_select = forms.MultipleChoiceField(
        choices=DAY_CHOICES,
        widget=forms.CheckboxSelectMultiple,
        label='Active School Days',
        initial=['MON', 'TUE', 'WED', 'THU', 'FRI'],
    )

    class Meta:
        model = TimetableConfiguration
        fields = ['name', 'time_slots', 'is_active']
        widgets = {
            'time_slots': forms.CheckboxSelectMultiple,
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk and self.instance.active_days:
            self.fields['active_days_select'].initial = self.instance.active_days

    def clean_name(self):
        # TenantManager scopes this to the current school only.
        name = self.cleaned_data['name'].strip()
        qs = TimetableConfiguration.objects.filter(name__iexact=name)
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError("A timetable with this name already exists.")
        return name

    def save(self, commit=True):
        obj = super().save(commit=False)
        obj.active_days = self.cleaned_data['active_days_select']
        if commit:
            obj.save()
            self._save_m2m()
        return obj


class TimeSlotForm(forms.ModelForm):
    class Meta:
        model = TimeSlot
        fields = ['name', 'start_time', 'end_time', 'order', 'is_break']
        widgets = {
            'start_time': forms.TimeInput(attrs={'type': 'time'}),
            'end_time': forms.TimeInput(attrs={'type': 'time'}),
        }


class TeacherSubjectClassForm(TenantScopedFormMixin, forms.ModelForm):
    class Meta:
        model = TeacherSubjectClass
        fields = ['staff', 'subject', 'school_class', 'periods_per_week', 'double_periods', 'room_kind']

    def __init__(self, *args, **kwargs):
        self.config = kwargs.pop('config', None)
        super().__init__(*args, **kwargs)
        self.fields['staff'].queryset = Staff.objects.filter(role='TEACHER').order_by('full_name')
        self.fields['subject'].queryset = Subject.objects.all().order_by('name')     # TenantManager auto-scopes
        self.fields['school_class'].queryset = Class.objects.all().order_by('name')  # TenantManager auto-scopes
        from .models import TeachingRoom
        self.fields['room_kind'].widget = forms.Select(choices=[('', "The class's own room")] + TeachingRoom.KINDS)
        self.fields['periods_per_week'].widget.attrs.update({'min': 1, 'max': 15})

    def save(self, commit=True):
        obj = super().save(commit=False)
        if self.config:
            obj.config = self.config
        if commit:
            obj.save()
        return obj