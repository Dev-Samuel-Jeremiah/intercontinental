from django.urls import path

from . import views

app_name = 'staffchat'

urlpatterns = [
    path('', views.home, name='home'),
    path('<int:pk>/', views.home, name='room'),
    path('<int:pk>/manage/', views.manage, name='manage'),
    path('direct/<int:user_id>/', views.direct, name='direct'),
    path('groups/new/', views.new_group, name='new_group'),
    path('complaints/new/', views.new_complaint, name='new_complaint'),
    path('file/<int:msg_id>/', views.attachment, name='file'),
    path('api/poll/', views.api_poll, name='api_poll'),
    path('api/unread/', views.api_unread, name='api_unread'),
    path('api/rooms/<int:pk>/', views.api_room, name='api_room'),
    path('api/rooms/<int:pk>/send/', views.api_send, name='api_send'),
    path('api/rooms/<int:pk>/typing/', views.api_typing, name='api_typing'),
    path('api/messages/<int:msg_id>/react/', views.api_react, name='api_react'),
    path('api/messages/<int:msg_id>/delete/', views.api_delete, name='api_delete'),
    path('api/messages/<int:msg_id>/edit/', views.api_edit, name='api_edit'),
]
