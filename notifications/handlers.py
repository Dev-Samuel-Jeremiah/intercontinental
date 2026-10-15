"""
notifications/handlers.py
─────────────────────────
Turns school events into notifications. Most events are picked up from model
signals here; two that happen in bulk are announced from where they happen:
results publishing (results/release.py → events.results_published) and the
daily register (users/views.py → events.absences_marked).

Every receiver is wrapped so that a notification problem can never break the
save that triggered it.
"""
import functools
import logging

from django.db.models.signals import m2m_changed, post_save, pre_save
from django.dispatch import receiver
from django.urls import reverse

from . import services as svc
from .models import Notification as N

log = logging.getLogger(__name__)


def safely(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception:
            log.exception('Notification handler %s failed', fn.__name__)
    return wrapper


def _old(sender, instance, *fields):
    """Remember field values from before this save, for spotting changes."""
    if instance.pk:
        before = sender._base_manager.filter(pk=instance.pk).values(*fields).first()
        instance._notify_before = before or {}
    else:
        instance._notify_before = {}


def _url(name, *args):
    try:
        return reverse(name, args=args)
    except Exception:
        return ''


def _when(dt, fmt='%a %d %b, %H:%M'):
    from django.utils import timezone
    return timezone.localtime(dt).strftime(fmt) if dt else ''


# ─────────────────────────────────────────────────────────────────────────────
# Announcements → students and/or parents (a class, or the whole school)
# ─────────────────────────────────────────────────────────────────────────────

@receiver(pre_save, sender='announcement.Announcement')
@safely
def _ann_before(sender, instance, **kwargs):
    _old(sender, instance, 'published')


@receiver(post_save, sender='announcement.Announcement')
@safely
def announcement_published(sender, instance, created, **kwargs):
    was = getattr(instance, '_notify_before', {}).get('published', False)
    if not instance.published or (was and not created):
        return
    a = instance
    body = ' '.join((a.message or '').split())[:200]
    url = _url('announcements:detail', a.pk)
    who = {'students': ['students'], 'parents': ['parents']}.get(a.audience, ['students', 'parents'])
    for i, group in enumerate(who):
        if a.school_class_id:
            audience = N.CLASS_STUDENTS if group == 'students' else N.CLASS_PARENTS
        else:
            audience = N.ALL_STUDENTS if group == 'students' else N.ALL_PARENTS
        svc.notify(a.tenant, 'announcement', a.title, audience, school_class=a.school_class, body=body,
                   url=url, actor=a.created_by, admin_feed=(i == 0), dedupe=f'ann:{a.pk}:{group}')


# ─────────────────────────────────────────────────────────────────────────────
# CBT exams → students of each class, when published
# ─────────────────────────────────────────────────────────────────────────────

@receiver(pre_save, sender='cbt.Exam')
@safely
def _exam_before(sender, instance, **kwargs):
    _old(sender, instance, 'published')


def _announce_exam(exam, classes):
    for i, cls in enumerate(classes):
        svc.notify(exam.tenant, 'exam', f'New exam: {exam.title}', N.CLASS_STUDENTS, school_class=cls,
                   body=f'{exam.subject.name} · {cls.name} · starts {_when(exam.start_time)}',
                   url=_url('cbt:available_exams'), actor=getattr(exam.created_by, 'user', None),
                   admin_feed=(i == 0), dedupe=f'exam:{exam.pk}:{cls.pk}')


@receiver(post_save, sender='cbt.Exam')
@safely
def exam_published(sender, instance, created, **kwargs):
    was = getattr(instance, '_notify_before', {}).get('published', False)
    if instance.published and (created or not was):
        _announce_exam(instance, list(instance.classes.all()))


@receiver(m2m_changed, sender='cbt.Exam_classes')
@safely
def exam_classes_added(sender, instance, action, pk_set, **kwargs):
    # A published exam that gains a class tells that class too (dedupe stops repeats)
    if action == 'post_add' and getattr(instance, 'published', False) and pk_set:
        from users.models import Class
        _announce_exam(instance, list(Class.objects.filter(pk__in=pk_set)))


# ─────────────────────────────────────────────────────────────────────────────
# Homework → the class's students and parents
# ─────────────────────────────────────────────────────────────────────────────

@receiver(post_save, sender='curriculum.Homework')
@safely
def homework_set(sender, instance, created, **kwargs):
    if not created:
        return
    hw = instance
    body = f'{hw.subject.name} · {hw.school_class.name} · due {hw.due_on:%a %d %b}'
    actor = getattr(hw.set_by, 'user', None)
    for i, audience in enumerate((N.CLASS_STUDENTS, N.CLASS_PARENTS)):
        svc.notify(hw.tenant, 'homework', f'Homework: {hw.title}', audience, school_class=hw.school_class,
                   body=body, url=_url('curriculum:family'), actor=actor, admin_feed=(i == 0),
                   dedupe=f'hw:{hw.pk}:{audience}')


# ─────────────────────────────────────────────────────────────────────────────
# Online classroom: new assignment → enrolled students; submission → teacher;
# grade → student and family
# ─────────────────────────────────────────────────────────────────────────────

@receiver(post_save, sender='classroom.Assignment')
@safely
def assignment_set(sender, instance, created, **kwargs):
    if not created:
        return
    course = instance.section.course
    body = f'{course.name} · due {_when(instance.due_date)}'
    url = _url('classroom:student_course_content', course.pk)
    for user in course.students.all():
        svc.to_user(user, 'assignment', f'New assignment: {instance.title}', tenant=course.tenant, body=body,
                    url=url, actor=course.teacher, admin_feed=False, dedupe=f'asg:{instance.pk}:{user.pk}')
    svc.to_admins(course.tenant, 'assignment', f'New assignment: {instance.title}',
                  body=f'{course.name} · {course.students.count()} student(s) · due {_when(instance.due_date)}',
                  url=_url('classroom:teacher_course_detail', course.pk), actor=course.teacher,
                  dedupe=f'asg:{instance.pk}:admins')


@receiver(pre_save, sender='classroom.Submission')
@safely
def _submission_before(sender, instance, **kwargs):
    _old(sender, instance, 'grade')


@receiver(post_save, sender='classroom.Submission')
@safely
def submission_saved(sender, instance, created, **kwargs):
    sub = instance
    assignment = sub.assignment
    course = assignment.section.course
    who = sub.student.get_full_name() or sub.student.username
    if created:
        svc.to_user(course.teacher, 'submission', f'New submission: {assignment.title}', tenant=course.tenant,
                    body=f'{who} · {course.name}', url=_url('classroom:grade_assignment', assignment.pk),
                    actor=sub.student, admin_feed=False, dedupe=f'sub:{sub.pk}')
        return
    before = getattr(sub, '_notify_before', {})
    if sub.grade is not None and before.get('grade') is None:
        body = f'{course.name} · score {sub.grade:g}'
        url = _url('classroom:student_course_content', course.pk)
        svc.to_user(sub.student, 'graded', f'Assignment graded: {assignment.title}', tenant=course.tenant,
                    body=body, url=url, level='success', actor=course.teacher, admin_feed=False,
                    dedupe=f'graded:{sub.pk}:student')
        student = sub.student.student_profiles.filter(tenant=course.tenant).first()
        if student:
            svc.to_family(student, 'graded', f"{student.full_name}'s assignment was graded",
                          body=f'{assignment.title} · {body}', url=url, actor=course.teacher, admin_feed=False,
                          dedupe=f'graded:{sub.pk}:family')


# ─────────────────────────────────────────────────────────────────────────────
# Admissions → administrators
# ─────────────────────────────────────────────────────────────────────────────

@receiver(post_save, sender='admissions.Application')
@safely
def application_received(sender, instance, created, **kwargs):
    if not created:
        return
    app = instance
    cls = getattr(app, 'applying_for', None)
    svc.to_admins(app.tenant, 'admission', f'New admission application: {app.full_name}',
                  body=f'{cls.name if cls else "Class not chosen"} · parent {app.parent_name} · {app.parent_phone}',
                  url=_url('admissions:detail', app.pk), dedupe=f'app:{app.pk}')


# ─────────────────────────────────────────────────────────────────────────────
# Staff leave: request → administrators; decision → the staff member
# ─────────────────────────────────────────────────────────────────────────────

@receiver(pre_save, sender='safety.StaffLeave')
@safely
def _leave_before(sender, instance, **kwargs):
    _old(sender, instance, 'status')


@receiver(post_save, sender='safety.StaffLeave')
@safely
def leave_saved(sender, instance, created, **kwargs):
    lv = instance
    span = f'{lv.get_leave_type_display()} · {lv.start_date:%d %b} – {lv.end_date:%d %b}'
    url = _url('safety:staff_leave')
    if created and lv.status == 'PENDING':
        svc.to_admins(lv.tenant, 'leave', f'Leave request: {lv.staff.full_name}', body=span, url=url,
                      actor=lv.staff.user, level='warning', dedupe=f'leave:{lv.pk}:req')
        return
    before = getattr(lv, '_notify_before', {}).get('status')
    if lv.status in ('APPROVED', 'REJECTED') and before != lv.status:
        approved = lv.status == 'APPROVED'
        svc.to_user(lv.staff.user, 'leave', f"{lv.staff.full_name}'s leave was {'approved' if approved else 'declined'}",
                    tenant=lv.tenant, body=span + (f' · {lv.review_note}' if lv.review_note else ''), url=url,
                    level='success' if approved else 'warning', actor=lv.reviewed_by, admin_feed=False,
                    dedupe=f'leave:{lv.pk}:{lv.status}')


# ─────────────────────────────────────────────────────────────────────────────
# Parent ↔ teacher chat (private: never in the admin feed)
# ─────────────────────────────────────────────────────────────────────────────

@receiver(post_save, sender='communications.ChatMessage')
@safely
def chat_message(sender, instance, created, **kwargs):
    if not created or instance.hidden:
        return
    conv = instance.conversation
    snippet = ' '.join(instance.body.split())[:160]
    url = _url('communications:chat_thread', conv.pk)
    if instance.sender_id == conv.parent_id:
        svc.to_user(conv.teacher.user, 'message', f'Message from the parent of {conv.student.full_name}',
                    tenant=conv.tenant, body=f'{conv.subject} — {snippet}', url=url, actor=instance.sender,
                    admin_feed=False)
    else:
        svc.to_family(conv.student, 'message', f'New message from {conv.teacher.full_name}',
                      body=f'{conv.subject} — {snippet}', url=url, actor=instance.sender, admin_feed=False)


# ─────────────────────────────────────────────────────────────────────────────
# Feedback & complaints: new ticket / parent reply → administrators (and the
# assigned staff member); school reply → the family
# ─────────────────────────────────────────────────────────────────────────────

@receiver(post_save, sender='family.Ticket')
@safely
def ticket_opened(sender, instance, created, **kwargs):
    if not created:
        return
    t = instance
    about = f' about {t.student.full_name}' if t.student_id else ''
    body = f'{t.get_kind_display()}{about} · {t.subject}'
    url = _url('family:ticket_detail', t.pk)
    svc.to_admins(t.tenant, 'ticket', f'New {t.get_kind_display().lower()} from a parent', body=body, url=url,
                  actor=t.parent, level='warning' if t.kind == 'COMPLAINT' else 'info', dedupe=f'ticket:{t.pk}')
    if t.assigned_to_id:
        svc.to_user(t.assigned_to.user, 'ticket', f'{t.get_kind_display()} assigned to you', tenant=t.tenant,
                    body=body, url=url, actor=t.parent, admin_feed=False, dedupe=f'ticket:{t.pk}:assignee')


@receiver(post_save, sender='family.TicketMessage')
@safely
def ticket_reply(sender, instance, created, **kwargs):
    if not created or instance.internal:
        return
    m, t = instance, instance.ticket
    snippet = ' '.join(m.body.split())[:160]
    if m.author_id and m.author_id == t.parent_id:
        svc.to_admins(t.tenant, 'ticket', f'Parent replied: {t.subject}', body=snippet,
                      url=_url('family:ticket_detail', t.pk), actor=m.author)
        if t.assigned_to_id:
            svc.to_user(t.assigned_to.user, 'ticket', f'Parent replied: {t.subject}', tenant=t.tenant,
                        body=snippet, url=_url('family:ticket_detail', t.pk), actor=m.author, admin_feed=False)
    elif t.student_id:
        svc.to_family(t.student, 'ticket', f'The school replied: {t.subject}', body=snippet,
                      url=_url('family:feedback_detail', t.pk), actor=m.author, admin_feed=False)


# ─────────────────────────────────────────────────────────────────────────────
# Fee payments → the family (and administrators)
# ─────────────────────────────────────────────────────────────────────────────

@receiver(pre_save, sender='finance.FeePayment')
@safely
def _fee_before(sender, instance, **kwargs):
    _old(sender, instance, 'amount_paid')


@receiver(post_save, sender='finance.FeePayment')
@safely
def fee_paid(sender, instance, created, **kwargs):
    from decimal import Decimal
    fp = instance
    before = getattr(fp, '_notify_before', {}).get('amount_paid') or Decimal('0')
    paid_now = (fp.amount_paid or Decimal('0')) - before
    if paid_now <= 0:
        return
    left = f' · ₦{fp.balance_due:,.0f} still due' if fp.balance_due and fp.balance_due > 0 else ' · fully paid'
    svc.to_family(fp.student, 'payment', f'Payment received for {fp.student.full_name}',
                  body=f'₦{paid_now:,.0f} towards {fp.fee_structure}{left}', url=_url('finance:parent_wallet'),
                  level='success')


# ─────────────────────────────────────────────────────────────────────────────
# School gate → the family (routine per-child notices stay out of the admin feed)
# ─────────────────────────────────────────────────────────────────────────────

@receiver(post_save, sender='idcards.GateScanLog')
@safely
def gate_scan(sender, instance, created, **kwargs):
    if not created:
        return
    s = instance.student
    arrived = instance.direction == 'IN'
    svc.to_family(s, 'gate', f"{s.full_name} {'arrived at' if arrived else 'left'} school",
                  body=f"Scanned at the gate at {_when(instance.scanned_at, '%H:%M on %a %d %b')}",
                  icon='door-open-fill' if arrived else 'door-closed-fill', level='success' if arrived else 'info',
                  admin_feed=False)
