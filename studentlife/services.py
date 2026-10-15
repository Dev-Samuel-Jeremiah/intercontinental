"""
studentlife/services.py
───────────────────────
    F284  opt-in leaderboards
    F286  e-portfolio
    F290  digital handbook with acknowledgement
    F291  anonymous bullying and safety reports
    F292  wellbeing check-ins
    F293  peer tutoring
    F296  goal setting
    F297  shared notes
"""
import hashlib
import secrets
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.db.models import Count, Q, Sum
from django.utils import timezone
from django.utils.html import escape

from users.models import ENROLLED_STATUSES, Staff, Student

from .models import (Checkin, Goal, GoalUpdate, HandbookAck, HandbookSection, LeaderboardEntry, LifeSettings,
                     NoteLike, PortfolioItem, ReportMessage, SafetyReport, SharedNote, TutorOffer, TutorSession)


class LifeError(ValueError):
    pass


def settings_for(tenant):
    return LifeSettings.objects.filter(tenant=tenant).first() or LifeSettings.objects.create(tenant=tenant)


def _email(to, subject, html):
    from communications.services import send_email
    return send_email(to, subject, html)


def check_file(upload):
    from users import records
    try:
        return records.check_upload(upload)
    except records.RecordError as problem:
        raise LifeError(str(problem))


def safeguarding_emails(tenant):
    cfg = settings_for(tenant)
    emails = [s.user.email for s in cfg.safeguarding_leads.select_related('user') if s.user.email]
    if not emails:
        from hr.services import admins_emails
        emails = admins_emails(tenant)
    return sorted(set(emails))


def form_teacher_email(student):
    cls = student.class_assigned
    if cls and cls.form_teacher_id and cls.form_teacher.user.email:
        return cls.form_teacher.user.email
    return None


# ─────────────────────────────────────────────────────────────────────────────
# F284 — leaderboards (only students who opt in ever appear)
# ─────────────────────────────────────────────────────────────────────────────

BOARDS = {'points': 'House points', 'practice': 'Practice questions right', 'goals': 'Goals achieved',
          'tutoring': 'Tutoring sessions given'}


def join_board(student, nickname=''):
    entry, _ = LeaderboardEntry.objects.update_or_create(
        student=student, defaults={'tenant_id': student.tenant_id, 'nickname': (nickname or '').strip()[:40]})
    return entry


def leave_board(student):
    LeaderboardEntry.objects.filter(student=student).delete()


def board(tenant, metric, level=None, days=90, limit=20):
    """[{'name', 'class', 'value', 'student_id'}] ranked, for opted-in students only."""
    since = timezone.now() - timedelta(days=days)
    entries = LeaderboardEntry.objects.filter(tenant=tenant, student__status__in=ENROLLED_STATUSES) \
        .select_related('student__class_assigned')
    if level:
        entries = entries.filter(student__class_assigned__level=level)
    ids = {e.student_id: e for e in entries}
    if not ids:
        return []
    if metric == 'points':
        from users.models import HousePoint
        rows = HousePoint.objects.filter(student_id__in=ids, awarded_on__gte=since.date()) \
            .values('student_id').annotate(v=Sum('points'))
    elif metric == 'practice':
        from cbt.models import PracticeSession
        rows = PracticeSession.objects.filter(student_id__in=ids, submitted_at__gte=since) \
            .values('student_id').annotate(v=Sum('score'))
    elif metric == 'goals':
        rows = Goal.objects.filter(student_id__in=ids, status=Goal.ACHIEVED, achieved_at__gte=since) \
            .values('student_id').annotate(v=Count('id'))
    elif metric == 'tutoring':
        rows = TutorSession.objects.filter(offer__student_id__in=ids, status=TutorSession.DONE, updated_at__gte=since) \
            .values('offer__student_id').annotate(v=Count('id'))
        rows = [{'student_id': r['offer__student_id'], 'v': r['v']} for r in rows]
    else:
        raise LifeError('Unknown leaderboard.')
    out = []
    for r in rows:
        if not r['v']:
            continue
        e = ids[r['student_id']]
        st = e.student
        out.append({'student_id': st.pk, 'name': e.nickname or _short(st.full_name),
                    'class': st.class_assigned.name if st.class_assigned_id else '', 'value': r['v']})
    out.sort(key=lambda x: -x['value'])
    for i, row in enumerate(out, 1):
        row['rank'] = i
    return out[:limit]


def _short(name):
    parts = (name or '').split()
    return f'{parts[0]} {parts[-1][0]}.' if len(parts) > 1 else (parts[0] if parts else '')


# ─────────────────────────────────────────────────────────────────────────────
# F286 — e-portfolio
# ─────────────────────────────────────────────────────────────────────────────

def add_item(student, data, upload=None):
    from datetime import datetime
    title = (data.get('title') or '').strip()
    if not title:
        raise LifeError('Give your piece a title.')
    if data.get('kind') not in dict(PortfolioItem.KINDS):
        raise LifeError('Choose what kind of work it is.')
    try:
        day = datetime.strptime(data.get('date') or '', '%Y-%m-%d').date()
    except ValueError:
        day = timezone.localdate()
    link = (data.get('link') or '').strip()
    if link and not link.startswith(('http://', 'https://')):
        raise LifeError('Links must start with http:// or https://')
    item = PortfolioItem(tenant_id=student.tenant_id, student=student, title=title[:160], kind=data['kind'],
                         description=(data.get('description') or '').strip(), date=day, link=link[:200],
                         share_with_family=bool(data.get('share_with_family')))
    if upload:
        check_file(upload)
        item.file = upload
    if not (item.file or link or item.description):
        raise LifeError('Add a file, a link or a description.')
    item.save()
    return item


# ─────────────────────────────────────────────────────────────────────────────
# F290 — handbook
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_HANDBOOK = [
    ('Welcome', 'This handbook explains how our school works and what we expect of every student.'),
    ('School day', 'Arrive before the bell. Assembly, lessons, break and dismissal follow the timetable on the '
                   'portal.'),
    ('Uniform and appearance', 'Wear the correct uniform neatly every day. Your form teacher will tell you about '
                               'special days.'),
    ('Behaviour and respect', 'Treat everyone with respect. Bullying of any kind — in person or online — is never '
                              'acceptable. You can report it anonymously on the portal.'),
    ('Homework and exams', 'Homework is set on the portal. Exam malpractice is a serious offence.'),
    ('Phones and devices', 'Follow the school rules on phones and devices. Never share photos of others without '
                           'permission.'),
    ('Getting help', 'Talk to your form teacher, the counsellor or any member of staff. Use the wellbeing check-in '
                     'if you would like someone to talk to you.'),
]


def ensure_handbook(tenant):
    if not HandbookSection.objects.filter(tenant=tenant).exists():
        for i, (title, body) in enumerate(DEFAULT_HANDBOOK):
            HandbookSection.objects.create(tenant=tenant, title=title, body=body, order=i)


def acknowledged(user, tenant):
    return HandbookAck.objects.filter(tenant=tenant, user=user, version=settings_for(tenant).handbook_version).exists()


def acknowledge(user, tenant):
    HandbookAck.objects.get_or_create(tenant=tenant, user=user, version=settings_for(tenant).handbook_version)


def new_version(tenant):
    cfg = settings_for(tenant)
    cfg.handbook_version += 1
    cfg.save(update_fields=['handbook_version'])
    return cfg.handbook_version


def ack_report(tenant):
    v = settings_for(tenant).handbook_version
    done = set(HandbookAck.objects.filter(tenant=tenant, version=v).values_list('user_id', flat=True))
    students = list(Student.objects.filter(tenant=tenant, status__in=ENROLLED_STATUSES).select_related(
        'class_assigned', 'user').order_by('class_assigned__name', 'full_name'))
    return {'version': v, 'done': sum(1 for s in students if s.user_id in done), 'total': len(students),
            'missing': [s for s in students if s.user_id not in done]}


# ─────────────────────────────────────────────────────────────────────────────
# F291 — anonymous reports
# ─────────────────────────────────────────────────────────────────────────────

def _hash(code):
    return hashlib.sha256((code or '').strip().upper().encode()).hexdigest()


def make_report(tenant, user, data):
    """Returns (report, follow-up code). The code is shown once; only its hash is kept."""
    if data.get('kind') not in dict(SafetyReport.KINDS):
        raise LifeError('Choose what you are reporting.')
    description = (data.get('description') or '').strip()
    if len(description) < 10:
        raise LifeError('Tell us a little more about what happened.')
    anonymous = bool(data.get('anonymous'))
    code = '-'.join(secrets.token_hex(2).upper() for _ in range(3))
    for _ in range(5):
        ref = f'R{secrets.randbelow(900000) + 100000}'
        try:
            with transaction.atomic():
                r = SafetyReport.objects.create(
                    tenant=tenant, reference=ref, code_hash=_hash(code), kind=data['kind'],
                    where=(data.get('where') or '')[:160], when=(data.get('when') or '')[:120],
                    who=(data.get('who') or '')[:255], description=description[:8000],
                    reporter=None if anonymous else user, urgent=bool(data.get('urgent')))
            break
        except IntegrityError:
            continue
    else:
        raise LifeError('Please try again.')
    flag = 'URGENT ' if r.urgent else ''
    for e in safeguarding_emails(tenant):
        _email(e, f'{flag}Student report {r.reference}: {r.get_kind_display()}',
               f'<p>A student has made a {"named" if r.reporter_id else "anonymous"} report '
               f'({escape(r.get_kind_display())}).</p><p>Open Student experience → Reports to read it. The details '
               f'are only on the portal.</p>')
    return r, code


def find_report(tenant, reference, code):
    r = SafetyReport.objects.filter(tenant=tenant, reference=(reference or '').strip().upper()).first()
    if not r or r.code_hash != _hash(code):
        raise LifeError('That reference and code do not match.')
    return r


def report_reply(report, body, user=None, from_school=True, internal=False, status=None):
    body = (body or '').strip()
    if body:
        ReportMessage.objects.create(tenant_id=report.tenant_id, report=report, from_school=from_school,
                                     author=user if from_school else None, body=body[:6000], internal=internal)
    if status and from_school and status in dict(SafetyReport.STATUSES):
        report.status = status
    report.save()


# ─────────────────────────────────────────────────────────────────────────────
# F292 — wellbeing
# ─────────────────────────────────────────────────────────────────────────────

FEELINGS = ['Happy', 'Calm', 'Excited', 'Tired', 'Worried', 'Stressed', 'Lonely', 'Angry', 'Sad', 'Unwell']


def check_in(student, data):
    mood = str(data.get('mood') or '')
    if mood not in ('1', '2', '3', '4', '5'):
        raise LifeError('Choose how you feel today.')
    sleep = str(data.get('sleep') or '')
    feelings = [f for f in (data.getlist('feelings') if hasattr(data, 'getlist') else data.get('feelings', []))
                if f in FEELINGS]
    today = timezone.localdate()
    c, _ = Checkin.objects.update_or_create(
        student=student, date=today,
        defaults={'tenant_id': student.tenant_id, 'mood': int(mood),
                  'sleep': int(sleep) if sleep.isdigit() and int(sleep) <= 24 else None,
                  'feelings': feelings, 'note': (data.get('note') or '').strip()[:500],
                  'wants_talk': bool(data.get('wants_talk'))})
    recent = list(Checkin.objects.filter(student=student, date__gte=today - timedelta(days=6)).values_list('mood',
                                                                                                         flat=True))
    low_streak = len(recent) >= 3 and sum(1 for m in recent if m <= 2) >= 3
    should_flag = c.wants_talk or c.mood == 1 or low_streak
    if should_flag and not c.flagged:
        c.flagged = True
        c.save(update_fields=['flagged'])
        to = set(safeguarding_emails(student.tenant))
        if form_teacher_email(student):
            to.add(form_teacher_email(student))
        why = 'asked to talk to someone' if c.wants_talk else 'has been feeling low'
        for e in sorted(to):
            _email(e, f'Wellbeing: {student.full_name} {why}',
                   f'<p>{escape(student.full_name)} ({escape(str(student.class_assigned or ""))}) {why} in today\'s '
                   f'check-in. Please find a quiet moment to talk, then mark it followed up on the portal.</p>')
    return c


def class_wellbeing(students, days=14):
    since = timezone.localdate() - timedelta(days=days)
    rows = Checkin.objects.filter(student__in=students, date__gte=since)
    by_student = {}
    for c in rows:
        by_student.setdefault(c.student_id, []).append(c.mood)
    avg = round(sum(c.mood for c in rows) / rows.count(), 1) if rows.exists() else None
    return {'average': avg, 'count': rows.count(), 'students': len(by_student),
            'low': [s for s in students if by_student.get(s.pk) and sum(by_student[s.pk]) / len(by_student[s.pk]) <= 2]}


# ─────────────────────────────────────────────────────────────────────────────
# F293 — peer tutoring
# ─────────────────────────────────────────────────────────────────────────────

def offer(student, subject, about, when):
    if not settings_for(student.tenant).tutoring:
        raise LifeError('Peer tutoring is switched off.')
    o, created = TutorOffer.objects.get_or_create(
        student=student, subject=subject,
        defaults={'tenant_id': student.tenant_id, 'about': (about or '')[:255], 'when': (when or '')[:120]})
    if not created:
        o.about, o.when, o.active = (about or '')[:255], (when or '')[:120], True
        o.save(update_fields=['about', 'when', 'active'])
    return o


def tutors(tenant, subject=None, exclude=None):
    qs = TutorOffer.objects.filter(tenant=tenant, active=True, approved=True,
                                   student__status__in=ENROLLED_STATUSES).select_related('student__class_assigned',
                                                                                         'subject')
    if subject:
        qs = qs.filter(subject=subject)
    if exclude:
        qs = qs.exclude(student=exclude)
    return qs


def request_session(learner, offer_obj, topic, proposed):
    if not (offer_obj.active and offer_obj.approved):
        raise LifeError('This tutor is not available.')
    if offer_obj.student_id == learner.pk:
        raise LifeError('You cannot book yourself.')
    topic = (topic or '').strip()
    if not topic:
        raise LifeError('Say what you need help with.')
    if TutorSession.objects.filter(offer=offer_obj, learner=learner, status__in=('REQUESTED', 'ACCEPTED')).exists():
        raise LifeError('You already have a request with this tutor.')
    return TutorSession.objects.create(tenant_id=learner.tenant_id, offer=offer_obj, learner=learner,
                                       topic=topic[:160], proposed=(proposed or '')[:120])


def update_session(session, student, action, rating=None):
    tutor_side = session.offer.student_id == student.pk
    learner_side = session.learner_id == student.pk
    if action in ('accept', 'decline') and tutor_side and session.status == TutorSession.REQUESTED:
        session.status = TutorSession.ACCEPTED if action == 'accept' else TutorSession.DECLINED
    elif action == 'done' and (tutor_side or learner_side) and session.status == TutorSession.ACCEPTED:
        session.status = TutorSession.DONE
    elif action == 'cancel' and (tutor_side or learner_side) and session.status in ('REQUESTED', 'ACCEPTED'):
        session.status = TutorSession.CANCELLED
    elif action == 'rate' and learner_side and session.status == TutorSession.DONE and \
            str(rating) in ('1', '2', '3', '4', '5'):
        session.rating = int(rating)
    else:
        raise LifeError('That is not possible now.')
    session.save()
    return session


# ─────────────────────────────────────────────────────────────────────────────
# F296 — goals
# ─────────────────────────────────────────────────────────────────────────────

def add_goal(student, data):
    from datetime import datetime
    title = (data.get('title') or '').strip()
    if not title:
        raise LifeError('Write your goal.')
    if data.get('category') not in dict(Goal.CATEGORIES):
        raise LifeError('Choose a kind of goal.')
    try:
        target = datetime.strptime(data.get('target_date') or '', '%Y-%m-%d').date()
    except ValueError:
        target = None
    if Goal.objects.filter(student=student, status=Goal.ACTIVE).count() >= 8:
        raise LifeError('Focus on up to eight goals at a time.')
    return Goal.objects.create(tenant_id=student.tenant_id, student=student, title=title[:160],
                               category=data['category'], measure=(data.get('measure') or '')[:255],
                               target_date=target)


def update_goal(goal, progress, note=''):
    p = str(progress or '')
    if not p.isdigit() or int(p) > 100:
        raise LifeError('Progress is a number from 0 to 100.')
    goal.progress = int(p)
    if goal.progress == 100 and goal.status == Goal.ACTIVE:
        goal.status, goal.achieved_at = Goal.ACHIEVED, timezone.now()
    goal.save()
    GoalUpdate.objects.create(tenant_id=goal.tenant_id, goal=goal, progress=goal.progress, note=(note or '')[:500])
    return goal


# ─────────────────────────────────────────────────────────────────────────────
# F297 — shared notes
# ─────────────────────────────────────────────────────────────────────────────

def share_note(student, subject, title, body, upload=None):
    if not student.class_assigned_id:
        raise LifeError('You need a class to share notes.')
    title = (title or '').strip()
    if not title or not ((body or '').strip() or upload):
        raise LifeError('Give a title and the notes (typed or as a file).')
    note = SharedNote(tenant_id=student.tenant_id, author=student, subject=subject, title=title[:160],
                      body=(body or '').strip(), level=student.class_assigned.level or student.class_assigned.name)
    if upload:
        check_file(upload)
        note.file = upload
    note.status = SharedNote.PENDING if settings_for(student.tenant).notes_need_approval else SharedNote.APPROVED
    note.save()
    return note


def notes_for(student):
    if not student.class_assigned_id:
        return SharedNote.objects.none()
    level = student.class_assigned.level or student.class_assigned.name
    return SharedNote.objects.filter(tenant_id=student.tenant_id, level=level).filter(
        Q(status=SharedNote.APPROVED) | Q(author=student)).select_related('author', 'subject') \
        .annotate(n_likes=Count('likes'))


def like(note, student):
    if note.author_id == student.pk:
        return False
    try:
        with transaction.atomic():
            NoteLike.objects.create(tenant_id=note.tenant_id, note=note, student=student)
        return True
    except IntegrityError:
        NoteLike.objects.filter(note=note, student=student).delete()
        return False


def report_note(note):
    note.reports += 1
    if note.reports >= 3:
        note.status = SharedNote.HIDDEN
    note.save(update_fields=['reports', 'status'])


def may_review_notes(user):
    from users.views_profile import can_view
    return can_view(user)


def staff_for(user, tenant):
    return Staff.objects.filter(user=user, tenant=tenant).first()
