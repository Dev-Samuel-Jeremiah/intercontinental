"""
classroom/services.py
─────────────────────
Late submissions, course announcements, live classes with attendance, and
copying a course into a new term.
"""
import math
import secrets
from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.text import slugify

from users.models import ENROLLED_STATUSES, Student

from .models import (Assignment, AssignmentQuestion, Course, CourseAnnouncement, LiveAttendance, LiveClass, Material,
                     Lesson, Question, Quiz, Section)

JOIN_EARLY_MINUTES = 15


class ClassroomError(ValueError):
    pass


def students_of(course):
    """Student users in the course: enrolled directly, or in the course's class."""
    q = Q(user__in=course.students.all())
    if course.class_assigned_id:
        q |= Q(class_assigned_id=course.class_assigned_id)
    return (Student.objects.filter(tenant_id=course.tenant_id, status__in=ENROLLED_STATUSES).filter(q)
            .select_related('user').order_by('full_name').distinct())


# ─────────────────────────────────────────────────────────────────────────────
# Late submissions
# ─────────────────────────────────────────────────────────────────────────────

def late_status(assignment, when=None):
    """
    {'open', 'late_days', 'penalty', 'message'} for a submission made now.
    Within the grace period counts as on time; each started day after that
    takes the per-day penalty, up to the last day allowed.
    """
    when = when or timezone.now()
    deadline = assignment.due_date + timedelta(minutes=assignment.grace_minutes or 0)
    if when <= deadline:
        return {'open': True, 'late_days': 0, 'penalty': 0, 'message': ''}
    days = math.ceil((when - deadline).total_seconds() / 86400)
    if not assignment.allow_late:
        return {'open': False, 'late_days': days, 'penalty': 100,
                'message': 'The deadline has passed and late work is not accepted.'}
    if days > assignment.max_late_days:
        return {'open': False, 'late_days': days, 'penalty': 100,
                'message': f'Late submissions closed {assignment.max_late_days} day(s) after the deadline.'}
    penalty = min(100, days * assignment.late_penalty_per_day)
    return {'open': True, 'late_days': days, 'penalty': penalty,
            'message': f'This is {days} day{"s" if days != 1 else ""} late — {penalty}% will come off the mark.'}


# ─────────────────────────────────────────────────────────────────────────────
# Announcements
# ─────────────────────────────────────────────────────────────────────────────

def announce(course, author, title, body, pinned=False, email_parents=False):
    title, body = (title or '').strip()[:160], (body or '').strip()
    if not title or not body:
        raise ClassroomError('Write a title and the announcement.')
    a = CourseAnnouncement.objects.create(tenant_id=course.tenant_id, course=course, author=author, title=title,
                                          body=body, pinned=bool(pinned))
    if email_parents:
        from lms_project.tasks import run_in_background
        run_in_background(_email_parents, a.id, tenant=course.tenant)
    return a


def _email_parents(announcement_id):
    from communications.services import _is_opted_out, send_email
    from django.utils.html import escape, linebreaks
    from users.records import contacts
    a = CourseAnnouncement.objects.select_related('course', 'course__tenant').get(pk=announcement_id)
    for st in students_of(a.course):
        if _is_opted_out(st, 'EMAIL'):
            continue
        for _name, email in contacts(st, 'EMAIL'):
            send_email(email, f'{a.course.name}: {a.title}',
                       f'<p><b>{escape(a.course.name)}</b> — {escape(a.course.tenant.name)}</p>'
                       f'{linebreaks(escape(a.body))}', st)


# ─────────────────────────────────────────────────────────────────────────────
# Live classes
# ─────────────────────────────────────────────────────────────────────────────

def schedule_live(course, user, title, starts_at, minutes=40, platform=LiveClass.JITSI, url=''):
    title = (title or '').strip()[:160]
    if not title:
        raise ClassroomError('Give the class a title.')
    if not starts_at:
        raise ClassroomError('Choose when it starts.')
    minutes = int(minutes or 40)
    if not 10 <= minutes <= 240:
        raise ClassroomError('A live class lasts 10 to 240 minutes.')
    if platform == LiveClass.LINK:
        url = (url or '').strip()
        if not url.startswith('https://'):
            raise ClassroomError('Paste the full meeting link (starting https://) from Zoom or Google Meet.')
    room = ''
    if platform == LiveClass.JITSI:
        # Hard to guess, so only people with the join link from the portal find the room.
        room = f'{slugify(course.tenant.subdomain)}-{slugify(course.name)[:30]}-{secrets.token_hex(5)}'
    return LiveClass.objects.create(tenant_id=course.tenant_id, course=course, title=title, starts_at=starts_at,
                                    minutes=minutes, platform=platform, url=url if platform == LiveClass.LINK else '',
                                    room=room, created_by=user)


def join_window(live, now=None):
    now = now or timezone.now()
    opens = live.starts_at - timedelta(minutes=JOIN_EARLY_MINUTES)
    if now < opens:
        return False, f'You can join from {timezone.localtime(opens):%-I:%M %p on %d %b}.'
    if now > live.ends_at:
        return False, 'This live class has ended.'
    return True, ''


def join(live, user):
    """Record the student's attendance and return the meeting link."""
    ok, why = join_window(live)
    if not ok:
        raise ClassroomError(why)
    att, created = LiveAttendance.objects.get_or_create(tenant_id=live.tenant_id, live_class=live, student=user)
    if not created:
        att.joins += 1
        att.save(update_fields=['joins', 'last_joined'])
    return live.join_url


def attendance(live):
    joined = {a.student_id: a for a in live.attendance.all()}
    rows = [{'student': s, 'att': joined.get(s.user_id)} for s in students_of(live.course)]
    return rows, sum(1 for r in rows if r['att'])


# ─────────────────────────────────────────────────────────────────────────────
# Copying a course
# ─────────────────────────────────────────────────────────────────────────────

@transaction.atomic
def clone(course, teacher, name, school_class=None, shift_days=0):
    """
    Copy sections, lessons, materials, assignments (with questions and due
    dates moved by shift_days) and quizzes (with questions). Videos and files
    are shared, not duplicated. Students, submissions and progress aren't copied.
    """
    name = (name or '').strip()[:255]
    if not name:
        raise ClassroomError('Name the new course.')
    shift = timedelta(days=int(shift_days or 0))
    new = Course.objects.create(tenant_id=course.tenant_id, name=name, description=course.description,
                                teacher=teacher, class_assigned=school_class)
    for sec in course.sections.all().order_by('order'):
        s2 = Section.objects.create(tenant_id=course.tenant_id, course=new, title=sec.title, order=sec.order)
        for l in sec.lessons.all():
            Lesson.objects.create(tenant_id=course.tenant_id, section=s2, title=l.title, video=l.video.name,
                                  duration_minutes=l.duration_minutes, order=l.order)
        for m in sec.materials.all():
            Material.objects.create(tenant_id=course.tenant_id, section=s2, title=m.title, file=m.file.name,
                                    description=m.description)
        for a in sec.assignments.all():
            a2 = Assignment.objects.create(
                tenant_id=course.tenant_id, section=s2, title=a.title, description=a.description,
                due_date=a.due_date + shift, allow_late=a.allow_late, grace_minutes=a.grace_minutes,
                late_penalty_per_day=a.late_penalty_per_day, max_late_days=a.max_late_days)
            AssignmentQuestion.objects.bulk_create([AssignmentQuestion(tenant_id=course.tenant_id, assignment=a2,
                                                                       question_text=q.question_text)
                                                    for q in a.questions.all()])
        for qz in sec.quizzes.all():
            q2 = Quiz.objects.create(tenant_id=course.tenant_id, section=s2, title=qz.title, description=qz.description,
                                     duration_minutes=qz.duration_minutes, pass_mark=qz.pass_mark)
            Question.objects.bulk_create([Question(
                tenant_id=course.tenant_id, quiz=q2, question_text=q.question_text, option_a=q.option_a,
                option_b=q.option_b, option_c=q.option_c, option_d=q.option_d, correct_option=q.correct_option)
                for q in qz.questions.all()])
    if school_class:
        new.students.set([s.user for s in Student.objects.filter(tenant_id=course.tenant_id, class_assigned=school_class,
                                                                 status__in=ENROLLED_STATUSES, user__isnull=False)])
    return new
