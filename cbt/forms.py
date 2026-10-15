from django import forms

from tenants.forms import TenantScopedFormMixin
from .models import Exam

class ExamForm(TenantScopedFormMixin, forms.ModelForm):
    class Meta:
        model = Exam
        fields = '__all__'  # or list fields you want editable

    def clean(self):
        data = super().clean()
        start, end, minutes = data.get('start_time'), data.get('end_time'), data.get('duration_minutes')
        if start and end:
            if end <= start:
                self.add_error('end_time', 'The exam must end after it starts.')
            elif minutes and minutes > (end - start).total_seconds() / 60:
                self.add_error('end_time', f'The window from start to end is shorter than the {minutes}-minute exam.')
        return data


from django import forms

class BulkQuestionUploadForm(forms.Form):
    exam_part = forms.IntegerField()
    file = forms.FileField(
        help_text="Upload CSV or Excel file (.csv, .xlsx)"
    )
