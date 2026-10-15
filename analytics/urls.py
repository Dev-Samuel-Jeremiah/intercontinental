from django.urls import path

from . import views

app_name = 'analytics'

urlpatterns = [
    path('', views.home, name='home'),
    path('fees/', views.fees, name='fees'),
    path('enrolment/', views.enrolment, name='enrolment'),
    path('demographics/', views.demographics, name='demographics'),
    path('cohorts/', views.cohort, name='cohort'),
    path('students/', views.students, name='students'),
    path('targets/', views.kpis, name='kpis'),
    path('activity/', views.activity, name='activity'),
    path('board-pack/', views.board_pack, name='board_pack'),
    path('emails/', views.email_reports, name='email_reports'),
]
