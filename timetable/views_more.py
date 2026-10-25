"""
timetable/views_more.py
───────────────────────
Timetable part 2 pages: room booking, the free period finder, version
history, parent–teacher meeting slots, calendar sync, the corridor bell
display and printable personal timetables.
"""
import json
from datetime import datetime, timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from lms_project.tasks import run_in_background
from users.models import Class, Staff
from users.views_profile import can_manage, can_view

from . import ical, scheduling
from .lessons import DAY_CODES, published_timetable
from .models import (CalendarFeed, GeneratedTimetable, MeetingBooking, MeetingEvening, RoomBooking,
                     TeachingRoom, TimetableConfiguration, TimetableVersion)

DAYS = dict(TimetableConfiguration.DAYS)


def _deny(request, text='Only staff can use this page.'):
    messages.error(request, text)
    return redirect('dashboard:router')


def _date(raw, default=None):
    try:
        return datetime.strptime(raw or '', '%Y-%m-%d').date()
    except ValueError:
        return default


def _time(raw):
    try:
        return datetime.strptime(raw or '', '%H:%M').time()
    except ValueError:
        return None


def _pk(raw):
    raw = str(raw or '')
    return int(raw) if raw.isdigit() else 0


def _staff(request):
    return request.user.staff_profiles.filter(tenant=request.tenant).first()


# ─────────────────────────────────────────────────────────────────────────────
# Room booking (F144)
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def room_bookings(request):
    if not can_view(request.user):
        return _deny(request)
    day = _date(request.GET.get('date') or request.POST.get('date'), timezone.localdate())
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'cancel':
            b = get_object_or_404(RoomBooking, pk=_pk(request.POST.get('id')), tenant=request.tenant)
            if b.booked_by_id != request.user.id and not can_manage(request.user):
                messages.error(request, 'Only the person who booked it, or an administrator, can cancel this.')
            else:
                b.cancelled = True
                b.save(update_fields=['cancelled'])
                messages.success(request, f'{b.room.name} on {b.date:%a %d %b} is free again.')
            return redirect(f"{reverse('timetable:room_bookings')}?date={day:%Y-%m-%d}")
        room = get_object_or_404(TeachingRoom, pk=_pk(request.POST.get('room')), tenant=request.tenant)
        try:
            created, skipped = scheduling.book_room(
                room, day, _time(request.POST.get('start')), _time(request.POST.get('end')),
                request.POST.get('purpose'), request.user, request.POST.get('repeat') or 1)
            if created:
                messages.success(request, f'{room.name} booked on {len(created)} day{"s" if len(created) != 1 else ""}.')
            for d, reasons in skipped:
                messages.error(request, f'{d:%a %d %b} not booked: {" ".join(reasons)}')
        except (scheduling.SchedulingError, ValueError) as problem:
            messages.error(request, str(problem))
        return redirect(f"{reverse('timetable:room_bookings')}?date={day:%Y-%m-%d}")
    mine = RoomBooking.objects.filter(tenant=request.tenant, booked_by=request.user, cancelled=False,
                                      date__gte=timezone.localdate()).select_related('room')[:20]
    return render(request, 'timetable/room_bookings.html', {
        'day': day, 'prev': day - timedelta(days=1), 'next': day + timedelta(days=1),
        'plan': scheduling.day_plan(request.tenant, day), 'mine': mine, 'is_admin': can_manage(request.user),
        'rooms': TeachingRoom.objects.filter(tenant=request.tenant, active=True),
        'max_repeat': scheduling.MAX_REPEAT_WEEKS})


# ─────────────────────────────────────────────────────────────────────────────
# Free period finder (F158)
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def free_finder(request):
    if not can_view(request.user):
        return _deny(request)
    tt = published_timetable(request.tenant)
    teachers = Staff.objects.filter(tenant=request.tenant, role__in=('TEACHER', 'ADMIN')).order_by('full_name')
    rooms = TeachingRoom.objects.filter(tenant=request.tenant, active=True)
    chosen = list(teachers.filter(pk__in=[i for i in request.GET.getlist('t') if i.isdigit()]))
    room = rooms.filter(pk=request.GET.get('room')).first() if (request.GET.get('room') or '').isdigit() else None
    on_date = _date(request.GET.get('date'))
    result = scheduling.free_grid(tt, chosen, room, on_date) if tt and (chosen or room) else None
    return render(request, 'timetable/free_finder.html', {
        'tt': tt, 'teachers': teachers, 'rooms': rooms, 'chosen_ids': {t.pk for t in chosen}, 'chosen': chosen,
        'room': room, 'on_date': on_date, 'result': result})


# ─────────────────────────────────────────────────────────────────────────────
# Version history (F147)
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def versions(request, config_pk):
    if not can_manage(request.user):
        return _deny(request, 'Only administrators can see timetable versions.')
    config = get_object_or_404(TimetableConfiguration, pk=config_pk, tenant=request.tenant)
    if request.method == 'POST':
        if request.POST.get('action') == 'restore':
            v = get_object_or_404(TimetableVersion, pk=_pk(request.POST.get('id')), config=config)
            tt, skipped = scheduling.restore(v, request.user)
            note = f' {skipped} lesson{"s" if skipped != 1 else ""} could not come back (deleted class, subject, teacher or period).' if skipped else ''
            messages.success(request, f'Version {v.number} restored and published.{note}')
            return redirect('timetable:preview', pk=tt.pk)
        live = config.generated_timetables.filter(is_published=True).first()
        if not live:
            messages.error(request, 'Publish a timetable first — there is nothing to save yet.')
        else:
            v = scheduling.save_version(live, request.user, request.POST.get('label'))
            messages.success(request, f'Saved as version {v.number}.')
        return redirect('timetable:versions', config_pk=config.pk)
    items = list(config.versions.select_related('saved_by'))
    a = next((v for v in items if str(v.pk) == request.GET.get('a')), None)
    b = next((v for v in items if str(v.pk) == request.GET.get('b')), None)
    live = config.generated_timetables.filter(is_published=True).first()
    diff = None
    if a:
        new_entries = b.entries if b else (scheduling.snapshot(live) if live else [])
        diff = scheduling.compare(a.entries, new_entries)
    return render(request, 'timetable/admin/versions.html', {
        'config': config, 'versions': items, 'a': a, 'b': b, 'live': live, 'diff': diff})


# ─────────────────────────────────────────────────────────────────────────────
# Parent–teacher meetings (F152)
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def meetings(request):
    if not can_manage(request.user):
        return _deny(request, 'Only administrators can set up parent meetings.')
    if request.method == 'POST':
        d, start, end = _date(request.POST.get('date')), _time(request.POST.get('start')), _time(request.POST.get('end'))
        title = (request.POST.get('title') or '').strip()[:120]
        minutes = request.POST.get('minutes') or '10'
        if not (title and d and start and end) or start >= end:
            messages.error(request, 'Give the meeting a title, a date and a start time before the end time.')
        elif not minutes.isdigit() or not 3 <= int(minutes) <= 60:
            messages.error(request, 'Slots must be between 3 and 60 minutes.')
        else:
            ev = MeetingEvening.objects.create(
                tenant=request.tenant, title=title, date=d, start_time=start, end_time=end, slot_minutes=int(minutes),
                location=(request.POST.get('location') or '').strip()[:120],
                notes=(request.POST.get('notes') or '').strip()[:255], created_by=request.user)
            ev.classes.set(Class.objects.filter(tenant=request.tenant, pk__in=[_pk(i) for i in request.POST.getlist('classes')]))
            ev.teachers.set(Staff.objects.filter(tenant=request.tenant, pk__in=[_pk(i) for i in request.POST.getlist('teachers')]))
            messages.success(request, f'{ev.title} created — parents can book now.')
            return redirect('timetable:meeting_detail', pk=ev.pk)
        return redirect('timetable:meetings')
    evenings = list(MeetingEvening.objects.filter(tenant=request.tenant))
    for ev in evenings:
        ev.booked = ev.bookings.count()
    return render(request, 'timetable/admin/meetings.html', {
        'evenings': evenings, 'today': timezone.localdate(),
        'classes': Class.objects.filter(tenant=request.tenant).order_by('name'),
        'teachers': Staff.objects.filter(tenant=request.tenant, role__in=('TEACHER', 'ADMIN')).order_by('full_name')})


@login_required
def meeting_detail(request, pk):
    if not can_manage(request.user):
        return _deny(request, 'Only administrators can manage parent meetings.')
    ev = get_object_or_404(MeetingEvening, pk=pk, tenant=request.tenant)
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'toggle':
            ev.is_open = not ev.is_open
            ev.save(update_fields=['is_open'])
            messages.success(request, 'Booking is open.' if ev.is_open else 'Booking is closed.')
        elif action == 'cancel':
            b = get_object_or_404(MeetingBooking, pk=_pk(request.POST.get('id')), evening=ev)
            text = f'{b.start_time:%-I:%M %p} with {b.teacher.full_name} cancelled; the parent is told.'
            scheduling.cancel_booking(b)
            messages.success(request, text)
        elif action == 'delete':
            ev.delete()
            messages.success(request, 'Meeting session deleted.')
            return redirect('timetable:meetings')
        return redirect('timetable:meeting_detail', pk=ev.pk)
    teachers = scheduling.evening_teachers(ev)
    times = scheduling.slot_times(ev)
    by = {(b.teacher_id, b.start_time): b for b in ev.bookings.select_related('student__class_assigned')}
    rows = [{'teacher': t, 'cells': [by.get((t.pk, s)) for s in times],
             'count': sum(1 for s in times if (t.pk, s) in by)} for t in teachers]
    return render(request, 'timetable/admin/meeting_detail.html', {
        'ev': ev, 'times': times, 'rows': rows, 'total': len(by), 'school': request.tenant,
        'classes': list(ev.classes.all())})


@login_required
def my_meetings(request):
    staff = _staff(request)
    if not staff:
        return _deny(request, 'This page is for teachers.')
    today = timezone.localdate()
    sessions = []
    for ev in MeetingEvening.objects.filter(tenant=request.tenant, date__gte=today - timedelta(days=7)):
        if staff in scheduling.evening_teachers(ev):
            sessions.append({'ev': ev, 'slots': scheduling.teacher_schedule(ev, staff)})
    return render(request, 'timetable/my_meetings.html', {'sessions': sessions, 'staff': staff,
                                                           'school': request.tenant})


@login_required
def parent_meetings(request):
    from users.views_achievements import _parent_children
    children = [c for c in _parent_children(request) if c.class_assigned_id]
    if not getattr(request.user, 'is_parent', False):
        messages.error(request, 'This page is for parents.')
        return redirect('dashboard:router')
    if request.method == 'POST':
        ev = get_object_or_404(MeetingEvening, pk=_pk(request.POST.get('evening')), tenant=request.tenant)
        child = next((c for c in children if str(c.id) == request.POST.get('child')), None)
        if request.POST.get('action') == 'cancel':
            b = MeetingBooking.objects.filter(pk=_pk(request.POST.get('id')), evening=ev,
                                              student__in=children).first()
            if b and ev.is_open:
                scheduling.cancel_booking(b)
                messages.success(request, 'Slot cancelled.')
            else:
                messages.error(request, 'That slot can no longer be cancelled here — please contact the school.')
        elif not child:
            messages.error(request, 'Choose one of your children.')
        else:
            teacher = Staff.objects.filter(tenant=request.tenant, pk=_pk(request.POST.get('teacher'))).first()
            start = _time(request.POST.get('time'))
            try:
                if not teacher or not start:
                    raise scheduling.SchedulingError('Pick a teacher and a time.')
                b = scheduling.book_meeting(ev, teacher, child, start, request.user, request.POST.get('note'))
                run_in_background(scheduling.notify_booking, b.pk, tenant=request.tenant)
                messages.success(request, f'Booked: {teacher.full_name} at {start:%-I:%M %p}.')
            except scheduling.SchedulingError as problem:
                messages.error(request, str(problem))
        return redirect('timetable:parent_meetings')
    today = timezone.localdate()
    sessions = []
    for ev in MeetingEvening.objects.filter(tenant=request.tenant, date__gte=today).order_by('date', 'start_time'):
        kids = []
        family_busy = set(MeetingBooking.objects.filter(evening=ev, student__in=children)
                          .values_list('start_time', flat=True))
        for c in children:
            if not scheduling.evening_covers(ev, c.class_assigned):
                continue
            mine = {b.teacher_id: b for b in MeetingBooking.objects.filter(evening=ev, student=c)}
            teachers = [{'staff': s, 'label': label, 'booking': mine.get(s.pk),
                         'free': [] if s.pk in mine else [t for t in scheduling.free_times(ev, s)
                                                          if t not in family_busy]}
                        for s, label in scheduling.teachers_for(ev, c)]
            kids.append({'child': c, 'teachers': teachers})
        if kids:
            sessions.append({'ev': ev, 'kids': kids})
    return render(request, 'parents/meetings.html', {'sessions': sessions, 'children': children})


# ─────────────────────────────────────────────────────────────────────────────
# Calendar sync (F146)
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def calendar_sync(request):
    if request.method == 'POST':
        ical.reset_feed(request.user, request.tenant)
        messages.success(request, 'New link made. The old link has stopped working — add the new one to your calendar.')
        return redirect('timetable:calendar_sync')
    feed = ical.feed_for(request.user, request.tenant)
    url = request.build_absolute_uri(reverse('timetable:calendar_feed', args=[feed.token]))
    webcal = 'webcal://' + url.split('://', 1)[-1]
    template = 'timetable/calendar_sync.html' if can_view(request.user) else 'timetable/calendar_sync_portal.html'
    return render(request, template,
                  {'feed': feed, 'url': url, 'webcal': webcal})


def calendar_feed(request, token):
    """The .ics file itself. No login: calendar apps can't sign in, so the long random token is the key."""
    feed = CalendarFeed.objects.filter(token=token, tenant=request.tenant).select_related('user').first() \
        if getattr(request, 'tenant', None) else None
    if not feed or not feed.user.is_active:
        raise Http404
    CalendarFeed.objects.filter(pk=feed.pk).update(last_used_at=timezone.now())
    resp = HttpResponse(ical.build(feed.user, request.tenant), content_type='text/calendar; charset=utf-8')
    resp['Content-Disposition'] = f'inline; filename="{request.tenant.subdomain}-timetable.ics"'
    resp['Cache-Control'] = 'private, max-age=900'
    return resp


@login_required
def calendar_download(request):
    resp = HttpResponse(ical.build(request.user, request.tenant), content_type='text/calendar; charset=utf-8')
    resp['Content-Disposition'] = f'attachment; filename="{request.tenant.subdomain}-timetable.ics"'
    return resp


# ─────────────────────────────────────────────────────────────────────────────
# Bell schedule display (F153)
# ─────────────────────────────────────────────────────────────────────────────

def todays_periods(tenant, day):
    """Today's periods and breaks as [{name, start, end, kind}], honouring a day override (early close, clubs)."""
    tt = published_timetable(tenant)
    if not tt or day.weekday() >= 6 or DAY_CODES[day.weekday()] not in tt.config.active_days:
        return tt, []
    code = DAY_CODES[day.weekday()]
    override = tt.config.day_overrides.filter(day=code, is_active=True).first()
    out = []
    for s in tt.config.time_slots.all().order_by('order', 'start_time'):
        if override and s.start_time >= override.lessons_end_time:
            continue
        out.append({'name': s.name, 'start': s.start_time.strftime('%H:%M'), 'end': s.end_time.strftime('%H:%M'),
                    'kind': 'break' if s.is_break else 'lesson'})
    if override:
        for ss in override.special_slots.all():
            out.append({'name': ss.name, 'start': ss.start_time.strftime('%H:%M'),
                        'end': ss.end_time.strftime('%H:%M'), 'kind': 'special'})
    out.sort(key=lambda p: p['start'])
    return tt, out


@login_required
def bell_display(request):
    if not can_view(request.user):
        return _deny(request)
    today = timezone.localdate()
    tt, periods = todays_periods(request.tenant, today)
    return render(request, 'timetable/bell_display.html', {
        'school': request.tenant, 'periods': periods, 'periods_json': json.dumps(periods).replace('<', '\\u003c'), 'today': today,
        'now': timezone.localtime().strftime('%H:%M:%S'), 'tt': tt})


# ─────────────────────────────────────────────────────────────────────────────
# Printable personal timetables (F145)
# ─────────────────────────────────────────────────────────────────────────────

def teacher_sheet(timetable, staff):
    config = timetable.config
    days = [(c, DAYS[c]) for c, _ in TimetableConfiguration.DAYS if c in config.active_days]
    cells = {(e.day, e.time_slot_id): e for e in timetable.slots.filter(teacher=staff)
             .select_related('subject', 'school_class', 'room')}
    rows = [{'slot': p, 'cells': [{'day': d, 'entry': cells.get((d, p.id))} for d, _ in days]}
            for p in config.time_slots.all().order_by('order', 'start_time')]
    return {'title': f'{staff.full_name} — teaching timetable', 'kind': 'teacher', 'days': days, 'rows': rows,
            'count': len(cells)}


@login_required
def print_mine(request):
    staff = _staff(request)
    tt = published_timetable(request.tenant)
    if not staff:
        return _deny(request, 'This page is for teachers.')
    if not tt:
        messages.error(request, 'There is no published timetable yet.')
        return redirect('timetable:teacher_timetable')
    colour = getattr(request.tenant, 'highlight_color', '') or '#0a1f44'
    return render(request, 'timetable/admin/print_all.html', {
        'tt': tt, 'sheets': [teacher_sheet(tt, staff)], 'school': request.tenant, 'colour': colour,
        'personal': True})


@login_required
def print_teachers(request, pk):
    if not can_manage(request.user):
        return _deny(request, 'Only administrators can print every teacher\'s timetable.')
    tt = get_object_or_404(GeneratedTimetable.objects.select_related('config'), pk=pk, tenant=request.tenant)
    staff = Staff.objects.filter(tenant=request.tenant, pk__in=tt.slots.values('teacher')).order_by('full_name')
    if (request.GET.get('teacher') or '').isdigit():
        staff = staff.filter(pk=request.GET['teacher'])
    colour = getattr(request.tenant, 'highlight_color', '') or '#0a1f44'
    return render(request, 'timetable/admin/print_all.html', {
        'tt': tt, 'sheets': [teacher_sheet(tt, s) for s in staff], 'school': request.tenant, 'colour': colour,
        'teachers_mode': True})


@require_POST
@login_required
def save_version_now(request, pk):
    """Button on the preview page: freeze the timetable as it is now."""
    if not can_manage(request.user):
        return _deny(request, 'Only administrators can save timetable versions.')
    tt = get_object_or_404(GeneratedTimetable, pk=pk, tenant=request.tenant)
    v = scheduling.save_version(tt, request.user, request.POST.get('label') or 'Saved after edits')
    messages.success(request, f'Saved as version {v.number}.')
    return redirect('timetable:versions', config_pk=tt.config_id)
