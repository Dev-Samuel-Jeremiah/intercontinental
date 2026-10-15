"""
curriculum/services.py
──────────────────────
Who heads which department, schemes of work and how far each class has got
through them, reviewing lesson notes, homework, and the school calendar.
"""
import calendar as _cal
import re
from collections import defaultdict
from datetime import date, timedelta

from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone

from curriculum.models import (CalendarEvent, Department, Homework, LessonNote, SchemeOfWork,
                               SchemeTopic)

BEHIND_BY = 2      # topics behind before a scheme is flagged


class CurriculumError(ValueError):
    pass


# ─────────────────────────────────────────────────────────────────────────────
# Departments
# ─────────────────────────────────────────────────────────────────────────────

def hod_subject_ids(staff):
    """Subjects this staff member heads the department for."""
    if not staff:
        return set()
    return set(Department.objects.filter(tenant_id=staff.tenant_id, hod=staff)
               .values_list('subjects__id', flat=True)) - {None}


def teaching_pairs(user, tenant):
    """(subject, class) pairs this person plans for: all of them for administrators."""
    from users.models import ClassSubject, StaffSubjectClass
    from users.views_profile import can_manage
    if can_manage(user):
        return [(cs.subject, cs.school_class) for cs in ClassSubject.objects.filter(tenant=tenant)
                .select_related('subject', 'school_class').order_by('school_class__name', 'subject__name')]
    staff = getattr(user, 'staff', None)
    if not staff:
        return []
    pairs = {(a.subject_id, a.school_class_id): (a.subject, a.school_class) for a in
             StaffSubjectClass.objects.filter(tenant=tenant, staff=staff).select_related('subject', 'school_class')}
    heads = hod_subject_ids(staff)
    if heads:
        for cs in ClassSubject.objects.filter(tenant=tenant, subject_id__in=heads).select_related('subject', 'school_class'):
            pairs.setdefault((cs.subject_id, cs.school_class_id), (cs.subject, cs.school_class))
    return sorted(pairs.values(), key=lambda p: (p[1].name, p[0].name))


def may_plan(user, subject, school_class):
    """Administrators, the subject's head of department, or a teacher of that subject in that class."""
    from users.models import StaffSubjectClass
    if may_review(user, subject):
        return True
    staff = getattr(user, 'staff', None)
    return bool(staff) and StaffSubjectClass.objects.filter(tenant_id=staff.tenant_id, staff=staff,
                                                            subject=subject, school_class=school_class).exists()


def may_review(user, subject):
    from users.views_profile import can_manage
    staff = getattr(user, 'staff', None)
    return can_manage(user) or (subject.id in hod_subject_ids(staff))


# ─────────────────────────────────────────────────────────────────────────────
# Schemes of work and coverage
# ─────────────────────────────────────────────────────────────────────────────

def current_week(term, day=None):
    if not term or not term.start_date:
        return 0
    day = day or timezone.localdate()
    if day < term.start_date:
        return 0
    return (day - term.start_date).days // 7 + 1


def parse_topics(text, weeks):
    """
    Turn pasted lines into (week, topic) pairs. "Week 3: Fractions" or
    "3. Fractions" set the week; plain lines take the next week in turn.
    """
    out, week = [], 0
    for raw in (text or '').splitlines():
        line = raw.strip()
        if not line:
            continue
        m = re.match(r'^(?:week\s*)?(\d{1,2})\s*[:.)\-–]\s*(.+)$', line, re.I)
        if m:
            week, topic = int(m.group(1)), m.group(2).strip()
        else:
            week, topic = week + 1, line
        if not 1 <= week <= max(weeks, 1):
            raise CurriculumError(f'"{line}" is outside the {weeks} weeks of the term.')
        out.append((week, topic[:200]))
    return out


@transaction.atomic
def add_topics(scheme, text):
    pairs = parse_topics(text, scheme.weeks)
    if not pairs:
        raise CurriculumError('Paste at least one topic.')
    counts = defaultdict(int, {w: n for w, n in scheme.topics.values_list('week').annotate(n=Count('id'))})
    created = []
    for week, topic in pairs:
        counts[week] += 1
        created.append(SchemeTopic.objects.create(tenant_id=scheme.tenant_id, scheme=scheme, week=week,
                                                  order=counts[week], topic=topic))
    return created


@transaction.atomic
def copy_scheme(source, school_class, term, staff=None):
    """Start a new scheme from an existing one (another class or last year's term)."""
    if SchemeOfWork.objects.filter(tenant_id=source.tenant_id, subject=source.subject,
                                   school_class=school_class, term=term).exists():
        raise CurriculumError('That class already has a scheme for this subject this term.')
    new = SchemeOfWork.objects.create(tenant_id=source.tenant_id, subject=source.subject,
                                      school_class=school_class, term=term, weeks=source.weeks,
                                      created_by=staff)
    SchemeTopic.objects.bulk_create([
        SchemeTopic(tenant_id=source.tenant_id, scheme=new, week=t.week, order=t.order, topic=t.topic,
                    subtopics=t.subtopics, objectives=t.objectives, resources=t.resources)
        for t in source.topics.all()])
    return new


def mark_taught(topic, staff, on=None):
    on = on or timezone.localdate()
    if on > timezone.localdate():
        raise CurriculumError("A topic can't be taught in the future.")
    topic.taught_on, topic.taught_by = on, staff
    topic.save(update_fields=['taught_on', 'taught_by'])
    return topic


def unmark_taught(topic):
    topic.taught_on, topic.taught_by = None, None
    topic.save(update_fields=['taught_on', 'taught_by'])
    return topic


def coverage(scheme, day=None):
    week = current_week(scheme.term, day)
    topics = list(scheme.topics.all())
    due = [t for t in topics if t.week <= week]
    taught = [t for t in topics if t.taught]
    behind = sum(1 for t in due if not t.taught)
    if not topics:
        state = 'empty'
    elif behind >= BEHIND_BY:
        state = 'behind'
    elif len(taught) > len(due):
        state = 'ahead'
    else:
        state = 'on_track'
    return {'scheme': scheme, 'week': week, 'total': len(topics), 'taught': len(taught), 'due': len(due),
            'behind': behind, 'state': state,
            'percent': round(100 * len(taught) / len(topics)) if topics else 0,
            'expected_percent': round(100 * len(due) / len(topics)) if topics else 0}


def coverage_report(tenant, term, day=None):
    schemes = (SchemeOfWork.objects.filter(tenant=tenant, term=term)
               .select_related('subject', 'school_class', 'created_by').prefetch_related('topics'))
    rows = [coverage(s, day) for s in schemes]
    order = {'behind': 0, 'empty': 1, 'on_track': 2, 'ahead': 3}
    return sorted(rows, key=lambda r: (order[r['state']], r['scheme'].school_class.name, r['scheme'].subject.name))


# ─────────────────────────────────────────────────────────────────────────────
# Lesson notes
# ─────────────────────────────────────────────────────────────────────────────

def submit_note(note):
    if note.status not in (LessonNote.DRAFT, LessonNote.RETURNED):
        raise CurriculumError('This note has already been submitted.')
    if len((note.content or '').strip()) < 20:
        raise CurriculumError('Write the lesson note before submitting it.')
    note.status, note.submitted_at = LessonNote.SUBMITTED, timezone.now()
    note.save(update_fields=['status', 'submitted_at'])
    return note


@transaction.atomic
def review_note(note, approve, reviewer_user, comment=''):
    if note.status != LessonNote.SUBMITTED:
        raise CurriculumError('Only a submitted note can be reviewed.')
    if not may_review(reviewer_user, note.subject):
        raise CurriculumError("Only the head of this subject's department or an administrator can review it.")
    reviewer = getattr(reviewer_user, 'staff', None)
    if reviewer and reviewer.id == note.teacher_id:
        raise CurriculumError('Someone else has to review your own lesson note.')
    if not approve and not (comment or '').strip():
        raise CurriculumError('Say what needs changing when you send a note back.')
    note.status = LessonNote.APPROVED if approve else LessonNote.RETURNED
    note.reviewed_by, note.reviewed_at = reviewer, timezone.now()
    note.review_comment = (comment or '').strip()
    note.save(update_fields=['status', 'reviewed_by', 'reviewed_at', 'review_comment'])
    if approve and note.topic_id and not note.topic.taught:
        mark_taught(note.topic, note.teacher)
    return note


def notes_to_review(user, tenant):
    from users.views_profile import can_manage
    qs = (LessonNote.objects.filter(tenant=tenant, status=LessonNote.SUBMITTED)
          .select_related('teacher', 'subject', 'school_class', 'term'))
    if can_manage(user):
        return qs
    return qs.filter(subject_id__in=hod_subject_ids(getattr(user, 'staff', None)))


# ─────────────────────────────────────────────────────────────────────────────
# Homework
# ─────────────────────────────────────────────────────────────────────────────

def set_homework(staff, school_class, subject, title, details, due_on, set_on=None):
    set_on = set_on or timezone.localdate()
    if not (title or '').strip():
        raise CurriculumError('Give the homework a title.')
    if due_on < set_on:
        raise CurriculumError('Homework cannot be due before it is set.')
    if (due_on - set_on).days > 60:
        raise CurriculumError('Set homework no more than two months ahead.')
    return Homework.objects.create(tenant_id=school_class.tenant_id, school_class=school_class,
                                   subject=subject, set_by=staff, title=title.strip()[:160],
                                   details=(details or '').strip(), set_on=set_on, due_on=due_on)


def homework_for(school_class, days_back=7):
    since = timezone.localdate() - timedelta(days=days_back)
    return (Homework.objects.filter(tenant_id=school_class.tenant_id, school_class=school_class,
                                    due_on__gte=since).select_related('subject', 'set_by'))


# ─────────────────────────────────────────────────────────────────────────────
# Calendar and holidays
# ─────────────────────────────────────────────────────────────────────────────

def easter(year):
    """Western Easter Sunday (anonymous Gregorian algorithm)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return date(year, month, day)


def nigerian_holidays(year):
    """
    Federal public holidays whose dates are fixed or can be calculated, plus
    Children's Day (a school holiday). Eid-el-Fitr, Eid-el-Kabir and Mawlid
    depend on the moon and are declared by the Federal Government each year,
    so they are not guessed here.
    """
    e = easter(year)
    return [
        (date(year, 1, 1), "New Year's Day"),
        (e - timedelta(days=2), 'Good Friday'),
        (e + timedelta(days=1), 'Easter Monday'),
        (date(year, 5, 1), "Workers' Day"),
        (date(year, 5, 27), "Children's Day (schools)"),
        (date(year, 6, 12), 'Democracy Day'),
        (date(year, 10, 1), 'Independence Day'),
        (date(year, 12, 25), 'Christmas Day'),
        (date(year, 12, 26), 'Boxing Day'),
    ]


def import_holidays(tenant, year):
    """Add the year's holidays the school doesn't have yet. Returns the names added."""
    from academics.models import PublicHoliday
    have = set(PublicHoliday.objects.filter(tenant=tenant, date__year=year).values_list('date', flat=True))
    added = []
    for day, name in nigerian_holidays(year):
        if day not in have:
            PublicHoliday.objects.create(tenant=tenant, date=day, name=name)
            added.append(name)
    return added


def visible_events(qs, audience):
    if audience == 'STAFF':
        return qs
    return qs.filter(audience__in=('ALL', 'PARENTS'))


def month_grid(tenant, year, month, audience='STAFF'):
    """Weeks (Mon–Sun) of days, each with its events and any holiday."""
    from academics.models import PublicHoliday
    first = date(year, month, 1)
    last = date(year, month, _cal.monthrange(year, month)[1])
    events = list(visible_events(CalendarEvent.objects.filter(tenant=tenant, start_date__lte=last)
                                 .filter(Q(end_date__gte=first) | Q(end_date__isnull=True, start_date__gte=first)),
                                 audience))
    holidays = {h.date: h.name for h in PublicHoliday.objects.filter(tenant=tenant, date__range=(first, last))}
    start = first - timedelta(days=first.weekday())
    weeks, day = [], start
    today = timezone.localdate()
    while day <= last or day.weekday() != 0:
        week = []
        for _ in range(7):
            week.append({'date': day, 'in_month': day.month == month, 'today': day == today,
                         'holiday': holidays.get(day),
                         'events': [e for e in events if e.start_date <= day <= e.last_day]})
            day += timedelta(days=1)
        weeks.append(week)
        if day > last:
            break
    return weeks


def upcoming(tenant, audience='STAFF', days=60, limit=8):
    today = timezone.localdate()
    qs = CalendarEvent.objects.filter(tenant=tenant, start_date__lte=today + timedelta(days=days)).filter(
        Q(end_date__gte=today) | Q(end_date__isnull=True, start_date__gte=today))
    return list(visible_events(qs, audience)[:limit])
