"""My profile: one page where anyone signed in manages their picture, details and password."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render

from . import account as profile


@login_required
def my_profile(request):
    user = request.user
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action == 'photo':
                profile.save_photo(user, request.FILES.get('photo'))
                messages.success(request, 'Your picture is updated.')
            elif action == 'remove_photo':
                profile.remove_photo(user)
                messages.success(request, 'Your picture is removed.')
            elif action == 'details':
                changed = profile.save_details(user, request.POST)
                messages.success(request, 'Your details are saved.' if changed else 'Nothing changed.')
            elif action == 'password':
                profile.change_password(request, request.POST.get('current_password', ''),
                                        request.POST.get('new_password', ''), request.POST.get('confirm_password', ''))
                messages.success(request, 'Your password is changed. You stay signed in here; '
                                          'other phones and computers will need the new password.')
        except profile.ProfileError as problem:
            messages.error(request, str(problem))
            return redirect(f"{request.path}#{'password' if action == 'password' else 'details'}")
        return redirect(f"{request.path}#{'password' if action == 'password' else 'top'}")

    kind = profile.kind_of(user)
    from security import services as svc
    cfg = svc.settings_for(getattr(request, 'tenant', None)) if getattr(request, 'tenant', None) else None
    return render(request, 'users/my_profile.html', {
        'kind': kind, 'facts': profile.facts(user),
        'role_label': {'admin': 'School administrator', 'student': 'Student', 'parent': 'Parent'}.get(
            kind, user.staff.get_role_display() if user.staff else 'Staff'),
        'min_length': max(getattr(cfg, 'min_length', 8), 8),
        'need_mix': getattr(cfg, 'require_mix', False), 'need_symbol': getattr(cfg, 'require_symbol', False),
        'password_note': {
            'student': 'This also becomes the password you sign in with, instead of the one on your slip.',
            'parent': "This becomes the password you sign in with, together with your child's admission number.",
        }.get(kind, ''),
    })
