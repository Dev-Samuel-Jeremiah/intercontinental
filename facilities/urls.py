from django.urls import path

from . import views

app_name = 'facilities'

urlpatterns = [
    path('', views.home, name='home'),
    path('requisitions/', views.requisitions, name='requisitions'),
    path('orders/', views.orders, name='orders'),
    path('orders/<int:pk>/', views.order, name='order'),
    path('orders/<int:pk>/print/', views.order_print, name='order_print'),
    path('vendors/', views.vendors, name='vendors'),
    path('vendors/<int:pk>/', views.vendor, name='vendor'),
    path('vendors/contracts/<int:pk>/file/', views.contract_file, name='contract_file'),
    path('assets/', views.assets, name='assets'),
    path('assets/<int:pk>/', views.asset, name='asset'),
    path('assets/labels/', views.tag_labels, name='tag_labels'),
    path('tag/<uuid:uid>/', views.tag_scan, name='tag_scan'),
    path('disposals/', views.disposals, name='disposals'),
    path('stock-counts/', views.counts, name='counts'),
    path('stock-counts/<int:pk>/', views.count, name='count'),
    path('maintenance/', views.maintenance, name='maintenance'),
    path('maintenance/<int:pk>/photo/', views.job_photo, name='job_photo'),
    path('preventive/', views.preventive, name='preventive'),
    path('generators/', views.generators, name='generators'),
    path('utilities/', views.utilities, name='utilities'),
    path('utilities/<int:pk>/file/', views.bill_file, name='bill_file'),
    path('spaces/', views.spaces, name='spaces'),
    path('cleaning/', views.cleaning, name='cleaning'),
    path('patrols/', views.patrols, name='patrols'),
    path('patrols/sheet/', views.patrol_sheet, name='patrol_sheet'),
    path('patrol/<uuid:uid>/', views.patrol_check, name='patrol_check'),
    path('chemicals/', views.chemicals, name='chemicals'),
    path('chemicals/<int:pk>/sds/', views.sds_file, name='sds_file'),
    path('kitchen/', views.kitchen, name='kitchen'),
    path('hire/', views.hire, name='hire'),
    path('hire/<int:pk>/', views.booking, name='booking'),
    path('hire/<int:pk>/invoice/', views.invoice_print, name='invoice_print'),
    path('settings/', views.settings_page, name='settings'),
]
