from django.contrib import admin

from .models import AssetTag, Chemical, HireBooking, MaintenanceRequest, PurchaseOrder, Requisition, Space, Vendor

admin.site.register([Space, Vendor, Requisition, PurchaseOrder, AssetTag, MaintenanceRequest, Chemical, HireBooking])
