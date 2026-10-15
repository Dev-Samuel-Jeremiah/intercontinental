"""
tenants/managers.py
───────────────────
TenantQuerySet & TenantManager — drop-in replacements for models.Manager
that automatically scope all queries to the current tenant.

Usage in any app model:

    from tenants.managers import TenantManager

    class Student(models.Model):
        tenant  = models.ForeignKey('tenants.Tenant', on_delete=models.CASCADE,
                                     db_index=True)
        ...
        objects = TenantManager()

Then every queryset call (Student.objects.all(), .filter(), etc.) is
automatically scoped to the current request's tenant.

If no tenant is set (e.g. management commands, signals, tests) the full
queryset is returned so you can still do cross-tenant admin work.
"""

from django.db import models
from tenants.middleware import get_current_tenant


class TenantQuerySet(models.QuerySet):

    def for_tenant(self, tenant):
        """Explicit per-tenant scope (use in management commands)."""
        return self.filter(tenant=tenant)

    def current(self):
        """Scope to the thread-local current tenant."""
        tenant = get_current_tenant()
        if tenant is not None:
            return self.filter(tenant=tenant)
        return self  # no tenant context — return unscoped (admin/mgmt use)


class TenantManager(models.Manager):

    def get_queryset(self):
        qs = TenantQuerySet(self.model, using=self._db)
        tenant = get_current_tenant()
        if tenant is not None:
            return qs.filter(tenant=tenant)
        return qs  # no tenant context — unscoped (admin / management commands)

    def for_tenant(self, tenant):
        return TenantQuerySet(self.model, using=self._db).for_tenant(tenant)

    def current(self):
        return TenantQuerySet(self.model, using=self._db).current()


class TenantModelMixin(models.Model):
    """
    Abstract mixin — add to any model that must be tenant-scoped.
    Adds the FK, the manager, and auto-assigns tenant on save().

    class AcademicSession(TenantModelMixin):
        name = models.CharField(...)
    """
    tenant = models.ForeignKey(
        'tenants.Tenant',
        on_delete=models.CASCADE,
        db_index=True,
        null=True,          # null during migration squashing / imports
        blank=True,
    )

    objects = TenantManager()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self.tenant_id:
            tenant = get_current_tenant()
            if tenant:
                self.tenant = tenant
        super().save(*args, **kwargs)