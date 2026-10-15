from django import forms

from tenants.forms import TenantScopedFormMixin
from .models import Exam

class ExamForm(TenantScopedFormMixin, forms.ModelForm):
    class Meta:
        model = Exam
        fields = '__all__'  # or list fields you want editable


from django import forms

class BulkQuestionUploadForm(forms.Form):
    exam_part = forms.IntegerField()
    file = forms.FileField(
        help_text="Upload CSV or Excel file (.csv, .xlsx)"
    )
