from django.urls import path

from . import views

app_name = 'welfare'

urlpatterns = [
    path('', views.home, name='home'),
    path('sick-bay/', views.sick_bay, name='sick_bay'),
    path('medical/<int:student_id>/', views.medical, name='medical'),
    path('immunisations/', views.immunisations, name='immunisations'),
    path('immunisations/<int:pk>/proof/', views.immunisation_file, name='immunisation_file'),
    path('medication/', views.medication, name='medication'),
    path('screening/', views.screening, name='screening'),
    path('health-plans/', views.health_plans, name='health_plans'),
    path('accidents/', views.accidents, name='accidents'),
    path('emergency/', views.emergency, name='emergency'),
    path('incidents/', views.incidents, name='incidents'),
    path('points/', views.points, name='points'),
    path('detentions/', views.detentions, name='detentions'),
    path('suspensions/', views.suspensions, name='suspensions'),
    path('suspensions/<int:pk>/letter/', views.suspension_letter, name='letter'),
    path('restorative/', views.restorative, name='restorative'),
    path('counselling/', views.counselling, name='counselling'),
    path('counselling/<int:pk>/', views.case, name='case'),
    path('safeguarding/', views.safeguarding, name='safeguarding'),
    path('safeguarding/<int:pk>/', views.concern, name='concern'),
    path('support-plans/', views.support, name='support'),
    path('support-plans/<int:pk>/', views.plan, name='plan'),
    path('sports/', views.sports, name='sports'),
    path('resources/', views.resources, name='resources'),
    path('settings/', views.settings_page, name='settings'),
    path('me/', views.mine, name='mine'),
    path('family/', views.family, name='family'),
]
