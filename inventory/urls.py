from django.urls import path
from . import views

app_name = 'inventory'

urlpatterns = [
    path('dashboard/', views.dashboard, name='dashboard'),

    # Assets
    path('assets/', views.asset_list, name='asset_list'),
    path('assets/add/', views.add_asset, name='add_asset'),
    path('assets/<int:asset_id>/', views.asset_detail, name='asset_detail'),
    path('assets/<int:asset_id>/edit/', views.edit_asset, name='edit_asset'),

    # Stock
    path('stock/in/', views.stock_in, name='stock_in'),
    path('stock/adjust/', views.stock_adjust, name='stock_adjust'),

    # Issue & Return
    path('issue/', views.issue_asset_view, name='issue_asset'),
    path('return/', views.return_asset_view, name='return_asset'),

    # Maintenance
    path('maintenance/add/', views.add_maintenance_log, name='add_maintenance'),

    # Reports
    path('reports/stock-ledger/', views.stock_ledger_view, name='stock_ledger'),
    path('reports/asset-register/', views.asset_register_view, name='asset_register'),
    path('reports/assignments/', views.assignment_report_view, name='assignment_report'),
    path('reports/maintenance/', views.maintenance_report_view, name='maintenance_report'),

    # Categories & Locations
    path('categories/', views.categories_page, name='categories_page'),
    path('categories/delete/<int:pk>/', views.delete_category, name='delete_category'),
    path('locations/', views.locations_page, name='locations_page'),
    path('locations/delete/<int:pk>/', views.delete_location, name='delete_location'),

    # Alias for old views
    path('assets/manage/', views.asset_list, name='assets_page'),
    path('access/', views.manage_inventory_access, name='manage_access'),
]
