"""
admissions/views.py
───────────────────
Public: a parent applies and can check the status of their application.
Staff:  a pipeline board, an application detail page, decisions and enrolment.
"""
from users.models import ENROLLED_STATUSES
from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from datetime import datetime, time

from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from users.models import Class, Student

from .forms import AdmissionSettingsForm, ApplicationForm, DecisionForm
from .models import Application, ApplicationDocument
from .services import (change_status, enrol_applicant, find_sibling, funnel_counts,
                       generate_reference, get_settings, paystack_configured,
                       rank_applicants, record_event, schedule_exam, seats_left,
                       start_fee_payment, verify_fee_payment)


def _staff_required(view):
    """School staff only (the same rule the rest of the portal uses)."""
    @wraps(view)
    @login_required
    def wrapper(request, *args, **kwargs):
        user = request.user
        if not (user.is_superuser or user.is_staff_user):
            messages.error(request, "You do not have permission to manage admissions.")
            return redirect('dashboard:router')
        return view(request, *args, **kwargs)
    return wrapper


# ─────────────────────────────────────────────────────────────────────────────
# Public
# ─────────────────────────────────────────────────────────────────────────────

def apply(request):
    """The public application form for this school."""
    tenant = getattr(request, 'tenant', None)
    if tenant is None:
        raise Http404
    settings_obj = get_settings(tenant)

    if not settings_obj.applications_open:
        return render(request, 'admissions/closed.html',
                      {'tenant': tenant, 'settings_obj': settings_obj}, status=200)

    if request.method == 'POST':
        form = ApplicationForm(request.POST, request.FILES)
        documents = request.FILES.getlist('documents')
        if settings_obj.require_documents and not documents:
            form.add_error(None, 'Please attach at least one document.')
        if form.is_valid():
            application = form.save(commit=False)
            application.tenant = tenant
            application.reference = generate_reference(tenant)
            application.session = settings_obj.session
            application.fee_amount = settings_obj.application_fee
            application.sibling_of = find_sibling(
                tenant, application.parent_email, application.parent_phone)
            application.save()

            for upload in documents[:10]:
                ApplicationDocument.objects.create(
                    tenant=tenant, application=application, file=upload)

            record_event(application, note='Application submitted', to_status=Application.NEW)
            request.session['last_application_reference'] = application.reference
            return redirect('applied', reference=application.reference)
    else:
        # Details a parent already typed on the landing page (see apply_start)
        form = ApplicationForm(initial=request.session.pop('apply_prefill', {}))

    return render(request, 'admissions/apply.html', {
        'form': form, 'tenant': tenant, 'settings_obj': settings_obj,
    })


APPLY_PREFILL_FIELDS = ('parent_name', 'parent_phone', 'parent_email', 'full_name', 'previous_school')


def apply_start(request):
    """The short form on the landing page: keep what the parent typed (in the
    session, not the URL) and carry it into the full application form."""
    if request.method == 'POST':
        prefill = {f: request.POST.get(f, '').strip()[:255] for f in APPLY_PREFILL_FIELDS}
        request.session['apply_prefill'] = {k: v for k, v in prefill.items() if v}
    return redirect('apply')


def applied(request, reference):
    """Confirmation page with the reference to keep."""
    application = get_object_or_404(Application, reference=reference)
    return render(request, 'admissions/applied.html', {'application': application})


def check_status(request):
    """A parent looks up their application with the reference."""
    application = None
    searched = False
    reference = (request.GET.get('reference') or '').strip().upper()
    if reference:
        searched = True
        application = Application.objects.filter(reference=reference).first()
    return render(request, 'admissions/status.html', {
        'application': application, 'reference': reference, 'searched': searched,
    })




def pay_fee(request, reference):
    """Parent pays the application fee online (Paystack)."""
    application = get_object_or_404(Application, reference=reference)
    if application.fee_paid:
        messages.info(request, 'This application fee has already been paid.')
        return redirect('applied', reference=reference)

    callback = request.build_absolute_uri(
        f'/apply/{application.reference}/fee/callback/')
    url, error = start_fee_payment(application, callback)
    if error:
        return render(request, 'admissions/fee_error.html',
                      {'application': application, 'error': error}, status=200)
    return redirect(url)


def fee_callback(request, reference):
    """Paystack sends the parent back here after paying."""
    application = get_object_or_404(Application, reference=reference)
    paid, error = verify_fee_payment(
        application, request.GET.get('reference') or request.GET.get('trxref') or '')
    if not paid:
        return render(request, 'admissions/fee_error.html',
                      {'application': application, 'error': error}, status=200)
    return redirect('applied', reference=application.reference)


# ─────────────────────────────────────────────────────────────────────────────
# Staff
# ─────────────────────────────────────────────────────────────────────────────

@_staff_required
def board(request):
    """Pipeline board: one column per stage."""
    tenant = request.tenant
    applications = Application.objects.select_related('applying_for', 'sibling_of')
    search = (request.GET.get('q') or '').strip()
    class_id = request.GET.get('class_id') or ''
    if search:
        applications = applications.filter(
            Q(full_name__icontains=search) | Q(reference__icontains=search)
            | Q(parent_name__icontains=search) | Q(parent_phone__icontains=search))
    if class_id:
        applications = applications.filter(applying_for_id=class_id)

    columns = []
    for status in Application.PIPELINE:
        items = [a for a in applications if a.status == status]
        columns.append({
            'status': status,
            'label': dict(Application.STATUS_CHOICES)[status],
            'items': items,
            'count': len(items),
        })
    closed = [a for a in applications if a.status in (Application.REJECTED,
                                                      Application.WAITLIST,
                                                      Application.WITHDRAWN)]
    return render(request, 'admissions/board.html', {
        'columns': columns, 'closed': closed, 'counts': funnel_counts(tenant),
        'classes': Class.objects.all().order_by('name'),
        'search': search, 'class_id': class_id,
        'settings_obj': get_settings(tenant),
    })


@_staff_required
def application_list(request):
    """A plain table, for schools that prefer a list to a board."""
    applications = Application.objects.select_related('applying_for', 'student')
    status = request.GET.get('status') or ''
    if status:
        applications = applications.filter(status=status)
    return render(request, 'admissions/list.html', {
        'applications': applications, 'status': status,
        'statuses': Application.STATUS_CHOICES,
        'counts': funnel_counts(request.tenant),
    })


@_staff_required
def application_detail(request, pk):
    application = get_object_or_404(
        Application.objects.select_related('applying_for', 'student', 'sibling_of'), pk=pk)

    if request.method == 'POST':
        action = request.POST.get('action')

        if action == 'save_decision':
            form = DecisionForm(request.POST, instance=application)
            if form.is_valid():
                form.save()
                record_event(application, actor=request.user, note='Scores and notes updated')
                messages.success(request, 'Saved.')
            else:
                messages.error(request, 'Please check the scores you entered.')
            return redirect('admissions:detail', pk=application.pk)

        if action == 'change_status':
            new_status = request.POST.get('status')
            valid = dict(Application.STATUS_CHOICES)
            if new_status not in valid:
                messages.error(request, 'Unknown status.')
            else:
                change_status(application, new_status, actor=request.user,
                              note=request.POST.get('note', '').strip())
                messages.success(request, f'Moved to “{valid[new_status]}”.')
            return redirect('admissions:detail', pk=application.pk)

        if action == 'add_note':
            note = request.POST.get('note', '').strip()
            if note:
                record_event(application, actor=request.user, note=note)
                messages.success(request, 'Note added.')
            return redirect('admissions:detail', pk=application.pk)

        if action == 'mark_fee_paid':
            application.fee_paid = True
            application.fee_reference = request.POST.get('fee_reference', '').strip()
            application.save(update_fields=['fee_paid', 'fee_reference', 'updated_at'])
            record_event(application, actor=request.user, note='Application fee marked as paid')
            messages.success(request, 'Application fee marked as paid.')
            return redirect('admissions:detail', pk=application.pk)

        if action == 'schedule_exam':
            raw = (request.POST.get('exam_date') or '').strip()
            when = parse_datetime(raw) or (parse_date(raw) and datetime.combine(
                parse_date(raw), time(9, 0)))
            if not when:
                messages.error(request, 'Pick a date and time for the assessment.')
            else:
                if timezone.is_naive(when):
                    when = timezone.make_aware(when)
                schedule_exam(application, when, venue=request.POST.get('exam_venue', '').strip(),
                              actor=request.user, notify=bool(request.POST.get('notify')))
                messages.success(request, f'Assessment set for {when:%d %b %Y, %I:%M %p}.')
            return redirect('admissions:detail', pk=application.pk)

        if action == 'send_offer':
            application.status = Application.OFFERED
            application.offer_sent_at = timezone.now()
            application.save(update_fields=['status', 'offer_sent_at', 'updated_at'])
            record_event(application, actor=request.user, to_status=Application.OFFERED,
                         note='Offer letter issued')
            messages.success(request, 'Offer recorded. Download or email the letter below.')
            return redirect('admissions:detail', pk=application.pk)

        if action == 'enrol':
            if application.student_id:
                messages.info(request, 'This applicant is already enrolled.')
                return redirect('admissions:detail', pk=application.pk)
            student, credentials = enrol_applicant(
                application, actor=request.user,
                send_credentials=bool(request.POST.get('send_credentials')))
            messages.success(request, f'{student.full_name} is now a student.')
            return render(request, 'admissions/enrolled.html', {
                'application': application, 'student': student, 'credentials': credentials,
            })

    return render(request, 'admissions/detail.html', {
        'application': application,
        'form': DecisionForm(instance=application),
        'statuses': Application.STATUS_CHOICES,
        'events': application.events.select_related('actor')[:50],
        'seats_left': seats_left(request.tenant, application.applying_for) if application.applying_for else None,
    })


@_staff_required
def settings_page(request):
    settings_obj = get_settings(request.tenant)
    if request.method == 'POST':
        form = AdmissionSettingsForm(request.POST, instance=settings_obj)
        if form.is_valid():
            form.save()
            messages.success(request, 'Admission settings saved.')
            return redirect('admissions:settings')
        messages.error(request, 'Please check the form.')
    else:
        form = AdmissionSettingsForm(instance=settings_obj)

    quotas = []
    for school_class in Class.objects.all().order_by('name'):
        quotas.append({
            'school_class': school_class,
            'left': seats_left(request.tenant, school_class),
            'enrolled': Student.objects.filter(class_assigned=school_class,
                                               status__in=ENROLLED_STATUSES).count(),
        })
    return render(request, 'admissions/settings.html', {
        'form': form, 'settings_obj': settings_obj, 'quotas': quotas,
        'apply_url': request.build_absolute_uri('/apply/'),
    })


@_staff_required
def set_quota(request):
    from .models import AdmissionQuota
    if request.method == 'POST':
        school_class = get_object_or_404(Class, pk=request.POST.get('class_id'))
        seats = int(request.POST.get('seats') or 0)
        AdmissionQuota.objects.update_or_create(
            tenant=request.tenant, school_class=school_class, defaults={'seats': seats})
        messages.success(request, f'Seats for {school_class} set to {seats or "no limit"}.')
    return redirect('admissions:settings')


@_staff_required
def analytics(request):
    """Funnel, sources and conversion."""
    tenant = request.tenant
    counts = funnel_counts(tenant)
    applications = Application.objects.all()
    offered = counts[Application.OFFERED] + counts[Application.ACCEPTED] + counts[Application.ENROLLED]
    enrolled = counts[Application.ENROLLED]
    by_class = (applications.values('applying_for__name')
                .annotate(n=Count('id')).order_by('-n'))
    by_source = (applications.exclude(source='').values('source')
                 .annotate(n=Count('id')).order_by('-n'))
    sources = dict(Application.SOURCE_CHOICES)
    return render(request, 'admissions/analytics.html', {
        'counts': counts,
        'total': counts['TOTAL'],
        'offered': offered,
        'enrolled': enrolled,
        'offer_rate': round(offered / counts['TOTAL'] * 100) if counts['TOTAL'] else 0,
        'enrol_rate': round(enrolled / offered * 100) if offered else 0,
        'by_class': by_class,
        'by_source': [{'label': sources.get(r['source'], r['source']), 'n': r['n']} for r in by_source],
        'recent': applications.order_by('-created_at')[:10],
        'this_month': applications.filter(
            created_at__year=timezone.now().year,
            created_at__month=timezone.now().month).count(),
    })


@_staff_required
def offer_letter(request, pk):
    """Download the offer letter as a PDF."""
    application = get_object_or_404(Application, pk=pk)
    from .pdf import build_offer_letter
    buffer = build_offer_letter(application)
    filename = f"offer-{application.reference}-{application.full_name.split(' ')[0]}.pdf"
    return FileResponse(buffer, as_attachment=True, filename=filename,
                        content_type='application/pdf')


@_staff_required
def email_offer_letter(request, pk):
    """Email the offer letter to the parent."""
    application = get_object_or_404(Application, pk=pk)
    if not application.parent_email:
        messages.error(request, 'This application has no email address.')
        return redirect('admissions:detail', pk=pk)

    from lms_project.tasks import run_in_background
    run_in_background(_send_offer_email, application.pk, tenant=request.tenant)
    application.offer_sent_at = timezone.now()
    if application.status in (Application.NEW, Application.SCREENING,
                              Application.EXAM, Application.INTERVIEW):
        application.status = Application.OFFERED
    application.save(update_fields=['offer_sent_at', 'status', 'updated_at'])
    record_event(application, actor=request.user, to_status=application.status,
                 note=f'Offer letter emailed to {application.parent_email}')
    messages.success(request, f'Offer letter is on its way to {application.parent_email}.')
    return redirect('admissions:detail', pk=pk)


def _send_offer_email(application_id):
    """Build the PDF and send it (runs in the background)."""
    from django.core.mail import EmailMessage
    from django.conf import settings as dj_settings

    from .pdf import build_offer_letter

    application = Application.objects.filter(pk=application_id).first()
    if not application:
        return 'application missing'
    school = getattr(application.tenant, 'name', 'The school')
    message = EmailMessage(
        subject=f'{school}: offer of admission for {application.full_name}',
        body=(f"Dear {application.parent_name or 'Parent/Guardian'},\n\n"
              f"We are pleased to offer {application.full_name} a place at {school}. "
              f"The offer letter is attached.\n\n"
              f"Application reference: {application.reference}\n\n"
              f"Regards,\n{school}"),
        from_email=getattr(dj_settings, 'DEFAULT_FROM_EMAIL', None),
        to=[application.parent_email],
    )
    message.attach(f'offer-{application.reference}.pdf',
                   build_offer_letter(application).read(), 'application/pdf')
    message.send(fail_silently=False)
    return f'offer emailed for {application.reference}'


@_staff_required
def ranking(request):
    """Applicants ordered by score, with the seats available in each class."""
    class_id = request.GET.get('class_id') or ''
    school_class = Class.objects.filter(pk=class_id).first() if class_id else None
    ranked = rank_applicants(request.tenant, school_class=school_class)
    return render(request, 'admissions/ranking.html', {
        'ranked': ranked, 'classes': Class.objects.all().order_by('name'),
        'class_id': class_id, 'school_class': school_class,
        'seats_left': seats_left(request.tenant, school_class) if school_class else None,
    })


@_staff_required
def waitlist(request):
    """Waitlisted applicants, best scores first, ready to be offered a place."""
    ranked = rank_applicants(request.tenant, statuses=[Application.WAITLIST])
    classes = []
    for school_class in Class.objects.all().order_by('name'):
        left = seats_left(request.tenant, school_class)
        if left:
            classes.append({'school_class': school_class, 'left': left})
    return render(request, 'admissions/waitlist.html', {
        'ranked': ranked, 'openings': classes,
    })
