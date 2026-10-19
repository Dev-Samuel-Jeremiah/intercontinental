from django.urls import path

from . import views

app_name = 'schoolai'

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('ask/', views.ask, name='ask'),
    path('ask/<int:pk>/rate/', views.rate, name='rate'),
    path('students/', views.students, name='students'),
    path('knowledge/', views.knowledge_list, name='knowledge'),
    path('questions/', views.questions, name='questions'),
    path('models/<str:kind>/', views.model_detail, name='model'),
]
