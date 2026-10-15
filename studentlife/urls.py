from django.urls import path

from . import views

app_name = 'studentlife'

urlpatterns = [
    path('', views.hub, name='hub'),
    path('leaderboards/', views.leaderboards, name='leaderboards'),
    path('portfolio/', views.portfolio, name='portfolio'),
    path('portfolio/student/<int:student_id>/', views.portfolio, name='portfolio_of'),
    path('portfolio/file/<int:pk>/', views.portfolio_file, name='portfolio_file'),
    path('careers/', views.careers, name='careers'),
    path('careers/combination/', views.combination, name='combination'),
    path('careers/student/<int:student_id>/', views.career_note, name='career_note'),
    path('handbook/', views.handbook, name='handbook'),
    path('handbook/edit/', views.handbook_edit, name='handbook_edit'),
    path('report/', views.report, name='report'),
    path('report/check/', views.report_check, name='report_check'),
    path('reports/', views.reports, name='reports'),
    path('reports/<int:pk>/', views.report_detail, name='report_detail'),
    path('checkin/', views.checkin, name='checkin'),
    path('wellbeing/', views.wellbeing, name='wellbeing'),
    path('tutoring/', views.tutoring, name='tutoring'),
    path('tutoring/manage/', views.tutoring_manage, name='tutoring_manage'),
    path('goals/', views.goals, name='goals'),
    path('goals/class/', views.class_goals, name='class_goals'),
    path('notes/', views.notes, name='notes'),
    path('notes/file/<int:pk>/', views.note_file, name='note_file'),
    path('notes/review/', views.notes_review, name='notes_review'),
    path('settings/', views.life_settings, name='settings'),
]
