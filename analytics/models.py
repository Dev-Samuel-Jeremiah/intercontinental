"""
analytics/models.py
───────────────────
Targets a school sets for itself (KPIs) and who gets the weekly or monthly
summary email.
"""
from django.conf import settings
from django.db import models

from tenants.managers import TenantManager, TenantModelMixin

METRICS = [
    ('attendance', 'Attendance, last 30 days (%)'),
    ('fees', 'Fees collected this term (%)'),
    ('pass_rate', 'Students averaging 50% or more, latest term (%)'),
    ('average', 'Average score, latest term (%)'),
    ('enrolment', 'Students on roll'),
]
HIGHER_IS_BETTER = {'attendance', 'fees', 'pass_rate', 'average', 'enrolment'}


class KPITarget(TenantModelMixin, models.Model):
    metric = models.CharField(max_length=12, choices=METRICS)
    target = models.DecimalField(max_digits=10, decimal_places=1)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantManager()

    class Meta:
        unique_together = ('tenant', 'metric')


class ReportSubscription(TenantModelMixin, models.Model):
    WEEKLY, MONTHLY = 'WEEKLY', 'MONTHLY'
    email = models.EmailField()
    frequency = models.CharField(max_length=7, choices=[(WEEKLY, 'Every Monday'), (MONTHLY, 'First of the month')],
                                 default=WEEKLY)
    active = models.BooleanField(default=True)
    last_sent_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantManager()

    class Meta:
        unique_together = ('tenant', 'email')
        ordering = ['email']
