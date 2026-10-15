"""
analytics/export.py
───────────────────
One way to hand any table back as a spreadsheet: `?export=csv` or
`?export=xlsx` on an analytics page returns the same rows the page shows.
"""
import csv
from io import BytesIO

from django.http import HttpResponse
from django.utils.text import slugify


def wanted(request):
    fmt = (request.GET.get('export') or '').lower()
    return fmt if fmt in ('csv', 'xlsx') else None


def respond(fmt, title, columns, rows, school=''):
    name = slugify(f'{school} {title}')[:80] or 'export'
    if fmt == 'xlsx':
        from openpyxl import Workbook
        from openpyxl.styles import Font
        wb = Workbook()
        ws = wb.active
        ws.title = (title or 'Export')[:31].replace('/', '-')
        ws.append([str(c) for c in columns])
        for cell in ws[1]:
            cell.font = Font(bold=True)
        for r in rows:
            ws.append([v if isinstance(v, (int, float)) or v is None else str(v) for v in r])
        for col in ws.columns:
            width = max(len(str(c.value or '')) for c in col)
            ws.column_dimensions[col[0].column_letter].width = min(50, max(10, width + 2))
        buf = BytesIO()
        wb.save(buf)
        resp = HttpResponse(buf.getvalue(),
                            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        resp['Content-Disposition'] = f'attachment; filename="{name}.xlsx"'
        return resp
    resp = HttpResponse(content_type='text/csv; charset=utf-8')
    resp['Content-Disposition'] = f'attachment; filename="{name}.csv"'
    resp.write('﻿')                       # so Excel opens naira signs and accents correctly
    w = csv.writer(resp)
    w.writerow(columns)
    for r in rows:
        w.writerow(['' if v is None else v for v in r])
    return resp
