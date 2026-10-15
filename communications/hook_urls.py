"""Provider webhooks — included at /hooks/ on the main domain and on school addresses."""
from django.urls import path

from communications import views_connect as vc

urlpatterns = [
    path('sms-inbound/<str:token>/', vc.sms_inbound, name='hook_sms_inbound'),
    path('whatsapp/', vc.whatsapp_hook, name='hook_whatsapp'),
]
