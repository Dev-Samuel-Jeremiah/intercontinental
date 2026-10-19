from django.urls import path

from . import views

app_name = 'moments'

urlpatterns = [
    path('', views.home, name='home'),
    path('new/', views.new, name='new'),
    path('review/', views.review, name='review'),
    path('<int:pk>/', views.detail, name='detail'),
    path('<int:pk>/like/', views.like, name='like'),
    path('<int:pk>/likes/', views.likers, name='likers'),
    path('<int:pk>/save/', views.save, name='save'),
    path('<int:pk>/comment/', views.comment, name='comment'),
    path('<int:pk>/edit/', views.edit, name='edit'),
    path('<int:pk>/delete/', views.delete, name='delete'),
    path('<int:pk>/report/', views.report, name='report'),
    path('<int:pk>/status/', views.status, name='status'),
    path('comments/<int:comment_id>/delete/', views.comment_delete, name='comment_delete'),
    path('comments/<int:comment_id>/review/', views.comment_review, name='comment_review'),
    path('media/<int:media_id>/', views.file, name='file'),
    path('media/<int:media_id>/thumb/', views.file, {'which': 'thumb'}, name='thumb'),
]
