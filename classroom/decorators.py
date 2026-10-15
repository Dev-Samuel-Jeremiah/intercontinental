# classroom/decorators.py
from django.core.exceptions import PermissionDenied
from functools import wraps

def staff_required(view_func):
    """
    Allow access to:
    - Users marked as staff (is_staff_user=True)
    - Superusers (is_superuser=True)
    """
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not getattr(request.user, 'is_staff_user', False) and not request.user.is_superuser:
            raise PermissionDenied
        return view_func(request, *args, **kwargs)
    return wrapper


def student_required(view_func):
    """
    Allow access to students only:
    - Users marked as students (is_student=True)
    - Superusers are NOT allowed (unless you want to allow)
    """
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not getattr(request.user, 'is_student', False):
            raise PermissionDenied
        return view_func(request, *args, **kwargs)
    return wrapper
