# dashboard/urls.py
from django.urls import path
from . import views

app_name = "dashboard"

urlpatterns = [
    path("", views.dashboard_router, name="router"),

    path("super-admin/", views.super_admin_dashboard, name="super_admin"),
    path("admin/", views.admin_dashboard, name="admin"),
    path("teacher/", views.teacher_dashboard, name="teacher"),
    path("student/", views.student_dashboard, name="student"),
   
]
