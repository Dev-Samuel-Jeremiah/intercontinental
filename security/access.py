"""
security/access.py
──────────────────
One rule for "may this person use this part of the system?", so the staff
permission switches set on the Roles page actually decide access.

Someone may use an area if they are an administrator (superuser), if their
staff role has always included it (e.g. Account Officers and finance), or if
an administrator has switched the permission on for them. Everyone else is
refused — and the refusal is written to the audit trail.
"""
# Staff roles that have an area without needing the switch
BASELINE = {
    'can_access_finance': ('ADMIN', 'ACCOUNT', 'PROPRIETOR'),
    'can_approve_payments': ('ADMIN', 'ACCOUNT'),
    'can_access_hostel': ('ADMIN', 'ACCOUNT'),
    'can_manage_boarders': ('ADMIN',),
    'can_access_inventory': ('ADMIN',),
}
AREAS = {
    'can_access_finance': 'finance', 'can_approve_payments': 'payment approval',
    'can_access_hostel': 'boarding', 'can_manage_boarders': 'boarding', 'can_access_inventory': 'the store',
}


def staff_of(user):
    try:
        return user.staff
    except Exception:
        return None


def allowed(user, *perms):
    """True if the user has any of the given permissions (by role or by switch)."""
    if not getattr(user, 'is_authenticated', False):
        return False
    if user.is_superuser:
        return True
    staff = staff_of(user)
    if staff is None:
        return False
    return any(getattr(staff, p, False) or staff.role in BASELINE.get(p, ('ADMIN',)) for p in perms)


def refused(request, perm):
    """Record a refused attempt (never raises)."""
    from . import services as svc
    user = getattr(request, 'user', None)
    svc.log_event('DENIED', f'{getattr(user, "username", "Someone")} was refused {AREAS.get(perm, perm)}: '
                            f'{request.path}'[:500], request=request, category='ACCESS')
