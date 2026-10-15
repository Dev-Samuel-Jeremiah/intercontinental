from django.urls import path

from . import views

app_name = 'transport'

urlpatterns = [
    path('', views.home, name='home'),
    path('routes/<int:pk>/', views.route_detail, name='route'),
    path('routes/<int:pk>/manifest/', views.manifest, name='manifest'),
    path('fleet/', views.fleet, name='fleet'),
    path('fleet/documents/<int:pk>/', views.document_file, name='document_file'),
    path('crew/', views.crew, name='crew'),
    path('trips/', views.my_trips, name='my_trips'),
    path('trips/route/<int:route_id>/', views.run_trip, name='run_trip'),
    path('trips/field/<int:field_id>/', views.run_trip, name='run_field_trip'),
    path('trips/<int:trip_id>/position/', views.position, name='position'),
    path('fees/', views.fees, name='fees'),
    path('field-trips/', views.field_trips, name='field_trips'),
    path('pickup-changes/', views.changes, name='changes'),
    path('incidents/', views.incidents, name='incidents'),
    path('reports/', views.reports, name='reports'),
    path('family/', views.family, name='family'),
    path('family/position/<int:trip_id>/', views.family_position, name='family_position'),
]
