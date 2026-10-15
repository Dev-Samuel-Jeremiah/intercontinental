"""
curriculum/views_events.py
──────────────────────────
One event: its details, replies (coming / maybe / not coming) and, for
administrators, the guest list, a CSV download and "send reminder now".
"""
import csv

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from users.views_profile import can_manage

from . import events as svc
from .models import CalendarEvent, EventRSVP


@login_required
def event(request, pk):
    ev = get_object_or_404(CalendarEvent, pk=pk, tenant=request.tenant)
    if not svc.can_see(request.user, ev):
        raise Http404
    manager = can_manage(request.user)
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'remind' and manager:
            sent = svc.send_reminder(ev)
            messages.success(request, f'Reminder sent to {sent} email address{"es" if sent != 1 else ""}.')
        elif action == 'csv' and manager:
            return _csv(ev)
        else:
            try:
                svc.reply(ev, request.user, request.POST.get('answer'), request.POST.get('guests'),
                          request.POST.get('note'))
                messages.success(request, 'Thank you — your reply is saved.')
            except svc.EventError as problem:
                messages.error(request, str(problem))
        return redirect('curriculum:event', pk=ev.pk)
    mine = EventRSVP.objects.filter(event=ev, user=request.user).first()
    ctx = {'ev': ev, 'mine': mine, 'answers': EventRSVP.ANSWERS, 'manager': manager,
           'summary': svc.summary(ev) if (manager or ev.capacity is not None) else None,
           'today': timezone.localdate(), 'max_guests': svc.MAX_GUESTS}
    portal = getattr(request.user, 'is_parent', False) and not svc.is_staff_user(request.user)
    return render(request, 'parents/event.html' if portal else 'curriculum/event.html', ctx)


def _csv(ev):
    resp = HttpResponse(content_type='text/csv')
    resp['Content-Disposition'] = f'attachment; filename="event-{ev.pk}-replies.csv"'
    w = csv.writer(resp)
    w.writerow(['Name', 'Username', 'Email', 'Reply', 'Guests', 'Note', 'Replied'])
    for r in ev.rsvps.select_related('user'):
        w.writerow([r.user.get_full_name(), r.user.username, r.user.email, r.get_answer_display(), r.guests, r.note,
                    timezone.localtime(r.updated_at).strftime('%Y-%m-%d %H:%M')])
    return resp
