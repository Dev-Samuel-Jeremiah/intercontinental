from django import forms

from tenants.forms import TenantScopedFormMixin
from django.contrib.auth import get_user_model
from .models import Asset, AssetAssignment, Category, Location, MaintenanceLog, StockMovement

User = get_user_model()


def _is_taken(model, field, value, instance):
    """True if another row in the current school already uses this value.
    (`objects` is tenant-scoped, so other schools' rows are ignored.)"""
    qs = model.objects.filter(**{f'{field}__iexact': value})
    if instance.pk:
        qs = qs.exclude(pk=instance.pk)
    return qs.exists()


class AssetForm(TenantScopedFormMixin, forms.ModelForm):
    class Meta:
        model = Asset
        fields = ['name', 'asset_code', 'category', 'asset_type', 'unit', 'reorder_level', 'status']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Asset name'}),
            'asset_code': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. CHR-001'}),
            'category': forms.Select(attrs={'class': 'form-select'}),
            'asset_type': forms.Select(attrs={'class': 'form-select'}),
            'unit': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'pcs / litres / kg'}),
            'reorder_level': forms.NumberInput(attrs={'class': 'form-control', 'min': 0}),
            'status': forms.Select(attrs={'class': 'form-select'}),
        }

    def clean_asset_code(self):
        code = self.cleaned_data['asset_code'].strip()
        if _is_taken(Asset, 'asset_code', code, self.instance):
            raise forms.ValidationError("An asset with this code already exists.")
        return code


class StockInForm(TenantScopedFormMixin, forms.Form):
    asset = forms.ModelChoiceField(
        queryset=Asset.objects.filter(status='ACTIVE'),
        widget=forms.Select(attrs={'class': 'form-select'}),
        label='Asset'
    )
    quantity = forms.IntegerField(
        min_value=1,
        widget=forms.NumberInput(attrs={'class': 'form-control', 'min': 1}),
        label='Quantity'
    )
    reason = forms.CharField(
        widget=forms.Textarea(attrs={'class': 'form-control', 'rows': 3, 'placeholder': 'Reason for stock-in (e.g. Purchase, Donation)'}),
        label='Reason'
    )


class StockAdjustForm(TenantScopedFormMixin, forms.Form):
    asset = forms.ModelChoiceField(
        queryset=Asset.objects.filter(status='ACTIVE'),
        widget=forms.Select(attrs={'class': 'form-select'}),
        label='Asset'
    )
    adjustment = forms.IntegerField(
        widget=forms.NumberInput(attrs={'class': 'form-control', 'placeholder': 'Use negative to reduce stock'}),
        label='Adjustment Quantity (positive = add, negative = remove)'
    )
    reason = forms.CharField(
        widget=forms.Textarea(attrs={'class': 'form-control', 'rows': 3, 'placeholder': 'Reason for adjustment'}),
        label='Reason'
    )


class IssueAssetForm(TenantScopedFormMixin, forms.Form):
    asset = forms.ModelChoiceField(
        queryset=Asset.objects.filter(status='ACTIVE'),
        widget=forms.Select(attrs={'class': 'form-select'}),
        label='Asset'
    )
    quantity = forms.IntegerField(
        min_value=1,
        widget=forms.NumberInput(attrs={'class': 'form-control'}),
        label='Quantity'
    )
    assigned_to_user = forms.ModelChoiceField(
        queryset=User.objects.all(),
        required=False,
        widget=forms.Select(attrs={'class': 'form-select'}),
        label='Assign to User (optional)'
    )
    location = forms.ModelChoiceField(
        queryset=Location.objects.all(),
        required=False,
        widget=forms.Select(attrs={'class': 'form-select'}),
        label='Assign to Location (optional)'
    )
    condition_at_issue = forms.ChoiceField(
        choices=AssetAssignment.CONDITION,
        initial='GOOD',
        widget=forms.Select(attrs={'class': 'form-select'}),
        label='Condition at Issue'
    )
    expected_return_date = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={'class': 'form-control', 'type': 'date'}),
        label='Expected Return Date (leave blank for consumables)'
    )

    def clean(self):
        cleaned = super().clean()
        user = cleaned.get('assigned_to_user')
        location = cleaned.get('location')
        if not user and not location:
            raise forms.ValidationError("Please assign to either a user or a location.")
        if user and location:
            raise forms.ValidationError("Cannot assign to both a user and a location.")
        return cleaned


class ReturnAssetForm(TenantScopedFormMixin, forms.Form):
    assignment = forms.ModelChoiceField(
        queryset=AssetAssignment.objects.filter(returned=False),
        widget=forms.Select(attrs={'class': 'form-select'}),
        label='Select Assignment to Return'
    )


class MaintenanceLogForm(TenantScopedFormMixin, forms.ModelForm):
    class Meta:
        model = MaintenanceLog
        fields = ['asset', 'issue_reported', 'action_taken', 'cost', 'date']
        widgets = {
            'asset': forms.Select(attrs={'class': 'form-select'}),
            'issue_reported': forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
            'action_taken': forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
            'cost': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}),
            'date': forms.DateInput(attrs={'class': 'form-control', 'type': 'date'}),
        }


class CategoryForm(forms.ModelForm):
    class Meta:
        model = Category
        fields = ['name', 'description']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Category name'}),
            'description': forms.Textarea(attrs={'class': 'form-control', 'rows': 2}),
        }

    def clean_name(self):
        name = self.cleaned_data['name'].strip()
        if _is_taken(Category, 'name', name, self.instance):
            raise forms.ValidationError("A category with this name already exists.")
        return name


class LocationForm(forms.ModelForm):
    class Meta:
        model = Location
        fields = ['name', 'location_type', 'description']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control'}),
            'location_type': forms.Select(attrs={'class': 'form-select'}),
            'description': forms.Textarea(attrs={'class': 'form-control', 'rows': 2}),
        }
