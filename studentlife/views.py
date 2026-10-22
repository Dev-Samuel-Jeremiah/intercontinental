"""
studentlife/views.py
────────────────────
Student pages (the "My school life" hub and everything off it) and the
staff pages that look after them.
"""
import os

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from users.models import ENROLLED_STATUSES, Class, Student, StudentSubject, Subject
from users.views_profile import can_manage, can_view

from . import careers as cr
from . import services as svc
from .models import (Checkin, Goal, HandbookSection, LeaderboardEntry, PortfolioItem, SafetyReport, SharedNote,
                     TutorOffer, TutorSession)


def _deny(request, text='Only staff can do that.'):
    messages.error(request, text)
    return redirect('dashboard:router')


def _student(request):
    return Student.objects.filter(user=request.user, tenant=request.tenant).select_related('class_assigned').first()


def _student_only(view):
    def wrap(request, *a, **kw):
        st = _student(request)
        if not st:
            return _deny(request, 'This page is for students.')
        request.me = st
        return view(request, *a, **kw)
    wrap.__name__ = view.__name__
    return login_required(wrap)


def _serve(field):
    if not field:
        raise Http404
    from users.records import CONTENT_TYPES
    ext = os.path.splitext(field.name)[1].lower()
    try:
        return FileResponse(field.open('rb'), content_type=CONTENT_TYPES.get(ext, 'application/octet-stream'),
                            filename=f'file{ext}')
    except (FileNotFoundError, OSError):
        raise Http404


def _subjects_of(student):
    return list(StudentSubject.objects.filter(student=student).select_related('subject')
                .values_list('subject__name', flat=True))


# ─────────────────────────────────────────────────────────────────────────────
# Student hub
# ─────────────────────────────────────────────────────────────────────────────

@_student_only
def hub(request):
    me = request.me
    today = timezone.localdate()
    return render(request, 'studentlife/hub.html', {
        'me': me, 'checked_in': Checkin.objects.filter(student=me, date=today).first(),
        'goals': Goal.objects.filter(student=me, status=Goal.ACTIVE)[:4],
        'handbook_due': not svc.acknowledged(request.user, request.tenant),
        'requests': TutorSession.objects.filter(offer__student=me, status='REQUESTED').count(),
        'on_board': LeaderboardEntry.objects.filter(student=me).exists()})


# ─────────────────────────────────────────────────────────────────────────────
# F284 — leaderboards
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def leaderboards(request):
    me = _student(request)
    if not (me or can_view(request.user)):
        return _deny(request, 'Leaderboards are for students and staff.')
    if not svc.settings_for(request.tenant).leaderboards:
        return _deny(request, 'Leaderboards are switched off at this school.')
    if request.method == 'POST' and me:
        if request.POST.get('action') == 'leave':
            svc.leave_board(me)
            messages.success(request, 'You are no longer on the leaderboards.')
        else:
            svc.join_board(me, request.POST.get('nickname'))
            messages.success(request, 'You are on the leaderboards. You can leave any time.')
        return redirect('studentlife:leaderboards')
    metric = request.GET.get('board', 'points')
    if metric not in svc.BOARDS:
        metric = 'points'
    level = request.GET.get('level') or (me.class_assigned.level if me and me.class_assigned_id else '')
    entry = LeaderboardEntry.objects.filter(student=me).first() if me else None
    levels = sorted({c.level for c in Class.objects.filter(tenant=request.tenant) if c.level})
    return render(request, 'studentlife/leaderboards.html', {
        'rows': svc.board(request.tenant, metric, level or None), 'metric': metric, 'boards': svc.BOARDS,
        'level': level, 'levels': levels, 'me': me, 'entry': entry})


# ─────────────────────────────────────────────────────────────────────────────
# F286 — e-portfolio
# ─────────────────────────────────────────────────────────────────────────────

def _may_see_portfolio(request, student):
    if request.user.pk == student.user_id or can_view(request.user):
        return True
    if getattr(request.user, 'is_parent', False):
        from family.services import children_of
        return student in children_of(request.user, request.tenant)
    return False


@login_required
def portfolio(request, student_id=None):
    me = _student(request)
    student = get_object_or_404(Student, pk=student_id, tenant=request.tenant) if student_id else me
    if not student or not _may_see_portfolio(request, student):
        return _deny(request, 'You cannot see this portfolio.')
    own = me and student.pk == me.pk
    staff = svc.staff_for(request.user, request.tenant)
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action == 'add' and own:
                svc.add_item(student, request.POST, request.FILES.get('file'))
                messages.success(request, 'Added to your portfolio.')
            elif action in ('delete', 'feature') and own:
                item = get_object_or_404(PortfolioItem, pk=request.POST.get('id'), student=student)
                if action == 'delete':
                    item.delete()
                else:
                    item.featured = not item.featured
                    item.save(update_fields=['featured'])
            elif action == 'comment' and staff:
                item = get_object_or_404(PortfolioItem, pk=request.POST.get('id'), student=student)
                item.teacher_comment, item.commented_by = (request.POST.get('comment') or '').strip(), staff
                item.save(update_fields=['teacher_comment', 'commented_by'])
        except svc.LifeError as problem:
            messages.error(request, str(problem))
        return redirect(request.path)
    items = PortfolioItem.objects.filter(student=student)
    if getattr(request.user, 'is_parent', False) and not own and not staff:
        items = items.filter(share_with_family=True)
    return render(request, 'studentlife/portfolio.html', {'student': student, 'items': items, 'own': own,
                                                          'staff': staff, 'kinds': PortfolioItem.KINDS,
                                                          'today': timezone.localdate()})


@login_required
def portfolio_file(request, pk):
    item = get_object_or_404(PortfolioItem.objects.select_related('student'), pk=pk, tenant=request.tenant)
    if not _may_see_portfolio(request, item.student):
        raise Http404
    if getattr(request.user, 'is_parent', False) and not item.share_with_family:
        raise Http404
    return _serve(item.file)


# ─────────────────────────────────────────────────────────────────────────────
# F287 / F288 — careers and subject combinations
# ─────────────────────────────────────────────────────────────────────────────

@_student_only
def careers(request):
    from .models import CareerProfile
    me = request.me
    profile, _ = CareerProfile.objects.get_or_create(student=me, defaults={'tenant_id': me.tenant_id})
    subjects = _subjects_of(me)
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'quiz':
            answers = {str(i): request.POST.get(f'q{i}', '0') for i in range(len(cr.QUIZ))}
            profile.answers = answers
            profile.clusters = [k for k, v in cr.score_quiz(answers) if v > 0][:3]
            profile.save()
            messages.success(request, 'Here are the areas that suit your interests.')
        elif action == 'choose':
            name = request.POST.get('course')
            if cr.course(name):
                chosen = [c for c in profile.chosen if c != name]
                if request.POST.get('remove') != '1':
                    chosen = (chosen + [name])[-5:]
                profile.chosen = chosen
                profile.save(update_fields=['chosen', 'updated_at'])
        elif action == 'notes':
            profile.notes = (request.POST.get('notes') or '')[:4000]
            profile.save(update_fields=['notes', 'updated_at'])
        return redirect('studentlife:careers')
    scored = cr.score_quiz(profile.answers) if profile.answers else []
    return render(request, 'studentlife/careers.html', {
        'profile': profile, 'quiz': list(enumerate(cr.QUIZ)), 'subjects': subjects,
        'top': [(cr.CLUSTERS[k], v) for k, v in scored[:3] if v > 0],
        'suggest': cr.suggestions(scored, subjects) if scored else [],
        'chosen': [{'course': cr.course(n), **cr.check(cr.course(n), subjects)} for n in profile.chosen if cr.course(n)],
        'all_courses': cr.COURSES, 'notes': me.career_notes.select_related('staff')[:10]})


@login_required
def combination(request):
    """F288: check any course against any set of subjects (students, parents, staff)."""
    me = _student(request)
    chosen = request.GET.getlist('s') or (_subjects_of(me) if me else [])
    name = request.GET.get('course') or ''
    result = cr.check(cr.course(name), chosen) if cr.course(name) else None
    all_subjects = sorted({cr.canonical(s) for s in Subject.objects.filter(tenant=request.tenant)
                           .values_list('name', flat=True)} - {None} | {s for c in cr.COURSES for s in c['required']})
    return render(request, 'studentlife/combination.html', {
        'courses': cr.COURSES, 'course': name, 'chosen': [cr.canonical(s) for s in chosen], 'result': result,
        'all_subjects': all_subjects, 'row': cr.course(name)})


@login_required
def career_note(request, student_id):
    staff = svc.staff_for(request.user, request.tenant)
    if not staff:
        return _deny(request)
    from .models import CareerNote, CareerProfile
    student = get_object_or_404(Student, pk=student_id, tenant=request.tenant)
    if request.method == 'POST' and (request.POST.get('note') or '').strip():
        CareerNote.objects.create(tenant=request.tenant, student=student, staff=staff, note=request.POST['note'][:4000])
        messages.success(request, 'Note added.')
        return redirect('studentlife:career_note', student_id=student.pk)
    profile = CareerProfile.objects.filter(student=student).first()
    subjects = _subjects_of(student)
    return render(request, 'studentlife/career_staff.html', {
        'student': student, 'profile': profile, 'subjects': subjects,
        'chosen': [{'course': cr.course(n), **cr.check(cr.course(n), subjects)}
                   for n in (profile.chosen if profile else []) if cr.course(n)],
        'clusters': [cr.CLUSTERS.get(k, k) for k in (profile.clusters if profile else [])],
        'notes': student.career_notes.select_related('staff')})


# ─────────────────────────────────────────────────────────────────────────────
# F290 — handbook
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def handbook(request):
    svc.ensure_handbook(request.tenant)
    if request.method == 'POST' and request.POST.get('action') == 'ack':
        svc.acknowledge(request.user, request.tenant)
        messages.success(request, 'Thank you — noted that you have read the handbook.')
        return redirect('studentlife:handbook')
    q = (request.GET.get('q') or '').strip()
    sections = HandbookSection.objects.filter(tenant=request.tenant, published=True)
    if q:
        from django.db.models import Q
        sections = sections.filter(Q(title__icontains=q) | Q(body__icontains=q))
    return render(request, 'studentlife/handbook.html', {
        'sections': sections, 'q': q, 'acked': svc.acknowledged(request.user, request.tenant),
        'version': svc.settings_for(request.tenant).handbook_version, 'admin': can_manage(request.user)})


@login_required
def handbook_edit(request):
    if not can_manage(request.user):
        return _deny(request)
    svc.ensure_handbook(request.tenant)
    if request.method == 'POST':
        a = request.POST.get('action')
        if a == 'save':
            s = get_object_or_404(HandbookSection, pk=request.POST.get('id'), tenant=request.tenant)
            s.title, s.body = (request.POST.get('title') or s.title)[:160], request.POST.get('body') or s.body
            order = str(request.POST.get('order') or '')
            s.order = int(order) if order.isdigit() else s.order
            s.published = bool(request.POST.get('published'))
            s.save()
        elif a == 'add' and (request.POST.get('title') or '').strip():
            HandbookSection.objects.create(tenant=request.tenant, title=request.POST['title'][:160],
                                           body=request.POST.get('body') or '', order=99)
        elif a == 'version':
            v = svc.new_version(request.tenant)
            messages.success(request, f'Version {v} published — everyone will be asked to read it again.')
        return redirect('studentlife:handbook_edit')
    return render(request, 'studentlife/handbook_edit.html', {
        'sections': HandbookSection.objects.filter(tenant=request.tenant), 'rep': svc.ack_report(request.tenant)})


# ─────────────────────────────────────────────────────────────────────────────
# F291 — reports
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def report(request):
    made = None
    if request.method == 'POST':
        try:
            r, code = svc.make_report(request.tenant, request.user, request.POST)
            made = {'ref': r.reference, 'code': code, 'anonymous': not r.reporter_id}
        except svc.LifeError as problem:
            messages.error(request, str(problem))
    return render(request, 'studentlife/report.html', {'kinds': SafetyReport.KINDS, 'made': made})


@login_required
def report_check(request):
    r = None
    if request.method == 'POST':
        try:
            r = svc.find_report(request.tenant, request.POST.get('reference'), request.POST.get('code'))
            if (request.POST.get('body') or '').strip():
                svc.report_reply(r, request.POST.get('body'), from_school=False)
                messages.success(request, 'Sent to the school.')
        except svc.LifeError as problem:
            messages.error(request, str(problem))
    return render(request, 'studentlife/report_check.html', {
        'r': r, 'msgs': r.messages.filter(internal=False) if r else [],
        'reference': request.POST.get('reference', ''), 'code': request.POST.get('code', '')})


def _safeguarding(request):
    staff = svc.staff_for(request.user, request.tenant)
    leads = svc.settings_for(request.tenant).safeguarding_leads.all()
    return can_manage(request.user) or (staff is not None and staff in leads)


@login_required
def reports(request):
    if not _safeguarding(request):
        return _deny(request, 'Reports are for administrators and safeguarding leads.')
    show = request.GET.get('show', 'open')
    qs = SafetyReport.objects.filter(tenant=request.tenant)
    if show == 'open':
        qs = qs.exclude(status=SafetyReport.CLOSED)
    return render(request, 'studentlife/reports.html', {'items': qs, 'show': show})


@login_required
def report_detail(request, pk):
    if not _safeguarding(request):
        return _deny(request, 'Reports are for administrators and safeguarding leads.')
    r = get_object_or_404(SafetyReport, pk=pk, tenant=request.tenant)
    if request.method == 'POST':
        svc.report_reply(r, request.POST.get('body'), user=request.user, internal=bool(request.POST.get('internal')),
                         status=request.POST.get('status') or None)
        return redirect('studentlife:report_detail', pk=r.pk)
    return render(request, 'studentlife/report_detail.html', {'r': r, 'msgs': r.messages.select_related('author'),
                                                              'statuses': SafetyReport.STATUSES})


# ─────────────────────────────────────────────────────────────────────────────
# F292 — wellbeing
# ─────────────────────────────────────────────────────────────────────────────

@_student_only
def checkin(request):
    me = request.me
    if request.method == 'POST':
        try:
            c = svc.check_in(me, request.POST)
            messages.success(request, 'Thanks for checking in.' + (' Someone will find you for a chat soon.'
                                                                   if c.wants_talk else ''))
        except svc.LifeError as problem:
            messages.error(request, str(problem))
        return redirect('studentlife:checkin')
    return render(request, 'studentlife/checkin.html', {
        'today': Checkin.objects.filter(student=me, date=timezone.localdate()).first(), 'moods': Checkin.MOODS,
        'feelings': svc.FEELINGS, 'history': Checkin.objects.filter(student=me)[:14]})


@login_required
def wellbeing(request):
    staff = svc.staff_for(request.user, request.tenant)
    if not (staff or can_manage(request.user)):
        return _deny(request)
    if request.method == 'POST':
        c = get_object_or_404(Checkin, pk=request.POST.get('id'), tenant=request.tenant)
        c.followed_up_by, c.followed_up_at = staff, timezone.now()
        c.save(update_fields=['followed_up_by', 'followed_up_at'])
        messages.success(request, 'Marked as followed up.')
        return redirect('studentlife:wellbeing')
    lead = _safeguarding(request)
    classes = Class.objects.filter(tenant=request.tenant) if lead else Class.objects.filter(form_teachers=staff)
    students = list(Student.objects.filter(class_assigned__in=classes, status__in=ENROLLED_STATUSES))
    flagged = Checkin.objects.filter(student__in=students, flagged=True, followed_up_at__isnull=True) \
        .select_related('student__class_assigned')
    summary = [(cls, svc.class_wellbeing([s for s in students if s.class_assigned_id == cls.pk])) for cls in classes]
    return render(request, 'studentlife/wellbeing.html', {'flagged': flagged, 'summary': summary, 'lead': lead})


# ─────────────────────────────────────────────────────────────────────────────
# F293 — peer tutoring
# ─────────────────────────────────────────────────────────────────────────────

@_student_only
def tutoring(request):
    me = request.me
    if request.method == 'POST':
        a = request.POST.get('action')
        try:
            if a == 'offer':
                subject = get_object_or_404(Subject, pk=request.POST.get('subject'), tenant=request.tenant)
                svc.offer(me, subject, request.POST.get('about'), request.POST.get('when'))
                messages.success(request, 'Thank you! A teacher will approve your offer before it is shown.')
            elif a == 'stop':
                TutorOffer.objects.filter(student=me, pk=request.POST.get('id')).update(active=False)
            elif a == 'request':
                o = get_object_or_404(TutorOffer, pk=request.POST.get('id'), tenant=request.tenant)
                svc.request_session(me, o, request.POST.get('topic'), request.POST.get('proposed'))
                messages.success(request, f'Request sent to {o.student.full_name.split()[0]}.')
            else:
                s = get_object_or_404(TutorSession, pk=request.POST.get('id'), tenant=request.tenant)
                svc.update_session(s, me, a, request.POST.get('rating'))
        except svc.LifeError as problem:
            messages.error(request, str(problem))
        return redirect('studentlife:tutoring')
    subject = Subject.objects.filter(tenant=request.tenant, pk=request.GET.get('subject') or 0).first()
    return render(request, 'studentlife/tutoring.html', {
        'tutors': svc.tutors(request.tenant, subject, exclude=me), 'subject': subject,
        'subjects': Subject.objects.filter(tenant=request.tenant).order_by('name'),
        'my_offers': TutorOffer.objects.filter(student=me).select_related('subject'),
        'incoming': TutorSession.objects.filter(offer__student=me).select_related('learner', 'offer__subject')[:20],
        'outgoing': TutorSession.objects.filter(learner=me).select_related('offer__student', 'offer__subject')[:20]})


@login_required
def tutoring_manage(request):
    if not can_view(request.user):
        return _deny(request)
    if request.method == 'POST':
        o = get_object_or_404(TutorOffer, pk=request.POST.get('id'), tenant=request.tenant)
        o.approved = request.POST.get('action') == 'approve'
        o.active = o.approved or o.active
        o.save(update_fields=['approved', 'active'])
        return redirect('studentlife:tutoring_manage')
    return render(request, 'studentlife/tutoring_manage.html', {
        'offers': TutorOffer.objects.filter(tenant=request.tenant).select_related('student__class_assigned', 'subject')
        .order_by('approved', '-created_at'),
        'sessions': TutorSession.objects.filter(tenant=request.tenant).select_related(
            'offer__student', 'learner', 'offer__subject')[:50]})


# ─────────────────────────────────────────────────────────────────────────────
# F296 — goals
# ─────────────────────────────────────────────────────────────────────────────

@_student_only
def goals(request):
    me = request.me
    if request.method == 'POST':
        a = request.POST.get('action')
        try:
            if a == 'add':
                svc.add_goal(me, request.POST)
                messages.success(request, 'Goal added. You can do it!')
            else:
                g = get_object_or_404(Goal, pk=request.POST.get('id'), student=me)
                if a == 'progress':
                    svc.update_goal(g, request.POST.get('progress'), request.POST.get('note'))
                elif a == 'drop':
                    g.status = Goal.DROPPED
                    g.save(update_fields=['status'])
        except svc.LifeError as problem:
            messages.error(request, str(problem))
        return redirect('studentlife:goals')
    return render(request, 'studentlife/goals.html', {
        'goals': Goal.objects.filter(student=me).prefetch_related('updates'), 'categories': Goal.CATEGORIES})


@login_required
def class_goals(request):
    staff = svc.staff_for(request.user, request.tenant)
    if not staff:
        return _deny(request)
    if request.method == 'POST':
        g = get_object_or_404(Goal, pk=request.POST.get('id'), tenant=request.tenant)
        g.teacher_comment = (request.POST.get('comment') or '')[:500]
        g.save(update_fields=['teacher_comment'])
        return redirect('studentlife:class_goals')
    classes = Class.objects.filter(tenant=request.tenant) if can_manage(request.user) else \
        Class.objects.filter(form_teachers=staff)
    items = Goal.objects.filter(student__class_assigned__in=classes).exclude(status=Goal.DROPPED) \
        .select_related('student__class_assigned')
    return render(request, 'studentlife/class_goals.html', {'items': items})


# ─────────────────────────────────────────────────────────────────────────────
# F297 — shared notes
# ─────────────────────────────────────────────────────────────────────────────

@_student_only
def notes(request):
    me = request.me
    if request.method == 'POST':
        a = request.POST.get('action')
        try:
            if a == 'share':
                subject = get_object_or_404(Subject, pk=request.POST.get('subject'), tenant=request.tenant)
                n = svc.share_note(me, subject, request.POST.get('title'), request.POST.get('body'),
                                   request.FILES.get('file'))
                messages.success(request, 'Shared!' if n.status == SharedNote.APPROVED else
                                 'Thanks! A teacher will check your notes before classmates see them.')
            else:
                n = get_object_or_404(SharedNote, pk=request.POST.get('id'), tenant=request.tenant)
                if n not in svc.notes_for(me):
                    raise Http404
                if a == 'like':
                    svc.like(n, me)
                elif a == 'report':
                    svc.report_note(n)
                    messages.success(request, 'Thanks — a teacher will look at it.')
                elif a == 'delete' and n.author_id == me.pk:
                    n.delete()
        except svc.LifeError as problem:
            messages.error(request, str(problem))
        return redirect(request.get_full_path())
    subject = Subject.objects.filter(tenant=request.tenant, pk=request.GET.get('subject') or 0).first()
    items = svc.notes_for(me)
    if subject:
        items = items.filter(subject=subject)
    return render(request, 'studentlife/notes.html', {
        'items': items, 'subject': subject, 'me': me,
        'subjects': Subject.objects.filter(tenant=request.tenant).order_by('name')})


@login_required
def note_file(request, pk):
    n = get_object_or_404(SharedNote, pk=pk, tenant=request.tenant)
    me = _student(request)
    if not (can_view(request.user) or (me and n in svc.notes_for(me))):
        raise Http404
    return _serve(n.file)


@login_required
def notes_review(request):
    if not can_view(request.user):
        return _deny(request)
    staff = svc.staff_for(request.user, request.tenant)
    if request.method == 'POST':
        n = get_object_or_404(SharedNote, pk=request.POST.get('id'), tenant=request.tenant)
        n.status = SharedNote.APPROVED if request.POST.get('action') == 'approve' else SharedNote.HIDDEN
        n.reviewed_by, n.reports = staff, 0 if n.status == SharedNote.APPROVED else n.reports
        n.save(update_fields=['status', 'reviewed_by', 'reports'])
        return redirect('studentlife:notes_review')
    from django.db.models import Q
    return render(request, 'studentlife/notes_review.html', {
        'items': SharedNote.objects.filter(tenant=request.tenant).filter(
            Q(status=SharedNote.PENDING) | Q(reports__gt=0)).select_related('author__class_assigned', 'subject')})


# ─────────────────────────────────────────────────────────────────────────────
# Settings (admins)
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def life_settings(request):
    if not can_manage(request.user):
        return _deny(request)
    from users.models import Staff
    cfg = svc.settings_for(request.tenant)
    if request.method == 'POST':
        cfg.leaderboards = bool(request.POST.get('leaderboards'))
        cfg.notes_need_approval = bool(request.POST.get('notes_need_approval'))
        cfg.tutoring = bool(request.POST.get('tutoring'))
        cfg.save()
        cfg.safeguarding_leads.set(Staff.objects.filter(tenant=request.tenant,
                                                        pk__in=request.POST.getlist('leads')))
        messages.success(request, 'Saved.')
        return redirect('studentlife:settings')
    return render(request, 'studentlife/settings.html', {
        'cfg': cfg, 'leads': set(cfg.safeguarding_leads.values_list('pk', flat=True)),
        'staff': Staff.objects.filter(tenant=request.tenant, user__is_active=True).order_by('full_name')})
