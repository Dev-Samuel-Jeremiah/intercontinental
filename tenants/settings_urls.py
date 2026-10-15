"""
tenants/settings_urls.py
──────────────────────────
Mounted at /settings/ on every school subdomain (see lms_project/tenant_urls.py).
"""
from django.urls import path
from . import settings_views

app_name = 'school_settings'

urlpatterns = [
    path('', settings_views.school_settings, name='home'),
]