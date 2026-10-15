from django.contrib import admin

from .models import Driver, Incident, Rider, Route, Stop, TransportBill, Vehicle, VehicleDocument


class StopInline(admin.TabularInline):
    model = Stop
    extra = 0


@admin.register(Route)
class RouteAdmin(admin.ModelAdmin):
    list_display = ('name', 'tenant', 'vehicle', 'driver', 'active')
    list_filter = ('active',)
    inlines = [StopInline]


admin.site.register([Vehicle, VehicleDocument, Driver, Rider, TransportBill, Incident])
