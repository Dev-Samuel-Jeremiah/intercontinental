"""
curriculum/staffing.py
──────────────────────
Cover for absent teachers, lesson observations, and clubs and societies.
"""
from collections import Counter
from datetime import date, timedelta

from django.db import transaction
from django.utils import timezone

from users.models import ENROLLED_STATUSES, Staff, StaffSubjectClass

from .models import (SCALE, Club, ClubAttendance, ClubMeeting, ClubMember, CoverAssignment, LessonObservation,
                     ObservationTemplate)


class StaffingError(ValueError):
    pass


# ─────────────────────────────────────────────────────────────────────────────
# Cover
# ─────────────────────────────────────────────────────────────────────────────

def _day_code(day):
    from timetable.lessons import DAY_CODES
    return DAY_CODES[day.weekday()]


def _timetable_slots(tenant, day):
    from timetable.lessons import published_timetable
    from timetable.models import TimetableSlot
    tt = published_timetable(tenant)
    if not tt:
        return TimetableSlot.objects.none()
    return (TimetableSlot.objects.filter(tenant=tenant, timetable=tt, day=_day_code(day))
            .select_related('school_class', 'subject', 'teacher', 'time_slot'))


def on_leave(tenant, day):
    from safety.models import StaffLeave
    return {lv.staff_id: lv for lv in StaffLeave.objects.filter(
        tenant=tenant, status=StaffLeave.APPROVED, start_date__lte=day, end_date__gte=day).select_related('staff')}


def absences(tenant, day, extra_ids=()):
    """
    Teachers away on `day` who have lessons: those on approved leave plus any
    the administrator names. [{teacher, leave, lessons: [{slot, cover}]}]
    """
    slots = list(_timetable_slots(tenant, day).order_by('time_slot__order'))
    leave = on_leave(tenant, day)
    covers = {c.slot_id: c for c in CoverAssignment.objects.filter(tenant=tenant, date=day)
              .select_related('cover_teacher')}
    away = set(leave) | {int(i) for i in extra_ids} | {c.absent_teacher_id for c in covers.values()}
    out = []
    for staff in Staff.objects.filter(tenant=tenant, id__in=away).order_by('full_name'):
        mine = [s for s in slots if s.teacher_id == staff.id]
        if not mine:
            continue
        out.append({'teacher': staff, 'leave': leave.get(staff.id),
                    'lessons': [{'slot': s, 'cover': covers.get(s.id)} for s in mine]})
    return out


def free_teachers(slot, day):
    """
    Teachers who could cover the lesson: not teaching then, not on leave, not
    already covering then. Best first: teaches the subject, then fewest covers
    in the last 30 days.
    """
    tenant = slot.tenant
    busy = set(_timetable_slots(tenant, day).filter(time_slot=slot.time_slot).values_list('teacher_id', flat=True))
    busy |= set(CoverAssignment.objects.filter(tenant=tenant, date=day, slot__time_slot=slot.time_slot)
                .exclude(slot=slot).values_list('cover_teacher_id', flat=True))
    busy |= set(on_leave(tenant, day))
    busy.add(slot.teacher_id)
    recent = Counter(CoverAssignment.objects.filter(tenant=tenant, date__gte=day - timedelta(days=30))
                     .values_list('cover_teacher_id', flat=True))
    knows = set(StaffSubjectClass.objects.filter(tenant=tenant, subject=slot.subject).values_list('staff_id', flat=True))
    teaching_today = Counter(_timetable_slots(tenant, day).values_list('teacher_id', flat=True))
    rows = [{'staff': s, 'subject_teacher': s.id in knows, 'recent_covers': recent.get(s.id, 0),
             'lessons_today': teaching_today.get(s.id, 0)}
            for s in Staff.objects.filter(tenant=tenant, role__in=('TEACHER', 'ADMIN')).exclude(id__in=busy)]
    rows.sort(key=lambda r: (not r['subject_teacher'], r['recent_covers'], r['lessons_today'], r['staff'].full_name))
    return rows


def assign_cover(slot, day, cover, user=None, note='', notify=True):
    if day < timezone.localdate():
        raise StaffingError('Cover can only be arranged for today or later.')
    if _day_code(day) != slot.day:
        raise StaffingError('That lesson is not on the timetable that day.')
    if cover.id not in {r['staff'].id for r in free_teachers(slot, day)}:
        raise StaffingError(f'{cover.full_name} is not free for {slot.time_slot.name} that day.')
    existing = CoverAssignment.objects.filter(tenant=slot.tenant, slot=slot, date=day).first()
    c = existing or CoverAssignment(tenant=slot.tenant, slot=slot, date=day)
    c.absent_teacher, c.cover_teacher = slot.teacher, cover
    c.note, c.assigned_by, c.notified_at = (note or '').strip()[:255], user, None
    c.save()
    if notify:
        from lms_project.tasks import run_in_background
        run_in_background(_notify_cover, c.id, tenant=slot.tenant)
    return c


def _notify_cover(cover_id):
    from communications.services import send_email
    c = (CoverAssignment.objects.filter(pk=cover_id)
         .select_related('slot__time_slot', 'slot__subject', 'slot__school_class', 'cover_teacher__user',
                         'absent_teacher', 'tenant').first())
    email = c and getattr(c.cover_teacher.user, 'email', '')
    if not email:
        return
    s = c.slot
    body = (f"<p>Hello {c.cover_teacher.full_name},</p><p>Please cover <b>{s.subject.name}</b> for "
            f"<b>{s.school_class.name}</b> on <b>{c.date:%A %d %B}</b>, {s.time_slot.name} "
            f"({s.time_slot.start_time:%-I:%M %p}–{s.time_slot.end_time:%-I:%M %p}), for {c.absent_teacher.full_name}.</p>"
            + (f"<p>Note: {c.note}</p>" if c.note else '')
            + "<p>It's on your My Lessons page, where you can take the register.</p>"
            + f"<p>{c.tenant.name}</p>")
    ok, _ = send_email(email, f'Cover: {s.subject.name} {s.school_class.name}, {c.date:%a %d %b}', body)
    if ok:
        CoverAssignment.objects.filter(pk=c.pk).update(notified_at=timezone.now())


def covers_for(staff, day):
    return list(CoverAssignment.objects.filter(tenant_id=staff.tenant_id, cover_teacher=staff, date=day)
                .select_related('slot__time_slot', 'slot__subject', 'slot__school_class', 'absent_teacher'))


def is_covering(staff, slot, day):
    return bool(staff) and CoverAssignment.objects.filter(tenant_id=staff.tenant_id, cover_teacher=staff,
                                                          slot=slot, date=day).exists()


def cover_summary(tenant, start, end):
    """Covers given and needed per teacher over a period."""
    qs = CoverAssignment.objects.filter(tenant=tenant, date__range=(start, end))
    given = Counter(qs.values_list('cover_teacher_id', flat=True))
    needed = Counter(qs.values_list('absent_teacher_id', flat=True))
    ids = set(given) | set(needed)
    return sorted([{'staff': s, 'given': given.get(s.id, 0), 'needed': needed.get(s.id, 0)}
                   for s in Staff.objects.filter(tenant=tenant, id__in=ids)], key=lambda r: -r['given'])


# ─────────────────────────────────────────────────────────────────────────────
# Lesson observations
# ─────────────────────────────────────────────────────────────────────────────

def template_for(tenant):
    return ObservationTemplate.objects.filter(tenant=tenant).first() or ObservationTemplate(tenant=tenant)


def may_observe(user, teacher, subject=None):
    from users.views_profile import can_manage
    from .services import hod_subject_ids
    if can_manage(user):
        return True
    me = getattr(user, 'staff', None)
    if not me or me.id == teacher.id:
        return False
    heads = hod_subject_ids(me)
    if subject is not None:
        return subject.id in heads
    return bool(heads & set(StaffSubjectClass.objects.filter(tenant_id=teacher.tenant_id, staff=teacher)
                            .values_list('subject_id', flat=True)))


def record_observation(teacher, user, data, criteria):
    """`data` is the posted form; criteria come from the school's template."""
    from users.models import Class, Subject
    subject = Subject.objects.filter(tenant_id=teacher.tenant_id, pk=data.get('subject') or None).first()
    if not may_observe(user, teacher, subject):
        raise StaffingError(f"You can't observe {teacher.full_name}"
                            + (f' in {subject.name}' if subject else '') + '.')
    scores, valid = [], dict(SCALE)
    for i, criterion in enumerate(criteria):
        raw = data.get(f'score_{i}')
        if raw in (None, ''):
            raise StaffingError(f'Score "{criterion}".')
        try:
            score = int(raw)
        except ValueError:
            raise StaffingError(f'Score "{criterion}" from 1 to 4.')
        if score not in valid:
            raise StaffingError(f'Score "{criterion}" from 1 to 4.')
        scores.append({'criterion': criterion, 'score': score,
                       'comment': (data.get(f'comment_{i}') or '').strip()[:255]})
    try:
        day = date.fromisoformat(data.get('date')) if data.get('date') else timezone.localdate()
    except ValueError:
        day = timezone.localdate()
    if day > timezone.localdate():
        raise StaffingError("An observation can't be in the future.")
    share = bool(data.get('share'))
    return LessonObservation.objects.create(
        tenant_id=teacher.tenant_id, teacher=teacher, observer=getattr(user, 'staff', None), observer_user=user,
        date=day, subject=subject,
        school_class=Class.objects.filter(tenant_id=teacher.tenant_id, pk=data.get('school_class') or None).first(),
        topic=(data.get('topic') or '').strip()[:200], scores=scores,
        strengths=(data.get('strengths') or '').strip(), improvements=(data.get('improvements') or '').strip(),
        shared=share, shared_at=timezone.now() if share else None)


def share(obs):
    if not obs.shared:
        obs.shared, obs.shared_at = True, timezone.now()
        obs.save(update_fields=['shared', 'shared_at'])
    return obs


def acknowledge(obs, staff, response=''):
    if obs.teacher_id != getattr(staff, 'id', None):
        raise StaffingError('Only the observed teacher can acknowledge this.')
    if not obs.shared:
        raise StaffingError('This observation has not been shared with you yet.')
    obs.acknowledged_at = timezone.now()
    obs.teacher_response = (response or '').strip()
    obs.save(update_fields=['acknowledged_at', 'teacher_response'])
    return obs


def teacher_trend(teacher):
    """[(date, percent)] oldest first, for the teacher's history."""
    return [(o.date, o.percent) for o in LessonObservation.objects.filter(
        tenant_id=teacher.tenant_id, teacher=teacher).order_by('date') if o.percent is not None]


# ─────────────────────────────────────────────────────────────────────────────
# Clubs
# ─────────────────────────────────────────────────────────────────────────────

def may_run(user, club):
    from users.views_profile import can_manage
    staff = getattr(user, 'staff', None)
    return can_manage(user) or bool(staff and club.patrons.filter(pk=staff.pk).exists())


def add_members(club, students, role='MEMBER'):
    added, full = 0, False
    for st in students:
        if club.capacity and club.members.count() >= club.capacity:
            full = True
            break
        if st.status not in ENROLLED_STATUSES:
            continue
        _, created = ClubMember.objects.get_or_create(tenant_id=club.tenant_id, club=club, student=st,
                                                      defaults={'role': role})
        added += created
    return added, full


def set_role(member, role):
    if role not in dict(ClubMember.ROLES):
        raise StaffingError('Choose a valid role.')
    if role != 'MEMBER' and ClubMember.objects.filter(tenant_id=member.tenant_id, club=member.club, role=role) \
            .exclude(pk=member.pk).exists():
        raise StaffingError(f'{member.club.name} already has a {dict(ClubMember.ROLES)[role].lower()}.')
    member.role = role
    member.save(update_fields=['role'])
    return member


@transaction.atomic
def record_meeting(club, day, present_ids, topic='', notes='', staff=None):
    if day > timezone.localdate():
        raise StaffingError("You can't record a meeting that hasn't happened.")
    meeting, _ = ClubMeeting.objects.update_or_create(
        tenant_id=club.tenant_id, club=club, date=day,
        defaults={'topic': (topic or '').strip()[:160], 'notes': (notes or '').strip(), 'recorded_by': staff})
    present = {int(i) for i in present_ids}
    for m in club.members.all():
        ClubAttendance.objects.update_or_create(tenant_id=club.tenant_id, meeting=meeting, student_id=m.student_id,
                                                defaults={'present': m.student_id in present})
    return meeting


def member_rates(club):
    """{student_id: percent of recorded meetings attended}."""
    meetings = club.meetings.count()
    if not meetings:
        return {}
    counts = Counter(ClubAttendance.objects.filter(tenant_id=club.tenant_id, meeting__club=club, present=True)
                     .values_list('student_id', flat=True))
    return {m.student_id: round(100 * counts.get(m.student_id, 0) / meetings) for m in club.members.all()}


def clubs_of(student):
    return list(ClubMember.objects.filter(tenant_id=student.tenant_id, student=student, club__active=True)
                .select_related('club'))
