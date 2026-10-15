from django.db.models import Sum
from .models import Asset, AssetAssignment, StockMovement, MaintenanceLog


def inventory_dashboard_metrics():
    total_assets = Asset.objects.count()
    consumables = Asset.objects.filter(asset_type='CONSUMABLE', status='ACTIVE').count()
    fixed_assets = Asset.objects.filter(asset_type='FIXED', status='ACTIVE').count()

    low_stock_count = sum(
        1 for a in Asset.objects.filter(asset_type='CONSUMABLE', status='ACTIVE')
        if a.is_low_stock()
    )

    active_assignments = AssetAssignment.objects.filter(returned=False).count()

    return {
        'total_assets': total_assets,
        'consumables': consumables,
        'fixed_assets': fixed_assets,
        'low_stock_count': low_stock_count,
        'active_assignments': active_assignments,
    }


def get_low_stock_assets():
    return [
        a for a in Asset.objects.filter(asset_type='CONSUMABLE', status='ACTIVE')
        if a.is_low_stock()
    ]


def stock_ledger_report(asset=None, start_date=None, end_date=None):
    qs = StockMovement.objects.select_related('asset', 'performed_by')
    if asset:
        qs = qs.filter(asset=asset)
    if start_date:
        qs = qs.filter(date__date__gte=start_date)
    if end_date:
        qs = qs.filter(date__date__lte=end_date)
    return qs.order_by('-date')


def asset_register():
    return Asset.objects.filter(asset_type='FIXED').order_by('name')


def assignment_report(active_only=True):
    qs = AssetAssignment.objects.select_related('asset')
    if active_only:
        qs = qs.filter(returned=False)
    return qs.order_by('-issued_at')


def maintenance_cost_report():
    return MaintenanceLog.objects.select_related('asset').order_by('-date')
