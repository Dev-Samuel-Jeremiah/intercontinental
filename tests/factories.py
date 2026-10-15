"""
tests/factories.py
──────────────────
Small helpers for building a school (tenant) with people in it, so tests read
like the thing they are checking rather than like setup code.

Every helper sets the thread-local current tenant where the tenant-scoped
managers need it, and clears it again via `TenantTestCase.tearDown`.
"""
from django.core.cache import cache
from django.test import TestCase

from tenants.middleware import clear_current_tenant, set_current_tenant
from tenants.models import Plan, Tenant
from users.models import Class, Staff, Student, User

HOST = 'localhost'  # tests run with ROOT_DOMAIN='localhost'


def make_plan(code='test', name='Test Plan', **kwargs):
    plan, _ = Plan.objects.get_or_create(
        code=code, defaults={'name': name, 'price_per_student_term': 1000, **kwargs})
    return plan


def make_school(subdomain, name=None, status='ACTIVE', plan=None):
    """An active school, ready to log in to."""
    return Tenant.objects.create(
        name=name or f'{subdomain.title()} Academy',
        subdomain=subdomain,
        email=f'head@{subdomain}.example.com',
        status=status,
        plan=plan or make_plan(),
    )


def make_admin(tenant, username=None, password='admin-pass-1'):
    """A school administrator (is_superuser is how this app marks school admins)."""
    set_current_tenant(tenant)
    return User.objects.create_user(
        username=username or f'{tenant.subdomain}admin', password=password,
        tenant=tenant, is_staff_user=True, is_superuser=True,
    )


def make_teacher(tenant, username=None, password='teach-pass-1', full_name='Teacher One'):
    set_current_tenant(tenant)
    user = User.objects.create_user(
        username=username or f'{tenant.subdomain}teacher', password=password,
        tenant=tenant, is_staff_user=True,
    )
    Staff.objects.create(user=user, tenant=tenant, full_name=full_name, role='TEACHER')
    return user


def make_class(tenant, name='JSS 1'):
    set_current_tenant(tenant)
    return Class.objects.create(tenant=tenant, name=name)


def make_student(tenant, admission_number, full_name='Student One',
                 student_password='stud-pass-1', parent_password='par-pass-1',
                 school_class=None, parent_email=None):
    set_current_tenant(tenant)
    user = User.objects.create_user(
        username=admission_number, password=student_password,
        tenant=tenant, is_student=True,
    )
    user.term_password = student_password
    user.save(update_fields=['term_password'])
    return Student.objects.create(
        user=user, tenant=tenant, full_name=full_name,
        admission_number=admission_number, class_assigned=school_class,
        parent_term_password=parent_password,
        parent_email=parent_email or f'parent-{admission_number}@example.com'.replace('/', '-'),
        parent_name='Parent One',
    )


class TenantTestCase(TestCase):
    """Base class: clears the tenant cache and thread-local between tests."""

    def setUp(self):
        super().setUp()
        cache.clear()
        clear_current_tenant()

    def tearDown(self):
        clear_current_tenant()
        cache.clear()
        super().tearDown()

    @staticmethod
    def host(tenant):
        return f'{tenant.subdomain}.{HOST}'
