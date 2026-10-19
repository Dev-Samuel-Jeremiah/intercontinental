from django.urls import path

from . import views

app_name = 'labs'

urlpatterns = [
    path('', views.home, name='home'),
    path('new/', views.lab_form, name='lab_new'),
    path('<int:pk>/', views.lab, name='lab'),
    path('<int:pk>/edit/', views.lab_form, name='lab_edit'),
    path('incidents/<int:pk>/', views.incident, name='incident'),
    path('practicals/', views.practicals, name='practicals'),
    path('practicals/new/', views.practical_form, name='practical_new'),
    path('practicals/<int:pk>/', views.practical, name='practical'),
    path('practicals/<int:pk>/edit/', views.practical_form, name='practical_edit'),
    path('sessions/<int:pk>/', views.session, name='session'),
    path('readiness/', views.readiness, name='readiness'),
    path('my/', views.my, name='my'),
]
