from django.urls import path
from . import views

app_name = 'announcements'

urlpatterns = [
    path('', views.announcement_list, name='announcement_list'),
    path('create/', views.create_announcement, name='create_announcement'),
    path('announcement/read/<int:announcement_id>/', views.mark_announcement_read, name='mark_announcement_read'),
    path('<int:pk>/', views.announcement_detail, name='detail'),
    path('<int:pk>/react/', views.announcement_react, name='react'),
    path('<int:pk>/like/', views.announcement_like, name='like'),  # <-- add this
    path('<int:pk>/read/', views.mark_announcement_read, name='mark_read'),


]
