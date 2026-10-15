from django.urls import path
from . import views

app_name = "academics"

urlpatterns = [
    path("sessions/", views.manage_sessions, name="manage_sessions"),
    path("sessions/<int:session_id>/terms/", views.manage_terms, name="manage_terms"),
    path(
        "holidays/",
        views.manage_public_holidays,
        name="manage_public_holidays"
    ),
    path(
        "terms/<int:term_id>/update-school-days/",
        views.update_school_days,
        name="update_school_days",
    ),
]
