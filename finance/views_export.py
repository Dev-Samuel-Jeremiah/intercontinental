"""
finance/views_export.py
───────────────────────
The accounting export page: choose a period, check the totals, download the
books for the accountant, and keep the school's account codes.
"""
from datetime import date

from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.text import slugify

from .models import AccountingCodes, ExpenseCategory
from .services import export
from .views import _require_finance_staff


def _period(request):
    """The chosen range, defaulting to the start of this month until today."""
    today = timezone.localdate()

    def read(name, fallback):
        try:
            return date.fromisoformat(request.GET.get(name) or '')
        except ValueError:
            return fallback

    start = read('start', today.replace(day=1))
    end = read('end', today)
    if start > end:
        start, end = end, start
    return start, end


def _filename(request, start, end, extension, suffix=''):
    school = slugify(getattr(request.tenant, 'name', '') or 'school')[:40]
    return f'{school}-books-{start:%Y%m%d}-{end:%Y%m%d}{suffix}.{extension}'


@_require_finance_staff
def accounting_export(request):
    codes = AccountingCodes.for_school(request.tenant)

    if request.method == 'POST':
        changed = []
        for field in AccountingCodes.FIELDS:
            code = (request.POST.get(field) or '').strip()[:20]
            name = (request.POST.get(f'{field}_name') or '').strip()[:80]
            if code and code != getattr(codes, field):
                setattr(codes, field, code); changed.append(field)
            if name and name != getattr(codes, f'{field}_name'):
                setattr(codes, f'{field}_name', name); changed.append(field)
        codes.save()
        for category in ExpenseCategory.objects.all():
            code = (request.POST.get(f'category_{category.id}') or '').strip()[:20]
            if code != category.account_code:
                category.account_code = code
                category.save(update_fields=['account_code'])
                changed.append(category.name)
        messages.success(request, 'Account codes saved.' if changed
                         else 'No account codes changed.')
        return redirect(request.get_full_path())

    start, end = _period(request)
    entries = export.journal(request.tenant, start, end)
    debit, credit = export.totals(entries)
    fixed = [{'key': f, 'code': getattr(codes, f), 'name': getattr(codes, f'{f}_name')}
             for f in AccountingCodes.FIELDS]
    return render(request, 'finance/accounting_export.html', {
        'start': start, 'end': end, 'entries': entries[:12], 'entry_count': len(entries),
        'debit': debit, 'credit': credit, 'balanced': debit == credit,
        'trial_balance': export.trial_balance(entries),
        'fixed': fixed, 'general_code': codes.expenses,
        'categories': ExpenseCategory.objects.all(),
        'formats': export.FORMATS,
    })


@_require_finance_staff
def export_journal_csv(request):
    start, end = _period(request)
    fmt = request.GET.get('format') if request.GET.get('format') in export.FORMATS else 'generic'
    response = HttpResponse(export.journal_csv(request.tenant, start, end, fmt),
                            content_type='text/csv; charset=utf-8')
    suffix = '' if fmt == 'generic' else f'-{fmt}'
    response['Content-Disposition'] = \
        f'attachment; filename="{_filename(request, start, end, "csv", "-journal" + suffix)}"'
    return response


@_require_finance_staff
def export_workbook(request):
    start, end = _period(request)
    response = HttpResponse(
        export.workbook(request.tenant, start, end),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = \
        f'attachment; filename="{_filename(request, start, end, "xlsx")}"'
    return response
