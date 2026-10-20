from django.contrib.auth.decorators import login_required, user_passes_test
from django.shortcuts import get_object_or_404, render, redirect
from django.utils import timezone
from django.contrib import messages
from django.db.models import Q, Count
from .models import Announcement
from .forms import AnnouncementForm
# Users app
from users.models import (
    User,
    Student,
    Class,
    Attendance,
    ClassSubject,
    Subject,
    StudentSubject,

    TERM_OPTIONS,
    STATUS_OPTIONS,
)





def is_staff(user):
    return user.is_staff



"""
@login_required
def student_announcements(request):
    student = request.user.student_profile

    announcements = Announcement.objects.filter(
        published=True
    ).filter(
        models.Q(audience='students') | models.Q(audience='both')
    ).filter(
        models.Q(school_class=student.current_class) | models.Q(school_class__isnull=True)
    )

    return render(request, 'students/announcements.html', {
        'announcements': announcements
    })
"""



from django.contrib.auth.decorators import login_required, user_passes_test
from django.shortcuts import render, redirect
from django.utils import timezone
from django.contrib import messages

from .models import Announcement, AnnouncementRead, AnnouncementReaction
from .forms import AnnouncementForm


def is_staff(user):
    return user.is_staff


@login_required
@user_passes_test(is_staff)
def announcement_list(request):
    announcements = Announcement.objects.all()

    return render(request, 'announcements/announcement_list.html', {
        'announcements': announcements
    })


@login_required
@user_passes_test(is_staff)
def create_announcement(request):
    if request.method == 'POST':
        form = AnnouncementForm(request.POST)
        if form.is_valid():
            announcement = form.save(commit=False)
            announcement.created_by = request.user

            if announcement.published:
                announcement.published_at = timezone.now()

            announcement.save()
            messages.success(request, "Announcement created successfully.")
            return redirect('announcements:announcement_list')
    else:
        form = AnnouncementForm()

    return render(request, 'announcements/create_announcement.html', {
        'form': form
    })



from django.http import JsonResponse
from django.contrib.auth.decorators import login_required
from .models import Announcement, AnnouncementReaction

@login_required
def mark_announcement_read(request, pk=None, announcement_id=None):
    if request.method == 'POST':
        announcement = get_object_or_404(Announcement, pk=pk or announcement_id)
        # Create or update reaction object
        reaction, created = AnnouncementReaction.objects.get_or_create(
            user=request.user,
            announcement=announcement
        )
        reaction.read = True
        reaction.save()
        return JsonResponse({'status': 'success'})
    return JsonResponse({'status': 'error'})



from django.shortcuts import get_object_or_404, render
from .models import Announcement



@login_required
def announcement_detail(request, pk):
    """Display a single announcement along with parent/student info."""
    
    announcement = get_object_or_404(Announcement, pk=pk)
    # Opening it counts as reading it (read receipts for staff, F259).
    AnnouncementRead.objects.get_or_create(announcement=announcement, user=request.user,
                                           defaults={'tenant_id': announcement.tenant_id})

    # ----------------------------
    # LINKED STUDENTS
    # ----------------------------
    parent_email = request.user.email
    students = Student.objects.filter(parent_email=parent_email)
    if not students.exists():
        return render(request, "parents/error.html", {"message": "No students linked to your account."})

    selected_student_id = request.GET.get("student")
    if selected_student_id:
        student = get_object_or_404(students, id=selected_student_id)
    else:
        student = students.first()

    # ----------------------------
    # ANNOUNCEMENTS
    # ----------------------------
    now = timezone.now()
    # Fetch latest announcements visible to this parent/student
    announcements_qs = Announcement.objects.filter(
        published=True
    ).filter(
        Q(audience='parents') | Q(audience='both')
    ).filter(
        Q(school_class=student.class_assigned) | Q(school_class__isnull=True)
    ).filter(
        Q(expires_at__gte=now) | Q(expires_at__isnull=True)
    ).order_by('-published_at')

    # Track read announcements
    read_ids = set(
        AnnouncementRead.objects.filter(user=request.user).values_list('announcement_id', flat=True)
    )

    # Limit to 5 latest announcements
    announcements = []
    for ann in announcements_qs:
        if len(announcements) >= 5:
            break
        announcements.append(ann)

        # Mark as read if not already
        if ann.id not in read_ids:
            AnnouncementRead.objects.get_or_create(announcement=ann, user=request.user)

    unread_announcements_count = sum(1 for ann in announcements if ann.id not in read_ids)

    # ----------------------------
    # LIKES
    # ----------------------------
    like_count = announcement.reactions.filter(liked=True).count()
    user_liked = announcement.reactions.filter(user=request.user, liked=True).exists()

    # ----------------------------
    # CONTEXT
    # ----------------------------
    translations = list(announcement.translations.filter(approved=True))
    chosen = next((t for t in translations if t.language == request.GET.get('lang')), None)

    context = {
        "translations": translations,
        "chosen": chosen,
        "announcement": announcement,
        "like_count": like_count,
        "user_liked": user_liked,
        "students": students,
        "student": student,
        "announcements": announcements,
        "unread_announcements_count": unread_announcements_count,
    }

    return render(request, 'announcements/detail.html', context)



@login_required
def announcement_react(request, pk):
    """AJAX endpoint for liking/unliking announcements"""
    if request.method == "POST":
        announcement = get_object_or_404(Announcement, pk=pk)
        reaction, created = AnnouncementReaction.objects.get_or_create(
            announcement=announcement, user=request.user
        )
        # Toggle like
        reaction.liked = not reaction.liked
        reaction.save()
        return JsonResponse({"liked": reaction.liked, "total_likes": announcement.reactions.filter(liked=True).count()})
    return JsonResponse({"error": "Invalid request"}, status=400)

from django.http import JsonResponse
from django.contrib.auth.decorators import login_required
from .models import Announcement, AnnouncementReaction

@login_required
def announcement_like(request, pk):
    if request.method == 'POST':
        announcement = Announcement.objects.get(pk=pk)
        reaction, created = AnnouncementReaction.objects.get_or_create(
            announcement=announcement,
            user=request.user,
        )
        # Toggle like
        reaction.liked = not reaction.liked
        reaction.save()

        # Count total likes
        total_likes = announcement.reactions.filter(liked=True).count()

        return JsonResponse({
            'liked': reaction.liked,
            'total_likes': total_likes,
            'error': False
        })
    return JsonResponse({'error': True, 'message': 'Invalid request'})
