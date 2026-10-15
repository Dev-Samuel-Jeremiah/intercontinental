from django.urls import path

from . import views

app_name = 'teaching'

urlpatterns = [
    path('', views.home, name='home'),
    path('class/', views.class_dashboard, name='class_dashboard'),
    path('class/<int:class_id>/', views.class_dashboard, name='class_dashboard_for'),
    path('gradebook/', views.gradebooks, name='gradebooks'),
    path('gradebook/<int:pk>/', views.gradebook, name='gradebook'),
    path('gradebook/item/<int:pk>/', views.grade_item, name='grade_item'),
    path('behaviour/', views.behaviour, name='behaviour'),
    path('comments/', views.comments, name='comments'),
    path('comments/remarks/', views.remarks, name='remarks'),
    path('resources/', views.resources, name='resources'),
    path('resources/<int:pk>/file/', views.resource_file, name='resource_file'),
    path('picker/', views.picker, name='picker'),
    path('tasks/', views.tasks, name='tasks'),
    path('duties/', views.duties, name='duties'),
    path('handovers/', views.handovers, name='handovers'),
    path('handovers/<int:pk>/', views.handover, name='handover'),
]
