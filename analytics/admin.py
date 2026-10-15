from django.contrib import admin

from .models import KPITarget, ReportSubscription

admin.site.register(KPITarget)
admin.site.register(ReportSubscription)
