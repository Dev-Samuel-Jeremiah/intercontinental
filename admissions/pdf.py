"""
admissions/pdf.py
─────────────────
The offer letter a school sends an admitted applicant: school letterhead,
the offer, the fee summary and the acceptance deadline.
"""
from io import BytesIO


def build_offer_letter(application):
    """Return a BytesIO holding the offer letter PDF for this application."""
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.platypus import (HRFlowable, Image, Paragraph, SimpleDocTemplate,
                                    Spacer, Table, TableStyle)

    tenant = application.tenant
    school = getattr(tenant, 'name', 'The School')
    gold = colors.HexColor('#9c7c43')
    ink = colors.HexColor('#1c1917')
    muted = colors.HexColor('#6b7280')

    styles = getSampleStyleSheet()
    h1 = ParagraphStyle('h1', parent=styles['Normal'], fontName='Helvetica-Bold',
                        fontSize=17, leading=21, textColor=ink, alignment=TA_CENTER,
                        spaceAfter=3)
    sub = ParagraphStyle('sub', parent=styles['Normal'], fontSize=8.5, leading=12,
                         textColor=muted, alignment=TA_CENTER)
    title = ParagraphStyle('t', parent=styles['Normal'], fontName='Helvetica-Bold',
                           fontSize=12.5, leading=16, textColor=gold, alignment=TA_CENTER,
                           spaceBefore=14, spaceAfter=10)
    body = ParagraphStyle('b', parent=styles['Normal'], fontSize=10.5, leading=16,
                          textColor=ink, spaceAfter=8)
    small = ParagraphStyle('s', parent=styles['Normal'], fontSize=8, leading=11,
                           textColor=muted)

    story = []

    logo = getattr(tenant, 'logo', None)
    if logo:
        try:
            # Size from the real image so the flowable reserves the right height
            # (otherwise the heading overlaps the address line).
            from PIL import Image as PILImage
            with PILImage.open(logo.path) as img:
                ratio = (img.height / img.width) if img.width else 1
            height = min(2.2 * cm, max(1.1 * cm, 2.6 * cm * ratio))
            story.append(Image(logo.path, width=height / ratio if ratio else 2.2 * cm,
                               height=height, hAlign='CENTER'))
            story.append(Spacer(1, 10))
        except Exception:
            pass  # a missing or unreadable logo must not stop the letter

    story += [
        Paragraph(school.upper(), h1),
        Paragraph(getattr(tenant, 'address', '') or '', sub),
        Paragraph(' · '.join(filter(None, [getattr(tenant, 'phone', ''),
                                           getattr(tenant, 'email', '')])), sub),
        Spacer(1, 8),
        HRFlowable(width='100%', thickness=2, color=gold),
        Paragraph('OFFER OF PROVISIONAL ADMISSION', title),
    ]

    from django.utils import timezone
    details = [
        ['Date', timezone.now().strftime('%d %B %Y')],
        ['Application', application.reference],
        ['Applicant', application.full_name],
        ['Class offered', getattr(application.applying_for, 'name', '—')],
    ]
    if application.session:
        details.append(['Session', str(application.session)])
    if application.offer_expires_on:
        details.append(['Accept before', application.offer_expires_on.strftime('%d %B %Y')])

    table = Table(details, colWidths=[4 * cm, 11.5 * cm])
    table.setStyle(TableStyle([
        ('FONT', (0, 0), (0, -1), 'Helvetica-Bold', 9.5),
        ('FONT', (1, 0), (1, -1), 'Helvetica', 9.5),
        ('TEXTCOLOR', (0, 0), (0, -1), muted),
        ('TEXTCOLOR', (1, 0), (1, -1), ink),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('LINEBELOW', (0, 0), (-1, -2), 0.4, colors.HexColor('#e5e7eb')),
    ]))
    story += [table, Spacer(1, 14)]

    parent = application.parent_name or 'Parent/Guardian'
    class_name = getattr(application.applying_for, 'name', 'the class applied for')
    story += [
        Paragraph(f'Dear {parent},', body),
        Paragraph(
            f'Following the assessment of <b>{application.full_name}</b>, we are pleased to offer '
            f'a provisional place in <b>{class_name}</b> at {school}.', body),
    ]

    if application.fee_amount and not application.fee_paid:
        story.append(Paragraph(
            f'An application fee of <b>NGN {application.fee_amount:,.0f}</b> is outstanding and '
            f'should be settled before resumption.', body))

    if application.offer_expires_on:
        story.append(Paragraph(
            f'This offer is valid until <b>{application.offer_expires_on:%d %B %Y}</b>. '
            f'Please confirm acceptance before that date so the place can be held.', body))
    else:
        story.append(Paragraph(
            'Please contact the school to confirm acceptance so the place can be held.', body))

    story += [
        Paragraph('Admission is subject to the submission of original documents and compliance '
                  'with the school\'s rules and regulations.', body),
        Spacer(1, 18),
        Paragraph('Yours faithfully,', body),
        Spacer(1, 22),
        HRFlowable(width=6 * cm, thickness=0.6, color=muted, hAlign='LEFT'),
        Paragraph('Admissions Office', small),
        Paragraph(school, small),
        Spacer(1, 16),
        HRFlowable(width='100%', thickness=0.5, color=colors.HexColor('#e5e7eb')),
        Paragraph(f'{application.reference} · Generated by {school} on '
                  f'{timezone.now():%d %b %Y}', small),
    ]

    buffer = BytesIO()
    SimpleDocTemplate(buffer, pagesize=A4, leftMargin=2.2 * cm, rightMargin=2.2 * cm,
                      topMargin=1.8 * cm, bottomMargin=1.8 * cm,
                      title=f'Offer letter — {application.full_name}').build(story)
    buffer.seek(0)
    return buffer
