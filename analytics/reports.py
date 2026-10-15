"""
analytics/reports.py
────────────────────
The summary email for proprietors and governors (F183): attendance, fees,
results, enrolment and targets for the week or month, sent by
`python manage.py send_school_reports` (run it daily — each subscriber gets
theirs on Mondays or on the 1st).
"""
from django.template.loader import render_to_string
from django.utils import timezone

from . import metrics
from .models import ReportSubscription


def summary(tenant):
    return {'school': tenant, 'today': timezone.localdate(), 'attendance': metrics.attendance_rate(tenant, 7),
            'attendance_30': metrics.attendance_rate(tenant, 30), 'fees': metrics.fee_position(tenant),
            'collections': metrics.collections(tenant, weeks=4), 'results': metrics.results_summary(tenant),
            'roll': metrics.on_roll(tenant).count(), 'kpis': metrics.kpis(tenant),
            'movement': metrics.joiners_and_leavers(tenant, 1)[-1], 'url': tenant.portal_url}


def send(subscription, data=None):
    from communications.services import send_email
    data = data or summary(subscription.tenant)
    html = render_to_string('analytics/email_summary.html', data)
    ok, _ = send_email(subscription.email, f'{subscription.tenant.name} — school summary {data["today"]:%d %b %Y}', html)
    if ok:
        ReportSubscription.objects.filter(pk=subscription.pk).update(last_sent_at=timezone.now())
    return ok


def due(subscription, today=None):
    today = today or timezone.localdate()
    if not subscription.active:
        return False
    if subscription.last_sent_at and timezone.localtime(subscription.last_sent_at).date() >= today:
        return False
    return today.weekday() == 0 if subscription.frequency == ReportSubscription.WEEKLY else today.day == 1


def run(today=None):
    from tenants.middleware import clear_current_tenant, set_current_tenant
    from tenants.models import Tenant
    sent = 0
    for tenant in Tenant.objects.filter(status__in=('ACTIVE', 'TRIAL')):
        set_current_tenant(tenant)
        try:
            subs = [s for s in ReportSubscription.objects.filter(tenant=tenant, active=True) if due(s, today)]
            if subs:
                data = summary(tenant)
                sent += sum(send(s, data) for s in subs)
        finally:
            clear_current_tenant()
    return sent
