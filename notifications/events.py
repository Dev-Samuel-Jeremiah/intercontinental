"""
notifications/events.py
───────────────────────
School events that happen in bulk (so no per-row save signal fires) and are
announced from where they happen. Each child's family and the child get
their own notice; administrators get one summary instead of one per child.
Rows are written in one batch so publishing a whole year group stays quick.
"""
import logging

from django.urls import reverse
from django.utils import timezone

from . import services as svc
from .models import Notification as N

log = logging.getLogger(__name__)


def results_published(tenant, student_ids, term_id, actor=None):
    """Called by results publishing for the students whose results just went live."""
    try:
        _results_published(tenant, student_ids, term_id, actor)
    except Exception:           # never let a notice stop results going out
        log.exception('Could not record result notifications')


def _results_published(tenant, student_ids, term_id, actor):
    from academics.models import Term
    from users.models import Student
    term = Term.objects.filter(pk=term_id).select_related('session').first()
    if not student_ids or term is None:
        return
    label = f'{term.name} {term.session.name}'
    students = list(Student.objects.filter(tenant=tenant, pk__in=student_ids).select_related('user', 'class_assigned'))
    parent_url, student_url = reverse('users:parent_dashboard'), reverse('dashboard:router')
    rows = []
    for st in students:
        rows.append(svc.build(tenant, 'result', f"{st.full_name}'s {term.name} result is ready", N.FAMILY,
                              student=st, body=f'{label} · open the parent portal to view it', url=parent_url,
                              level='success', actor=actor, admin_feed=False,
                              dedupe=f'result:{term.pk}:{st.pk}:family'))
        if st.user_id:
            rows.append(svc.build(tenant, 'result', f'Your {term.name} result is ready', N.USER,
                                  recipient=st.user, student=st, body=label, url=student_url, level='success',
                                  actor=actor, admin_feed=False, dedupe=f'result:{term.pk}:{st.pk}:student'))
    if not svc.notify_many(tenant, rows):
        return                  # all of these families were already told
    classes = sorted({s.class_assigned.name for s in students if s.class_assigned_id})
    svc.to_admins(tenant, 'result', f'Results published for {len(students)} student{"s" if len(students) != 1 else ""}',
                  body=f'{label} · {", ".join(classes) or "no class"}', url=reverse('results:publish_results_page'),
                  level='success', actor=actor)


def absences_marked(tenant, school_class, day, actor=None):
    """Called after a class register is saved: tell each absent child's family once.
    Only for today's register, the same rule as the absence texts."""
    try:
        _absences_marked(tenant, school_class, day, actor)
    except Exception:
        log.exception('Could not record absence notifications')


def _absences_marked(tenant, school_class, day, actor):
    from users.models import Attendance
    if day != timezone.localdate():
        return
    marks = Attendance.objects.filter(tenant=tenant, school_class=school_class, date=day, status='A',
                                      subject__isnull=True, absence_reason='').select_related('student')
    url = reverse('users:parent_absences')
    rows = [svc.build(tenant, 'absence', f'{m.student.full_name} was marked absent', N.FAMILY, student=m.student,
                      body=f'{school_class.name} register · {day:%A %d %B}. If this is wrong or you know why, '
                           f'tell the school from the parent portal.',
                      url=url, level='warning', actor=actor, admin_feed=False,
                      dedupe=f'absent:{m.student_id}:{day.isoformat()}')
            for m in marks]
    new = svc.notify_many(tenant, rows)
    if new:
        svc.to_admins(tenant, 'absence', f'{new} absence{"s" if new != 1 else ""} in {school_class.name}',
                      body=f'Register for {day:%A %d %B}',
                      url=reverse('users:view_attendance', args=[school_class.pk]), level='warning', actor=actor)
