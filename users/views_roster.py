"""
users/views_roster.py
─────────────────────
Class arms, sports houses, bulk edits, duplicate records, printable
registers and passport photos.
"""
from datetime import date

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from academics.utils import get_active_term

from . import roster
from .models import (ENROLLED_STATUSES, STATUS_OPTIONS, TERM_OPTIONS, Class, House, HousePoint,
                     Staff, Student)
from .views_profile import can_manage, can_view


def _deny(request, what):
    messages.error(request, f'Only a school administrator can {what}.')
    return redirect('dashboard:router')


# ─────────────────────────────────────────────────────────────────────────────
# Class arms
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def class_arms(request):
    if not can_manage(request.user):
        return _deny(request, 'set up class arms')
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action == 'arms':
                created, existing = roster.create_arms(request.tenant, request.POST.get('level'),
                                                       request.POST.get('arms'))
                bits = []
                if created:
                    bits.append('created ' + ', '.join(c.name for c in created))
                if existing:
                    bits.append('already there: ' + ', '.join(c.name for c in existing))
                messages.success(request, ('; '.join(bits) or 'Nothing to do') + '.')
            elif action == 'fill':
                n = roster.fill_levels(request.tenant)
                messages.success(request, f'Level worked out for {n} class(es) from their names.'
                                 if n else 'Every class already has a level.')
            elif action == 'update':
                cls = get_object_or_404(Class, pk=request.POST.get('class_id'))
                cls.level = (request.POST.get('level') or '').strip()[:40]
                cls.arm = (request.POST.get('arm') or '').strip()[:10]
                cls.form_teacher = Staff.objects.filter(pk=request.POST.get('form_teacher') or 0).first()
                cls.save(update_fields=['level', 'arm', 'form_teacher'])
                messages.success(request, f'{cls.name} saved.')
        except roster.RosterError as problem:
            messages.error(request, str(problem))
        return redirect('users:class_arms')
    return render(request, 'students/class_arms.html', {
        'levels': roster.classes_by_level(request.tenant),
        'staff': Staff.objects.order_by('full_name'),
        'unlevelled': Class.objects.filter(level='').count(),
    })


# ─────────────────────────────────────────────────────────────────────────────
# Houses
# ─────────────────────────────────────────────────────────────────────────────

def _period(request):
    choice = request.GET.get('period') or 'term'
    term = get_active_term()
    if choice == 'term' and term and term.start_date:
        return choice, term.start_date, term.end_date
    if choice == 'month':
        today = timezone.localdate()
        return choice, today.replace(day=1), today
    return 'all', None, None


@login_required
def houses(request):
    if not can_view(request.user):
        return _deny(request, 'see houses')
    manage = can_manage(request.user)
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action == 'award':
                house = get_object_or_404(House, pk=request.POST.get('house_id'))
                student = Student.objects.filter(pk=request.POST.get('student_id') or 0).first()
                hp = roster.award(house, request.POST.get('points'), request.POST.get('reason'),
                                  request.user, student)
                messages.success(request, f'{hp.points:+d} to {house.name}.')
            elif not manage:
                return _deny(request, 'change houses')
            elif action == 'add':
                name = (request.POST.get('name') or '').strip()[:60]
                if not name:
                    raise roster.RosterError('Give the house a name.')
                if House.objects.filter(name__iexact=name).exists():
                    raise roster.RosterError(f'There is already a house called {name}.')
                House.objects.create(tenant=request.tenant, name=name,
                                     colour=_colour(request.POST.get('colour')),
                                     motto=(request.POST.get('motto') or '').strip()[:160])
                messages.success(request, f'{name} house created.')
            elif action == 'update':
                house = get_object_or_404(House, pk=request.POST.get('house_id'))
                house.colour = _colour(request.POST.get('colour'), house.colour)
                house.motto = (request.POST.get('motto') or '').strip()[:160]
                house.house_master = Staff.objects.filter(pk=request.POST.get('house_master') or 0).first()
                captain = Student.objects.filter(pk=request.POST.get('captain') or 0).first()
                if captain and captain.house_id != house.id:
                    raise roster.RosterError(f'{captain.full_name} is not in {house.name}.')
                house.captain = captain
                house.save()
                messages.success(request, f'{house.name} saved.')
            elif action == 'share':
                n = roster.share_into_houses(request.tenant, request.user)
                messages.success(request, f'{n} student(s) put into houses, keeping them even.'
                                 if n else 'Every student already has a house.')
            elif action == 'undo_point':
                hp = get_object_or_404(HousePoint, pk=request.POST.get('point_id'))
                hp.delete()
                messages.success(request, 'Points entry removed.')
        except roster.RosterError as problem:
            messages.error(request, str(problem))
        return redirect(request.get_full_path())

    period, start, end = _period(request)
    all_houses = House.objects.filter(is_active=True).select_related('house_master', 'captain')
    return render(request, 'students/houses.html', {
        'standings': roster.standings(request.tenant, start, end),
        'houses': all_houses,
        'recent': HousePoint.objects.select_related('house', 'student', 'awarded_by')[:30],
        'period': period, 'can_manage': manage,
        'staff': Staff.objects.order_by('full_name') if manage else [],
        'members': {h.id: list(h.members.filter(status__in=ENROLLED_STATUSES)
                               .order_by('full_name').only('id', 'full_name'))
                    for h in all_houses} if manage else {},
        'unhoused': Student.objects.filter(status__in=ENROLLED_STATUSES, house__isnull=True).count(),
    })


def _colour(raw, fallback='#0a1f44'):
    raw = (raw or '').strip()
    import re
    return raw if re.fullmatch(r'#[0-9a-fA-F]{6}', raw) else fallback


# ─────────────────────────────────────────────────────────────────────────────
# Bulk edit
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def bulk_edit(request):
    if request.method != 'POST':
        return redirect('users:all_students')
    if not can_manage(request.user):
        return _deny(request, 'change many students at once')
    ids = [i for i in request.POST.getlist('student_ids') if i.isdigit()]
    try:
        changed, skipped = roster.bulk_apply(request.tenant, ids, request.POST.get('bulk_action'),
                                             request.POST.get('bulk_value'), request.user,
                                             request.POST.get('reason') or '')
        messages.success(request, f'{changed} student(s) updated.')
        for line in skipped[:5]:
            messages.warning(request, line)
        if len(skipped) > 5:
            messages.warning(request, f'…and {len(skipped) - 5} more not changed.')
    except roster.RosterError as problem:
        messages.error(request, str(problem))
    back = request.POST.get('next') or reverse('users:all_students')
    return redirect(back if back.startswith('/') and not back.startswith('//') else
                    reverse('users:all_students'))


# ─────────────────────────────────────────────────────────────────────────────
# Duplicates
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def duplicates(request):
    if not can_manage(request.user):
        return _deny(request, 'review duplicate records')
    if request.method == 'POST':
        a = get_object_or_404(Student, pk=request.POST.get('first'))
        b = get_object_or_404(Student, pk=request.POST.get('second'))
        roster.dismiss_duplicate(request.tenant, a, b, request.user)
        messages.success(request, f'Marked {a.full_name} and {b.full_name} as different students.')
        return redirect(request.get_full_path())
    include_left = request.GET.get('all') == '1'
    return render(request, 'students/duplicates.html', {
        'pairs': roster.find_duplicates(request.tenant, include_left), 'include_left': include_left,
    })


# ─────────────────────────────────────────────────────────────────────────────
# Printable registers
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def class_register(request):
    if not can_view(request.user):
        return _deny(request, 'print registers')
    classes = Class.objects.order_by('level', 'arm', 'name')
    cls = Class.objects.filter(pk=request.GET.get('class') or 0).select_related('form_teacher').first()
    try:
        columns = max(0, min(15, int(request.GET.get('columns', 5))))
    except ValueError:
        columns = 5
    options = {'photos': request.GET.get('photos', '1') == '1',
               'phones': request.GET.get('phones', '1') == '1',
               'columns': columns, 'title': (request.GET.get('title') or 'Class register')[:60]}
    students = []
    if cls:
        students = (Student.objects.filter(class_assigned=cls, status__in=ENROLLED_STATUSES)
                    .select_related('house').order_by('full_name'))
    return render(request, 'students/class_register.html', {
        'classes': classes, 'cls': cls, 'students': students, 'opts': options,
        'column_range': range(columns), 'today': timezone.localdate(), 'school': request.tenant,
        'boys': sum(1 for s in students if s.gender == 'Male'),
        'girls': sum(1 for s in students if s.gender == 'Female'),
    })


# ─────────────────────────────────────────────────────────────────────────────
# Photos
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def student_photo(request, student_id):
    """Save a photo cropped in the browser (or a plain upload, centre-cropped)."""
    student = get_object_or_404(Student, pk=student_id)
    if request.method != 'POST' or not can_manage(request.user):
        return JsonResponse({'ok': False, 'error': 'Only a school administrator can change photos.'},
                            status=403)
    upload = request.FILES.get('photo')
    if not upload:
        return JsonResponse({'ok': False, 'error': 'Choose a photo.'}, status=400)
    crop = None
    if all(request.POST.get(k) for k in ('x', 'y', 'w', 'h')):
        crop = [request.POST.get(k) for k in ('x', 'y', 'w', 'h')]
    try:
        warnings = roster.save_passport(student, upload, crop, request.user)
    except (roster.RosterError, ValueError) as problem:
        return JsonResponse({'ok': False, 'error': str(problem)}, status=400)
    return JsonResponse({'ok': True, 'url': student.passport.url, 'warnings': warnings})


MAX_BULK_PHOTOS = 200


@login_required
def bulk_photos(request):
    if not can_manage(request.user):
        return _deny(request, 'upload photos')
    report = None
    if request.method == 'POST':
        files = request.FILES.getlist('photos')
        if not files:
            messages.error(request, 'Choose the photos to upload.')
        elif len(files) > MAX_BULK_PHOTOS:
            messages.error(request, f'Upload at most {MAX_BULK_PHOTOS} photos at a time.')
        else:
            replace = request.POST.get('replace') == '1'
            pairs, unmatched = roster.match_photos(request.tenant, files)
            report = {'saved': [], 'kept': [], 'failed': [], 'unmatched': unmatched}
            for student, f in pairs:
                if student.passport and not replace:
                    report['kept'].append(student)
                    continue
                try:
                    warnings = roster.save_passport(student, f, user=request.user)
                    report['saved'].append((student, warnings))
                except roster.RosterError as problem:
                    report['failed'].append((f.name, str(problem)))
    stats = Student.objects.filter(status__in=ENROLLED_STATUSES).aggregate(
        total=Count('id'), missing=Count('id', filter=Q(passport='') | Q(passport__isnull=True)))
    return render(request, 'students/bulk_photos.html', {'report': report, 'stats': stats,
                                                         'limit': MAX_BULK_PHOTOS})
