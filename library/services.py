"""
library/services.py
───────────────────
Everything the library pages do, kept out of the views so it can be tested:
the catalogue and ISBN lookup, issuing and returning by barcode, renewals,
fines and lost-book charges, reservations, textbook rentals, reminders,
reading logs and challenges, stock-taking, purchase requests, donations,
visits, clearance for leavers, reports and barcode labels.
"""
import logging
import re
from datetime import datetime, timedelta
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import Count, Q, Sum
from django.utils import timezone
from django.utils.html import escape

from users.models import ENROLLED_STATUSES, Staff, Student

from .models import (Book, Branch, ChallengeBadge, Charge, Copy, Donation, EResource, LibrarySettings, Loan,
                     PurchaseRequest, ReadingChallenge, ReadingLog, Reservation, StockScan, StockTake, Visit)

log = logging.getLogger(__name__)
ZERO = Decimal('0')


class LibraryError(ValueError):
    pass


def settings_for(tenant):
    cfg, _ = LibrarySettings.objects.get_or_create(tenant=tenant)
    return cfg


def staff_of(user, tenant):
    if not getattr(user, 'is_authenticated', False):
        return None
    return Staff.objects.filter(user=user, tenant=tenant).first()


def is_librarian(user, tenant):
    from users.views_profile import can_manage
    if can_manage(user):
        return True
    staff = staff_of(user, tenant)
    return bool(staff and (staff.role == 'LIB' or settings_for(tenant).managers.filter(pk=staff.pk).exists()))


def manager_emails(tenant):
    from hr.services import admins_emails
    out = set(admins_emails(tenant))
    for s in Staff.objects.filter(tenant=tenant, role='LIB').select_related('user'):
        if s.user.email and s.user.is_active:
            out.add(s.user.email)
    for s in settings_for(tenant).managers.select_related('user'):
        if s.user.email:
            out.add(s.user.email)
    return sorted(out)


def _email(to, subject, html):
    from communications.services import send_email
    return send_email(to, subject, html)


def tell(student=None, staff=None, subject='', text='', sms=False):
    """Email (and optionally text) a borrower — a student's family, or the member of staff."""
    if student is not None:
        from users.records import contacts
        for _n, e in contacts(student, 'EMAIL'):
            _email(e, subject, f'<p>{escape(text)}</p>')
        if sms:
            from communications.services import send_sms
            for _n, p in contacts(student, 'SMS'):
                send_sms(p, text)
    elif staff is not None and staff.user.email:
        _email(staff.user.email, subject, f'<p>{escape(text)}</p>')


def _who(student, staff):
    if bool(student) == bool(staff):
        raise LibraryError('Choose one borrower.')
    return {'student': student, 'staff': None} if student else {'student': None, 'staff': staff}


def name_of(student, staff):
    return student.full_name if student else staff.full_name


# ─────────────────────────────────────────────────────────────────────────────
# F361 — catalogue and ISBN lookup
# ─────────────────────────────────────────────────────────────────────────────

def clean_isbn(raw):
    return re.sub(r'[^0-9Xx]', '', raw or '').upper()


def isbn_ok(isbn):
    isbn = clean_isbn(isbn)
    if len(isbn) == 10:
        if not re.fullmatch(r'\d{9}[\dX]', isbn):
            return False
        total = sum((10 - i) * (10 if c == 'X' else int(c)) for i, c in enumerate(isbn))
        return total % 11 == 0
    if len(isbn) == 13 and isbn.isdigit():
        total = sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(isbn))
        return total % 10 == 0
    return False


def lookup_isbn(isbn):
    """Book details from Open Library (free, no key). None if not found or the service is unreachable."""
    isbn = clean_isbn(isbn)
    if not isbn_ok(isbn):
        raise LibraryError('That is not a valid ISBN.')
    import requests
    try:
        r = requests.get('https://openlibrary.org/api/books',
                         params={'bibkeys': f'ISBN:{isbn}', 'format': 'json', 'jscmd': 'data'}, timeout=8)
        data = r.json().get(f'ISBN:{isbn}') if r.status_code == 200 else None
    except Exception as problem:   # network, JSON — the librarian can still type it in
        log.warning('ISBN lookup failed: %s', problem)
        return None
    if not data:
        return None
    year = re.search(r'(1[5-9]\d\d|20\d\d)', data.get('publish_date') or '')
    return {
        'title': (data.get('title') or '')[:255],
        'authors': ', '.join(a.get('name', '') for a in data.get('authors') or [])[:255],
        'publisher': ', '.join(p.get('name', '') for p in data.get('publishers') or [])[:160],
        'year': int(year.group(1)) if year else None,
        'subject': ((data.get('subjects') or [{}])[0].get('name') or '')[:80],
        'cover_url': ((data.get('cover') or {}).get('medium') or '')[:200],
        'isbn': isbn,
    }


def next_barcode(tenant):
    """The next free library barcode: LIB000001, LIB000002 …"""
    last = 0
    for code in Copy.objects.filter(tenant=tenant, barcode__regex=r'^LIB\d{6}$').values_list('barcode', flat=True):
        last = max(last, int(code[3:]))
    return f'LIB{last + 1:06d}'


def add_copies(book, count=1, branch=None, shelf='', price=None, donation=None, barcodes=None, acquired_on=None):
    count = int(count or 0)
    barcodes = [b.strip() for b in (barcodes or []) if b.strip()]
    if barcodes:
        count = len(barcodes)
    if not 1 <= count <= 200:
        raise LibraryError('Add between 1 and 200 copies at a time.')
    made = []
    for i in range(count):
        code = barcodes[i] if barcodes else next_barcode(book.tenant)
        if Copy.objects.filter(tenant=book.tenant, barcode__iexact=code).exists():
            raise LibraryError(f'Barcode {code} is already used.')
        made.append(Copy.objects.create(tenant=book.tenant, book=book, branch=branch, barcode=code[:40],
                                        shelf=(shelf or '')[:40], price=price, donation=donation,
                                        acquired_on=acquired_on or timezone.localdate()))
    return made


def search(tenant, q='', subject='', level='', branch=None, textbooks=False):
    books = Book.objects.filter(tenant=tenant)
    q = (q or '').strip()
    if q:
        isbn = clean_isbn(q)
        cond = Q(title__icontains=q) | Q(authors__icontains=q) | Q(keywords__icontains=q) | Q(subject__icontains=q)
        if len(isbn) in (10, 13):
            cond |= Q(isbn=isbn)
        cond |= Q(copies__barcode__iexact=q)
        books = books.filter(cond)
    if subject:
        books = books.filter(subject__iexact=subject)
    if level:
        books = books.filter(level__in=(level, 'ALL'))
    if textbooks:
        books = books.filter(is_textbook=True)
    shelf = Q(copies__status='IN')
    total = ~Q(copies__status__in=('LOST', 'GONE'))
    if branch:
        shelf &= Q(copies__branch=branch)
        total &= Q(copies__branch=branch)
        books = books.filter(copies__branch=branch)
    return books.annotate(available=Count('copies', filter=shelf, distinct=True),
                          total=Count('copies', filter=total, distinct=True)).distinct()


def availability(book):
    rows = []
    for br in [None] + list(Branch.objects.filter(tenant=book.tenant, active=True)):
        copies = book.copies.filter(branch=br).exclude(status__in=('LOST', 'GONE'))
        if copies.exists():
            rows.append({'branch': br, 'in': copies.filter(status='IN').count(), 'total': copies.count(),
                         'shelves': sorted({c.shelf for c in copies if c.shelf})})
    due = Loan.objects.filter(copy__book=book, returned_at__isnull=True).order_by('due_on').first()
    return {'rows': rows, 'next_due': due.due_on if due else None,
            'queue': book.reservations.filter(status='WAITING').count()}


# ─────────────────────────────────────────────────────────────────────────────
# F362 — borrowing and returning by barcode
# ─────────────────────────────────────────────────────────────────────────────

UUID_RE = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', re.I)


def resolve_borrower(tenant, code):
    """(student, staff) from a scanned ID card QR, an admission number or a staff username."""
    code = (code or '').strip()
    if not code:
        return None, None
    m = UUID_RE.search(code)
    if m:
        from idcards.models import IDCard
        card = IDCard.objects.filter(tenant=tenant, card_uid=m.group(0), is_active=True) \
            .select_related('student', 'staff').first()
        return (card.student, card.staff) if card else (None, None)
    st = Student.objects.filter(tenant=tenant, admission_number__iexact=code).first()
    if st:
        return st, None
    return None, Staff.objects.filter(tenant=tenant, user__username__iexact=code).first()


def resolve_copy(tenant, barcode):
    return Copy.objects.filter(tenant=tenant, barcode__iexact=(barcode or '').strip()).select_related('book').first()


def open_loans(student=None, staff=None):
    qs = Loan.objects.filter(returned_at__isnull=True).select_related('copy__book')
    return qs.filter(student=student) if student else qs.filter(staff=staff)


def owing(student=None, staff=None):
    qs = Charge.objects.filter(status='UNPAID')
    qs = qs.filter(student=student) if student else qs.filter(staff=staff)
    return qs.aggregate(s=Sum('amount'))['s'] or ZERO


def check_can_borrow(student=None, staff=None, rental=False):
    who = _who(student, staff)
    tenant = (student or staff).tenant
    cfg = settings_for(tenant)
    if student and student.status not in ENROLLED_STATUSES:
        raise LibraryError(f'{student.full_name} is no longer on the roll.')
    if not rental:
        limit = cfg.student_max_loans if student else cfg.staff_max_loans
        n = open_loans(**who).filter(rental_term__isnull=True).count()
        if n >= limit:
            raise LibraryError(f'{name_of(student, staff)} already has {n} book{"s" if n != 1 else ""} out '
                               f'(limit {limit}).')
    if open_loans(**who).filter(due_on__lt=timezone.localdate()).exists():
        raise LibraryError(f'{name_of(student, staff)} has an overdue book. Return it first.')
    if cfg.block_when_owing and owing(**who) > 0:
        raise LibraryError(f'{name_of(student, staff)} owes ₦{owing(**who):,.2f} in library charges.')


@transaction.atomic
def issue(copy, user, student=None, staff=None, rental_term=None, days=None):
    who = _who(student, staff)
    copy = Copy.objects.select_for_update().select_related('book').get(pk=copy.pk)
    held = None
    if copy.status == 'HELD':
        held = Reservation.objects.filter(copy=copy, status='READY', **who).first()
        if not held:
            raise LibraryError(f'This copy is being kept for someone who reserved "{copy.book.title}".')
    elif copy.status != 'IN':
        raise LibraryError(f'This copy is marked "{copy.get_status_display()}" — it cannot be lent.')
    if rental_term is not None:
        if not copy.book.is_textbook:
            raise LibraryError('Only textbooks can be rented for a term.')
        if not student:
            raise LibraryError('Textbook rental is for students.')
    check_can_borrow(student, staff, rental=rental_term is not None)
    today = timezone.localdate()
    if rental_term is not None:
        due = rental_term.end_date or today + timedelta(days=90)
        if due < today:
            raise LibraryError('That term has already ended.')
    else:
        due = today + timedelta(days=int(days or settings_for(copy.tenant).loan_days))
    loan = Loan.objects.create(tenant_id=copy.tenant_id, copy=copy, due_on=due, issued_by=user,
                               rental_term=rental_term, condition_out=copy.condition, **who)
    copy.status = 'OUT'
    copy.save(update_fields=['status'])
    if held:
        held.status = 'DONE'
        held.save(update_fields=['status'])
    if rental_term is not None and copy.book.rental_fee > 0:
        Charge.objects.create(tenant_id=copy.tenant_id, student=student, loan=loan, kind='RENTAL',
                              amount=copy.book.rental_fee,
                              note=f'{copy.book.title} — {rental_term.name}'[:160])
    return loan


def fine_for(loan, on=None):
    if loan.rental_term_id:
        return ZERO
    cfg = settings_for(loan.tenant)
    on = on or timezone.localdate()
    late = (on - loan.due_on).days - cfg.grace_days
    if late <= 0:
        return ZERO
    fine = cfg.fine_per_day * late
    return min(fine, cfg.fine_cap) if cfg.fine_cap > 0 else fine


def _next_reservation(copy):
    """Give a returned copy to the first person waiting for the book, or put it back on the shelf."""
    nxt = Reservation.objects.filter(book=copy.book, status='WAITING').order_by('created_at').first()
    if not nxt:
        copy.status = 'IN'
        copy.save(update_fields=['status'])
        return None
    cfg = settings_for(copy.tenant)
    nxt.status, nxt.copy = 'READY', copy
    nxt.hold_until = timezone.localdate() + timedelta(days=cfg.hold_days)
    nxt.save(update_fields=['status', 'copy', 'hold_until'])
    copy.status = 'HELD'
    copy.save(update_fields=['status'])
    tell(nxt.student, nxt.staff, f'Library: "{copy.book.title}" is ready',
         f'The book you reserved, "{copy.book.title}", is waiting at the library desk for '
         f'{nxt.borrower_name} until {nxt.hold_until:%a %d %b}.')
    return nxt


@transaction.atomic
def return_copy(copy, user, condition='', damage=None, note=''):
    loan = Loan.objects.filter(copy=copy, returned_at__isnull=True).select_related('copy__book').first()
    if not loan:
        raise LibraryError(f'"{copy.book.title}" [{copy.barcode}] is not on loan.')
    loan.returned_at, loan.returned_to = timezone.now(), user
    if condition in dict(Copy.CONDITIONS):
        loan.condition_in = condition
        copy.condition = condition
        copy.save(update_fields=['condition'])
    loan.save(update_fields=['returned_at', 'returned_to', 'condition_in'])
    fine = fine_for(loan)
    charges = []
    if fine > 0:
        days = (timezone.localdate() - loan.due_on).days
        charges.append(Charge.objects.create(tenant_id=loan.tenant_id, student=loan.student, staff=loan.staff,
                                             loan=loan, kind='FINE', amount=fine,
                                             note=f'{copy.book.title}: {days} day{"s" if days != 1 else ""} late'))
    if damage:
        amount = Decimal(str(damage))
        if amount > 0:
            charges.append(Charge.objects.create(tenant_id=loan.tenant_id, student=loan.student, staff=loan.staff,
                                                 loan=loan, kind='DAMAGE', amount=amount,
                                                 note=(note or f'{copy.book.title}: damaged')[:160]))
    if copy.condition == 'DAMAGED':
        copy.status = 'REPAIR'
        copy.save(update_fields=['status'])
    else:
        _next_reservation(copy)
    return loan, charges


def renew(loan, user=None):
    cfg = settings_for(loan.tenant)
    if loan.returned_at:
        raise LibraryError('This book has been returned.')
    if loan.rental_term_id:
        raise LibraryError('Rentals run to the end of term.')
    if loan.renewals >= cfg.max_renewals:
        raise LibraryError('This book cannot be renewed again.')
    if loan.due_on < timezone.localdate():
        raise LibraryError('Overdue books must be brought back, not renewed.')
    if Reservation.objects.filter(book=loan.copy.book, status='WAITING').exists():
        raise LibraryError('Someone is waiting for this book, so it cannot be renewed.')
    loan.due_on = max(loan.due_on, timezone.localdate()) + timedelta(days=cfg.loan_days)
    loan.renewals += 1
    loan.reminded_at = None
    loan.save(update_fields=['due_on', 'renewals', 'reminded_at'])
    return loan


# ─────────────────────────────────────────────────────────────────────────────
# F372 — lost books
# ─────────────────────────────────────────────────────────────────────────────

@transaction.atomic
def mark_lost(loan, user):
    if loan.returned_at:
        raise LibraryError('This book has been returned.')
    copy = loan.copy
    loan.lost, loan.returned_at, loan.returned_to = True, timezone.now(), user
    loan.save(update_fields=['lost', 'returned_at', 'returned_to'])
    copy.status = 'LOST'
    copy.save(update_fields=['status'])
    out = []
    if copy.cost and copy.cost > 0:
        out.append(Charge.objects.create(tenant_id=loan.tenant_id, student=loan.student, staff=loan.staff,
                                         loan=loan, kind='LOST', amount=copy.cost,
                                         note=f'Replacement: {copy.book.title} [{copy.barcode}]'[:160]))
    fine = fine_for(loan)
    if fine > 0:
        out.append(Charge.objects.create(tenant_id=loan.tenant_id, student=loan.student, staff=loan.staff,
                                         loan=loan, kind='FINE', amount=fine, note=f'{copy.book.title}: late'))
    total = sum((c.amount for c in out), ZERO)
    tell(loan.student, loan.staff, f'Library: "{copy.book.title}" recorded as lost',
         f'"{copy.book.title}" borrowed by {loan.borrower_name} has been recorded as lost. '
         f'The charge is ₦{total:,.2f}' + ('. It can be paid from the family wallet on the parent portal.'
                                           if loan.student else '.'))
    return out


def found_again(copy, user):
    """A lost or missing copy turned up: back on the shelf. Charges already paid are not refunded here."""
    if copy.status not in ('LOST', 'MISSING'):
        raise LibraryError('This copy is not lost or missing.')
    _next_reservation(copy)


# ─────────────────────────────────────────────────────────────────────────────
# F363 — charges, paid from the family wallet
# ─────────────────────────────────────────────────────────────────────────────

def pay_from_wallet(charge, user):
    from finance.views import _get_family_wallet_for_student
    if charge.status != 'UNPAID':
        raise LibraryError('This charge is not waiting for payment.')
    if not charge.student_id:
        raise LibraryError('Staff charges are paid at the library desk.')
    wallet = _get_family_wallet_for_student(charge.student)
    if not wallet:
        raise LibraryError('No family wallet is set up for this child.')
    with transaction.atomic():
        try:
            category = 'TEXTBOOK' if charge.kind == 'RENTAL' else 'OTHER'
            txn = wallet.debit(charge.amount, f'Library {charge.get_kind_display().lower()}: '
                                              f'{charge.student.full_name} — {charge.note}'[:300],
                               performed_by=user, category=category)
        except ValueError as problem:
            raise LibraryError(str(problem))
        charge.status, charge.transaction, charge.settled_by = 'PAID', txn, user
        charge.save(update_fields=['status', 'transaction', 'settled_by'])
    return charge


def settle(charge, status, user, note=''):
    if status not in ('PAID', 'WAIVED'):
        raise LibraryError('Choose paid or waived.')
    if charge.status != 'UNPAID':
        raise LibraryError('Already settled.')
    charge.status, charge.settled_by = status, user
    if note:
        charge.note = f'{charge.note} · {note}'[:160]
    charge.save(update_fields=['status', 'settled_by', 'note'])
    return charge


# ─────────────────────────────────────────────────────────────────────────────
# F364 — reservations
# ─────────────────────────────────────────────────────────────────────────────

def reserve(book, student=None, staff=None):
    who = _who(student, staff)
    if book.copies.filter(status='IN').exists():
        raise LibraryError(f'"{book.title}" is on the shelf now — just ask at the desk.')
    if not book.copies.exclude(status__in=('LOST', 'GONE', 'MISSING')).exists():
        raise LibraryError('The library has no copy of this book to reserve.')
    if Reservation.objects.filter(book=book, status__in=('WAITING', 'READY'), **who).exists():
        raise LibraryError('Already reserved.')
    if open_loans(**who).filter(copy__book=book).exists():
        raise LibraryError('You already have this book.')
    return Reservation.objects.create(tenant_id=book.tenant_id, book=book, **who)


def cancel_reservation(res):
    if res.status not in ('WAITING', 'READY'):
        raise LibraryError('This reservation is already closed.')
    copy = res.copy if res.status == 'READY' else None
    res.status = 'CANCELLED'
    res.save(update_fields=['status'])
    if copy:
        _next_reservation(copy)


def expire_holds(tenant):
    n = 0
    for res in Reservation.objects.filter(tenant=tenant, status='READY',
                                          hold_until__lt=timezone.localdate()).select_related('copy__book'):
        res.status = 'EXPIRED'
        res.save(update_fields=['status'])
        if res.copy:
            _next_reservation(res.copy)
        n += 1
    return n


# ─────────────────────────────────────────────────────────────────────────────
# F363 — reminders (daily command)
# ─────────────────────────────────────────────────────────────────────────────

def send_reminders(tenant):
    cfg = settings_for(tenant)
    today = timezone.localdate()
    now = timezone.now()
    soon = Loan.objects.filter(tenant=tenant, returned_at__isnull=True, reminded_at__isnull=True,
                               due_on__gte=today, due_on__lte=today + timedelta(days=cfg.remind_days_before),
                               rental_term__isnull=True).select_related('copy__book', 'student', 'staff__user')
    late = Loan.objects.filter(tenant=tenant, returned_at__isnull=True, overdue_notice_at__isnull=True,
                               due_on__lt=today).select_related('copy__book', 'student', 'staff__user')
    counts = {'due': 0, 'overdue': 0, 'expired': 0}
    for loan in soon:
        tell(loan.student, loan.staff, f'Library book due {loan.due_on:%a %d %b}',
             f'"{loan.copy.book.title}" borrowed by {loan.borrower_name} is due back on {loan.due_on:%A %d %B}.')
        loan.reminded_at = now
        loan.save(update_fields=['reminded_at'])
        counts['due'] += 1
    for loan in late:
        fine = fine_for(loan)
        extra = f' The late fine so far is ₦{fine:,.2f} and grows by ₦{cfg.fine_per_day:,.2f} a day.' if fine else ''
        tell(loan.student, loan.staff, 'Library book overdue',
             f'"{loan.copy.book.title}" borrowed by {loan.borrower_name} was due on {loan.due_on:%d %b}. '
             f'Please return it.{extra}', sms=True)
        loan.overdue_notice_at = now
        loan.save(update_fields=['overdue_notice_at'])
        counts['overdue'] += 1
    counts['expired'] = expire_holds(tenant)
    return counts


def run_reminders():
    from tenants.middleware import clear_current_tenant, set_current_tenant
    from tenants.models import Tenant
    total = {'due': 0, 'overdue': 0, 'expired': 0}
    for t in Tenant.objects.filter(status__in=('ACTIVE', 'TRIAL')):
        set_current_tenant(t)
        try:
            for k, v in send_reminders(t).items():
                total[k] += v
        finally:
            clear_current_tenant()
    return total


# ─────────────────────────────────────────────────────────────────────────────
# F366 / F374 — reading logs and challenges
# ─────────────────────────────────────────────────────────────────────────────

def log_reading(student, data, book=None):
    title = (data.get('title') or (book.title if book else '')).strip()
    if not title:
        raise LibraryError('Give the book title.')
    try:
        finished = datetime.strptime(data.get('finished_on') or '', '%Y-%m-%d').date()
    except ValueError:
        finished = timezone.localdate()
    if finished > timezone.localdate():
        raise LibraryError('The finish date cannot be in the future.')
    rating = str(data.get('rating') or '')
    pages = str(data.get('pages') or '')
    return ReadingLog.objects.create(
        tenant_id=student.tenant_id, student=student, book=book, title=title[:255],
        author=(data.get('author') or (book.authors if book else '')).strip()[:160],
        pages=int(pages) if pages.isdigit() else None, finished_on=finished,
        rating=int(rating) if rating in ('1', '2', '3', '4', '5') else None,
        review=(data.get('review') or '').strip()[:3000])


def approve_log(entry, user, approve=True):
    if approve:
        entry.approved, entry.approved_by = True, user
        entry.save(update_fields=['approved', 'approved_by'])
        return check_badges(entry.student)
    entry.delete()
    return []


def challenge_open_to(challenge, student):
    ids = list(challenge.classes.values_list('pk', flat=True))
    return not ids or student.class_assigned_id in ids


def challenge_count(challenge, student):
    return ReadingLog.objects.filter(student=student, approved=True, finished_on__gte=challenge.starts,
                                     finished_on__lte=challenge.ends).count()


def check_badges(student):
    """Award every challenge badge the student has now earned (also recorded as an award on their profile)."""
    from users.models import Award
    won = []
    for ch in ReadingChallenge.objects.filter(tenant_id=student.tenant_id):
        if not challenge_open_to(ch, student) or challenge_count(ch, student) < ch.target:
            continue
        try:
            with transaction.atomic():
                ChallengeBadge.objects.create(tenant_id=student.tenant_id, challenge=ch, student=student)
        except IntegrityError:
            continue
        Award.objects.create(tenant_id=student.tenant_id, student=student, title=ch.badge[:160], category='OTHER',
                             description=f'Read {ch.target} books in "{ch.title}"'[:255], show_on_report=True)
        won.append(ch)
    return won


def challenge_board(challenge):
    from users.models import Student as S
    qs = S.objects.filter(tenant_id=challenge.tenant_id, status__in=ENROLLED_STATUSES)
    ids = list(challenge.classes.values_list('pk', flat=True))
    if ids:
        qs = qs.filter(class_assigned_id__in=ids)
    qs = qs.annotate(n=Count('reading_logs', filter=Q(reading_logs__approved=True,
                                                    reading_logs__finished_on__gte=challenge.starts,
                                                    reading_logs__finished_on__lte=challenge.ends)))
    return list(qs.filter(n__gt=0).order_by('-n', 'full_name').select_related('class_assigned')[:50])


# ─────────────────────────────────────────────────────────────────────────────
# F378 — recommendations
# ─────────────────────────────────────────────────────────────────────────────

def level_for(student):
    name = f'{getattr(student.class_assigned, "level", "")} {getattr(student.class_assigned, "name", "")}'.upper()
    if re.search(r'\bS\.?S\.?\s*\d|SSS|SENIOR', name):
        return 'SSS'
    if re.search(r'\bJ\.?S\.?S?\.?\s*\d|JSS|JUNIOR', name):
        return 'JSS'
    m = re.search(r'(PRIMARY|PRY|BASIC|GRADE|YEAR|P)\s*(\d)', name)
    if m:
        n = int(m.group(2))
        return 'LOWER' if n <= 3 else 'UPPER'
    if re.search(r'NURSERY|KG|KINDERGARTEN|CRECHE|PRE', name):
        return 'NURSERY'
    return ''


def recommendations(student, limit=8):
    """Books at the child's level, leaning towards subjects they have enjoyed, most borrowed first."""
    read_ids = set(ReadingLog.objects.filter(student=student, book__isnull=False).values_list('book_id', flat=True))
    read_ids |= set(Loan.objects.filter(student=student).values_list('copy__book_id', flat=True))
    liked = list(ReadingLog.objects.filter(student=student, book__isnull=False)
                 .exclude(rating__lt=3).exclude(book__subject='').values_list('book__subject', flat=True))
    liked += list(Loan.objects.filter(student=student).exclude(copy__book__subject='')
                  .values_list('copy__book__subject', flat=True))
    level = level_for(student)
    since = timezone.now() - timedelta(days=365)
    qs = Book.objects.filter(tenant_id=student.tenant_id).exclude(pk__in=read_ids).exclude(level='STAFF')
    if level:
        qs = qs.filter(level__in=(level, 'ALL'))
    qs = qs.annotate(pop=Count('copies__loans', filter=Q(copies__loans__issued_at__gte=since)),
                     available=Count('copies', filter=Q(copies__status='IN'), distinct=True))
    books = list(qs.filter(available__gt=0)[:200]) or list(qs[:200])
    fav = {s.lower(): liked.count(s) for s in set(liked)}
    books.sort(key=lambda b: (-fav.get((b.subject or '').lower(), 0), -b.pop, b.title))
    for b in books:
        b.why = 'Because you enjoyed ' + b.subject if fav.get((b.subject or '').lower()) else \
            ('Popular in the library' if b.pop else 'New to you')
    return books[:limit]


# ─────────────────────────────────────────────────────────────────────────────
# F368 — stock-taking
# ─────────────────────────────────────────────────────────────────────────────

def start_stocktake(tenant, user, branch=None, note=''):
    if StockTake.objects.filter(tenant=tenant, branch=branch, closed_at__isnull=True).exists():
        raise LibraryError('A stock-take is already open for this library.')
    return StockTake.objects.create(tenant=tenant, branch=branch, started_by=user, note=(note or '')[:255])


def stock_scan(st, barcode, user):
    if st.closed_at:
        raise LibraryError('This stock-take is closed.')
    copy = resolve_copy(st.tenant, barcode)
    if not copy:
        raise LibraryError(f'No copy has the barcode {barcode}.')
    try:
        with transaction.atomic():
            StockScan.objects.create(tenant_id=st.tenant_id, stocktake=st, copy=copy, by=user)
    except IntegrityError:
        raise LibraryError(f'{copy} was already counted.')
    if copy.status == 'MISSING':
        copy.status = 'IN'
        copy.save(update_fields=['status'])
    return copy


def _expected(st):
    qs = Copy.objects.filter(tenant_id=st.tenant_id, status__in=('IN', 'HELD', 'REPAIR', 'MISSING'))
    return qs.filter(branch=st.branch) if st.branch_id else qs


def stocktake_summary(st):
    scanned = set(st.scans.values_list('copy_id', flat=True))
    expected = list(_expected(st).select_related('book'))
    wrong = Copy.objects.filter(pk__in=scanned).exclude(branch=st.branch) if st.branch_id else Copy.objects.none()
    on_loan = Copy.objects.filter(pk__in=scanned, status='OUT')
    return {'expected': len(expected), 'scanned': len(scanned),
            'missing': [c for c in expected if c.pk not in scanned],
            'wrong_branch': list(wrong.select_related('book', 'branch')),
            'on_loan': list(on_loan.select_related('book'))}


def close_stocktake(st, user, mark_missing=True):
    if st.closed_at:
        raise LibraryError('Already closed.')
    summary = stocktake_summary(st)
    if mark_missing:
        Copy.objects.filter(pk__in=[c.pk for c in summary['missing']]).update(status='MISSING')
    st.closed_at, st.missing_count = timezone.now(), len(summary['missing'])
    st.save(update_fields=['closed_at', 'missing_count'])
    return summary


# ─────────────────────────────────────────────────────────────────────────────
# F369 — purchase requests, F379 donations, F377 visits
# ─────────────────────────────────────────────────────────────────────────────

def request_purchase(tenant, user, data):
    title, reason = (data.get('title') or '').strip(), (data.get('reason') or '').strip()
    if not (title and reason):
        raise LibraryError('Give the title and why it is needed.')
    copies = str(data.get('copies') or '1')
    price = str(data.get('est_price') or '').replace(',', '')
    try:
        price = Decimal(price) if price else None
    except Exception:
        raise LibraryError('The price must be a number.')
    pr = PurchaseRequest.objects.create(
        tenant=tenant, title=title[:255], authors=(data.get('authors') or '').strip()[:255],
        isbn=clean_isbn(data.get('isbn'))[:17], copies=max(1, min(int(copies) if copies.isdigit() else 1, 500)),
        reason=reason[:255], est_price=price, requested_by=user)
    for e in manager_emails(tenant):
        _email(e, f'Book request: {pr.title}', f'<p>{escape(user.get_full_name() or user.username)} asked for '
                                                f'{pr.copies} × <b>{escape(pr.title)}</b>: {escape(reason)}</p>')
    return pr


def decide_purchase(pr, status, user, note=''):
    if status not in ('APPROVED', 'ORDERED', 'RECEIVED', 'DECLINED'):
        raise LibraryError('Choose a status.')
    pr.status, pr.decided_by, pr.note = status, user, (note or pr.note)[:255]
    pr.save(update_fields=['status', 'decided_by', 'note'])
    if pr.requested_by and pr.requested_by.email:
        _email(pr.requested_by.email, f'Book request: {pr.get_status_display().lower()}',
               f'<p>Your request for <b>{escape(pr.title)}</b> is now: {escape(pr.get_status_display())}.'
               f'{" " + escape(note) if note else ""}</p>')
    return pr


def acknowledge_donation(donation):
    if not donation.donor_email:
        raise LibraryError('There is no email address for this donor.')
    titles = sorted({c.book.title for c in donation.copies.select_related('book')})
    n = donation.copies.count()
    school = donation.tenant.name
    listing = ''.join(f'<li>{escape(t)}</li>' for t in titles)
    _email(donation.donor_email, f'Thank you from {school} library',
           f'<p>Dear {escape(donation.donor_name)},</p><p>Thank you for donating {n} book{"s" if n != 1 else ""} to '
           f'the {escape(school)} library on {donation.date:%d %B %Y}. They are now catalogued and on loan to our '
           f'pupils.</p>{"<ul>" + listing + "</ul>" if listing else ""}<p>With gratitude,<br>The Librarian</p>')
    donation.acknowledged_at = timezone.now()
    donation.save(update_fields=['acknowledged_at'])


def visit_stats(tenant, days=30):
    since = timezone.now() - timedelta(days=days)
    qs = Visit.objects.filter(tenant=tenant, at__gte=since)
    by_class = {}
    for v in qs.select_related('student__class_assigned', 'school_class'):
        cls = v.school_class or (v.student.class_assigned if v.student_id else None)
        key = cls.name if cls else 'Other'
        by_class[key] = by_class.get(key, 0) + v.headcount
    by_hour = {}
    for v in qs:
        h = timezone.localtime(v.at).hour
        by_hour[h] = by_hour.get(h, 0) + v.headcount
    return {'total': sum(by_class.values()), 'by_class': sorted(by_class.items(), key=lambda x: -x[1]),
            'by_hour': sorted(by_hour.items())}


# ─────────────────────────────────────────────────────────────────────────────
# F373 — clearance for leavers
# ─────────────────────────────────────────────────────────────────────────────

def clearance(student):
    loans = list(open_loans(student=student))
    charges = list(Charge.objects.filter(student=student, status='UNPAID'))
    return {'loans': loans, 'charges': charges, 'owing': sum((c.amount for c in charges), ZERO),
            'clear': not loans and not charges}


def clearance_problem(student):
    """A sentence if the student cannot be cleared by the library, else ''."""
    c = clearance(student)
    if c['clear']:
        return ''
    bits = []
    if c['loans']:
        bits.append(f'{len(c["loans"])} library book{"s" if len(c["loans"]) != 1 else ""} not returned')
    if c['charges']:
        bits.append(f'₦{c["owing"]:,.2f} in library charges unpaid')
    return ' and '.join(bits)


# ─────────────────────────────────────────────────────────────────────────────
# F367 — usage reports
# ─────────────────────────────────────────────────────────────────────────────

def report(tenant, days=90):
    since = timezone.now() - timedelta(days=days)
    loans = Loan.objects.filter(tenant=tenant, issued_at__gte=since)
    top = (Book.objects.filter(tenant=tenant, copies__loans__issued_at__gte=since)
           .annotate(n=Count('copies__loans')).order_by('-n', 'title')[:10])
    readers = (Student.objects.filter(tenant=tenant, library_loans__issued_at__gte=since)
               .annotate(n=Count('library_loans')).order_by('-n', 'full_name').select_related('class_assigned')[:10])
    by_class = list(loans.filter(student__isnull=False).values('student__class_assigned__name')
                    .annotate(n=Count('id')).order_by('-n'))
    by_subject = list(loans.exclude(copy__book__subject='').values('copy__book__subject')
                      .annotate(n=Count('id')).order_by('-n')[:10])
    months = {}
    for d in loans.values_list('issued_at', flat=True):
        k = timezone.localtime(d).strftime('%Y-%m')
        months[k] = months.get(k, 0) + 1
    copies = Copy.objects.filter(tenant=tenant)
    charges = Charge.objects.filter(tenant=tenant, created_at__gte=since)
    return {
        'loans': loans.count(), 'returned_late': sum(1 for x in loans.filter(returned_at__isnull=False)
                                                     if timezone.localtime(x.returned_at).date() > x.due_on),
        'overdue_now': Loan.objects.filter(tenant=tenant, returned_at__isnull=True,
                                           due_on__lt=timezone.localdate()).count(),
        'on_loan': copies.filter(status='OUT').count(), 'books': Book.objects.filter(tenant=tenant).count(),
        'copies': copies.exclude(status__in=('LOST', 'GONE')).count(), 'lost': copies.filter(status='LOST').count(),
        'top': list(top), 'readers': list(readers), 'by_class': by_class, 'by_subject': by_subject,
        'months': sorted(months.items()),
        'fines': charges.filter(kind='FINE').aggregate(s=Sum('amount'))['s'] or ZERO,
        'collected': charges.filter(status='PAID').aggregate(s=Sum('amount'))['s'] or ZERO,
        'never': Book.objects.filter(tenant=tenant).annotate(n=Count('copies__loans')).filter(n=0).count(),
        'downloads': list(EResource.objects.filter(tenant=tenant, downloads__gt=0).order_by('-downloads')[:5]),
        'readers_count': loans.filter(student__isnull=False).values('student').distinct().count(),
    }


# ─────────────────────────────────────────────────────────────────────────────
# F370 — barcode labels (Code 128, drawn as SVG — no extra library needed)
# ─────────────────────────────────────────────────────────────────────────────

CODE128 = (
    '212222 222122 222221 121223 121322 131222 122213 122312 132212 221213 221312 231212 112232 122132 122231 '
    '113222 123122 123221 223211 221132 221231 213212 223112 312131 311222 321122 321221 312212 322112 322211 '
    '212123 212321 232121 111323 131123 131321 112313 132113 132311 211313 231113 231311 112133 112331 132131 '
    '113123 113321 133121 313121 211331 231131 213113 213311 213131 311123 311321 331121 312113 312311 332111 '
    '314111 221411 431111 111224 111422 121124 121421 141122 141221 112214 112412 122114 122411 142112 142211 '
    '241211 221114 413111 241112 134111 111242 121142 121241 114212 124112 124211 411212 421112 421211 212141 '
    '214121 412121 111143 111341 131141 114113 114311 411113 411311 113141 114131 311141 411131 211412 211214 '
    '211232').split()
STOP = '2331112'


def code128_widths(text):
    """Bar/space widths for Code 128 set B (printable ASCII)."""
    if not text or any(not 32 <= ord(c) <= 126 for c in text):
        raise LibraryError('Barcodes can only use letters, digits and common symbols.')
    values = [104] + [ord(c) - 32 for c in text]
    check = (values[0] + sum(i * v for i, v in enumerate(values[1:], 1))) % 103
    return ''.join(CODE128[v] for v in values + [check]) + STOP


def barcode_svg(text, height=40, module=1.4):
    widths = code128_widths(text)
    x, quiet, bars = 0.0, 10 * module, []
    for i, w in enumerate(widths):
        width = int(w) * module
        if i % 2 == 0:
            bars.append(f'<rect x="{quiet + x:.1f}" y="0" width="{width:.1f}" height="{height}"/>')
        x += width
    total = x + 2 * quiet
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{total:.0f}" height="{height}" '
            f'viewBox="0 0 {total:.1f} {height}" role="img" aria-label="{escape(text)}">{"".join(bars)}</svg>')


def labels(copies):
    return [{'copy': c, 'svg': barcode_svg(c.barcode)} for c in copies]


def record_donation(tenant, user, data):
    name = (data.get('donor_name') or '').strip()
    if not name:
        raise LibraryError('Give the donor\'s name.')
    try:
        day = datetime.strptime(data.get('date') or '', '%Y-%m-%d').date()
    except ValueError:
        day = timezone.localdate()
    return Donation.objects.create(tenant=tenant, donor_name=name[:160],
                                   donor_email=(data.get('donor_email') or '').strip()[:254],
                                   donor_phone=(data.get('donor_phone') or '').strip()[:20], date=day,
                                   note=(data.get('note') or '').strip()[:255], recorded_by=user)
