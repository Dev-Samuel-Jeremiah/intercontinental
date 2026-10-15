from django.urls import path
from payroll import views, views_runs

app_name = "payroll"

urlpatterns = [
    path("",                                        views.payroll_dashboard, name="dashboard"),
    path("staff/add/",                              views.add_staff_payroll, name="add_staff"),
    path("process/<int:payroll_id>/",               views.process_salary,    name="process_salary"),
    path("payslip/<int:payroll_id>/<int:month>/<int:year>/",
                                                    views.staff_payslip,     name="payslip"),
    path("runs/", views_runs.run_list, name="runs"),
    path("runs/<int:pk>/", views_runs.run_detail, name="run"),
    path("runs/<int:pk>/download/<str:what>/", views_runs.run_download, name="run_download"),
    path("payslips/<int:pk>/", views_runs.payslip, name="payslip_v2"),
    path("staff/<int:staff_id>/", views_runs.staff_pay, name="staff_pay"),
    path("settings/", views_runs.payroll_settings, name="settings"),
    path("me/", views_runs.me, name="me"),
    path("requests/", views_runs.change_requests, name="change_requests"),
]
