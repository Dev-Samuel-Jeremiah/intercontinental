from django.urls import path

from . import views

app_name = 'admissions'

urlpatterns = [
    # Staff
    path('', views.board, name='board'),
    path('list/', views.application_list, name='list'),
    path('analytics/', views.analytics, name='analytics'),
    path('settings/', views.settings_page, name='settings'),
    path('settings/quota/', views.set_quota, name='set_quota'),
    path('ranking/', views.ranking, name='ranking'),
    path('waitlist/', views.waitlist, name='waitlist'),
    path('<int:pk>/', views.application_detail, name='detail'),
    path('<int:pk>/offer-letter/', views.offer_letter, name='offer_letter'),
    path('<int:pk>/offer-letter/email/', views.email_offer_letter, name='email_offer_letter'),
]
