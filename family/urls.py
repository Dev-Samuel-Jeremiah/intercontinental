from django.urls import path

from . import views

app_name = 'family'

urlpatterns = [
    # Parents
    path('', views.hub, name='hub'),
    path('today/', views.digest_view, name='digest'),
    path('statement/<int:student_id>/', views.statement, name='statement'),
    path('teachers/', views.teachers, name='teachers'),
    path('book/', views.book, name='book'),
    path('pta/', views.pta, name='pta'),
    path('volunteer/', views.volunteer, name='volunteer'),
    path('feedback/', views.feedback, name='feedback'),
    path('feedback/<int:pk>/', views.feedback_detail, name='feedback_detail'),
    # Sign in without a password
    path('login/', views.magic_request, name='magic_request'),
    path('login/code/', views.magic_code, name='magic_code'),
    path('login/<str:token>/', views.magic_link, name='magic_link'),
    # Staff
    path('manage/', views.manage, name='manage'),
    path('appointments/', views.staff_appointments, name='staff_appointments'),
    path('pta/manage/', views.pta_manage, name='pta_manage'),
    path('pta/levy/<int:pk>/', views.pta_levy, name='pta_levy'),
    path('volunteers/', views.volunteer_manage, name='volunteer_manage'),
    path('tickets/', views.tickets, name='tickets'),
    path('tickets/<int:pk>/', views.ticket_detail, name='ticket_detail'),
]
