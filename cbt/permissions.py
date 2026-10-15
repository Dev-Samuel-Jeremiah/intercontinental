def is_super_admin(user):
    return user.is_authenticated and user.is_superuser


def is_admin(user):
    return (
        user.is_authenticated and
        hasattr(user, 'staff') and
        user.staff.role == 'ADMIN'
    )


def is_teacher(user):
    return (
        user.is_authenticated and
        hasattr(user, 'staff') and
        user.staff.role == 'TEACHER'
    )


def can_manage_exams(user):
    """
    Super Admin: FULL ACCESS
    Admin: FULL ACCESS
    Teacher: ONLY if can_manage_exams flag is explicitly granted
    Student: NO ACCESS
    """
    if is_super_admin(user):
        return True
    if is_admin(user):
        return True
    # Teachers (and any other role) must have the explicit permission flag
    staff = getattr(user, 'staff', None)
    if staff and getattr(staff, 'can_manage_exams', False):
        return True
    return False
