from django.urls import path

from . import views

app_name = 'security'

urlpatterns = [
    # Everyone signed in
    path('me/', views.me, name='me'),
    path('2fa/setup/', views.setup, name='setup'),
    path('2fa/verify/', views.verify, name='verify'),
    path('2fa/backup-codes/', views.backup_codes, name='backup_codes'),
    path('sessions/', views.sessions, name='sessions'),
    # School administrators
    path('', views.overview, name='overview'),
    path('settings/', views.settings_page, name='settings'),
    path('people/', views.people, name='people'),
    path('password-help/', views.password_help, name='password_help'),
    path('audit/', views.audit, name='audit'),
    path('platform/', views.platform_page, name='platform'),
    path('platform/backups/<str:name>/', views.backup_download, name='backup_download'),
    path('roles/', views.roles, name='roles'),
    path('menu/', views.menu_access, name='menu_access'),
    path('privacy/', views.privacy, name='privacy'),
    path('consents/', views.consents, name='consents'),
    path('requests/', views.requests_list, name='requests'),
    path('requests/<int:pk>/', views.request_detail, name='request'),
    path('requests/<int:pk>/file/', views.request_file, name='request_file'),
    path('breaches/', views.breaches, name='breaches'),
    path('breaches/<int:pk>/', views.breach_detail, name='breach'),
    path('retention/', views.retention, name='retention'),
    path('compliance/', views.compliance, name='compliance'),
    # Parents
    path('family/', views.family, name='family'),
    # Public
    path('disclosure/', views.disclosure, name='disclosure'),
    path('forgot-password/no-email/', views.password_help_ask, name='password_help_ask'),
]
