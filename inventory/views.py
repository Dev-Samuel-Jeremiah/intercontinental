from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required, permission_required
from django.contrib import messages
from django.db.models import Sum, Count, Q
from django.utils import timezone
from django.http import JsonResponse
from django.views.decorators.http import require_POST

from .models import Asset, StockMovement, AssetAssignment, MaintenanceLog, Category, Location
from .services import inventory_dashboard_metrics, get_low_stock_assets
from .forms import (
    AssetForm, StockInForm, StockAdjustForm,
    IssueAssetForm, ReturnAssetForm, MaintenanceLogForm,
    CategoryForm, LocationForm
)


def inventory_permission(view_func):
    """
    Access control for inventory:
    - Superusers always have access.
    - If InventoryAllowedEmail table is EMPTY → all staff_users can access (legacy).
    - If InventoryAllowedEmail table has ANY active record → ONLY those emails
      (matched against request.user.email) have access, plus superusers.
    """
    from functools import wraps
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            from django.contrib.auth.views import redirect_to_login
            return redirect_to_login(request.get_full_path())

        # Superusers bypass everything
        if request.user.is_superuser:
            return view_func(request, *args, **kwargs)

        try:
            from inventory.models import InventoryAllowedEmail
            whitelist = InventoryAllowedEmail.objects.filter(is_active=True)
            if whitelist.exists():
                # Whitelist mode — check user's email
                user_email = (request.user.email or '').strip().lower()
                allowed_emails = [e.strip().lower() for e in
                                  whitelist.values_list('email', flat=True)]
                if user_email and user_email in allowed_emails:
                    return view_func(request, *args, **kwargs)
                # Not in whitelist
                return render(request, 'inventory/403.html', {
                    'reason': (
                        f"Your email ({request.user.email or 'not set'}) is not "
                        "authorised to access the Inventory module. "
                        "Contact the administrator."
                    )
                }, status=403)
        except Exception:
            pass  # fallback to staff check

        # No whitelist — any staff_user can access
        if getattr(request.user, 'is_staff_user', False) or request.user.is_staff:
            return view_func(request, *args, **kwargs)

        return render(request, 'inventory/403.html', {
            'reason': 'You do not have permission to access the Inventory module.'
        }, status=403)
    return wrapper


# ─────────────────────────────────────────────
# DASHBOARD
# ─────────────────────────────────────────────

@login_required
@inventory_permission
def dashboard(request):
    metrics = inventory_dashboard_metrics()
    low_stock_assets = get_low_stock_assets()
    recent_movements = StockMovement.objects.select_related('asset', 'performed_by').order_by('-date')[:10]
    recent_assignments = AssetAssignment.objects.filter(returned=False).select_related('asset', 'assigned_to_user', 'location').order_by('-issued_at')[:8]

    context = {
        'metrics': metrics,
        'low_stock_assets': low_stock_assets,
        'recent_movements': recent_movements,
        'recent_assignments': recent_assignments,
    }
    return render(request, 'inventory/dashboard.html', context)


# ─────────────────────────────────────────────
# ASSETS
# ─────────────────────────────────────────────

@login_required
@inventory_permission
def asset_list(request):
    search = request.GET.get('q', '')
    category = request.GET.get('category', '')
    asset_type = request.GET.get('type', '')
    status = request.GET.get('status', 'ACTIVE')

    assets = Asset.objects.select_related('category').order_by('name')

    if search:
        assets = assets.filter(Q(name__icontains=search) | Q(asset_code__icontains=search))
    if category:
        assets = assets.filter(category_id=category)
    if asset_type:
        assets = assets.filter(asset_type=asset_type)
    if status:
        assets = assets.filter(status=status)

    categories = Category.objects.all()
    context = {
        'assets': assets,
        'categories': categories,
        'search': search,
        'selected_category': category,
        'selected_type': asset_type,
        'selected_status': status,
    }
    return render(request, 'inventory/asset_list.html', context)


@login_required
@inventory_permission
def asset_detail(request, asset_id):
    asset = get_object_or_404(Asset, pk=asset_id)
    movements = asset.movements.select_related('performed_by').order_by('-date')
    assignments = AssetAssignment.objects.filter(asset=asset).select_related('assigned_to_user', 'location').order_by('-issued_at')
    maintenance_logs = MaintenanceLog.objects.filter(asset=asset).order_by('-date')

    context = {
        'asset': asset,
        'movements': movements,
        'assignments': assignments,
        'maintenance_logs': maintenance_logs,
    }
    return render(request, 'inventory/asset_detail.html', context)


@login_required
@inventory_permission
def add_asset(request):
    if request.method == 'POST':
        form = AssetForm(request.POST)
        if form.is_valid():
            asset = form.save()
            messages.success(request, f'Asset "{asset.name}" added successfully.')
            return redirect('inventory:asset_detail', asset_id=asset.id)
    else:
        form = AssetForm()
    return render(request, 'inventory/asset_form.html', {'form': form, 'title': 'Add New Asset'})


@login_required
@inventory_permission
def edit_asset(request, asset_id):
    asset = get_object_or_404(Asset, pk=asset_id)
    if request.method == 'POST':
        form = AssetForm(request.POST, instance=asset)
        if form.is_valid():
            form.save()
            messages.success(request, f'Asset "{asset.name}" updated successfully.')
            return redirect('inventory:asset_detail', asset_id=asset.id)
    else:
        form = AssetForm(instance=asset)
    return render(request, 'inventory/asset_form.html', {'form': form, 'title': f'Edit: {asset.name}', 'asset': asset})


# ─────────────────────────────────────────────
# STOCK MOVEMENTS
# ─────────────────────────────────────────────

@login_required
@inventory_permission
def stock_in(request):
    if request.method == 'POST':
        form = StockInForm(request.POST)
        if form.is_valid():
            asset = form.cleaned_data['asset']
            qty = form.cleaned_data['quantity']
            reason = form.cleaned_data['reason']
            StockMovement.objects.create(
                asset=asset,
                movement_type='IN',
                quantity=qty,
                performed_by=request.user,
                reason=reason,
            )
            messages.success(request, f'Stock added: {qty} {asset.unit}(s) for {asset.name}.')
            return redirect('inventory:asset_detail', asset_id=asset.id)
    else:
        form = StockInForm()
        asset_id = request.GET.get('asset')
        if asset_id:
            form.initial['asset'] = asset_id
    return render(request, 'inventory/stock_in.html', {'form': form})


@login_required
@inventory_permission
def stock_adjust(request):
    if request.method == 'POST':
        form = StockAdjustForm(request.POST)
        if form.is_valid():
            asset = form.cleaned_data['asset']
            adjustment = form.cleaned_data['adjustment']
            reason = form.cleaned_data['reason']

            # Prevent going negative
            if adjustment < 0 and abs(adjustment) > asset.current_stock():
                form.add_error('adjustment', f'Adjustment would result in negative stock. Current stock: {asset.current_stock()}')
            else:
                StockMovement.objects.create(
                    asset=asset,
                    movement_type='ADJUST',
                    quantity=adjustment,
                    performed_by=request.user,
                    reason=reason,
                )
                direction = 'increased' if adjustment > 0 else 'decreased'
                messages.success(request, f'Stock {direction} by {abs(adjustment)} for {asset.name}.')
                return redirect('inventory:asset_detail', asset_id=asset.id)
    else:
        form = StockAdjustForm()
        asset_id = request.GET.get('asset')
        if asset_id:
            form.initial['asset'] = asset_id
    return render(request, 'inventory/stock_adjust.html', {'form': form})


# ─────────────────────────────────────────────
# ISSUE & RETURN
# ─────────────────────────────────────────────

@login_required
@inventory_permission
def issue_asset_view(request):
    if request.method == 'POST':
        form = IssueAssetForm(request.POST)
        if form.is_valid():
            asset = form.cleaned_data['asset']
            quantity = form.cleaned_data['quantity']
            assigned_to_user = form.cleaned_data['assigned_to_user']
            location = form.cleaned_data['location']
            condition = form.cleaned_data['condition_at_issue']
            expected_return = form.cleaned_data['expected_return_date']

            if quantity > asset.current_stock():
                form.add_error('quantity', f'Insufficient stock. Available: {asset.current_stock()}')
            else:
                # Create stock out movement
                StockMovement.objects.create(
                    asset=asset,
                    movement_type='OUT',
                    quantity=quantity,
                    performed_by=request.user,
                    reason=f'Issued to {assigned_to_user or location}',
                )
                # Create assignment record
                AssetAssignment.objects.create(
                    asset=asset,
                    assigned_to_user=assigned_to_user,
                    location=location,
                    quantity=quantity,
                    condition_at_issue=condition,
                    expected_return_date=expected_return if asset.asset_type == 'FIXED' else None,
                )
                messages.success(request, f'{asset.name} issued successfully.')
                return redirect('inventory:asset_list')
    else:
        form = IssueAssetForm()
        asset_id = request.GET.get('asset')
        if asset_id:
            form.initial['asset'] = asset_id
    return render(request, 'inventory/issue_asset.html', {'form': form})


@login_required
@inventory_permission
def return_asset_view(request):
    if request.method == 'POST':
        form = ReturnAssetForm(request.POST)
        if form.is_valid():
            assignment = form.cleaned_data['assignment']
            asset = assignment.asset

            if asset.asset_type == 'CONSUMABLE':
                form.add_error('assignment', 'Consumable items cannot be returned.')
            elif assignment.returned:
                form.add_error('assignment', 'This asset has already been returned.')
            else:
                # Stock back in
                StockMovement.objects.create(
                    asset=asset,
                    movement_type='IN',
                    quantity=assignment.quantity,
                    performed_by=request.user,
                    reason='Returned from assignment',
                )
                assignment.returned = True
                assignment.returned_at = timezone.now()
                assignment.save(update_fields=['returned', 'returned_at'])
                messages.success(request, f'{asset.name} returned successfully.')
                return redirect('inventory:asset_list')
    else:
        form = ReturnAssetForm()
    return render(request, 'inventory/return_asset.html', {'form': form})


# ─────────────────────────────────────────────
# MAINTENANCE
# ─────────────────────────────────────────────

@login_required
@inventory_permission
def add_maintenance_log(request):
    if request.method == 'POST':
        form = MaintenanceLogForm(request.POST)
        if form.is_valid():
            log = form.save()
            messages.success(request, f'Maintenance log added for {log.asset.name}.')
            return redirect('inventory:asset_detail', asset_id=log.asset.id)
    else:
        form = MaintenanceLogForm()
        asset_id = request.GET.get('asset')
        if asset_id:
            form.initial['asset'] = asset_id
    return render(request, 'inventory/maintenance_form.html', {'form': form})


# ─────────────────────────────────────────────
# REPORTS
# ─────────────────────────────────────────────

@login_required
@inventory_permission
def stock_ledger_view(request):
    asset_filter = request.GET.get('asset', '')
    start_date = request.GET.get('start', '')
    end_date = request.GET.get('end', '')

    movements = StockMovement.objects.select_related('asset', 'asset__category', 'performed_by').order_by('-date')

    if asset_filter:
        movements = movements.filter(asset_id=asset_filter)
    if start_date:
        movements = movements.filter(date__date__gte=start_date)
    if end_date:
        movements = movements.filter(date__date__lte=end_date)

    assets = Asset.objects.all()
    context = {
        'movements': movements,
        'assets': assets,
        'asset_filter': asset_filter,
        'start_date': start_date,
        'end_date': end_date,
    }
    return render(request, 'inventory/stock_ledger.html', context)


@login_required
@inventory_permission
def asset_register_view(request):
    assets = Asset.objects.filter(asset_type='FIXED').select_related('category').order_by('name')
    return render(request, 'inventory/asset_register.html', {'assets': assets})


@login_required
@inventory_permission
def assignment_report_view(request):
    show_all = request.GET.get('all', '')
    assignments = AssetAssignment.objects.select_related('asset', 'assigned_to_user', 'location').order_by('-issued_at')
    if not show_all:
        assignments = assignments.filter(returned=False)
    context = {'assignments': assignments, 'show_all': show_all}
    return render(request, 'inventory/assignment_report.html', context)


@login_required
@inventory_permission
def maintenance_report_view(request):
    logs = MaintenanceLog.objects.select_related('asset').order_by('-date')
    total_cost = logs.aggregate(total=Sum('cost'))['total'] or 0
    context = {'logs': logs, 'total_cost': total_cost}
    return render(request, 'inventory/maintenance_report.html', context)


# ─────────────────────────────────────────────
# CATEGORIES & LOCATIONS
# ─────────────────────────────────────────────

@login_required
@inventory_permission
def categories_page(request):
    categories = Category.objects.annotate(asset_count=Count('asset')).order_by('name')
    if request.method == 'POST':
        form = CategoryForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, 'Category added.')
            return redirect('inventory:categories_page')
    else:
        form = CategoryForm()
    return render(request, 'inventory/categories_page.html', {'categories': categories, 'form': form})


@login_required
@inventory_permission
def delete_category(request, pk):
    category = get_object_or_404(Category, pk=pk)
    if category.asset_set.exists():
        messages.error(request, 'Cannot delete category that has assets.')
    else:
        category.delete()
        messages.success(request, 'Category deleted.')
    return redirect('inventory:categories_page')


@login_required
@inventory_permission
def locations_page(request):
    locations = Location.objects.all().order_by('name')
    if request.method == 'POST':
        form = LocationForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, 'Location added.')
            return redirect('inventory:locations_page')
    else:
        form = LocationForm()
    return render(request, 'inventory/locations_page.html', {'locations': locations, 'form': form})


@login_required
@inventory_permission
def delete_location(request, pk):
    location = get_object_or_404(Location, pk=pk)
    location.delete()
    messages.success(request, 'Location deleted.')
    return redirect('inventory:locations_page')


@login_required
@inventory_permission
def assets_page(request):
    return redirect('inventory:asset_list')


# ─────────────────────────────────────────────────────────────────────────────
# INVENTORY ACCESS MANAGEMENT  (email whitelist CRUD)
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def manage_inventory_access(request):
    """Only superusers can manage the inventory access whitelist."""
    if not request.user.is_superuser:
        return render(request, 'inventory/403.html', {
            'reason': 'Only administrators can manage inventory access permissions.'
        }, status=403)

    from inventory.models import InventoryAllowedEmail

    if request.method == 'POST':
        action = request.POST.get('action')

        if action == 'add':
            email     = request.POST.get('email', '').strip().lower()
            full_name = request.POST.get('full_name', '').strip()
            note      = request.POST.get('note', '').strip()
            if email:
                obj, created = InventoryAllowedEmail.objects.get_or_create(
                    email=email,
                    defaults={'full_name': full_name, 'note': note,
                              'added_by': request.user}
                )
                if not created:
                    obj.is_active = True
                    obj.full_name = full_name or obj.full_name
                    obj.note      = note or obj.note
                    obj.save(update_fields=['is_active', 'full_name', 'note'])
                messages.success(request, f"✓ {email} added to inventory access list.")
            else:
                messages.error(request, "Email address is required.")

        elif action == 'remove':
            entry_id = request.POST.get('entry_id')
            InventoryAllowedEmail.objects.filter(id=entry_id).update(is_active=False)
            messages.success(request, "Access revoked.")

        elif action == 'delete':
            entry_id = request.POST.get('entry_id')
            InventoryAllowedEmail.objects.filter(id=entry_id).delete()
            messages.success(request, "Entry deleted permanently.")

        return redirect('inventory:manage_access')

    allowed = InventoryAllowedEmail.objects.select_related('added_by').order_by('email')
    return render(request, 'inventory/manage_access.html', {
        'allowed':       allowed,
        'whitelist_on':  allowed.filter(is_active=True).exists(),
    })

