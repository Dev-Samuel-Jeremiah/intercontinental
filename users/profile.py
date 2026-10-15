"""
users/profile.py
────────────────
Everything about one student, gathered for the 360° profile page: results
term by term, attendance, fees, siblings, status history, the change history,
and a single timeline of what has happened to them since they joined.

Each section reads only this school's records and copes with the parts a
school does not use — no results yet, no fees set up, no attendance taken.
Nothing here creates records; opening a profile never changes anything.
"""
from collections import defaultdict
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.db.models import Count, Q
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from users.models import Attendance, Student, StudentChange, StudentStatusChange


def _when(value):
    """A date or datetime as an aware datetime, so the timeline sorts cleanly."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if timezone.is_aware(value) else timezone.make_aware(value)
    return timezone.make_aware(datetime.combine(value, time(12, 0)))


def _url(name, *args, query=''):
    try:
        return reverse(name, args=args) + query
    except NoReverseMatch:
        return ''


def family_wallet(student):
    """The wallet the siblings share, without creating one just to look."""
    from finance.models import Wallet
    oldest = student
    if student.parent_email:
        oldest = (Student.objects.filter(tenant_id=student.tenant_id,
                                         parent_email=student.parent_email)
                  .order_by('id').first()) or student
    return Wallet.objects.filter(tenant_id=student.tenant_id, student=oldest).first()


def siblings(student):
    if not student.parent_email:
        return []
    return list(Student.objects.filter(tenant_id=student.tenant_id,
                                       parent_email=student.parent_email)
                .exclude(pk=student.pk).select_related('class_assigned').order_by('id'))


# ─────────────────────────────────────────────────────────────────────────────
# Results
# ─────────────────────────────────────────────────────────────────────────────

def results_history(student):
    """Per term: subjects, average, best and weakest subject — newest first."""
    from results.models import TermResult
    rows = (TermResult.objects.filter(tenant_id=student.tenant_id, student=student,
                                      is_not_offering=False)
            .select_related('term', 'session', 'subject', 'class_assigned'))
    terms = defaultdict(list)
    for r in rows:
        terms[(r.session_id, r.term_id)].append(r)
    out = []
    for (_, _), results in terms.items():
        first = results[0]
        best = max(results, key=lambda r: r.total_score)
        worst = min(results, key=lambda r: r.total_score)
        average = sum(r.total_score for r in results) / len(results)
        out.append({
            'session': first.session, 'term': first.term,
            'class_name': getattr(first.class_assigned, 'name', ''),
            'subjects': len(results), 'average': round(average, 1),
            'best': best, 'worst': worst,
            'published': all(r.published for r in results),
            'url': _url('results:term_results',
                        query=f'?class={first.class_assigned_id}&student={student.id}'
                              f'&term={first.term_id}&session={first.session_id}'),
        })
    # Newest session first, and within a session the latest term first.
    out.sort(key=lambda r: (r['session'].start_date or timezone.localdate().min,
                            r['term'].start_date or timezone.localdate().min,
                            r['term'].name), reverse=True)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Attendance
# ─────────────────────────────────────────────────────────────────────────────

def attendance_summary(student, term=None):
    """
    Present/absent counts this term and since joining — one mark per day, and
    only days that have happened (an absence a parent notified in advance is
    listed separately, not counted yet).
    """
    from users.attendance import daily_marks
    today = timezone.localdate()

    def counts(marks):
        p = sum(1 for m in marks.values() if m.status == 'P')
        a = sum(1 for m in marks.values() if m.status == 'A')
        return {'present': p, 'absent': a, 'days': p + a,
                'rate': round(100 * p / (p + a)) if (p + a) else None}

    overall = counts(daily_marks(student, end=today))
    this_term = None
    if term and term.start_date:
        this_term = counts(daily_marks(student, term.start_date, min(term.end_date or today, today)))
    past = daily_marks(student, end=today)
    recent_absences = [d for d in sorted(past, reverse=True) if past[d].status == 'A'][:5]
    upcoming = daily_marks(student, start=today + timedelta(days=1))
    planned = [m for d, m in sorted(upcoming.items()) if m.status == 'A']
    return {'overall': overall, 'term': this_term, 'recent_absences': recent_absences,
            'planned': planned}


# ─────────────────────────────────────────────────────────────────────────────
# Fees
# ─────────────────────────────────────────────────────────────────────────────

def fees_summary(student, session=None, term=None):
    from finance.models import ChildBilling, Payment, Refund
    wallet = family_wallet(student)
    billing = None
    if session and term:
        billing = (ChildBilling.objects.filter(tenant_id=student.tenant_id, student=student,
                                               parent_billing__session=session,
                                               parent_billing__term=term).first())
    payments = []
    if wallet:
        payments = list(Payment.objects.filter(tenant_id=student.tenant_id, wallet=wallet)
                        .order_by('-created_at')[:6])
    owing = (ChildBilling.objects.filter(tenant_id=student.tenant_id, student=student)
             .select_related('parent_billing__term', 'parent_billing__session'))
    arrears = [b for b in owing if b.balance > 0 and b != billing]
    return {
        'wallet': wallet, 'billing': billing, 'payments': payments,
        'shared_wallet': bool(wallet and wallet.student_id != student.id),
        'arrears': arrears,
        'arrears_total': sum((b.balance for b in arrears), Decimal('0.00')),
        'refunds': list(Refund.objects.filter(tenant_id=student.tenant_id, student=student)
                        .order_by('-created_at')[:5]),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Timeline
# ─────────────────────────────────────────────────────────────────────────────

STATUS_ICONS = {'Active': 'bi-person-check', 'Suspended': 'bi-pause-circle',
                'Withdrawn': 'bi-box-arrow-right', 'Graduated': 'bi-mortarboard',
                'Deceased': 'bi-flower1'}


def timeline(student, include_fees=True):
    """Every notable event, newest first."""
    events = []

    def add(when, icon, tone, title, detail='', url=''):
        when = _when(when)
        if when:
            events.append({'when': when, 'icon': icon, 'tone': tone, 'title': title,
                           'detail': detail, 'url': url})

    try:
        application = student.application      # set when they came through admissions
    except Exception:
        application = None
    if application:
        add(application.created_at, 'bi-file-earmark-text', 'gold',
            'Applied for admission', f'Application {application.reference}',
            _url('admissions:detail', application.pk))
    add(student.created_at, 'bi-person-plus', 'gold', 'Enrolled',
        f'Admission number {student.admission_number}'
        + (f' · {student.class_assigned.name}' if student.class_assigned else ''))

    for change in StudentStatusChange.objects.filter(tenant_id=student.tenant_id,
                                                     student=student).select_related('changed_by'):
        tone = {'Active': 'green', 'Suspended': 'amber', 'Graduated': 'gold'}.get(
            change.to_status, 'red')
        title = {'Active': 'Back on the roll', 'Suspended': 'Suspended',
                 'Withdrawn': 'Withdrew', 'Graduated': 'Graduated',
                 'Deceased': 'Passed away'}[change.to_status]
        detail = change.reason
        if change.until:
            detail = f'{detail} · until {change.until:%d %b %Y}'.strip(' ·')
        # Same-day changes use the moment they were recorded, so they sort after
        # anything earlier that day (such as enrolling) instead of at noon.
        when = (change.created_at if change.created_at and
                timezone.localdate(change.created_at) == change.effective_on
                else change.effective_on)
        add(when, STATUS_ICONS[change.to_status], tone, title, detail)

    for change in StudentChange.objects.filter(tenant_id=student.tenant_id, student=student,
                                               field='class_assigned'):
        verb = 'Promoted' if change.source == 'Promotion' else 'Moved class'
        add(change.changed_at, 'bi-arrow-up-right-circle', 'blue',
            f'{verb} to {change.new_value or "no class"}',
            f'from {change.old_value}' if change.old_value else '')

    for row in results_history(student):
        if row['published']:
            # Results carry no timestamp; date them by the end of their term, but
            # never in the future while the term is still running.
            when = row['term'].end_date or row['term'].start_date
            if when and when > timezone.localdate():
                when = timezone.localdate()
            add(when, 'bi-journal-check', 'blue',
                f'{row["term"].name} results — average {row["average"]}',
                f'{row["session"]} · best in {row["best"].subject.name}', row['url'])

    try:
        from finance.models import LateFeeCharge, Payment, Refund
        if include_fees:
            wallet = family_wallet(student)
            if wallet:
                for p in Payment.objects.filter(tenant_id=student.tenant_id, wallet=wallet,
                                                status=Payment.APPROVED):
                    add(p.created_at, 'bi-cash-coin', 'green',
                        f'Payment of ₦{p.amount:,.0f} received',
                        f'{p.get_method_display()} · {p.reference}'
                        + (' · family wallet' if wallet.student_id != student.id else ''))
            for r in Refund.objects.filter(tenant_id=student.tenant_id, student=student,
                                           status=Refund.PAID):
                add(r.paid_on, 'bi-arrow-counterclockwise', 'amber',
                    f'Refund of ₦{r.amount:,.0f}', r.get_reason_display())
            for c in LateFeeCharge.objects.filter(tenant_id=student.tenant_id,
                                                  child_billing__student=student, waived=False):
                add(c.charged_on, 'bi-alarm', 'red', f'Late payment charge ₦{c.amount:,.0f}',
                    c.rule.name)
    except Exception:
        pass   # finance not in use at this school

    events.sort(key=lambda e: e['when'], reverse=True)
    return events


def change_history(student, limit=60):
    return list(StudentChange.objects.filter(tenant_id=student.tenant_id, student=student)
                .select_related('changed_by')[:limit])
