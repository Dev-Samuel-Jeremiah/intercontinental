"""
teaching/services.py
────────────────────
    F303  class teacher dashboard (facts for a form class)
    F304  gradebook with weighted categories, sent to the report card
    F305  behaviour points (positive ones can add house points)
    F307  marking queue
    F308  comment bank, applied to report remarks
    F309  resource library
    F311  task list (teachers' own, or set by administrators)
    F312  duty roster with fair rotation and reminders
    F313  handover notes
"""
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Count, F, Q, Sum
from django.utils import timezone
from django.utils.html import escape

from users.models import ENROLLED_STATUSES, Staff, StaffSubjectClass, Student

from .models import (CATEGORIES, BehaviourEntry, CommentTemplate, DutyAssignment, DutyPost, Gradebook, GradeItem,
                     GradeMark, HandoverNote, Resource, TeacherTask)

ZERO = Decimal('0')


class TeachError(ValueError):
    pass


def _email(to, subject, html):
    from communications.services import send_email
    return send_email(to, subject, html)


def is_admin(user):
    from users.views_profile import can_manage
    return can_manage(user)


def check_file(upload):
    from users import records
    try:
        return records.check_upload(upload)
    except records.RecordError as problem:
        raise TeachError(str(problem))


def current_term(tenant):
    from academics.models import Term
    today = timezone.localdate()
    qs = Term.objects.filter(tenant=tenant).select_related('session')
    return (qs.filter(is_active=True).first() or qs.filter(start_date__lte=today, end_date__gte=today).first()
            or qs.order_by('-start_date').first())


def class_students(cls):
    return list(Student.objects.filter(class_assigned=cls, status__in=ENROLLED_STATUSES).order_by('full_name'))


def teaches(staff, cls, subject=None):
    q = StaffSubjectClass.objects.filter(staff=staff, school_class=cls)
    if subject:
        q = q.filter(subject=subject)
    return q.exists() or cls.has_form_teacher(staff)


def my_classes(staff):
    from users.models import Class
    ids = set(StaffSubjectClass.objects.filter(staff=staff).values_list('school_class_id', flat=True))
    ids |= set(Class.objects.filter(form_teachers=staff).values_list('pk', flat=True))
    return list(Class.objects.filter(pk__in=ids).order_by('name'))


# ─────────────────────────────────────────────────────────────────────────────
# F304 — gradebook
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_WEIGHTS = {'CLASSWORK': 20, 'HOMEWORK': 20, 'TEST': 60}


def gradebook_for(cls, subject, term, staff):
    from results.models import AssessmentScheme
    gb = Gradebook.objects.filter(school_class=cls, subject=subject, term=term).first()
    if gb:
        return gb
    scheme = AssessmentScheme.for_tenant(cls.tenant)
    cols = [c['key'] for c in scheme.ca_columns]
    mapping = dict(zip(['TEST', 'HOMEWORK', 'CLASSWORK'], cols))
    return Gradebook.objects.create(tenant_id=cls.tenant_id, school_class=cls, subject=subject, term=term,
                                    teacher=staff, weights=dict(DEFAULT_WEIGHTS), report_columns=mapping)


def set_weights(gb, data):
    from results.models import AssessmentScheme
    weights, columns = {}, {}
    valid_cols = {c['key'] for c in AssessmentScheme.for_tenant(gb.tenant).ca_columns}
    for key, _label in CATEGORIES:
        raw = str(data.get(f'w_{key}') or '0')
        if not raw.isdigit():
            raise TeachError('Weights are whole numbers.')
        if int(raw):
            weights[key] = int(raw)
        col = data.get(f'c_{key}') or ''
        if col and col in valid_cols:
            if col in columns.values():
                raise TeachError('Each report-card column can take only one category.')
            columns[key] = col
    if not weights:
        raise TeachError('Give at least one category a weight.')
    gb.weights, gb.report_columns = weights, columns
    gb.save(update_fields=['weights', 'report_columns'])


def add_item(gb, title, category, max_score, day):
    title = (title or '').strip()
    if not title:
        raise TeachError('Name the assessment.')
    if category not in dict(CATEGORIES):
        raise TeachError('Choose a category.')
    try:
        mx = Decimal(str(max_score))
    except (InvalidOperation, TypeError):
        raise TeachError('The maximum mark must be a number.')
    if mx <= 0:
        raise TeachError('The maximum mark must be above zero.')
    return GradeItem.objects.create(tenant_id=gb.tenant_id, gradebook=gb, title=title[:120], category=category,
                                    max_score=mx, date=day or timezone.localdate())


def save_marks(item, raw_by_student):
    """raw_by_student: {student_id: '7', 'x' (excused) or ''}. Returns problems (list of names)."""
    problems = []
    students = {s.pk: s for s in class_students(item.gradebook.school_class)}
    with transaction.atomic():
        for sid, raw in raw_by_student.items():
            st = students.get(int(sid))
            if not st:
                continue
            raw = (raw or '').strip().lower()
            excused, score = raw in ('x', 'ex', 'abs'), None
            if raw and not excused:
                try:
                    score = Decimal(raw)
                except InvalidOperation:
                    problems.append(st.full_name)
                    continue
                if score < 0 or score > item.max_score:
                    problems.append(st.full_name)
                    continue
            GradeMark.objects.update_or_create(item=item, student=st,
                                               defaults={'tenant_id': item.tenant_id, 'score': score,
                                                         'excused': excused})
    return problems


def standings(gb):
    """[{'student', 'cats': {cat: pct|None}, 'overall': pct|None}] and the items."""
    items = list(gb.items.all())
    marks = defaultdict(dict)
    for m in GradeMark.objects.filter(item__gradebook=gb):
        marks[m.student_id][m.item_id] = m
    rows = []
    for st in class_students(gb.school_class):
        got, out_of = defaultdict(lambda: ZERO), defaultdict(lambda: ZERO)
        for it in items:
            m = marks[st.pk].get(it.pk)
            if m and m.score is not None and not m.excused:
                got[it.category] += m.score
                out_of[it.category] += it.max_score
        cats = {c: (round(float(got[c] / out_of[c] * 100), 1) if out_of[c] else None) for c, _ in CATEGORIES}
        weight_used = sum(w for c, w in gb.weights.items() if cats.get(c) is not None)
        overall = round(sum(cats[c] * w for c, w in gb.weights.items() if cats.get(c) is not None) / weight_used, 1) \
            if weight_used else None
        rows.append({'student': st, 'cats': cats, 'overall': overall,
                     'marks': [marks[st.pk].get(it.pk) for it in items]})
    return items, rows


def push_to_report(gb):
    """Write each mapped category's percentage, scaled to the column's maximum, into the term result."""
    from results.models import AssessmentScheme, TermResult
    scheme = AssessmentScheme.for_tenant(gb.tenant)
    maxes = {c['key']: c['max'] for c in scheme.ca_columns}
    if not gb.report_columns:
        raise TeachError('Choose which report-card column each category goes into first.')
    _items, rows = standings(gb)
    n = 0
    for r in rows:
        values = {}
        for cat, col in gb.report_columns.items():
            pct = r['cats'].get(cat)
            if pct is not None and col in maxes:
                values[f'{col}_score'] = round(pct / 100 * maxes[col], 1)
        if not values:
            continue
        tr, _ = TermResult.objects.get_or_create(
            student=r['student'], subject=gb.subject, term=gb.term, session=gb.term.session,
            defaults={'tenant_id': gb.tenant_id, 'class_assigned': gb.school_class})
        if tr.published:
            continue
        for f, v in values.items():
            setattr(tr, f, v)
        tr.save()
        n += 1
    return n


# ─────────────────────────────────────────────────────────────────────────────
# F305 — behaviour points
# ─────────────────────────────────────────────────────────────────────────────

POSITIVE_KEYS = {k for k, _ in BehaviourEntry.POSITIVE}


def give_points(staff, students, category, note='', day=None, house_points=True):
    from users.models import HousePoint
    if category not in dict(BehaviourEntry.POSITIVE + BehaviourEntry.NEGATIVE):
        raise TeachError('Choose a reason.')
    if not students:
        raise TeachError('Choose at least one student.')
    positive = category in POSITIVE_KEYS
    day = day or timezone.localdate()
    label = dict(BehaviourEntry.POSITIVE + BehaviourEntry.NEGATIVE)[category]
    out = []
    for st in students:
        out.append(BehaviourEntry.objects.create(tenant_id=st.tenant_id, student=st, staff=staff, category=category,
                                                 points=1 if positive else -1, note=(note or '')[:255], date=day))
        if positive and house_points and st.house_id:
            HousePoint.objects.create(tenant_id=st.tenant_id, house_id=st.house_id, points=1, reason=label,
                                      student=st, awarded_by=staff.user)
    return out


def behaviour_summary(students, days=30):
    since = timezone.localdate() - timedelta(days=days)
    rows = BehaviourEntry.objects.filter(student__in=students, date__gte=since).values('student_id').annotate(
        plus=Sum('points', filter=Q(points__gt=0)), minus=Sum('points', filter=Q(points__lt=0)))
    by = {r['student_id']: r for r in rows}
    out = []
    for s in students:
        r = by.get(s.pk, {})
        plus, minus = r.get('plus') or 0, -(r.get('minus') or 0)
        out.append({'student': s, 'plus': plus, 'minus': minus, 'net': plus - minus})
    return out


# ─────────────────────────────────────────────────────────────────────────────
# F308 — comment bank
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_COMMENTS = [
    ('EXCELLENT', '{first} has done excellent work this term. Keep it up!'),
    ('EXCELLENT', 'An outstanding term. {first} is a hard-working and focused student.'),
    ('GOOD', '{first} has worked well this term and made good progress.'),
    ('GOOD', 'A good result. With a little more effort {first} can reach the top.'),
    ('FAIR', '{first} is capable of more. Regular revision will help.'),
    ('FAIR', 'Fair performance. {first} should ask questions in class when unsure.'),
    ('WEAK', '{first} needs to work much harder and complete all homework.'),
    ('WEAK', 'A weak result. {first} needs extra support and more practice at home.'),
    ('CONDUCT', '{first} is polite, helpful and well behaved.'),
    ('CONDUCT', '{first} must improve punctuality and attention in class.'),
]


def ensure_comments(tenant):
    if not CommentTemplate.objects.filter(tenant=tenant, staff__isnull=True).exists():
        for band, text in DEFAULT_COMMENTS:
            CommentTemplate.objects.create(tenant=tenant, band=band, text=text)


def comments_for(staff):
    return CommentTemplate.objects.filter(tenant_id=staff.tenant_id).filter(Q(staff__isnull=True) | Q(staff=staff))


def render_comment(text, student):
    first = (student.full_name or '').split()[0] if student.full_name else ''
    return (text or '').replace('{first}', first).replace('{name}', student.full_name or '').strip()[:255]


def band_for(total):
    if total is None:
        return None
    return 'EXCELLENT' if total >= 75 else 'GOOD' if total >= 60 else 'FAIR' if total >= 45 else 'WEAK'


def remarks_sheet(cls, subject, term):
    """Rows for applying comments: student, their total (if any), suggested band, current remark."""
    from results.models import SubjectTeacherRemark, TeacherRemark, TermResult
    students = class_students(cls)
    if subject:
        totals = {r.student_id: r.total_score for r in TermResult.objects.filter(
            student__in=students, subject=subject, term=term, session=term.session)}
        current = {r.student_id: r.remark for r in SubjectTeacherRemark.objects.filter(
            student__in=students, subject=subject, term=term, session=term.session)}
    else:
        totals = {}
        for r in TermResult.objects.filter(student__in=students, term=term, session=term.session,
                                           is_not_offering=False):
            totals.setdefault(r.student_id, []).append(r.total_score)
        totals = {k: round(sum(v) / len(v), 1) for k, v in totals.items()}
        current = {r.student_id: r.remark for r in TeacherRemark.objects.filter(
            student__in=students, term=term, session=term.session)}
    return [{'student': s, 'total': totals.get(s.pk), 'band': band_for(totals.get(s.pk)),
             'remark': current.get(s.pk, '')} for s in students]


def apply_remarks(staff, cls, subject, term, data, admin=False):
    """data: {student_id: text}; template ids in data as tpl_<student_id>. Returns remarks saved."""
    from results.models import SubjectTeacherRemark, TeacherRemark
    if subject and not (admin or teaches(staff, cls, subject)):
        raise TeachError('You do not teach this subject in this class.')
    if not subject and not (admin or cls.has_form_teacher(staff)):
        raise TeachError("Only the form teacher writes the class teacher's remark.")
    n = 0
    for st in class_students(cls):
        text = (data.get(f'r_{st.pk}') or '').strip()
        if not text:
            continue
        text = render_comment(text, st)
        if subject:
            SubjectTeacherRemark.objects.update_or_create(
                student=st, subject=subject, term=term, session=term.session,
                defaults={'tenant_id': st.tenant_id, 'remark': text})
        else:
            TeacherRemark.objects.update_or_create(student=st, term=term, session=term.session,
                                                   defaults={'tenant_id': st.tenant_id, 'remark': text})
        tpl = str(data.get(f't_{st.pk}') or '')
        if tpl.isdigit():
            CommentTemplate.objects.filter(pk=int(tpl), tenant_id=st.tenant_id).update(uses=F('uses') + 1)
        n += 1
    return n


# ─────────────────────────────────────────────────────────────────────────────
# F307 — marking queue
# ─────────────────────────────────────────────────────────────────────────────

def marking_queue(staff, user, tenant):
    from classroom.models import Submission
    from curriculum.services import notes_to_review
    subs = Submission.objects.filter(tenant=tenant, assignment__section__course__teacher=user,
                                     submitted_at__isnull=False, grade__isnull=True) \
        .select_related('assignment', 'student').order_by('submitted_at')
    by_assignment = defaultdict(list)
    for s in subs:
        by_assignment[s.assignment].append(s)
    notes = list(notes_to_review(user, tenant)) if staff else []
    blanks = []
    for gb in Gradebook.objects.filter(teacher=staff).select_related('school_class', 'subject'):
        size = len(class_students(gb.school_class))
        for it in gb.items.annotate(n=Count('marks', filter=Q(marks__score__isnull=False) | Q(marks__excused=True))):
            if it.n < size:
                blanks.append({'gradebook': gb, 'item': it, 'missing': size - it.n})
    tasks = list(TeacherTask.objects.filter(staff=staff, done=False, due_date__lte=timezone.localdate() +
                                            timedelta(days=7))) if staff else []
    return {'assignments': [{'assignment': a, 'subs': s, 'oldest': s[0].submitted_at} for a, s in by_assignment.items()],
            'notes': notes, 'blanks': blanks, 'tasks': tasks,
            'total': len(subs) + len(notes) + len(blanks) + len(tasks)}


# ─────────────────────────────────────────────────────────────────────────────
# F309 — resources
# ─────────────────────────────────────────────────────────────────────────────

def add_resource(staff, data, upload=None):
    from users.models import Subject
    title = (data.get('title') or '').strip()
    if not title:
        raise TeachError('Give the resource a title.')
    if data.get('kind') not in dict(Resource.KINDS):
        raise TeachError('Choose the kind of resource.')
    link = (data.get('link') or '').strip()
    if link and not link.startswith(('http://', 'https://')):
        raise TeachError('Links must start with http:// or https://')
    r = Resource(tenant_id=staff.tenant_id, title=title[:160], kind=data['kind'], link=link[:200],
                 subject=Subject.objects.filter(tenant_id=staff.tenant_id, pk=data.get('subject') or 0).first(),
                 level=(data.get('level') or '')[:40], description=(data.get('description') or '').strip(),
                 shared=bool(data.get('shared')), uploaded_by=staff)
    if upload:
        check_file(upload)
        r.file = upload
    if not (r.file or link):
        raise TeachError('Upload a file or give a link.')
    r.save()
    return r


def find_resources(staff, q='', subject=None, level='', kind='', saved=False):
    qs = Resource.objects.filter(tenant_id=staff.tenant_id).filter(Q(shared=True) | Q(uploaded_by=staff)) \
        .select_related('subject', 'uploaded_by')
    if saved:
        qs = qs.filter(saved_by=staff)
    if q:
        qs = qs.filter(Q(title__icontains=q) | Q(description__icontains=q))
    if subject:
        qs = qs.filter(subject=subject)
    if level:
        qs = qs.filter(level__iexact=level)
    if kind:
        qs = qs.filter(kind=kind)
    return qs


# ─────────────────────────────────────────────────────────────────────────────
# F311 — tasks
# ─────────────────────────────────────────────────────────────────────────────

def add_task(staff, title, due=None, priority='NORMAL', notes='', assigned_by=None):
    title = (title or '').strip()
    if not title:
        raise TeachError('Write the task.')
    t = TeacherTask.objects.create(tenant_id=staff.tenant_id, staff=staff, title=title[:200], due_date=due,
                                   priority=priority if priority in ('HIGH', 'NORMAL', 'LOW') else 'NORMAL',
                                   notes=(notes or '')[:2000], assigned_by=assigned_by)
    if assigned_by and staff.user.email and assigned_by != staff.user:
        _email(staff.user.email, f'New task: {t.title}',
               f'<p>{escape(assigned_by.get_full_name() or assigned_by.username)} gave you a task: '
               f'<b>{escape(t.title)}</b>{" — due " + t.due_date.strftime("%d %b %Y") if t.due_date else ""}.</p>'
               f'<p>{escape(t.notes)}</p>')
    return t


def toggle_task(task):
    task.done = not task.done
    task.done_at = timezone.now() if task.done else None
    task.save(update_fields=['done', 'done_at'])


# ─────────────────────────────────────────────────────────────────────────────
# F312 — duty roster
# ─────────────────────────────────────────────────────────────────────────────

def _on_leave(staff_ids, day, tenant):
    from safety.models import StaffLeave
    return set(StaffLeave.objects.filter(tenant=tenant, staff_id__in=staff_ids, status=StaffLeave.APPROVED,
                                         start_date__lte=day, end_date__gte=day).values_list('staff_id', flat=True))


def _overlap(a, b):
    return a.start_time < b.end_time and b.start_time < a.end_time


def generate_rota(tenant, start, end, staff):
    """Fill every active post on school days from start to end, fairly. Returns assignments created."""
    from academics.models import PublicHoliday
    if not start or not end or end < start:
        raise TeachError('Choose a start and an end date.')
    if (end - start).days > 120:
        raise TeachError('Make a rota for up to four months at a time.')
    staff = list(staff)
    if not staff:
        raise TeachError('Choose the staff to share the duties.')
    posts = list(DutyPost.objects.filter(tenant=tenant, active=True))
    if not posts:
        raise TeachError('Add at least one duty post first.')
    holidays = set(PublicHoliday.objects.filter(tenant=tenant, date__range=(start, end)).values_list('date', flat=True))
    ids = [s.pk for s in staff]
    load = defaultdict(int)
    for row in DutyAssignment.objects.filter(tenant=tenant, staff_id__in=ids).values('staff_id').annotate(n=Count('id')):
        load[row['staff_id']] = row['n']
    created = 0
    day = start
    while day <= end:
        if day.weekday() < 5 and day not in holidays:
            away = _on_leave(ids, day, tenant)
            today_posts = defaultdict(list)       # staff id -> posts already that day
            for a in DutyAssignment.objects.filter(tenant=tenant, date=day).select_related('post'):
                today_posts[a.staff_id].append(a.post)
            for post in posts:
                have = DutyAssignment.objects.filter(post=post, date=day).count()
                for _ in range(max(0, post.staff_needed - have)):
                    free = [s for s in staff if s.pk not in away and
                            not any(_overlap(post, p) for p in today_posts[s.pk])]
                    if not free:
                        break
                    pick = min(free, key=lambda s: (load[s.pk], len(today_posts[s.pk]), s.pk))
                    DutyAssignment.objects.create(tenant=tenant, post=post, staff=pick, date=day)
                    load[pick.pk] += 1
                    today_posts[pick.pk].append(post)
                    created += 1
        day += timedelta(days=1)
    return created


def send_duty_reminders(tenant, day):
    n = 0
    for a in DutyAssignment.objects.filter(tenant=tenant, date=day, reminded=False).select_related('post', 'staff__user'):
        if a.staff.user.email:
            _email(a.staff.user.email, f'Duty {day:%A %d %b}: {a.post.name}',
                   f'<p>You are on duty: <b>{escape(a.post.name)}</b>{", " + escape(a.post.location) if a.post.location else ""}'
                   f', {a.post.start_time:%-I:%M %p}–{a.post.end_time:%-I:%M %p} on {day:%A %d %B}.</p>')
        a.reminded = True
        a.save(update_fields=['reminded'])
        n += 1
    return n


def run_duty_reminders(day=None):
    from tenants.middleware import clear_current_tenant, set_current_tenant
    from tenants.models import Tenant
    day = day or timezone.localdate() + timedelta(days=1)
    total = 0
    for tenant in Tenant.objects.filter(status__in=('ACTIVE', 'TRIAL')):
        set_current_tenant(tenant)
        try:
            total += send_duty_reminders(tenant, day)
        finally:
            clear_current_tenant()
    return total


# ─────────────────────────────────────────────────────────────────────────────
# F313 — handover notes
# ─────────────────────────────────────────────────────────────────────────────

def create_handover(from_staff, data, upload=None):
    from users.models import Class, Subject
    to = Staff.objects.filter(tenant_id=from_staff.tenant_id, pk=data.get('to_staff') or 0).first()
    if not to or to.pk == from_staff.pk:
        raise TeachError('Choose the colleague taking over.')
    if data.get('kind') not in dict(HandoverNote.KINDS):
        raise TeachError('Choose the kind of handover.')
    if not any((data.get(k) or '').strip() for k in ('covered', 'next_steps', 'students', 'other')):
        raise TeachError('Write at least one section of the handover.')
    from datetime import datetime

    def d(raw):
        try:
            return datetime.strptime(raw or '', '%Y-%m-%d').date()
        except ValueError:
            return None
    h = HandoverNote(tenant_id=from_staff.tenant_id, from_staff=from_staff, to_staff=to, kind=data['kind'],
                     school_class=Class.objects.filter(tenant_id=from_staff.tenant_id,
                                                       pk=data.get('school_class') or 0).first(),
                     subject=Subject.objects.filter(tenant_id=from_staff.tenant_id, pk=data.get('subject') or 0).first(),
                     start=d(data.get('start')), end=d(data.get('end')),
                     covered=data.get('covered') or '', next_steps=data.get('next_steps') or '',
                     students=data.get('students') or '', other=data.get('other') or '')
    if upload:
        check_file(upload)
        h.file = upload
    h.save()
    if to.user.email:
        _email(to.user.email, f'Handover from {from_staff.full_name}',
               f'<p>{escape(from_staff.full_name)} has left you a handover note '
               f'({escape(h.get_kind_display().lower())}{", " + escape(h.school_class.name) if h.school_class else ""}).'
               f'</p><p>Open Teaching → Handover notes to read it.</p>')
    return h


# ─────────────────────────────────────────────────────────────────────────────
# F303 — class teacher dashboard
# ─────────────────────────────────────────────────────────────────────────────

def class_dashboard(cls):
    from users.models import Attendance
    today = timezone.localdate()
    students = class_students(cls)
    marks = {a.student_id: a for a in Attendance.objects.filter(student__in=students, date=today,
                                                                 subject__isnull=True)}
    since = today - timedelta(days=30)
    att = Attendance.objects.filter(student__in=students, date__gte=since, subject__isnull=True) \
        .values('student_id').annotate(total=Count('id'), present=Count('id', filter=Q(status='P')))
    rates = {r['student_id']: round(100 * r['present'] / r['total']) for r in att if r['total']}
    week_end = today + timedelta(days=7)
    birthdays = []
    for s in students:
        if s.date_of_birth:
            try:
                nxt = s.date_of_birth.replace(year=today.year)
            except ValueError:
                nxt = s.date_of_birth.replace(year=today.year, day=28)
            if today <= nxt <= week_end:
                birthdays.append((s, nxt))
    flagged = []
    try:
        from studentlife.models import Checkin
        flagged = list(Checkin.objects.filter(student__in=students, flagged=True, followed_up_at__isnull=True)
                       .select_related('student'))
    except Exception:
        pass
    from curriculum.models import Homework
    behaviour = behaviour_summary(students, days=7)
    return {
        'cls': cls, 'students': students, 'count': len(students),
        'marked': bool(marks), 'absent': [s for s in students if marks.get(s.pk) and marks[s.pk].status == 'A'],
        'late': [s for s in students if marks.get(s.pk) and marks[s.pk].minutes_late],
        'concerns': sorted([(s, rates[s.pk]) for s in students if s.pk in rates and rates[s.pk] < 85],
                           key=lambda x: x[1]),
        'birthdays': sorted(birthdays, key=lambda x: x[1]), 'flagged': flagged,
        'stars': sorted([b for b in behaviour if b['plus']], key=lambda b: -b['plus'])[:5],
        'watch': sorted([b for b in behaviour if b['minus'] >= 2], key=lambda b: -b['minus']),
        'homework': list(Homework.objects.filter(school_class=cls, due_on__range=(today, week_end))
                         .select_related('subject')),
    }
