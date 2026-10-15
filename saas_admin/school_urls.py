from django.urls import path

from . import views_school as v

app_name = 'platform_school'

urlpatterns = [
    path('account/branding/', v.branding, name='branding'),
    path('account/data/', v.data, name='data'),
    path('account/data/<int:pk>/download/', v.export_file, name='export_file'),
    path('account/backups/<int:pk>/download/', v.backup_file, name='backup_file'),
    path('account/sections/', v.campuses, name='campuses'),
    path('account/section/', v.pick_campus, name='pick_campus'),
    path('help/', v.help_home, name='help'),
    path('help/<slug:slug>/', v.help_article, name='help_article'),
    path('support/tours/<int:pk>/seen/', v.tour_seen, name='tour_seen'),
    path('support/notices/<int:pk>/dismiss/', v.dismiss_notice, name='dismiss_notice'),
]
