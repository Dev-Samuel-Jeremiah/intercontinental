from django.urls import path

from . import views

app_name = 'notifications'

urlpatterns = [
    path('', views.all_notifications, name='all'),
    path('feed/', views.feed, name='feed'),
    path('read/', views.mark_read, name='read'),
    path('<int:pk>/open/', views.open_notification, name='open'),
]
