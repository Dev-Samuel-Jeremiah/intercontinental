"""
HTML → PDF (wkhtmltopdf), safely.

wkhtmltopdf is a browser. Left at its defaults it runs JavaScript and can read
files on the server, so anything that slipped into a page (a teacher's remark,
say) could pull a file such as .env into the PDF (PYSEC-2026-2860). Every PDF
is therefore made here, with scripts off and local files off. A <base> tag
points the page at the site, so pictures referenced as /media/... load over
the web like in a browser; one that cannot be loaded is left out rather than
failing the whole PDF.
"""
import re

from django.conf import settings

WKHTMLTOPDF = '/usr/bin/wkhtmltopdf'
SAFE_OPTIONS = {
    'disable-javascript': None,
    'disable-local-file-access': None,
    'load-error-handling': 'ignore',
    'load-media-error-handling': 'ignore',
    'quiet': '',
}
A4 = {'print-media-type': None, 'page-size': 'A4', 'margin-top': '5mm', 'margin-bottom': '5mm',
      'margin-left': '5mm', 'margin-right': '5mm'}


def html_to_pdf(html, **options):
    """PDF bytes for an HTML page. Raises OSError if wkhtmltopdf fails."""
    import pdfkit
    base = f'<base href="{settings.SITE_URL}/">'
    html, found = re.subn(r'(<head[^>]*>)', r'\1' + base, html, count=1, flags=re.I)
    if not found:
        html = base + html
    return pdfkit.from_string(html, False, options={**SAFE_OPTIONS, **options},
                              configuration=pdfkit.configuration(wkhtmltopdf=WKHTMLTOPDF))
