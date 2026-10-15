from django.urls import path

from . import views, views_reports

app_name = 'hr'

urlpatterns = [
    path('', views.people, name='people'),
    path('directory/', views.directory, name='directory'),
    path('photo/<int:staff_id>/', views.photo, name='photo'),
    path('document/<str:kind>/<int:pk>/', views.document, name='document'),
    path('staff/<int:staff_id>/', views.staff_file, name='staff_file'),
    path('onboarding/', views.onboarding_tasks, name='onboarding_tasks'),
    path('appraisals/', views.appraisals, name='appraisals'),
    path('appraisals/cycle/<int:pk>/', views.cycle, name='cycle'),
    path('appraisals/<int:pk>/', views.appraisal, name='appraisal'),
    path('case/<int:pk>/', views.case, name='case'),
    path('exit/<int:pk>/', views.exit_case, name='exit'),
    path('vacancies/', views.vacancies, name='vacancies'),
    path('vacancies/<int:pk>/', views.vacancy, name='vacancy'),
    path('careers/', views.careers, name='careers'),
    path('careers/<int:pk>/', views.careers_apply, name='careers_apply'),
    path('me/', views.my_hr, name='my_hr'),
    # Staff work reports
    path('reports/', views_reports.staff_reports, name='staff_reports'),
    path('reports/mine/', views_reports.my_reports, name='my_reports'),
    path('reports/write/', views_reports.write_report, name='write_report'),
    path('reports/<int:pk>/', views_reports.report_detail, name='report'),
    path('reports/<int:pk>/attachment/', views_reports.report_attachment, name='report_attachment'),
]
