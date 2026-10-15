"""
tenants/settings_views.py
──────────────────────────
Self-service "Settings" page for the school admin — lets them upload their
own logo/motto (shown across the portal and on result sheets via
`tenant.logo` / `tenant.motto`, already wired into templates/results/term_results.html)
and customize result-sheet specifics: grading scale and head-teacher signature.

School administrators do not use Django's own /admin/ site, so this is
their self-service page.
"""
from django.contrib import messages
from django.db import IntegrityError
from django.shortcuts import render, redirect

from tenants.decorators import school_admin_required as _require_admin
from results.models import Grading, HeadTeacherSignature, Skill, ResultSheetSettings
from users.models import Subject


@_require_admin
def school_settings(request):
    tenant = request.tenant

    if request.method == 'POST':
        action = request.POST.get('action')

        # ── Branding: school name, logo, motto, address, phone ──────────
        if action == 'save_branding':
            # NOTE: this updates Tenant.name only — the display name used
            # everywhere (dashboards, result sheets, receipts, emails,
            # __str__). It intentionally never touches tenant.subdomain,
            # which is the portal's URL slug and is not editable here.
            school_name = request.POST.get('school_name', '').strip()
            if not school_name:
                messages.error(request, "School name cannot be empty.")
                return redirect('school_settings:home')
            if len(school_name) > 255:
                messages.error(request, "School name is too long (max 255 characters).")
                return redirect('school_settings:home')

            tenant.name    = school_name
            tenant.motto   = request.POST.get('motto', '').strip()
            tenant.address = request.POST.get('address', '').strip()
            tenant.phone   = request.POST.get('phone', '').strip()
            if request.FILES.get('logo'):
                tenant.logo = request.FILES['logo']
            tenant.save(update_fields=['name', 'motto', 'address', 'phone', 'logo', 'updated_at'])
            messages.success(request, "Branding updated.")
            return redirect('school_settings:home')

        # ── Result customization: head teacher signature ───────────────
        elif action == 'save_signature':
            if request.FILES.get('signature'):
                sig = HeadTeacherSignature.objects.first()
                if sig:
                    sig.signature = request.FILES['signature']
                    sig.save(update_fields=['signature'])
                else:
                    HeadTeacherSignature.objects.create(
                        tenant=tenant, signature=request.FILES['signature'])
                messages.success(request, "Signature updated.")
            else:
                messages.error(request, "Please choose an image to upload.")
            return redirect('school_settings:home')

        # ── Result customization: grading scale ─────────────────────────
        elif action == 'add_grade':
            grade       = request.POST.get('grade', '').strip().upper()
            min_score   = request.POST.get('min_score', '').strip()
            description = request.POST.get('description', '').strip()
            if not grade or not min_score:
                messages.error(request, "Grade and minimum score are required.")
            else:
                try:
                    Grading.objects.create(
                        tenant=tenant, grade=grade,
                        min_score=min_score, description=description,
                    )
                    messages.success(request, f"Grade '{grade}' added.")
                except (IntegrityError, ValueError):
                    messages.error(
                        request,
                        f"Couldn't add '{grade}' at {min_score}+ — check it isn't "
                        "a duplicate and the score is a valid number."
                    )
            return redirect('school_settings:home')

        elif action == 'delete_grade':
            Grading.objects.filter(pk=request.POST.get('grade_id')).delete()
            messages.success(request, "Grade removed.")
            return redirect('school_settings:home')

        # ── Subjects ─────────────────────────────────────────────────
        elif action == 'add_subject':
            name        = request.POST.get('name', '').strip()
            code        = request.POST.get('code', '').strip()
            description = request.POST.get('description', '').strip()
            if not name:
                messages.error(request, "Subject name is required.")
            else:
                try:
                    Subject.objects.create(
                        tenant=tenant, name=name,
                        code=code or None, description=description or None,
                    )
                    messages.success(request, f"Subject '{name}' added.")
                except IntegrityError:
                    messages.error(request, f"'{name}' is already a subject at your school.")
            return redirect('school_settings:home')

        elif action == 'delete_subject':
            Subject.objects.filter(pk=request.POST.get('subject_id')).delete()
            messages.success(request, "Subject removed.")
            return redirect('school_settings:home')

        # ── Skills & Behaviour ───────────────────────────────────────
        elif action == 'add_skill':
            name     = request.POST.get('skill_name', '').strip()
            category = request.POST.get('category', '').strip()
            if not name or category not in ('skill', 'behaviour'):
                messages.error(request, "Name and category are required.")
            else:
                try:
                    Skill.objects.create(tenant=tenant, name=name, category=category)
                    messages.success(request, f"'{name}' added.")
                except IntegrityError:
                    messages.error(request, f"'{name}' already exists.")
            return redirect('school_settings:home')

        elif action == 'delete_skill':
            Skill.objects.filter(pk=request.POST.get('skill_id')).delete()
            messages.success(request, "Removed.")
            return redirect('school_settings:home')

        # ── Result Sheet Layout ─────────────────────────────────────────
        elif action == 'save_result_layout':
            rs, _ = ResultSheetSettings.objects.get_or_create(tenant=tenant)
            theme = request.POST.get('theme', '').strip()
            if theme in dict(ResultSheetSettings.THEME_CHOICES):
                rs.theme = theme
            rs.show_ca_breakdown      = 'show_ca_breakdown' in request.POST
            rs.show_class_average     = 'show_class_average' in request.POST
            rs.show_skills_section    = 'show_skills_section' in request.POST
            rs.show_behaviour_section = 'show_behaviour_section' in request.POST
            rs.footer_note            = request.POST.get('footer_note', '').strip()
            rs.save()
            messages.success(request, "Result sheet layout updated.")
            return redirect('school_settings:home')

    grading_scale = Grading.objects.all().order_by('-min_score')
    signature     = HeadTeacherSignature.objects.first()
    subjects      = Subject.objects.all().order_by('name')
    skills        = Skill.objects.filter(category='skill').order_by('name')
    behaviours    = Skill.objects.filter(category='behaviour').order_by('name')
    result_settings, _ = ResultSheetSettings.objects.get_or_create(tenant=tenant)

    return render(request, 'tenants/school/settings.html', {
        'tenant':          tenant,
        'grading_scale':   grading_scale,
        'signature':       signature,
        'subjects':        subjects,
        'skills':          skills,
        'behaviours':      behaviours,
        'result_settings': result_settings,
        'theme_choices':   ResultSheetSettings.THEME_CHOICES,
    })