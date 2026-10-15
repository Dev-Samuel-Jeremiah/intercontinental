"""
classroom/access.py
───────────────────
Who may see and change a course: its teacher or an administrator teaches it;
a student is in it if enrolled or if it's for their class.
"""
from django.core.exceptions import PermissionDenied


def teaches(user, course):
    from users.views_profile import can_manage
    return bool(user.is_authenticated and (course.teacher_id == user.id or can_manage(user)))


def enrolled(user, course):
    if not user.is_authenticated:
        return False
    student = getattr(user, 'student', None)
    if student and course.class_assigned_id and student.class_assigned_id == course.class_assigned_id:
        return True
    return course.students.filter(pk=user.pk).exists()


def require_teacher(user, course):
    if not teaches(user, course):
        raise PermissionDenied


def require_enrolled(user, course):
    if not enrolled(user, course):
        raise PermissionDenied
