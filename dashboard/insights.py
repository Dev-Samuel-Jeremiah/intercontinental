"""
dashboard/insights.py
─────────────────────
What each person's dashboard shows. One builder per role, each made of small
sections. A section that fails (a module not set up, missing data) is left
out rather than breaking the whole dashboard.

  super admin   the command centre: the whole system — people, sections,
                platform health and backups, security, approvals waiting,
                the school AI and live activity
  admin         the school today: registers, staff in, lessons, approvals,
                welfare, results, students at risk, fees, events
  proprietor    the owner's view: money in and out, debt, enrolment and its
                forecast, results, targets, sections side by side
  teacher       my teaching day: lessons now and next, registers, marking,
                my form class (with the AI's concerns), deadlines
  student       my learning hub: today's lessons, exams, homework, results
                trend, quiz streak, practicals, school moments
  parent        my children: each child at a glance, then the chosen child's
                attendance, results, fees, homework, things to sign, events
"""
import logging
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal

from django.db.models import Count, Q, Sum
from django.utils import timezone

log = logging.getLogger(__name__)
ZERO = Decimal('0')


def safe(fn, default=None):
    try:
        return fn()
    except Exception:
        log.exception('Dashboard section failed: %s', getattr(fn, '__name__', fn))
        return default


def greeting():
    h = timezone.localtime().hour
    return 'Good morning' if h < 12 else 'Good afternoon' if h < 17 else 'Good evening'


def term_progress(term):
    if not (term and term.start_date and term.end_date and term.end_date > term.start_date):
        return None
    today = timezone.localdate()
    total = (term.end_date - term.start_date).days
    done = min(max((today - term.start_date).days, 0), total)
    weeks = -(-total // 7)
    return {'percent': round(done * 100 / total), 'week': min(done // 7 + 1, weeks), 'weeks': weeks,
            'days_left': total - done, 'ends': term.end_date}


def events(tenant, staff=True, days=21, n=6):
    from curriculum.models import CalendarEvent
    today = timezone.localdate()
    qs = CalendarEvent.objects.filter(tenant=tenant, start_date__gte=today, start_date__lte=today + timedelta(days=days))
    if not staff:
        qs = qs.exclude(audience='STAFF')
    return list(qs.order_by('start_date', 'start_time')[:n])


def moments(user, tenant, n=4):
    from moments import feed
    viewer = feed.Viewer(user, tenant)
    if not viewer.allowed:
        return []
    posts = list(viewer.posts().order_by('-created_at')[:n])
    return feed.decorate(viewer, posts, comments_preview=0)


def ai_risk(tenant, class_ids=None, n=5):
    from schoolai.models import Prediction
    qs = Prediction.objects.filter(tenant=tenant, kind='FAIL_RISK', band__in=('HIGH', 'MEDIUM')).select_related(
        'student__class_assigned')
    if class_ids is not None:
        qs = qs.filter(student__class_assigned_id__in=class_ids)
    return {'high': qs.filter(band='HIGH').count(), 'medium': qs.filter(band='MEDIUM').count(),
            'top': list(qs.order_by('-value')[:n])}


def ai_maturity(tenant):
    from schoolai import training
    return training.maturity(tenant)


def approvals(tenant):
    """Things waiting for a decision, with where to make it."""
    out = []

    def add(label, count, url, icon):
        if count:
            out.append({'label': label, 'count': count, 'url': url, 'icon': icon})
    try:
        from finance.models import Payment, TopUpRequest
        add('Payments to confirm', Payment.objects.filter(tenant=tenant, status=Payment.PENDING).count(),
            '/finance/admin/', 'cash-coin')
        add('Wallet top-ups to confirm', TopUpRequest.objects.filter(tenant=tenant, status='PENDING').count(),
            '/finance/admin/', 'wallet2')
    except Exception:
        pass
    try:
        from results.models import SubjectSheet
        add('Result sheets to review', SubjectSheet.objects.filter(tenant=tenant, status='SUBMITTED').count(),
            '/results/', 'journal-check')
    except Exception:
        pass
    try:
        from safety.models import StaffLeave
        add('Leave requests', StaffLeave.objects.filter(tenant=tenant, status='PENDING').count(), '/safety/leave/',
            'calendar2-x')
    except Exception:
        pass
    try:
        from moments.models import Comment, MomentReport
        add('Comments held for review', Comment.objects.filter(post__tenant=tenant, status='HELD').count(),
            '/moments/review/', 'chat-square-dots')
        add('Reported posts', MomentReport.objects.filter(tenant=tenant, resolved=False).count(), '/moments/review/', 'flag')
    except Exception:
        pass
    try:
        from staffchat.models import ChatRoom
        add('Staff complaints open', ChatRoom.objects.filter(tenant=tenant, kind='COMPLAINT', status__in=('OPEN', 'REVIEW'))
            .count(), '/staff-chat/', 'shield-exclamation')
    except Exception:
        pass
    try:
        from schoolai.models import AskedQuestion
        add('Questions for the school AI', AskedQuestion.objects.filter(tenant=tenant, status='UNSURE').count(),
            '/school-ai/questions/', 'mortarboard')
    except Exception:
        pass
    try:
        from labs.models import SafetyIncident
        add('Lab incidents open', SafetyIncident.objects.filter(tenant=tenant, closed=False).count(), '/labs/', 'bandaid')
    except Exception:
        pass
    return out


# Everything waiting for an administrator: (tone, label, how to count it, page, icon).
# red = urgent (people's safety, legal deadlines), amber = a decision to make, blue = a job to do.
def _attention_items():
    from django.db.models import Q
    today = timezone.localdate()

    def count(model_path, **filters):
        def run(t):
            module, name = model_path.rsplit('.', 1)
            model = getattr(__import__(module, fromlist=[name]), name)
            return model._default_manager.filter(tenant=t, **filters).count()
        return run

    def data_requests_overdue(t):
        from security.models import DataRequest
        return DataRequest._default_manager.filter(tenant=t, status__in=('NEW', 'VERIFYING', 'IN_PROGRESS'),
                                                   due_on__lt=today).count()

    def registers_not_taken(t):
        from analytics import metrics
        return len((metrics.attendance_today(t) or {}).get('unmarked') or [])

    def held_comments(t):
        from moments.models import Comment
        return Comment._default_manager.filter(post__tenant=t, status='HELD').count()

    return [
        ('red', 'Safeguarding concerns to look at', count('welfare.models.Concern', status='NEW'),
         'welfare:safeguarding', 'shield-exclamation'),
        ('red', 'Data breaches still open', count('security.models.Breach', status='OPEN'), 'security:breaches',
         'exclamation-octagon'),
        ('red', 'Data requests past their deadline', data_requests_overdue, 'security:requests', 'alarm'),
        ('red', 'Staff complaints open', count('staffchat.models.ChatRoom', kind='COMPLAINT', status__in=('OPEN', 'REVIEW')),
         'staffchat:home', 'chat-square-text'),
        ('red', 'Lab safety incidents open', count('labs.models.SafetyIncident', closed=False), 'labs:home', 'bandaid'),
        ('amber', 'Payments to confirm', count('finance.models.Payment', status='PENDING'), 'finance:admin_dashboard',
         'cash-coin'),
        ('amber', 'Wallet top-ups to confirm', count('finance.models.TopUpRequest', status='PENDING'),
         'finance:admin_dashboard', 'wallet2'),
        ('amber', 'Result sheets to approve', count('results.models.SubjectSheet', status='SUBMITTED'),
         'results:approvals', 'journal-check'),
        ('amber', 'Questions about results', count('results.models.ResultQuery', status='OPEN'), 'results:queries',
         'patch-question'),
        ('amber', 'Leave requests', count('safety.models.StaffLeave', status='PENDING'), 'safety:staff_leave',
         'calendar2-x'),
        ('amber', 'Lesson notes to review', count('curriculum.models.LessonNote', status='SUBMITTED'),
         'curriculum:review', 'journal-richtext'),
        ('amber', 'New admission applications', count('admissions.models.Application', status='NEW'),
         'admissions:board', 'person-plus'),
        ('amber', 'Corrections sent by parents', count('users.models.ProfileChangeRequest', status='PENDING'),
         'users:change_requests', 'pencil-square'),
        ('amber', 'People asking for a new password', count('security.models.PasswordHelpRequest', status='OPEN'),
         'security:password_help', 'key'),
        ('amber', 'Data protection requests', count('security.models.DataRequest', status__in=('NEW', 'VERIFYING')),
         'security:requests', 'inbox'),
        ('amber', 'Exeat requests', count('hostel.models.ExeatRequest', status='SCHOOL'), 'hostel:exeats', 'door-open'),
        ('amber', 'Bus pickup changes', count('transport.models.PickupChange', status='PENDING'), 'transport:changes',
         'bus-front'),
        ('amber', 'Reported posts', count('moments.models.MomentReport', resolved=False), 'moments:review', 'flag'),
        ('amber', 'Comments held for review', held_comments, 'moments:review', 'chat-square-dots'),
        ('blue', 'Registers not taken today', registers_not_taken, 'users:attendance_home', 'clipboard-x'),
        ('blue', 'Repairs reported', count('facilities.models.MaintenanceRequest', status='OPEN'),
         'facilities:maintenance', 'tools'),
        ('blue', 'Lost ID cards to reissue', count('safety.models.LostCardReport', status='REPORTED'),
         'safety:lost_cards', 'person-vcard'),
        ('blue', 'Questions the school AI could not answer', count('schoolai.models.AskedQuestion', status='UNSURE'),
         'schoolai:questions', 'mortarboard'),
    ]


def attention_for(request):
    """attention() for the person looking: administrators see everything; other staff on the office
    dashboard (account officers, librarians) only what leads to a page they can open."""
    from users.views_profile import can_manage, can_see_fees
    items = attention(request.tenant)
    if can_manage(request.user):
        return items
    from django.urls import resolve
    from dashboard import menu
    role = menu.restricted_role(request.user, request.tenant)
    hidden = menu.not_shown(request)
    open_pages = {i['name'] for g in menu.sections_for(role) for i in g['items'] if i['name'] not in hidden} if role else set()
    if can_see_fees(request.user):
        open_pages.add('finance:admin_dashboard')
    return [a for a in items if resolve(a['url']).view_name in open_pages]


def attention(tenant):
    """What is waiting for an administrator, most urgent first: [{'label', 'count', 'url', 'icon', 'tone'}]."""
    from django.urls import NoReverseMatch, reverse
    order = {'red': 0, 'amber': 1, 'blue': 2}
    out = []
    for tone, label, how, page, icon in _attention_items():
        try:
            n = how(tenant)
            url = reverse(page)
        except (NoReverseMatch, Exception):
            continue                                    # a module that is not set up must not hide the rest
        if n:
            out.append({'label': label, 'count': n, 'url': url, 'icon': icon, 'tone': tone})
    return sorted(out, key=lambda a: (order[a['tone']], -a['count']))


def staff_in_today(tenant):
    from idcards.models import StaffClockLog
    from users.models import Staff
    today = timezone.localdate()
    came = StaffClockLog.objects.filter(tenant=tenant, direction='IN', scanned_at__date=today).values('staff').distinct().count()
    total = Staff.objects.filter(tenant=tenant).count()
    return {'in': came, 'total': total, 'rate': round(100 * came / total) if total else None}


def sections(tenant):
    """Each section (campus) side by side: students, attendance (30 days) and fee collection this term."""
    from analytics import metrics
    from saas_admin.models import Campus
    from users.models import Attendance
    since = timezone.localdate() - timedelta(days=30)
    by_class = {r['class_id']: r for r in metrics.collection_by_class(tenant) if r['class_id']}
    out = []
    for c in Campus.objects.filter(tenant=tenant, active=True):
        ids = list(c.classes.values_list('pk', flat=True))
        roll = metrics.on_roll(tenant).filter(class_assigned_id__in=ids).count()
        att = Attendance.objects.filter(tenant=tenant, school_class_id__in=ids, subject__isnull=True, date__gte=since)
        total = att.count()
        expected = sum((by_class[i]['expected'] for i in ids if i in by_class), ZERO)
        paid = sum((by_class[i]['paid'] for i in ids if i in by_class), ZERO)
        out.append({'name': c.name, 'students': roll, 'attendance': metrics.pct(att.filter(status='P').count(), total),
                    'fees': metrics.pct(paid, expected), 'outstanding': expected - paid})
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Super admin — the command centre
# ─────────────────────────────────────────────────────────────────────────────

def super_admin(request):
    from analytics import metrics
    from security import platform
    from security.models import AuditEvent, LoginFailure, UserSession
    from users.models import Class, Staff, Student, User
    t = request.tenant
    now = timezone.now()
    checks = safe(lambda: platform.health(t), [])
    return {
        'people': safe(lambda: {
            'students': metrics.on_roll(t).count(), 'staff': Staff.objects.filter(tenant=t).count(),
            'parents': User.objects.filter(tenant=t, is_parent=True, is_active=True).count(),
            'classes': Class.objects.filter(tenant=t).count(),
            'online': UserSession.objects.filter(tenant=t, ended_at__isnull=True, last_seen__gte=now - timedelta(minutes=15)).count(),
        }, {}),
        'sections': safe(lambda: sections(t), []),
        'attendance': safe(lambda: metrics.attendance_today(t)),
        'health': checks, 'health_counts': platform.summary(checks) if checks else {},
        'backup': safe(platform.last_good), 'backup_fresh': safe(lambda: not platform.backup_due(), False),
        'security': safe(lambda: {
            'failed_24h': LoginFailure.objects.filter(at__gte=now - timedelta(hours=24)).count(),
            'denied_7d': AuditEvent.objects.filter(tenant=t, action='DENIED', at__gte=now - timedelta(days=7)).count(),
            'events_24h': AuditEvent.objects.filter(tenant=t, at__gte=now - timedelta(hours=24)).count(),
            'recent': list(AuditEvent.objects.filter(tenant=t).exclude(action__in=('LOGIN', 'LOGOUT'))[:6]),
        }, {}),
        'approvals': safe(lambda: attention(t), []),
        'ai': safe(lambda: ai_maturity(t)), 'risk': safe(lambda: ai_risk(t)),
        'activity': safe(lambda: metrics.activity(t, limit=10), []),
        'fees': safe(lambda: metrics.fee_position(t)),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Admin (principal / head) — the school today
# ─────────────────────────────────────────────────────────────────────────────

def admin(request):
    from analytics import metrics
    t = request.tenant
    today = timezone.localdate()

    def lessons():
        from timetable.lessons import lessons_on
        rows = lessons_on(t, today)
        now = timezone.localtime().time()
        done = [r for r in rows if r['slot'].time_slot.end_time <= now]
        return {'total': len(rows), 'taken': sum(1 for r in done if r['taken']), 'due': len(done),
                'missed': [r for r in done if not r['taken']][:6]}

    def welfare():
        out = {}
        try:
            from welfare.models import Concern
            out['concerns'] = Concern.objects.filter(tenant=t).exclude(status__in=('CLOSED', 'RESOLVED')).count()
        except Exception:
            pass
        try:
            from hostel.models import SickBayVisit
            out['sick_bay'] = SickBayVisit.objects.filter(tenant=t, discharged_at__isnull=True).count()
        except Exception:
            pass
        return out

    return {
        'attendance': safe(lambda: metrics.attendance_today(t)),
        'attendance_30': safe(lambda: metrics.attendance_rate(t)),
        'staff_in': safe(lambda: staff_in_today(t)),
        'lessons': safe(lessons),
        'approvals': safe(lambda: attention_for(request), []),
        'welfare': safe(welfare, {}),
        'results': safe(lambda: metrics.results_summary(t)),
        'risk': safe(lambda: ai_risk(t)),
        'fees': safe(lambda: metrics.fee_position(t)),
        'collections': safe(lambda: metrics.collections(t, weeks=6)),
        'events': safe(lambda: events(t), []),
        'kpis': safe(lambda: metrics.kpis(t), []),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Proprietor — the owner's view
# ─────────────────────────────────────────────────────────────────────────────

def proprietor(request):
    from analytics import metrics
    t = request.tenant
    session, term = metrics.active_period(t)

    def money():
        from finance.models import Expense, Payment
        start = (term.start_date if term and term.start_date else timezone.localdate() - timedelta(days=90))
        income = Payment.objects.filter(tenant=t, status=Payment.APPROVED, created_at__date__gte=start) \
            .aggregate(s=Sum('amount'))['s'] or ZERO
        spent = Expense.objects.filter(tenant=t, status__in=(Expense.APPROVED, Expense.PAID), spent_on__gte=start) \
            .aggregate(s=Sum('amount'))['s'] or ZERO
        return {'since': start, 'income': income, 'expenses': spent, 'net': income - spent}

    def enrolment():
        points, ahead = metrics.enrolment_series(t, months=12)
        moves = metrics.joiners_and_leavers(t, months=12)
        now = points[-1]['count'] if points else 0
        return {'series': points, 'forecast': ahead, 'now': now, 'change': now - (points[0]['count'] if points else 0),
                'joined': sum(m['joined'] for m in moves), 'left': sum(m['left'] for m in moves),
                'retention': metrics.retention(t)}

    def staff_mix():
        from users.models import Staff
        return list(Staff.objects.filter(tenant=t).values('role').annotate(n=Count('id')).order_by('-n'))

    return {
        'session': session, 'term': term, 'progress': term_progress(term),
        'fees': safe(lambda: metrics.fee_position(t)),
        'collections': safe(lambda: metrics.collections(t, weeks=10)),
        'ageing': safe(lambda: metrics.debt_ageing(t), []),
        'money': safe(money),
        'enrolment': safe(enrolment),
        'results': safe(lambda: metrics.results_summary(t)),
        'kpis': safe(lambda: metrics.kpis(t), []),
        'sections': safe(lambda: sections(t), []),
        'attendance_30': safe(lambda: metrics.attendance_rate(t)),
        'staff_mix': safe(staff_mix, []),
        'ai': safe(lambda: ai_maturity(t)), 'risk': safe(lambda: ai_risk(t)),
        'by_class': safe(lambda: sorted(metrics.collection_by_class(t), key=lambda r: (r['rate'] is None, r['rate'] or 0)), []),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Teacher — my teaching day
# ─────────────────────────────────────────────────────────────────────────────

def teacher(request, staff):
    t = request.tenant
    today = timezone.localdate()
    now = timezone.localtime().time()

    def day():
        from timetable.lessons import lessons_on
        rows = lessons_on(t, today, teacher=staff)
        current = next((r for r in rows if r['slot'].time_slot.start_time <= now < r['slot'].time_slot.end_time), None)
        upcoming = next((r for r in rows if r['slot'].time_slot.start_time > now), None)
        for r in rows:
            r['state'] = ('now' if r is current else 'done' if r['slot'].time_slot.end_time <= now else 'later')
        return {'rows': rows, 'now': current, 'next': upcoming}

    def classes():
        from teaching.services import my_classes
        from users.models import Attendance, ENROLLED_STATUSES, Student
        out = []
        for c in my_classes(staff):
            roll = Student.objects.filter(tenant=t, class_assigned=c, status__in=ENROLLED_STATUSES).count()
            marks = Attendance.objects.filter(tenant=t, school_class=c, date=today, subject__isnull=True)
            present = marks.filter(status='P').count()
            out.append({'cls': c, 'roll': roll, 'marked': marks.exists(), 'present': present,
                        'form': c.has_form_teacher(staff)})
        return out

    def queue():
        from teaching.services import marking_queue
        q = marking_queue(staff, request.user, t)
        return {'total': q['total'], 'submissions': sum(len(a['subs']) for a in q['assignments']),
                'notes': len(q['notes']), 'blanks': len(q['blanks']), 'tasks': q['tasks'][:5]}

    def deadline():
        from results.locks import lock_for
        from academics.models import Term
        term = Term.objects.filter(tenant=t, is_active=True).first()
        lock = lock_for(term)
        if not lock:
            return None
        return {'date': lock.locks_on, 'days': (lock.locks_on - today).days, 'locked': lock.is_locked}

    def exams():
        from cbt.models import Exam
        return list(Exam.objects.filter(tenant=t, created_by=staff, end_time__gte=timezone.now())
                    .select_related('subject').order_by('start_time')[:5])

    def homework():
        from curriculum.models import Homework
        return list(Homework.objects.filter(tenant=t, set_by=staff, due_on__gte=today)
                    .select_related('school_class', 'subject').order_by('due_on')[:5])

    def chat_unread():
        from staffchat import chat
        return chat.unread_total(request.user, t)

    form_ids = []
    try:
        from users.models import Class
        form_ids = list(Class.objects.filter(tenant=t, form_teachers=staff).values_list('pk', flat=True))
    except Exception:
        pass
    return {
        'day': safe(day, {'rows': [], 'now': None, 'next': None}),
        'classes': safe(classes, []),
        'queue': safe(queue),
        'deadline': safe(deadline),
        'exams': safe(exams, []),
        'homework': safe(homework, []),
        'risk': safe(lambda: ai_risk(t, form_ids), None) if form_ids else None,
        'chat_unread': safe(chat_unread, 0),
        'events': safe(lambda: events(t, n=4), []),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Student — my learning hub
# ─────────────────────────────────────────────────────────────────────────────

def _results_trend(student):
    from results.models import TermResult
    per = defaultdict(list)
    names = {}
    for r in (TermResult.objects.filter(student=student, published=True, is_not_offering=False)
              .select_related('term', 'session')):
        key = (r.session.name, r.term.start_date or r.term.name, r.term_id)
        per[key].append(r.total_score or 0)
        names[key] = f'{r.term.name.split()[0]} {r.session.name[-4:]}'
    rows = [{'label': names[k], 'average': round(sum(v) / len(v), 1)} for k, v in sorted(per.items(), key=lambda kv: (kv[0][0], str(kv[0][1])))]
    return rows[-8:]


def _attendance_term(student, term):
    from users.models import Attendance
    qs = Attendance.objects.filter(student=student, subject__isnull=True)
    if term and term.start_date:
        qs = qs.filter(date__gte=term.start_date)
    total = qs.count()
    return {'rate': round(100 * qs.filter(status='P').count() / total) if total else None, 'days': total,
            'absent': qs.filter(status='A').count()}


def _homework(student, days=10):
    from curriculum.models import Homework
    today = timezone.localdate()
    if not student.class_assigned_id:
        return []
    return list(Homework.objects.filter(tenant=student.tenant, school_class_id=student.class_assigned_id,
                                        due_on__gte=today, due_on__lte=today + timedelta(days=days))
                .select_related('subject').order_by('due_on')[:6])


def _practicals(student):
    from labs.models import PracticalRecord
    recs = PracticalRecord.objects.filter(student=student, session__status='DONE')
    total = recs.exclude(attendance='E').count()
    return {'total': total, 'attended': recs.filter(attendance__in=('P', 'L')).count()}


def student(request, student_obj, term):
    t = request.tenant
    today = timezone.localdate()

    def lessons():
        from timetable.lessons import lessons_on
        if not student_obj.class_assigned_id:
            return []
        now = timezone.localtime().time()
        rows = lessons_on(t, today, school_class=student_obj.class_assigned)
        for r in rows:
            ts = r['slot'].time_slot
            r['state'] = 'now' if ts.start_time <= now < ts.end_time else 'done' if ts.end_time <= now else 'later'
        return rows

    def quiz():
        from cbt import practice
        return practice.stats(student_obj)

    return {
        'lessons': safe(lessons, []),
        'homework': safe(lambda: _homework(student_obj), []),
        'trend': safe(lambda: _results_trend(student_obj), []),
        'att': safe(lambda: _attendance_term(student_obj, term)),
        'quiz': safe(quiz),
        'practicals': safe(lambda: _practicals(student_obj)),
        'moments': safe(lambda: moments(request.user, t, 3), []),
        'events': safe(lambda: events(t, staff=False, n=4), []),
        'progress': term_progress(term),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Parent — my children
# ─────────────────────────────────────────────────────────────────────────────

def _fees(student_obj, term):
    from finance.models import ChildBilling
    from finance.views import _get_family_wallet_for_student
    bill = None
    if term:
        bill = ChildBilling.objects.filter(student=student_obj, parent_billing__term=term).first()
    wallet = _get_family_wallet_for_student(student_obj)
    return {'due': bill.amount_due if bill else None, 'paid': bill.paid if bill else None,
            'balance': bill.balance if bill else None, 'wallet': wallet.balance if wallet else None}


def _latest_average(student_obj):
    trend = _results_trend(student_obj)
    return trend[-1]['average'] if trend else None


def parent(request, children, student_obj, term):
    t = request.tenant

    def to_sign():
        from communications.models import ConsentForm, ConsentResponse, Survey, SurveyResponse
        today = timezone.localdate()
        out = []
        for f in ConsentForm.objects.filter(tenant=t, closed=False, deadline__gte=today):
            if (not f.classes.exists() or f.classes.filter(pk=student_obj.class_assigned_id).exists()) and \
                    not ConsentResponse.objects.filter(form=f, student=student_obj).exists():
                out.append({'title': f.title, 'due': f.deadline, 'url': '/communications/consent/', 'kind': 'Consent form'})
        for s in Survey.objects.filter(tenant=t, published=True, closes__gte=today, audience__in=('PARENTS', 'BOTH')):
            if not SurveyResponse.objects.filter(survey=s, user=request.user).exists():
                out.append({'title': s.title, 'due': s.closes, 'url': f'/communications/surveys/{s.pk}/', 'kind': 'Survey'})
        return out[:5]

    cards = []
    for c in children:
        cards.append({'student': c, 'att': safe(lambda c=c: _attendance_term(c, term)),
                      'average': safe(lambda c=c: _latest_average(c)),
                      'fees': safe(lambda c=c: _fees(c, term))})
    return {
        'cards': cards,
        'att': safe(lambda: _attendance_term(student_obj, term)),
        'trend': safe(lambda: _results_trend(student_obj), []),
        'fees': safe(lambda: _fees(student_obj, term)),
        'homework': safe(lambda: _homework(student_obj, 14), []),
        'to_sign': safe(to_sign, []),
        'events': safe(lambda: events(t, staff=False, n=5), []),
        'moments': safe(lambda: moments(request.user, t, 3), []),
        'practicals': safe(lambda: _practicals(student_obj)),
        'progress': term_progress(term),
    }
