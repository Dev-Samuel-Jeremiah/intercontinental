from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib import messages
from .models import AcademicSession, Term
from django.core.exceptions import ValidationError
from datetime import datetime
from academics.models import PublicHoliday




def is_super_admin(user):
    return user.is_superuser


def _is_school_admin(request):
    """
    Returns True if the current user is either the platform superadmin
    OR an ADMIN-role staff member belonging to this school (tenant).
    """
    user = request.user
    if user.is_superuser:
        return True
    staff = getattr(user, 'staff', None)
    return staff is not None and staff.role == 'ADMIN'


@login_required
def manage_sessions(request):
    """
    School-scoped session management.
    Only the school's own ADMIN staff (or platform superadmin) may access this.
    All queries are automatically scoped to request.tenant via TenantManager.
    """
    if not _is_school_admin(request):
        messages.error(request, "You do not have permission to manage academic sessions.")
        return redirect("dashboard:router")

    # TenantManager auto-scopes to the current tenant thread-local
    sessions = AcademicSession.objects.all()

    if request.method == "POST":
        name = request.POST.get("name")
        start_date = request.POST.get("start_date")
        end_date = request.POST.get("end_date")
        is_active = request.POST.get("is_active") == "on"

        if not name:
            messages.error(request, "Session name is required.")
            return redirect("academics:manage_sessions")

        # Check for duplicate name WITHIN this school only
        if AcademicSession.objects.filter(name=name).exists():
            messages.error(request, f"Academic session '{name}' already exists for this school.")
            return redirect("academics:manage_sessions")

        try:
            start_date_obj = datetime.strptime(start_date, "%Y-%m-%d").date() if start_date else None
            end_date_obj = datetime.strptime(end_date, "%Y-%m-%d").date() if end_date else None

            # Explicitly pass tenant so the record is always tied to this school
            AcademicSession.objects.create(
                tenant=request.tenant,
                name=name,
                start_date=start_date_obj,
                end_date=end_date_obj,
                is_active=is_active,
            )
            messages.success(request, f"Academic session '{name}' created successfully.")
        except ValidationError as e:
            messages.error(request, f"Error creating session: {e}")
        except ValueError:
            messages.error(request, "Invalid date format. Use YYYY-MM-DD.")

        return redirect("academics:manage_sessions")

    return render(request, "academics/manage_sessions.html", {
        "sessions": sessions,
    })


@login_required
def manage_terms(request, session_id):
    """
    School-scoped term management.
    The session lookup is implicitly scoped to this school via TenantManager,
    so a school cannot access another school's sessions via URL manipulation.
    """
    if not _is_school_admin(request):
        messages.error(request, "You do not have permission to manage terms.")
        return redirect("dashboard:router")

    session = get_object_or_404(AcademicSession, id=session_id)
    terms = session.terms.all()

    # Get existing term names to disable in form
    existing_term_names = list(terms.values_list('name', flat=True))

    if request.method == "POST":
        name = request.POST.get("name")
        start_date = request.POST.get("start_date")
        end_date = request.POST.get("end_date")
        resumption_date_next_term = request.POST.get("resumption_date_next_term")
        is_active = request.POST.get("is_active") == "on"

        if name and start_date and end_date:
            try:
                Term.objects.create(
                    tenant=request.tenant,
                    session=session,
                    name=name,
                    start_date=start_date,
                    end_date=end_date,
                    resumption_date_next_term=resumption_date_next_term if resumption_date_next_term else None,
                    is_active=is_active,
                    # number_of_school_days is auto-calculated in model.save()
                )
                messages.success(request, "Term created successfully.")
                return redirect("academics:manage_terms", session_id=session.id)
            except ValidationError as e:
                messages.error(request, f"Error creating term: {e}")
        else:
            messages.error(request, "Term name, start date, and end date are required.")

    return render(request, "academics/manage_terms.html", {
        "session": session,
        "terms": terms,
        "existing_term_names": existing_term_names,  # pass to template
    })



@login_required
def manage_public_holidays(request):
    """School-scoped public holidays — each school manages its own calendar."""
    if not _is_school_admin(request):
        messages.error(request, "You do not have permission to manage public holidays.")
        return redirect("dashboard:router")
    holidays = PublicHoliday.objects.all().order_by('date')

    if request.method == "POST":
        name = request.POST.get("name")
        date = request.POST.get("date")

        if not name or not date:
            messages.error(request, "Holiday name and date are required.")
        else:
            try:
                PublicHoliday.objects.create(
                    tenant=request.tenant,
                    name=name,
                    date=date,
                )
                messages.success(request, "Public holiday added successfully.")
                return redirect("academics:manage_public_holidays")
            except ValidationError as e:
                messages.error(request, e)
            except Exception:
                messages.error(request, "Holiday with this date already exists.")

    return render(request, "academics/manage_public_holidays.html", {
        "holidays": holidays
    })



# ---------------------------------------------------------------------------
# AJAX: Update school days for a term (manual override)
# ---------------------------------------------------------------------------

from django.views.decorators.csrf import csrf_exempt
from django.http import JsonResponse
from django.views.decorators.http import require_POST

@csrf_exempt
@login_required
def update_school_days(request, term_id):
    """
    AJAX endpoint. Accepts POST with 'days' field.
    If days == 0, switches back to auto-calculation.
    """
    if request.method != 'POST':
        return JsonResponse({'status': 'method_not_allowed'}, status=405)

    term = get_object_or_404(Term, id=term_id)

    try:
        days = int(request.POST.get('days', '').strip() or 0)
        if days < 0:
            return JsonResponse({'status': 'invalid', 'detail': 'Days cannot be negative'}, status=400)
    except ValueError:
        return JsonResponse({'status': 'invalid', 'detail': 'Days must be a number'}, status=400)

    term.manual_school_days = days
    # Recalculate effective days (model.save() handles this, but we can't call full save
    # without running clean() which may raise errors on active term logic — so update directly)
    effective_days = days if days > 0 else term.calculate_school_days()
    Term.objects.filter(id=term_id).update(
        manual_school_days=days,
        number_of_school_days=effective_days,
    )

    return JsonResponse({
        'status': 'success',
        'effective_days': effective_days,
        'manual': days > 0,
    })
