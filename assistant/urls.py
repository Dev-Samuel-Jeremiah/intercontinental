from django.urls import path

from . import views, views_more

app_name = 'assistant'

urlpatterns = [
    path('', views.home, name='home'),
    path('settings/', views.ai_settings, name='settings'),
    path('questions/', views.question_generator, name='questions'),
    path('messages/new/', views.new_message, name='new_message'),
    path('messages/', views.drafts, name='drafts'),
    path('messages/<int:pk>/', views.draft, name='draft'),
    path('announcements/<int:pk>/translations/', views.translations, name='translations'),
    path('ask/', views_more.ask_page, name='ask'),
    path('at-risk/', views_more.at_risk, name='at_risk'),
    path('safety/', views_more.safety_queue, name='safety'),
    path('tutor/<int:course_id>/', views_more.tutor, name='tutor'),
    path('tutor/<int:course_id>/manage/', views_more.tutor_admin, name='tutor_admin'),
    path('essay/', views_more.essay, name='essay'),
    path('essay/<int:pk>/', views_more.essay_view, name='essay_view'),
    path('study-plan/', views_more.my_plan, name='my_plan'),
    path('study-plan/<int:student_id>/', views_more.study_plan, name='study_plan'),
]
