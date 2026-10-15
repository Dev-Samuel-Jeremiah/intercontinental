from django.urls import path

from . import views, views_events, views_progression as vp, views_staffing as vs

app_name = 'curriculum'

urlpatterns = [
    path('', views.home, name='home'),
    path('allocation/', views.allocation, name='allocation'),
    path('departments/', views.departments, name='departments'),
    path('schemes/', views.schemes, name='schemes'),
    path('schemes/<int:pk>/', views.scheme, name='scheme'),
    path('notes/', views.notes, name='notes'),
    path('notes/new/', views.note_edit, name='note_new'),
    path('notes/review/', views.review, name='review'),
    path('notes/<int:pk>/', views.note, name='note'),
    path('notes/<int:pk>/edit/', views.note_edit, name='note_edit'),
    path('notes/<int:pk>/file/', views.note_file, name='note_file'),
    path('homework/', views.homework, name='homework'),
    path('homework/<int:pk>/file/', views.homework_file, name='homework_file'),
    path('calendar/', views.school_calendar, name='calendar'),
    path('events/<int:pk>/', views_events.event, name='event'),
    path('diary/', views.family, name='family'),
    path('streams/', vp.streams, name='streams'),
    path('cover/', vs.cover, name='cover'),
    path('observations/', vs.observations, name='observations'),
    path('observations/new/', vs.observation_new, name='observation_new'),
    path('observations/<int:pk>/', vs.observation, name='observation'),
    path('clubs/', vs.clubs, name='clubs'),
    path('clubs/<int:pk>/', vs.club, name='club'),
    path('promotion/', vp.promotion, name='promotion'),
    path('promotion/<int:class_id>/', vp.promotion_class, name='promotion_class'),
    path('graduation/certificates/', vp.graduation_certificates, name='graduation_certificates'),
    path('graduation/verify/<str:reference>/', vp.verify_graduation, name='verify_graduation'),
    path('graduation/verify/<str:reference>/qr.png', vp.graduation_qr, name='graduation_qr'),
]
