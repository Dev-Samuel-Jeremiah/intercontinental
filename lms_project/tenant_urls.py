"""
lms_project/tenant_urls.py — the school portal (ROOT_URLCONF).

This is a single-school install, so the portal is the whole site:
the home page is the school's sign-in page.
"""
from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static

from admissions import views as admissions_views
from security import media as security_media
from security import views as security_views

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', include('users.urls')),
    path('cbt/', include('cbt.urls')),
    path('dashboard/', include('dashboard.urls')),
    path('academics/', include('academics.urls')),
    path('results/', include('results.urls')),
    path('classroom/', include('classroom.urls')),
    path('inventory/', include('inventory.urls', namespace='inventory')),
    path('announcement/', include('announcement.urls')),
    path('timetable/', include('timetable.urls', namespace='timetable')),
    path('finance/', include('finance.urls', namespace='finance')),
    path('payroll/', include('payroll.urls', namespace='payroll')),
    path('hostel/', include('hostel.urls', namespace='hostel')),
    path('communications/', include('communications.urls', namespace='communications')),
    path('idcards/', include('idcards.urls', namespace='idcards')),
    path('admissions/', include('admissions.urls', namespace='admissions')),
    path('safety/', include('safety.urls', namespace='safety')),
    path('notifications/', include('notifications.urls', namespace='notifications')),
    path('staff-chat/', include('staffchat.urls', namespace='staffchat')),
    path('curriculum/', include('curriculum.urls', namespace='curriculum')),
    path('assistant/', include('assistant.urls', namespace='assistant')),
    path('analytics/', include('analytics.urls', namespace='analytics')),
    path('hr/', include('hr.urls', namespace='hr')),
    path('family/', include('family.urls', namespace='family')),
    path('life/', include('studentlife.urls', namespace='studentlife')),
    path('teaching/', include('teaching.urls', namespace='teaching')),
    path('transport/', include('transport.urls', namespace='transport')),
    path('library/', include('library.urls', namespace='library')),
    path('welfare/', include('welfare.urls', namespace='welfare')),
    path('facilities/', include('facilities.urls', namespace='facilities')),
    path('', include('saas_admin.school_urls', namespace='platform_school')),   # branding, sections, data export, help
    path('security/', include('security.urls', namespace='security')),
    path('privacy/', security_views.public_notice, name='privacy_notice'),
    path('privacy/request/', security_views.public_request, name='privacy_request'),
    path('m/<str:token>/<str:name>', security_media.serve, name='signed_media'),
    path('.well-known/security.txt', security_views.security_txt, name='security_txt'),
    path('hooks/', include('communications.hook_urls')),
    # Public application pages, kept at short URLs parents can be told over the phone
    path('apply/', admissions_views.apply, name='apply'),
    path('apply/status/', admissions_views.check_status, name='apply_status'),
    path('apply/start/', admissions_views.apply_start, name='apply_start'),
    path('apply/<str:reference>/', admissions_views.applied, name='applied'),
    path('apply/<str:reference>/fee/', admissions_views.pay_fee, name='pay_fee'),
    path('apply/<str:reference>/fee/callback/', admissions_views.fee_callback, name='fee_callback'),
    path('settings/', include('tenants.settings_urls', namespace='school_settings')),
]

urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
# Branded error pages (used when DEBUG=False) — see lms_project/error_views.py
handler400 = 'lms_project.error_views.bad_request'
handler403 = 'lms_project.error_views.permission_denied'
handler404 = 'lms_project.error_views.page_not_found'
handler500 = 'lms_project.error_views.server_error'
