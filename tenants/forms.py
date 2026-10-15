"""
tenants/forms.py
────────────────
TenantScopedFormMixin — re-scopes every dropdown (ModelChoiceField /
ModelMultipleChoiceField) to the current school each time a form is built.

Why this is needed: a form's field querysets (class-level `queryset=...`
and the ones Django auto-generates for ModelForm ForeignKeys) are created
once, when forms.py is first imported at startup — when there is no
current tenant. TenantManager therefore returns an *unscoped* queryset,
and every school would see (and could submit) other schools' records.

Usage — put the mixin first:

    class ExamForm(TenantScopedFormMixin, forms.ModelForm):
        ...
"""
from tenants.middleware import get_current_tenant


class TenantScopedFormMixin:

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        tenant = get_current_tenant()
        if tenant is None:
            return
        for field in self.fields.values():
            qs = getattr(field, 'queryset', None)
            if qs is None:
                continue
            if any(f.name == 'tenant' for f in qs.model._meta.fields):
                field.queryset = qs.filter(tenant=tenant)
