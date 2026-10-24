from django.urls import path

from . import views

app_name = 'safety'

urlpatterns = [
    path('pickup/', views.pickup_desk, name='pickup_desk'),
    path('pickups/', views.pickup_log, name='pickup_log'),
    path('roll-call/', views.roll_calls, name='roll_calls'),
    path('roll-call/<int:roll_id>/', views.roll_call, name='roll_call'),
    path('lost-cards/', views.lost_cards, name='lost_cards'),
    path('settings/', views.safety_settings, name='settings'),
    path('parent/pickup/', views.parent_pickup, name='parent_pickup'),
    path('visitors/', views.visitor_desk, name='visitor_desk'),
    path('visitors/<int:visit_id>/badge/', views.visitor_badge, name='visitor_badge'),
    path('visitors/<int:visit_id>/photo/', views.visitor_photo, name='visitor_photo'),
    path('visitors/scan/<uuid:token>/', views.visitor_scan, name='visitor_scan'),
    path('pass/<uuid:token>/', views.visit_pass, name='visit_pass'),
    path('pass/<uuid:token>/qr.png', views.visit_qr, name='visit_qr'),
    path('staff/leave/', views.staff_leave, name='staff_leave'),
    path('staff/attendance/', views.staff_attendance, name='staff_attendance'),
    path('staff/attendance/times/', views.staff_hours, name='staff_hours'),
    path('clock/', views.phone_clock, name='phone_clock'),
]
