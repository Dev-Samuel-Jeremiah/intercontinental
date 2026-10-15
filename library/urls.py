from django.urls import path

from . import views

app_name = 'library'

urlpatterns = [
    path('', views.home, name='home'),
    path('desk/', views.desk, name='desk'),
    path('catalogue/', views.catalogue, name='catalogue'),
    path('catalogue/isbn/', views.isbn_lookup, name='isbn'),
    path('books/<int:pk>/', views.book, name='book'),
    path('labels/', views.labels, name='labels'),
    path('loans/', views.loans, name='loans'),
    path('charges/', views.charges, name='charges'),
    path('reservations/', views.reservations, name='reservations'),
    path('e-library/', views.eresources, name='eresources'),
    path('e-library/<int:pk>/file/', views.eresource_file, name='eresource_file'),
    path('reading/', views.reading, name='reading'),
    path('reading/challenges/<int:pk>/', views.challenge, name='challenge'),
    path('stock-take/', views.stocktakes, name='stocktakes'),
    path('stock-take/<int:pk>/', views.stocktake, name='stocktake'),
    path('requests/', views.requests_page, name='requests'),
    path('donations/', views.donations, name='donations'),
    path('visits/', views.visits, name='visits'),
    path('reports/', views.reports, name='reports'),
    path('clearance/<int:student_id>/', views.clearance, name='clearance'),
    path('search/', views.search, name='search'),
    path('search/<int:pk>/', views.search_book, name='search_book'),
    path('me/', views.mine, name='mine'),
    path('family/', views.family, name='family'),
]
