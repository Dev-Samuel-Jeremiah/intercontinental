"""
library/views.py
────────────────
Librarian pages (desk, catalogue, loans, charges, reservations, e-resources,
reading, stock-take, requests, donations, visits, reports), the catalogue
search everyone can use, "My library" for students and staff, and the
parents' library page.
"""
import os
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import F
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from users.models import ENROLLED_STATUSES, Class, Staff, Student

from . import services as svc
from .models import (LEVELS, Book, Branch, Charge, Copy, Donation, EResource, Loan, PurchaseRequest,
                     ReadingChallenge, ReadingLog, Reservation, StockTake, Visit)


def _deny(request, text='Only the library can do that.'):
    messages.error(request, text)
    return redirect('dashboard:router')


def _librarian_only(view):
    def wrap(request, *a, **kw):
        if not svc.is_librarian(request.user, request.tenant):
            return _deny(request)
        return view(request, *a, **kw)
    wrap.__name__ = view.__name__
    return login_required(wrap)


def _date(raw):
    try:
        return datetime.strptime(raw or '', '%Y-%m-%d').date()
    except ValueError:
        return None


def _money(raw, blank_none=False):
    raw = str(raw or '').replace(',', '').strip()
    if not raw:
        return None if blank_none else Decimal('0')
    try:
        v = Decimal(raw)
    except InvalidOperation:
        raise svc.LibraryError('Amounts must be numbers.')
    if v < 0:
        raise svc.LibraryError('Amounts cannot be negative.')
    return v


def _int(raw):
    raw = str(raw or '').strip()
    return int(raw) if raw.isdigit() else None


def _me(request):
    """(student, staff) for the signed-in user."""
    st = Student.objects.filter(user=request.user, tenant=request.tenant).select_related('class_assigned').first()
    return (st, None) if st else (None, svc.staff_of(request.user, request.tenant))


def _branch(request, key='branch'):
    return Branch.objects.filter(tenant=request.tenant, pk=request.POST.get(key) or request.GET.get(key) or 0).first()


def _serve(field, name):
    if not field:
        raise Http404
    from users.records import CONTENT_TYPES
    ext = os.path.splitext(field.name)[1].lower()
    try:
        return FileResponse(field.open('rb'), content_type=CONTENT_TYPES.get(ext, 'application/octet-stream'),
                            filename=f'{name}{ext}')
    except (FileNotFoundError, OSError):
        raise Http404


# ─────────────────────────────────────────────────────────────────────────────
# Librarian home
# ─────────────────────────────────────────────────────────────────────────────

@_librarian_only
def home(request):
    t, today = request.tenant, timezone.localdate()
    return render(request, 'library/home.html', {
        'overdue': Loan.objects.filter(tenant=t, returned_at__isnull=True, due_on__lt=today)
        .select_related('copy__book', 'student__class_assigned', 'staff').order_by('due_on')[:15],
        'due_today': Loan.objects.filter(tenant=t, returned_at__isnull=True, due_on=today).count(),
        'on_loan': Loan.objects.filter(tenant=t, returned_at__isnull=True).count(),
        'ready': Reservation.objects.filter(tenant=t, status='READY').select_related('book', 'student', 'staff'),
        'waiting': Reservation.objects.filter(tenant=t, status='WAITING').count(),
        'logs': ReadingLog.objects.filter(tenant=t, approved=False).count(),
        'requests': PurchaseRequest.objects.filter(tenant=t, status='PENDING').count(),
        'unpaid': Charge.objects.filter(tenant=t, status='UNPAID').count(),
        'books': Book.objects.filter(tenant=t).count(),
        'copies': Copy.objects.filter(tenant=t).exclude(status__in=('LOST', 'GONE')).count(),
        'repair': Copy.objects.filter(tenant=t, status='REPAIR').select_related('book')[:10],
        'open_takes': StockTake.objects.filter(tenant=t, closed_at__isnull=True)})


# ─────────────────────────────────────────────────────────────────────────────
# F362 — the desk: issue, return, renew by scanning
# ─────────────────────────────────────────────────────────────────────────────

@_librarian_only
def desk(request):
    t = request.tenant
    student = staff = None
    code = (request.POST.get('borrower') or request.GET.get('borrower') or '').strip()
    if code:
        student, staff = svc.resolve_borrower(t, code)
        if not (student or staff):
            messages.error(request, f'No student or staff member matches "{code}".')
    if request.method == 'POST':
        a = request.POST.get('action')
        try:
            if a == 'return':
                copy = svc.resolve_copy(t, request.POST.get('barcode'))
                if not copy:
                    raise svc.LibraryError('Book barcode not recognised.')
                loan, charges = svc.return_copy(copy, request.user, request.POST.get('condition') or '',
                                                _money(request.POST.get('damage'), True), request.POST.get('note'))
                text = f'Returned: {copy.book.title} from {loan.borrower_name}.'
                if charges:
                    text += ' Charged ' + ', '.join(f'₦{c.amount:,.2f} ({c.get_kind_display().lower()})'
                                                    for c in charges) + '.'
                copy.refresh_from_db()
                if copy.status == 'HELD':
                    text += ' Put it aside — it is reserved.'
                messages.success(request, text)
                return redirect('library:desk')
            if not (student or staff):
                raise svc.LibraryError('Scan the borrower\'s ID card first.')
            if a == 'issue':
                copy = svc.resolve_copy(t, request.POST.get('barcode'))
                if not copy:
                    raise svc.LibraryError('Book barcode not recognised.')
                term = None
                if request.POST.get('rental'):
                    from teaching.services import current_term
                    term = current_term(t)
                    if not term:
                        raise svc.LibraryError('Set up the current term before renting textbooks.')
                loan = svc.issue(copy, request.user, student, staff, rental_term=term,
                                 days=_int(request.POST.get('days')))
                messages.success(request, f'{copy.book.title} lent to {loan.borrower_name} — due {loan.due_on:%a %d %b}.')
            elif a == 'renew':
                loan = get_object_or_404(Loan, pk=request.POST.get('loan'), tenant=t)
                svc.renew(loan, request.user)
                messages.success(request, f'Renewed — now due {loan.due_on:%a %d %b}.')
        except svc.LibraryError as problem:
            messages.error(request, str(problem))
        return redirect(f'{request.path}?borrower={code}' if code else request.path)
    who = {'student': student} if student else {'staff': staff}
    return render(request, 'library/desk.html', {
        'code': code, 'student': student, 'staff': staff,
        'loans': svc.open_loans(**who) if (student or staff) else [],
        'owing': svc.owing(**who) if (student or staff) else 0,
        'ready': Reservation.objects.filter(status='READY', **who).select_related('book', 'copy')
        if (student or staff) else [],
        'conditions': Copy.CONDITIONS, 'today': timezone.localdate()})


# ─────────────────────────────────────────────────────────────────────────────
# F361 catalogue, F380 branches, F375 textbooks
# ─────────────────────────────────────────────────────────────────────────────

def _book_fields(book, post):
    book.title = (post.get('title') or '').strip()[:255]
    if not book.title:
        raise svc.LibraryError('Give the title.')
    book.authors = (post.get('authors') or '').strip()[:255]
    book.isbn = svc.clean_isbn(post.get('isbn'))[:17]
    if book.isbn and not svc.isbn_ok(book.isbn):
        raise svc.LibraryError('That ISBN does not look right — check the digits.')
    book.publisher = (post.get('publisher') or '').strip()[:160]
    book.year = _int(post.get('year'))
    book.edition = (post.get('edition') or '').strip()[:40]
    book.subject = (post.get('subject') or '').strip()[:80]
    book.level = post.get('level') if post.get('level') in dict(LEVELS) else 'ALL'
    book.keywords = (post.get('keywords') or '').strip()[:255]
    book.description = (post.get('description') or '').strip()
    book.cover_url = (post.get('cover_url') or '').strip()[:200]
    book.is_textbook = bool(post.get('is_textbook'))
    book.rental_fee = _money(post.get('rental_fee'))
    book.price = _money(post.get('price'))


@_librarian_only
def catalogue(request):
    t = request.tenant
    if request.method == 'POST':
        try:
            book = Book(tenant=t)
            _book_fields(book, request.POST)
            dup = Book.objects.filter(tenant=t, isbn=book.isbn).first() if book.isbn else None
            if dup:
                raise svc.LibraryError(f'"{dup.title}" already has that ISBN — add copies to it instead.')
            book.save()
            n = _int(request.POST.get('copies')) or 0
            if n:
                svc.add_copies(book, n, _branch(request), request.POST.get('shelf'),
                               donation=Donation.objects.filter(tenant=t, pk=request.POST.get('donation') or 0).first())
            messages.success(request, f'"{book.title}" added' + (f' with {n} cop{"ies" if n != 1 else "y"}.' if n
                                                                   else '. Now add copies.'))
            return redirect('library:book', pk=book.pk)
        except svc.LibraryError as problem:
            messages.error(request, str(problem))
            return redirect('library:catalogue')
    q, subject, level = request.GET.get('q', ''), request.GET.get('subject', ''), request.GET.get('level', '')
    books = svc.search(t, q, subject, level, _branch(request), bool(request.GET.get('textbooks')))
    return render(request, 'library/catalogue.html', {
        'books': books[:200], 'q': q, 'subject': subject, 'level': level, 'levels': LEVELS,
        'subjects': Book.objects.filter(tenant=t).exclude(subject='').values_list('subject', flat=True)
        .distinct().order_by('subject'),
        'branches': Branch.objects.filter(tenant=t, active=True),
        'donations': Donation.objects.filter(tenant=t)[:20]})


@_librarian_only
def isbn_lookup(request):
    try:
        data = svc.lookup_isbn(request.GET.get('isbn'))
    except svc.LibraryError as problem:
        return JsonResponse({'ok': False, 'error': str(problem)}, status=400)
    if not data:
        return JsonResponse({'ok': False, 'error': 'Not found online — type the details in.'})
    dup = Book.objects.filter(tenant=request.tenant, isbn=data['isbn']).first()
    return JsonResponse({'ok': True, 'book': data, 'existing': dup.pk if dup else None})


@_librarian_only
def book(request, pk):
    t = request.tenant
    b = get_object_or_404(Book, pk=pk, tenant=t)
    if request.method == 'POST':
        a = request.POST.get('action')
        try:
            if a == 'edit':
                _book_fields(b, request.POST)
                b.save()
            elif a == 'copies':
                codes = (request.POST.get('barcodes') or '').split()
                made = svc.add_copies(b, _int(request.POST.get('count')) or 1, _branch(request),
                                      request.POST.get('shelf'), _money(request.POST.get('price'), True),
                                      Donation.objects.filter(tenant=t, pk=request.POST.get('donation') or 0).first(),
                                      codes)
                messages.success(request, f'{len(made)} cop{"ies" if len(made) != 1 else "y"} added: '
                                          f'{made[0].barcode}' + (f' to {made[-1].barcode}' if len(made) > 1 else ''))
                return redirect('library:book', pk=b.pk)
            elif a == 'copy':
                c = get_object_or_404(Copy, pk=request.POST.get('id'), book=b)
                status = request.POST.get('status')
                if status in ('IN', 'REPAIR', 'GONE') and c.status in ('IN', 'REPAIR', 'GONE', 'MISSING', 'LOST'):
                    if status == 'IN' and c.status in ('LOST', 'MISSING'):
                        svc.found_again(c, request.user)
                    else:
                        c.status = status
                if request.POST.get('condition') in dict(Copy.CONDITIONS):
                    c.condition = request.POST['condition']
                c.shelf = (request.POST.get('shelf') or '')[:40]
                c.branch = _branch(request)
                c.save()
            messages.success(request, 'Saved.')
        except svc.LibraryError as problem:
            messages.error(request, str(problem))
        return redirect('library:book', pk=b.pk)
    return render(request, 'library/book.html', {
        'b': b, 'copies': b.copies.select_related('branch', 'donation'),
        'loans': Loan.objects.filter(copy__book=b).select_related('copy', 'student', 'staff')[:20],
        'queue': b.reservations.filter(status__in=('WAITING', 'READY')).select_related('student', 'staff'),
        'levels': LEVELS, 'conditions': Copy.CONDITIONS, 'branches': Branch.objects.filter(tenant=t, active=True),
        'donations': Donation.objects.filter(tenant=t)[:20], 'avail': svc.availability(b)})


@_librarian_only
def labels(request):
    t = request.tenant
    copies = Copy.objects.filter(tenant=t).select_related('book')
    if request.GET.get('book'):
        copies = copies.filter(book_id=request.GET['book'])
    elif request.GET.get('ids'):
        copies = copies.filter(pk__in=[i for i in request.GET['ids'].split(',') if i.isdigit()])
    elif request.GET.get('since'):
        copies = copies.filter(acquired_on__gte=_date(request.GET['since']) or timezone.localdate())
    else:
        copies = copies.filter(acquired_on=timezone.localdate())
    return render(request, 'library/labels.html', {'items': svc.labels(copies.exclude(status='GONE')[:300]),
                                                   'school': t.name})


# ─────────────────────────────────────────────────────────────────────────────
# Loans, charges (F363, F372), reservations (F364)
# ─────────────────────────────────────────────────────────────────────────────

@_librarian_only
def loans(request):
    t = request.tenant
    if request.method == 'POST':
        loan = get_object_or_404(Loan, pk=request.POST.get('id'), tenant=t)
        try:
            if request.POST.get('action') == 'lost':
                out = svc.mark_lost(loan, request.user)
                messages.success(request, f'Recorded as lost; ₦{sum(c.amount for c in out):,.2f} charged.')
            elif request.POST.get('action') == 'renew':
                svc.renew(loan, request.user)
                messages.success(request, 'Renewed.')
        except svc.LibraryError as problem:
            messages.error(request, str(problem))
        return redirect(request.get_full_path())
    show = request.GET.get('show', 'overdue')
    qs = Loan.objects.filter(tenant=t).select_related('copy__book', 'student__class_assigned', 'staff')
    if show == 'overdue':
        qs = qs.filter(returned_at__isnull=True, due_on__lt=timezone.localdate()).order_by('due_on')
    elif show == 'out':
        qs = qs.filter(returned_at__isnull=True).order_by('due_on')
    elif show == 'rentals':
        qs = qs.filter(returned_at__isnull=True, rental_term__isnull=False)
    rows = list(qs[:300])
    for loan in rows:
        loan.fine = svc.fine_for(loan) if not loan.returned_at else None
    return render(request, 'library/loans.html', {'rows': rows, 'show': show, 'today': timezone.localdate()})


@_librarian_only
def charges(request):
    t = request.tenant
    if request.method == 'POST':
        c = get_object_or_404(Charge, pk=request.POST.get('id'), tenant=t)
        try:
            if request.POST.get('action') == 'wallet':
                svc.pay_from_wallet(c, request.user)
            else:
                svc.settle(c, request.POST.get('action'), request.user, request.POST.get('note'))
            messages.success(request, 'Saved.')
        except svc.LibraryError as problem:
            messages.error(request, str(problem))
        return redirect(request.get_full_path())
    status = request.GET.get('status', 'UNPAID')
    qs = Charge.objects.filter(tenant=t).select_related('student', 'staff')
    if status in ('UNPAID', 'PAID', 'WAIVED'):
        qs = qs.filter(status=status)
    return render(request, 'library/charges.html', {'rows': qs[:300], 'status': status})


@_librarian_only
def reservations(request):
    t = request.tenant
    if request.method == 'POST':
        r = get_object_or_404(Reservation, pk=request.POST.get('id'), tenant=t)
        try:
            svc.cancel_reservation(r)
            messages.success(request, 'Cancelled.')
        except svc.LibraryError as problem:
            messages.error(request, str(problem))
        return redirect('library:reservations')
    qs = Reservation.objects.filter(tenant=t, status__in=('WAITING', 'READY')).select_related(
        'book', 'student__class_assigned', 'staff', 'copy')
    return render(request, 'library/reservations.html', {'ready': [r for r in qs if r.status == 'READY'],
                                                         'waiting': [r for r in qs if r.status == 'WAITING']})


# ─────────────────────────────────────────────────────────────────────────────
# F365 e-books, F376 past questions
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def eresources(request):
    t = request.tenant
    lib = svc.is_librarian(request.user, t)
    student, staff = _me(request)
    if not (lib or student or staff):
        return _deny(request, 'The e-library is for students and staff.')
    if request.method == 'POST' and lib:
        try:
            if request.POST.get('action') == 'delete':
                EResource.objects.filter(tenant=t, pk=request.POST.get('id')).delete()
            else:
                f = request.FILES.get('file')
                if not f or not (request.POST.get('title') or '').strip():
                    raise svc.LibraryError('Give a title and choose the file.')
                from users.records import check_upload
                try:
                    check_upload(f)
                except ValueError as problem:
                    raise svc.LibraryError(str(problem))
                EResource.objects.create(
                    tenant=t, title=request.POST['title'].strip()[:200],
                    kind=request.POST.get('kind') if request.POST.get('kind') in dict(EResource.KINDS) else 'EBOOK',
                    authors=(request.POST.get('authors') or '')[:200], subject=(request.POST.get('subject') or '')[:80],
                    level=request.POST.get('level') if request.POST.get('level') in dict(LEVELS) else 'ALL',
                    exam=request.POST.get('exam') if request.POST.get('exam') in dict(EResource.EXAMS) else '',
                    year=_int(request.POST.get('year')), paper=(request.POST.get('paper') or '')[:60], file=f,
                    licence_note=(request.POST.get('licence_note') or '')[:200],
                    for_students=bool(request.POST.get('for_students')), uploaded_by=request.user)
            messages.success(request, 'Saved.')
        except svc.LibraryError as problem:
            messages.error(request, str(problem))
        return redirect('library:eresources')
    qs = EResource.objects.filter(tenant=t)
    if student:
        qs = qs.filter(for_students=True)
    for key in ('kind', 'exam', 'subject'):
        if request.GET.get(key):
            qs = qs.filter(**{key: request.GET[key]})
    if request.GET.get('year', '').isdigit():
        qs = qs.filter(year=int(request.GET['year']))
    if request.GET.get('q'):
        qs = qs.filter(title__icontains=request.GET['q'])
    return render(request, 'library/eresources.html', {
        'items': qs[:300], 'lib': lib, 'kinds': EResource.KINDS, 'exams': EResource.EXAMS, 'levels': LEVELS,
        'subjects': EResource.objects.filter(tenant=t).exclude(subject='').values_list('subject', flat=True)
        .distinct().order_by('subject'),
        'years': EResource.objects.filter(tenant=t, year__isnull=False).values_list('year', flat=True)
        .distinct().order_by('-year'), 'get': request.GET})


@login_required
def eresource_file(request, pk):
    t = request.tenant
    r = get_object_or_404(EResource, pk=pk, tenant=t)
    student, staff = _me(request)
    if not (svc.is_librarian(request.user, t) or staff or (student and r.for_students)):
        raise Http404
    EResource.objects.filter(pk=r.pk).update(downloads=F('downloads') + 1)
    return _serve(r.file, r.title[:60])


# ─────────────────────────────────────────────────────────────────────────────
# F366 reading logs, F374 challenges
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def reading(request):
    t = request.tenant
    lib = svc.is_librarian(request.user, t)
    staff = svc.staff_of(request.user, t)
    if not (lib or staff):
        return _deny(request, 'Only staff can approve reading logs.')
    if request.method == 'POST':
        a = request.POST.get('action')
        try:
            if a in ('approve', 'reject'):
                entry = get_object_or_404(ReadingLog, pk=request.POST.get('id'), tenant=t)
                won = svc.approve_log(entry, request.user, a == 'approve')
                if won:
                    messages.success(request, f'{entry.student.full_name} earned: ' + ', '.join(c.badge for c in won))
            elif a == 'challenge' and lib:
                starts, ends = _date(request.POST.get('starts')), _date(request.POST.get('ends'))
                if not ((request.POST.get('title') or '').strip() and starts and ends and ends >= starts):
                    raise svc.LibraryError('Give a title and the start and end dates.')
                ch = ReadingChallenge.objects.create(
                    tenant=t, title=request.POST['title'].strip()[:160], starts=starts, ends=ends,
                    target=_int(request.POST.get('target')) or 5,
                    badge=(request.POST.get('badge') or 'Reading Champion').strip()[:80],
                    description=(request.POST.get('description') or '').strip(), created_by=request.user)
                ch.classes.set(Class.objects.filter(tenant=t, pk__in=request.POST.getlist('classes')))
                messages.success(request, 'Challenge started.')
        except svc.LibraryError as problem:
            messages.error(request, str(problem))
        return redirect('library:reading')
    pending = ReadingLog.objects.filter(tenant=t, approved=False).select_related('student__class_assigned')
    if not lib:
        pending = pending.filter(student__class_assigned__form_teachers=staff)
    return render(request, 'library/reading.html', {
        'pending': pending[:100], 'lib': lib,
        'challenges': ReadingChallenge.objects.filter(tenant=t).prefetch_related('classes')[:20],
        'classes': Class.objects.filter(tenant=t).order_by('name'), 'today': timezone.localdate()})


@login_required
def challenge(request, pk):
    ch = get_object_or_404(ReadingChallenge, pk=pk, tenant=request.tenant)
    return render(request, 'library/challenge.html', {'ch': ch, 'board': svc.challenge_board(ch),
                                                      'badges': ch.badges.count()})


# ─────────────────────────────────────────────────────────────────────────────
# F368 stock-taking
# ─────────────────────────────────────────────────────────────────────────────

@_librarian_only
def stocktakes(request):
    t = request.tenant
    if request.method == 'POST':
        try:
            st = svc.start_stocktake(t, request.user, _branch(request), request.POST.get('note'))
            return redirect('library:stocktake', pk=st.pk)
        except svc.LibraryError as problem:
            messages.error(request, str(problem))
        return redirect('library:stocktakes')
    return render(request, 'library/stocktakes.html', {
        'items': StockTake.objects.filter(tenant=t).select_related('branch', 'started_by')[:30],
        'branches': Branch.objects.filter(tenant=t, active=True)})


@_librarian_only
def stocktake(request, pk):
    st = get_object_or_404(StockTake.objects.select_related('branch'), pk=pk, tenant=request.tenant)
    if request.method == 'POST':
        try:
            if request.POST.get('action') == 'close':
                s = svc.close_stocktake(st, request.user, bool(request.POST.get('mark_missing')))
                messages.success(request, f'Stock-take closed: {len(s["missing"])} cop'
                                          f'{"ies" if len(s["missing"]) != 1 else "y"} not found.')
            else:
                codes = (request.POST.get('barcode') or '').split()
                done = []
                for code in codes:
                    try:
                        done.append(svc.stock_scan(st, code, request.user))
                    except svc.LibraryError as problem:
                        messages.error(request, str(problem))
                if done:
                    messages.success(request, f'Counted {len(done)}: ' + ', '.join(c.book.title for c in done[:5]))
        except svc.LibraryError as problem:
            messages.error(request, str(problem))
        return redirect('library:stocktake', pk=st.pk)
    return render(request, 'library/stocktake.html', {'st': st, 's': svc.stocktake_summary(st)})


# ─────────────────────────────────────────────────────────────────────────────
# F369 purchase requests, F379 donations, F377 visits
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def requests_page(request):
    t = request.tenant
    lib = svc.is_librarian(request.user, t)
    if not (lib or svc.staff_of(request.user, t)):
        return _deny(request, 'Only staff can ask for new books.')
    if request.method == 'POST':
        try:
            if request.POST.get('action') == 'decide' and lib:
                pr = get_object_or_404(PurchaseRequest, pk=request.POST.get('id'), tenant=t)
                svc.decide_purchase(pr, request.POST.get('status'), request.user, request.POST.get('note'))
            else:
                svc.request_purchase(t, request.user, request.POST)
            messages.success(request, 'Saved.')
        except svc.LibraryError as problem:
            messages.error(request, str(problem))
        return redirect('library:requests')
    qs = PurchaseRequest.objects.filter(tenant=t).select_related('requested_by')
    if not lib:
        qs = qs.filter(requested_by=request.user)
    return render(request, 'library/requests.html', {'items': qs[:100], 'lib': lib,
                                                     'statuses': PurchaseRequest.STATUSES})


@_librarian_only
def donations(request):
    t = request.tenant
    if request.method == 'POST':
        try:
            if request.POST.get('action') == 'thank':
                svc.acknowledge_donation(get_object_or_404(Donation, pk=request.POST.get('id'), tenant=t))
                messages.success(request, 'Thank-you letter emailed.')
            else:
                d = svc.record_donation(t, request.user, request.POST)
                messages.success(request, f'Donation from {d.donor_name} recorded. Add the books from the catalogue '
                                          f'and choose this donation.')
        except svc.LibraryError as problem:
            messages.error(request, str(problem))
        return redirect('library:donations')
    items = list(Donation.objects.filter(tenant=t)[:50])
    for d in items:
        d.n = d.copies.count()
    return render(request, 'library/donations.html', {'items': items, 'today': timezone.localdate()})


@_librarian_only
def visits(request):
    t = request.tenant
    if request.method == 'POST':
        at = timezone.now()
        code = (request.POST.get('code') or '').strip()
        try:
            purpose = request.POST.get('purpose') if request.POST.get('purpose') in dict(Visit.PURPOSES) else 'READ'
            if code:
                st, _staff = svc.resolve_borrower(t, code)
                if not st:
                    raise svc.LibraryError('Student not recognised.')
                Visit.objects.create(tenant=t, student=st, purpose=purpose, at=at, branch=_branch(request),
                                     recorded_by=request.user)
                messages.success(request, f'{st.full_name} signed in.')
            else:
                cls = Class.objects.filter(tenant=t, pk=request.POST.get('school_class') or 0).first()
                if not cls:
                    raise svc.LibraryError('Scan a card or choose a class.')
                Visit.objects.create(tenant=t, school_class=cls, headcount=_int(request.POST.get('headcount')) or 1,
                                     purpose=purpose, at=at, branch=_branch(request), recorded_by=request.user)
                messages.success(request, f'{cls.name} visit recorded.')
        except svc.LibraryError as problem:
            messages.error(request, str(problem))
        return redirect('library:visits')
    days = _int(request.GET.get('days')) or 30
    return render(request, 'library/visits.html', {
        'stats': svc.visit_stats(t, days), 'days': days,
        'recent': Visit.objects.filter(tenant=t).select_related('student', 'school_class')[:25],
        'classes': Class.objects.filter(tenant=t).order_by('name'), 'purposes': Visit.PURPOSES,
        'branches': Branch.objects.filter(tenant=t, active=True), 'hours': svc.settings_for(t).opening_hours})


# ─────────────────────────────────────────────────────────────────────────────
# F367 reports, settings and branches
# ─────────────────────────────────────────────────────────────────────────────

@_librarian_only
def reports(request):
    t = request.tenant
    if request.method == 'POST':
        a = request.POST.get('action')
        if a == 'branch' and (request.POST.get('name') or '').strip():
            Branch.objects.create(tenant=t, name=request.POST['name'].strip()[:80],
                                  location=(request.POST.get('location') or '')[:160])
        elif a == 'branch_toggle':
            br = get_object_or_404(Branch, pk=request.POST.get('id'), tenant=t)
            br.active = not br.active
            br.save(update_fields=['active'])
        elif a == 'settings':
            cfg = svc.settings_for(t)
            try:
                for f in ('loan_days', 'student_max_loans', 'staff_max_loans', 'max_renewals', 'grace_days',
                          'remind_days_before', 'hold_days'):
                    v = _int(request.POST.get(f))
                    if v is not None:
                        setattr(cfg, f, v)
                cfg.fine_per_day, cfg.fine_cap = _money(request.POST.get('fine_per_day')), \
                    _money(request.POST.get('fine_cap'))
            except svc.LibraryError as problem:
                messages.error(request, str(problem))
                return redirect('library:reports')
            cfg.block_when_owing = bool(request.POST.get('block_when_owing'))
            cfg.opening_hours = (request.POST.get('opening_hours') or '').strip()
            cfg.save()
            cfg.managers.set(Staff.objects.filter(tenant=t, pk__in=request.POST.getlist('managers')))
        messages.success(request, 'Saved.')
        return redirect('library:reports')
    days = _int(request.GET.get('days')) or 90
    cfg = svc.settings_for(t)
    return render(request, 'library/reports.html', {
        'r': svc.report(t, days), 'days': days, 'cfg': cfg, 'branches': Branch.objects.filter(tenant=t),
        'staff': Staff.objects.filter(tenant=t, user__is_active=True).order_by('full_name'),
        'mgrs': set(cfg.managers.values_list('pk', flat=True))})


# ─────────────────────────────────────────────────────────────────────────────
# F373 — clearance
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def clearance(request, student_id):
    from users.views_profile import can_view
    st = get_object_or_404(Student.objects.select_related('class_assigned'), pk=student_id, tenant=request.tenant)
    if not (svc.is_librarian(request.user, request.tenant) or can_view(request.user)):
        return _deny(request)
    return render(request, 'library/clearance.html', {'st': st, 'c': svc.clearance(st), 'today': timezone.localdate(),
                                                      'school': request.tenant.name})


# ─────────────────────────────────────────────────────────────────────────────
# F371 — catalogue search for everyone, with reservations
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def search(request):
    t = request.tenant
    q, subject, level = request.GET.get('q', ''), request.GET.get('subject', ''), request.GET.get('level', '')
    branch = _branch(request)
    books = svc.search(t, q, subject, level, branch) if (q or subject or level or branch) else \
        Book.objects.none()
    student, _staff = _me(request)
    return render(request, 'library/search.html', {
        'books': books[:100], 'q': q, 'subject': subject, 'level': level, 'levels': LEVELS,
        'subjects': Book.objects.filter(tenant=t).exclude(subject='').values_list('subject', flat=True)
        .distinct().order_by('subject'),
        'branches': Branch.objects.filter(tenant=t, active=True), 'branch': branch,
        'new': Book.objects.filter(tenant=t).order_by('-created_at')[:8] if not books else [],
        'recs': svc.recommendations(student, 6) if student and not books else [],
        'hours': svc.settings_for(t).opening_hours, 'portal': bool(getattr(request.user, 'is_parent', False)) and not student})


@login_required
def search_book(request, pk):
    t = request.tenant
    b = get_object_or_404(Book, pk=pk, tenant=t)
    student, staff = _me(request)
    if request.method == 'POST':
        try:
            if not (student or staff):
                raise svc.LibraryError('Students and staff can reserve books.')
            svc.reserve(b, student, staff)
            messages.success(request, 'Reserved. We will tell you when it is ready at the desk.')
        except svc.LibraryError as problem:
            messages.error(request, str(problem))
        return redirect('library:search_book', pk=b.pk)
    return render(request, 'library/search_book.html', {'b': b, 'avail': svc.availability(b),
                                                        'can_reserve': bool(student or staff)})


# ─────────────────────────────────────────────────────────────────────────────
# My library (students and staff)
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def mine(request):
    student, staff = _me(request)
    if not (student or staff):
        return _deny(request, 'This page is for students and staff.')
    who = {'student': student} if student else {'staff': staff}
    if request.method == 'POST':
        a = request.POST.get('action')
        try:
            if a == 'renew':
                svc.renew(get_object_or_404(Loan, pk=request.POST.get('id'), **who), request.user)
                messages.success(request, 'Renewed.')
            elif a == 'cancel':
                svc.cancel_reservation(get_object_or_404(Reservation, pk=request.POST.get('id'), **who))
                messages.success(request, 'Reservation cancelled.')
            elif a == 'log' and student:
                bk = Book.objects.filter(tenant=request.tenant, pk=request.POST.get('book') or 0).first()
                svc.log_reading(student, request.POST, bk)
                messages.success(request, 'Added to your reading log. A teacher or the librarian will check it.')
        except svc.LibraryError as problem:
            messages.error(request, str(problem))
        return redirect('library:mine')
    challenges = []
    if student:
        today = timezone.localdate()
        for ch in ReadingChallenge.objects.filter(tenant=request.tenant, ends__gte=today - timedelta(days=30)):
            if svc.challenge_open_to(ch, student):
                n = svc.challenge_count(ch, student)
                challenges.append({'ch': ch, 'n': n, 'pct': min(100, round(100 * n / ch.target)) if ch.target else 0,
                                   'won': ch.badges.filter(student=student).exists()})
    return render(request, 'library/mine.html', {
        'student': student, 'staff': staff, 'loans': svc.open_loans(**who),
        'reservations': Reservation.objects.filter(status__in=('WAITING', 'READY'), **who).select_related('book'),
        'charges': Charge.objects.filter(status='UNPAID', **who),
        'logs': ReadingLog.objects.filter(student=student)[:30] if student else [],
        'challenges': challenges, 'recs': svc.recommendations(student) if student else [],
        'history': Loan.objects.filter(returned_at__isnull=False, **who).select_related('copy__book')[:15],
        'today': timezone.localdate()})


# ─────────────────────────────────────────────────────────────────────────────
# Parents
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def family(request):
    from users.views_achievements import _parent_children
    kids = list(_parent_children(request))
    if not kids:
        return _deny(request, 'This page is for parents.')
    if request.method == 'POST':
        c = get_object_or_404(Charge, pk=request.POST.get('id'), student__in=kids)
        try:
            svc.pay_from_wallet(c, request.user)
            messages.success(request, 'Paid from your family wallet.')
        except svc.LibraryError as problem:
            messages.error(request, str(problem))
        return redirect('library:family')
    cards = [{'kid': k, 'loans': svc.open_loans(student=k),
              'charges': Charge.objects.filter(student=k, status='UNPAID'),
              'logs': ReadingLog.objects.filter(student=k, approved=True)[:8],
              'badges': k.reading_badges.select_related('challenge'),
              'read': ReadingLog.objects.filter(student=k, approved=True).count(),
              'ready': Reservation.objects.filter(student=k, status='READY').select_related('book')}
             for k in kids if k.status in ENROLLED_STATUSES]
    return render(request, 'library/family.html', {'page_title': 'Library', 'cards': cards,
                                                   'today': timezone.localdate()})
