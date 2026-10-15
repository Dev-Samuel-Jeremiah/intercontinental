"""
assistant/ask.py
────────────────
Questions about school data in plain English (F167), e.g.
"How many JSS2 students owe more than ₦50,000?"

The AI never touches the database. It only chooses one of the safe reports
below and fills in its settings; the report itself is ordinary Django code,
scoped to this school, that checks every setting before it runs. Fee reports
are only offered to people allowed to see fees.
"""
import json
import re
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from django.utils import timezone

from users.models import ENROLLED_STATUSES, Attendance, Class, Staff, Student, Subject

from .providers import AIError, complete

MAX_ROWS = 200


class Report:
    def __init__(self, title, columns, rows, summary=''):
        self.title, self.columns, self.rows, self.summary = title, columns, rows[:MAX_ROWS], summary
        self.total_rows = len(rows)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers for settings the AI fills in
# ─────────────────────────────────────────────────────────────────────────────

def _squash(text):
    return re.sub(r'[\s\-_/]', '', str(text or '')).lower()


def classes_matching(tenant, name):
    """'JSS2' matches JSS 2A and JSS 2B; 'SS 3 Gold' matches just that class. None/'' = every class."""
    if not name:
        return None
    key = _squash(name)
    classes = list(Class.objects.filter(tenant=tenant))
    exact = [c for c in classes if _squash(c.name) == key]
    if exact:
        return exact
    found = [c for c in classes if _squash(c.name).startswith(key) or _squash(c.level) == key]
    if not found:
        raise AIError(f'There is no class called "{name}".')
    return found


def _students(tenant, class_name):
    qs = Student.objects.filter(tenant=tenant, status__in=ENROLLED_STATUSES).select_related('class_assigned')
    classes = classes_matching(tenant, class_name)
    return qs.filter(class_assigned__in=classes) if classes is not None else qs


def _number(value, default=0, low=0, high=10 ** 9):
    try:
        n = Decimal(str(value).replace(',', '').replace('₦', '').strip()) if value not in (None, '') else Decimal(default)
    except (InvalidOperation, ValueError):
        n = Decimal(default)
    return max(Decimal(low), min(Decimal(high), n))


def _day(value):
    today = timezone.localdate()
    v = str(value or 'today').strip().lower()
    if v == 'today':
        return today
    if v == 'yesterday':
        return today - timedelta(days=1)
    try:
        return datetime.strptime(v, '%Y-%m-%d').date()
    except ValueError:
        raise AIError(f'I could not read the date "{value}". Try a date like 2026-10-05, today or yesterday.')


def _where(class_name):
    return f' in {class_name}' if class_name else ''


# ─────────────────────────────────────────────────────────────────────────────
# The safe reports
# ─────────────────────────────────────────────────────────────────────────────

def students_owing(tenant, min_amount=0, class_name=None):
    from academics.models import AcademicSession, Term
    from finance.models import ChildBilling
    session = AcademicSession.objects.filter(tenant=tenant, is_active=True).first()
    term = Term.objects.filter(tenant=tenant, is_active=True).first()
    if not (session and term):
        raise AIError('Set the current session and term first — fee balances are worked out per term.')
    floor = _number(min_amount)
    students = _students(tenant, class_name)
    rows = []
    for b in ChildBilling.objects.filter(tenant=tenant, parent_billing__session=session, parent_billing__term=term,
                                         student__in=students).select_related('student__class_assigned'):
        if b.balance > floor and b.balance > 0:
            rows.append([b.student.full_name, b.student.class_assigned.name if b.student.class_assigned else '',
                         f'₦{b.balance:,.2f}', b.balance])
    rows.sort(key=lambda r: -r[3])
    total = sum((r[3] for r in rows), Decimal('0'))
    return Report(f'Students{_where(class_name)} owing more than ₦{floor:,.0f} ({term.name})',
                  ['Student', 'Class', 'Balance'], [r[:3] for r in rows],
                  f'{len(rows)} student{"s" if len(rows) != 1 else ""} owe ₦{total:,.2f} in total.')


def student_count(tenant, class_name=None, gender=None):
    qs = _students(tenant, class_name)
    if gender in ('Male', 'Female'):
        qs = qs.filter(gender=gender)
    counts = defaultdict(lambda: [0, 0, 0])
    for s in qs:
        c = counts[s.class_assigned.name if s.class_assigned else 'No class']
        c[0] += 1
        c[1 if s.gender == 'Male' else 2 if s.gender == 'Female' else 0] += s.gender in ('Male', 'Female')
    rows = [[name, v[0], v[1], v[2]] for name, v in sorted(counts.items())]
    total = sum(r[1] for r in rows)
    label = f'{gender.lower()} ' if gender in ('Male', 'Female') else ''
    return Report(f'Number of {label}students{_where(class_name)}', ['Class', 'Students', 'Boys', 'Girls'], rows,
                  f'{total} {label}student{"s" if total != 1 else ""} on roll.')


def absent_on(tenant, date='today', class_name=None):
    day = _day(date)
    students = _students(tenant, class_name)
    rows = [[a.student.full_name, a.school_class.name, a.get_absence_reason_display() if a.absence_reason else '—']
            for a in Attendance.objects.filter(tenant=tenant, date=day, status='A', subject__isnull=True,
                                               student__in=students).select_related('student', 'school_class')
            .order_by('school_class__name', 'student__full_name')]
    return Report(f'Absent on {day:%A %d %B}{_where(class_name)}', ['Student', 'Class', 'Reason'], rows,
                  f'{len(rows)} absent.')


def low_attendance(tenant, below_percent=80, days=30, class_name=None):
    below = float(_number(below_percent, 80, 1, 100))
    days = int(_number(days, 30, 7, 366))
    since = timezone.localdate() - timedelta(days=days)
    students = {s.id: s for s in _students(tenant, class_name)}
    tally = defaultdict(lambda: [0, 0])
    for a in Attendance.objects.filter(tenant=tenant, student_id__in=students, subject__isnull=True,
                                       date__gte=since).values('student_id', 'status'):
        tally[a['student_id']][1] += 1
        tally[a['student_id']][0] += a['status'] == 'P'
    rows = []
    for sid, (present, total) in tally.items():
        pct = 100 * present / total if total else 100
        if pct < below:
            s = students[sid]
            rows.append([s.full_name, s.class_assigned.name if s.class_assigned else '', f'{pct:.0f}%',
                         f'{present} of {total}', pct])
    rows.sort(key=lambda r: r[4])
    return Report(f'Attendance below {below:.0f}% in the last {days} days{_where(class_name)}',
                  ['Student', 'Class', 'Attendance', 'Days present'], [r[:4] for r in rows],
                  f'{len(rows)} student{"s" if len(rows) != 1 else ""}.')


def ranking(tenant, class_name=None, order='top', count=10, subject=None):
    from results.models import TermResult
    count = int(_number(count, 10, 1, 100))
    students = _students(tenant, class_name)
    qs = TermResult.objects.filter(tenant=tenant, student__in=students)
    subj = None
    if subject:
        subj = Subject.objects.filter(tenant=tenant, name__iexact=str(subject).strip()).first() or \
            Subject.objects.filter(tenant=tenant, name__icontains=str(subject).strip()).first()
        if not subj:
            raise AIError(f'There is no subject called "{subject}".')
        qs = qs.filter(subject=subj)
    latest = qs.order_by('-session__name', '-term__start_date', '-term_id').values('session_id', 'term_id').first()
    if not latest:
        return Report('No results yet', ['Student'], [], 'No term results have been entered for these students.')
    scores = defaultdict(list)
    names = {}
    for r in qs.filter(**latest).select_related('student__class_assigned', 'term'):
        scores[r.student_id].append(r.total_score or 0)
        names[r.student_id] = (r.student.full_name, r.student.class_assigned.name if r.student.class_assigned else '',
                               r.term.name)
    rows = sorted(([names[s][0], names[s][1], round(sum(v) / len(v), 1)] for s, v in scores.items()),
                  key=lambda r: r[2], reverse=(order != 'bottom'))[:count]
    term = next(iter(names.values()))[2]
    what = f' in {subj.name}' if subj else ''
    return Report(f'{"Lowest" if order == "bottom" else "Highest"} {count}{what}{_where(class_name)} — {term}',
                  ['Student', 'Class', 'Average' if not subj else 'Score'], rows)


def staff_list(tenant, role=None):
    roles = dict(Staff.ROLE_CHOICES)
    qs = Staff.objects.filter(tenant=tenant).order_by('full_name')
    code = None
    if role:
        code = next((k for k, v in roles.items() if str(role).lower() in (k.lower(), v.lower())), None)
        if code:
            qs = qs.filter(role=code)
    rows = [[s.full_name, roles.get(s.role, s.role)] for s in qs]
    return Report('Staff' + (f' — {roles.get(code, "")}' if role and code else ''), ['Name', 'Role'], rows,
                  f'{len(rows)} staff.')


def birthdays(tenant, days=7, class_name=None):
    days = int(_number(days, 7, 1, 60))
    today = timezone.localdate()
    rows = []
    for s in _students(tenant, class_name).exclude(date_of_birth__isnull=True):
        try:
            nxt = s.date_of_birth.replace(year=today.year)
        except ValueError:            # 29 February
            nxt = s.date_of_birth.replace(year=today.year, day=28)
        if nxt < today:
            try:
                nxt = nxt.replace(year=today.year + 1)
            except ValueError:
                nxt = nxt.replace(year=today.year + 1, day=28)
        if (nxt - today).days <= days:
            rows.append([s.full_name, s.class_assigned.name if s.class_assigned else '', nxt.strftime('%a %d %b'),
                         nxt])
    rows.sort(key=lambda r: r[3])
    return Report(f'Birthdays in the next {days} days{_where(class_name)}', ['Student', 'Class', 'Birthday'],
                  [r[:3] for r in rows])


TOOLS = {
    'students_owing': (students_owing, True, 'Students who owe fees this term. Settings: min_amount (naira, number), '
                                             'class_name.'),
    'student_count': (student_count, False, 'How many students, by class, with boys and girls. Settings: class_name, '
                                            'gender ("Male" or "Female").'),
    'absent_on': (absent_on, False, 'Students absent on a day. Settings: date ("today", "yesterday" or YYYY-MM-DD), '
                                    'class_name.'),
    'low_attendance': (low_attendance, False, 'Students whose attendance is below a percentage. Settings: '
                                              'below_percent, days, class_name.'),
    'ranking': (ranking, False, 'Best or weakest students by latest term average or in one subject. Settings: '
                                'order ("top" or "bottom"), count, class_name, subject.'),
    'staff_list': (staff_list, False, 'Staff and their roles. Settings: role (Teacher, Administrator, Account '
                                      'Officer, Librarian).'),
    'birthdays': (birthdays, False, 'Students with birthdays coming up. Settings: days, class_name.'),
}


def catalogue(may_see_fees):
    return {k: v[2] for k, v in TOOLS.items() if may_see_fees or not v[1]}


def answer(tenant, user, question, may_see_fees=False):
    """(Report, tool name, settings) for a question. Raises AIError with a helpful message."""
    from .models import AdminQuery
    question = (question or '').strip()[:500]
    if len(question) < 4:
        raise AIError('Type a question, e.g. "How many JSS2 students owe more than ₦50,000?"')
    tools = catalogue(may_see_fees)
    classes = list(Class.objects.filter(tenant=tenant).order_by('name').values_list('name', flat=True))[:80]
    prompt = f"""A school administrator asked: "{question}"

Today is {timezone.localdate():%Y-%m-%d}. The school's classes: {', '.join(classes) or 'none yet'}.
You can answer only by choosing ONE of these reports and its settings:
{json.dumps(tools, indent=1)}

Return a JSON object: {{"tool": "<report name>", "settings": {{...}}}}.
Leave out settings that the question doesn't mention. If no report fits, return
{{"tool": null, "reply": "<one sentence on what you can answer instead>"}}."""
    data = complete(tenant, 'admin_query', 'You turn questions into report choices. You never invent data.', prompt,
                    user=user, json_mode=True, max_tokens=300, temperature=0).data
    if not isinstance(data, dict):
        raise AIError('I could not understand that — try asking another way.')
    tool = data.get('tool')
    if not tool:
        raise AIError(str(data.get('reply') or 'I can\'t answer that from the school\'s records yet.')[:300])
    if tool not in tools:
        if tool in TOOLS:
            raise AIError('That needs fee access, which your account doesn\'t have.')
        raise AIError('I could not match that to a report — try asking another way.')
    func = TOOLS[tool][0]
    allowed = func.__code__.co_varnames[1:func.__code__.co_argcount]
    params = {k: v for k, v in (data.get('settings') or {}).items() if k in allowed and v not in (None, '')}
    try:
        report = func(tenant, **params)
    except TypeError:
        raise AIError('I could not match that to a report — try asking another way.')
    AdminQuery.objects.create(tenant=tenant, user=user, question=question, tool=tool,
                              params={k: str(v) for k, v in params.items()}, rows=report.total_rows)
    return report, tool, params
