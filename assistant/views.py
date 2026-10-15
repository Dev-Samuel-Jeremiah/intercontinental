"""
assistant/views.py
──────────────────
AI tools for staff: the question generator, parent message drafts and their
review queue, announcement translations, and the AI settings and usage page.
(Lesson-note drafting lives on the lesson note page itself.)
"""
from collections import defaultdict

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.html import escape, linebreaks

from announcement.models import Announcement
from users.models import ENROLLED_STATUSES, Student, Subject
from users.views_profile import can_manage, can_see_fees, can_view

from . import providers, writers
from .models import FEATURES, PROVIDERS, AIDraft, AIUsage, AnnouncementTranslation


def _deny(request, text):
    messages.error(request, text)
    return redirect('dashboard:router')


def _staff(request):
    return request.user.staff_profiles.filter(tenant=request.tenant).first()


def can_approve(user):
    """Who may send AI-drafted messages to parents."""
    staff = getattr(user, 'staff', None)
    return can_manage(user) or bool(staff and staff.can_send_messages)


# ─────────────────────────────────────────────────────────────────────────────
# Hub
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def home(request):
    if not can_view(request.user):
        return _deny(request, 'Only staff can use the AI tools.')
    used, cap = providers.used_this_month(request.tenant), providers.allowance(request.tenant)
    return render(request, 'assistant/home.html', {
        'used': used, 'cap': cap, 'pct': min(100, round(100 * used / cap)) if cap else 0,
        'ready': bool(providers.available()), 'enabled': providers.school_settings(request.tenant).enabled,
        'pending': AIDraft.objects.filter(tenant=request.tenant, status=AIDraft.PENDING).count(),
        'is_admin': can_manage(request.user), 'approver': can_approve(request.user),
        'fees': can_see_fees(request.user)})


# ─────────────────────────────────────────────────────────────────────────────
# F174 / F175 — settings and usage
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def ai_settings(request):
    if not can_manage(request.user):
        return _deny(request, 'Only administrators can change AI settings.')
    s = providers.school_settings(request.tenant)
    valid = dict(PROVIDERS)
    if request.method == 'POST':
        s.enabled = bool(request.POST.get('enabled'))
        s.second_reviewer = bool(request.POST.get('second_reviewer'))
        s.default_provider = request.POST.get('default_provider') if request.POST.get('default_provider') in valid else ''
        s.feature_providers = {f: request.POST.get(f'p_{f}') for f, _ in FEATURES
                               if request.POST.get(f'p_{f}') in valid}
        cap = (request.POST.get('monthly_tokens') or '').replace(',', '').strip()
        s.monthly_tokens = int(cap) if cap.isdigit() else None
        s.save()
        messages.success(request, 'AI settings saved.')
        return redirect('assistant:settings')
    start = timezone.localtime().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    month = AIUsage.objects.filter(tenant=request.tenant, created_at__gte=start)
    by_feature = {r['feature']: (r['i'] or 0) + (r['o'] or 0) for r in
                  month.values('feature').annotate(i=Sum('input_tokens'), o=Sum('output_tokens'))}
    by_user = defaultdict(int)
    for r in month.filter(user__isnull=False).values('user__username', 'user__first_name', 'user__last_name') \
            .annotate(i=Sum('input_tokens'), o=Sum('output_tokens')):
        name = f"{r['user__first_name']} {r['user__last_name']}".strip() or r['user__username']
        by_user[name] += (r['i'] or 0) + (r['o'] or 0)
    used, cap = providers.used_this_month(request.tenant), providers.allowance(request.tenant)
    feature_names = dict(FEATURES)
    return render(request, 'assistant/settings.html', {
        's': s, 'providers': PROVIDERS, 'features': FEATURES, 'available': providers.available(),
        'used': used, 'cap': cap, 'pct': min(100, round(100 * used / cap)) if cap else 0,
        'by_feature': sorted(((feature_names.get(k, k), v) for k, v in by_feature.items()), key=lambda x: -x[1]),
        'by_user': sorted(by_user.items(), key=lambda x: -x[1])[:10],
        'failures': month.filter(ok=False).count(), 'calls': month.count(),
        'recent': AIUsage.objects.filter(tenant=request.tenant).select_related('user')[:25],
        'feature_names': feature_names})


# ─────────────────────────────────────────────────────────────────────────────
# F162 — question generator
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def question_generator(request):
    if not can_view(request.user):
        return _deny(request, 'Only staff can generate questions.')
    from cbt import bank
    subjects = Subject.objects.filter(tenant=request.tenant).order_by('name')
    form = {k: (request.POST.get(k) or '').strip() for k in
            ('subject', 'level', 'topic', 'count', 'difficulty', 'qtype', 'source')}
    generated = None
    if request.method == 'POST' and request.POST.get('action') == 'save':
        subject = get_object_or_404(Subject, pk=request.POST.get('subject') or 0, tenant=request.tenant)
        saved = 0
        for i in request.POST.getlist('keep'):
            if not i.isdigit():
                continue
            data = {'text': request.POST.get(f'q{i}_text'), 'correct': request.POST.get(f'q{i}_correct'),
                    'question_type': form['qtype'] or 'MCQ', 'level': form['level'], 'topic': form['topic'],
                    'difficulty': form['difficulty'], 'explanation': request.POST.get(f'q{i}_explanation'),
                    'for_practice': '1', 'marks': '1'}
            for j in range(6):
                data[f'option_{j}'] = request.POST.get(f'q{i}_option_{j}', '')
            try:
                bank.save_question(request.tenant, data, staff=_staff(request), subject=subject)
                saved += 1
            except bank.BankError as problem:
                messages.error(request, f'Question {int(i) + 1}: {problem}')
        if saved:
            messages.success(request, f'{saved} question{"s" if saved != 1 else ""} added to the question bank.')
            return redirect(f"/cbt/admin/bank/?subject={subject.pk}")
    elif request.method == 'POST':
        subject = subjects.filter(pk=form['subject'] or 0).first()
        try:
            if not subject:
                raise providers.AIError('Choose a subject.')
            generated = writers.questions(
                request.tenant, request.user, subject.name, form['level'], form['topic'],
                form['count'] or 5, form['difficulty'] or 'MEDIUM', 'TF' if form['qtype'] == 'TF' else 'MCQ',
                form['source'])
            for q in generated:
                q['slots'] = q['options'] + [''] * (6 - len(q['options'])) if form['qtype'] != 'TF' else q['options']
        except providers.AIError as problem:
            messages.error(request, str(problem))
    return render(request, 'assistant/questions.html', {
        'subjects': subjects, 'form': form, 'generated': generated,
        'difficulties': [('EASY', 'Easy'), ('MEDIUM', 'Medium'), ('HARD', 'Hard')]})


# ─────────────────────────────────────────────────────────────────────────────
# F166 / F180 — parent messages, reviewed before they are sent
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def new_message(request):
    if not can_view(request.user):
        return _deny(request, 'Only staff can draft messages to parents.')
    students = Student.objects.filter(tenant=request.tenant, status__in=ENROLLED_STATUSES) \
        .select_related('class_assigned').order_by('class_assigned__name', 'full_name')
    if request.method == 'POST':
        student = students.filter(pk=request.POST.get('student') or 0).first()
        purpose = request.POST.get('purpose') if request.POST.get('purpose') in dict(AIDraft.PURPOSES) else 'OTHER'
        channel = request.POST.get('channel') if request.POST.get('channel') in dict(AIDraft.CHANNELS) else 'EMAIL'
        language = request.POST.get('language') if request.POST.get('language') in writers.LANGUAGES else 'en'
        points = (request.POST.get('points') or '').strip()
        try:
            if not student:
                raise providers.AIError('Choose the student.')
            staff = _staff(request)
            subject, body = writers.parent_message(
                request.tenant, request.user, student, dict(AIDraft.PURPOSES)[purpose], points,
                request.POST.get('tone') or 'warm', language, 'SMS' if channel == 'SMS' else 'EMAIL',
                sender=staff.full_name if staff else request.user.get_full_name())
            draft = AIDraft.objects.create(tenant=request.tenant, student=student, purpose=purpose, language=language,
                                           channel=channel, brief=points[:4000], subject=subject, body=body,
                                           created_by=request.user)
            messages.success(request, 'Draft ready. Read it carefully and change anything before it is sent.')
            return redirect('assistant:draft', pk=draft.pk)
        except providers.AIError as problem:
            messages.error(request, str(problem))
    return render(request, 'assistant/new_message.html', {
        'students': students, 'purposes': AIDraft.PURPOSES, 'channels': AIDraft.CHANNELS,
        'tones': writers.TONES.items(), 'languages': writers.LANGUAGES.items(),
        'picked': request.POST.get('student') or request.GET.get('student') or ''})


@login_required
def drafts(request):
    if not can_view(request.user):
        return _deny(request, 'Only staff can see message drafts.')
    qs = AIDraft.objects.filter(tenant=request.tenant).select_related('student', 'created_by', 'reviewed_by')
    if not can_approve(request.user):
        qs = qs.filter(created_by=request.user)
    return render(request, 'assistant/drafts.html', {
        'pending': qs.filter(status=AIDraft.PENDING), 'done': qs.exclude(status=AIDraft.PENDING)[:40],
        'approver': can_approve(request.user)})


@login_required
def draft(request, pk):
    d = get_object_or_404(AIDraft.objects.select_related('student__class_assigned', 'created_by', 'reviewed_by'),
                          pk=pk, tenant=request.tenant)
    approver = can_approve(request.user)
    if not (approver or d.created_by_id == request.user.id):
        return _deny(request, 'You can only open your own drafts.')
    second = providers.school_settings(request.tenant).second_reviewer
    may_send = approver and not (second and d.created_by_id == request.user.id)
    if request.method == 'POST' and d.status == AIDraft.PENDING:
        action = request.POST.get('action')
        d.subject = (request.POST.get('subject') or d.subject).strip()[:200]
        d.body = (request.POST.get('body') or d.body).strip()
        if action == 'save':
            d.save(update_fields=['subject', 'body'])
            messages.success(request, 'Changes saved. It still needs approving before it is sent.')
        elif action == 'reject' and (approver or d.created_by_id == request.user.id):
            d.status, d.reviewed_by, d.reviewed_at = AIDraft.REJECTED, request.user, timezone.now()
            d.review_note = (request.POST.get('note') or '').strip()[:255]
            d.save()
            messages.success(request, 'Draft rejected — nothing was sent.')
        elif action == 'send':
            if not may_send:
                messages.error(request, 'Someone other than the writer must approve this message.' if approver
                               else 'You can\'t send messages to parents. Ask an administrator to approve it.')
                d.save(update_fields=['subject', 'body'])
                return redirect('assistant:draft', pk=d.pk)
            from communications.services import send_quick_message
            html = linebreaks(escape(d.body))
            result = send_quick_message(d.student, d.channel, d.subject, html, sent_by=request.user)
            d.status, d.reviewed_by, d.reviewed_at = AIDraft.SENT, request.user, timezone.now()
            d.send_error = '\n'.join(result.get('errors') or [])
            d.save()
            if result.get('success'):
                messages.success(request, f'Approved and sent to {d.student.full_name}\'s parent.')
            else:
                messages.error(request, 'Approved, but sending had problems: ' + d.send_error)
        return redirect('assistant:draft', pk=d.pk)
    return render(request, 'assistant/draft.html', {
        'd': d, 'approver': approver, 'may_send': may_send, 'second': second,
        'language': writers.LANGUAGES.get(d.language, d.language)})


# ─────────────────────────────────────────────────────────────────────────────
# F170 — translating announcements
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def translations(request, pk):
    if not can_view(request.user):
        return _deny(request, 'Only staff can translate announcements.')
    ann = get_object_or_404(Announcement, pk=pk, tenant=request.tenant)
    langs = AnnouncementTranslation.LANGUAGES
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'translate':
            made = 0
            for code in request.POST.getlist('languages'):
                if code not in dict(langs):
                    continue
                try:
                    title, message = writers.translate(request.tenant, request.user, ann.title, ann.message, code)
                except providers.AIError as problem:
                    messages.error(request, f'{dict(langs)[code]}: {problem}')
                    continue
                AnnouncementTranslation.objects.update_or_create(
                    announcement=ann, language=code,
                    defaults={'tenant': request.tenant, 'title': title, 'message': message, 'approved': False,
                              'approved_by': None, 'approved_at': None})
                made += 1
            if made:
                messages.success(request, f'{made} translation{"s" if made != 1 else ""} ready to check. Parents '
                                          'see a translation only after someone approves it.')
        else:
            t = get_object_or_404(AnnouncementTranslation, pk=request.POST.get('id') or 0, announcement=ann)
            if action == 'delete':
                t.delete()
                messages.success(request, 'Translation removed.')
            else:
                t.title = (request.POST.get('title') or t.title).strip()[:255]
                t.message = (request.POST.get('message') or t.message).strip()
                if action == 'approve':
                    t.approved, t.approved_by, t.approved_at = True, request.user, timezone.now()
                    messages.success(request, f'{t.get_language_display()} approved — parents can read it now.')
                else:
                    t.approved, t.approved_by, t.approved_at = False, None, None
                    messages.success(request, 'Saved. Approve it when it reads right.')
                t.save()
        return redirect('assistant:translations', pk=ann.pk)
    have = list(ann.translations.select_related('approved_by'))
    return render(request, 'assistant/translations.html', {
        'ann': ann, 'items': have, 'languages': langs, 'have': {t.language for t in have}})
