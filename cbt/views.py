from users.models import ENROLLED_STATUSES
from users.support import exam_open_for, exam_seconds, extra_time_percent, needs_large_print
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.utils import timezone
from django.http import JsonResponse, HttpResponse, HttpResponseForbidden
from django.core.paginator import Paginator
from django.urls import reverse
from django.db.models import Prefetch, Count
from django.db.models import Prefetch
from .word_parser import WordQuestionParser
from .plain_text_parser import parse_plain_text, validate_plain_text_questions
from academics.models import AcademicSession, Term
from academics.utils import get_active_session, get_active_term

from users.models import Student, Staff, Subject, StaffSubjectClass, Class

from cbt.models import (
    Exam,
    ExamPart,
    Question,
    Option,
    ExamAttempt,
    StudentAnswer,
    EssayScore,
    ExamStudentRestriction,
)

from results.services import update_term_result

from .forms import ExamForm

import pandas as pd
import io
import csv

# ------------------------
# Permissions Helper
# ------------------------
def can_manage_exams(user):
    """
    Returns True if user may create/edit exams.
    1. Superuser always allowed.
    2. Staff with role ADMIN always allowed.
    3. Staff with role TEACHER only if can_manage_exams flag is True.
    4. Other staff only if can_manage_exams flag is True.
    """
    if user.is_superuser:
        return True
    staff = getattr(user, 'staff', None)
    if staff:
        if staff.role == 'ADMIN':
            return True
        # TEACHER and all other roles: must have the explicit permission flag
        if getattr(staff, 'can_manage_exams', False):
            return True
    return False


def can_view_all_exams(user):
    """Returns True if user can see ALL exams (not just their own)."""
    if user.is_superuser:
        return True
    staff = getattr(user, 'staff', None)
    if staff:
        if staff.role == 'ADMIN':
            return True
        if getattr(staff, 'can_view_all_exams', False):
            return True
    return False

# ------------------------
# Exam Management Views
# ------------------------


def generate_times(start=6, end=22, step=30):
    """
    Generates time strings for dropdown in 12-hour format with AM/PM.
    
    Returns a list of tuples: (value, display)
      - value: "HH:MM" in 24-hour format (for backend)
      - display: "hh:mm AM/PM" (for dropdown)
    
    start: starting hour (inclusive, 24h)
    end: ending hour (inclusive, 24h)
    step: minutes interval (default 30)
    """
    times = []
    for hour in range(start, end + 1):  # include end hour
        for minute in range(0, 60, step):
            if hour == end and minute > 0:
                break  # stop if last hour + minute exceeds end
            value = f"{hour:02d}:{minute:02d}"  # backend value
            display_hour = hour % 12 or 12      # convert 24h -> 12h
            am_pm = "AM" if hour < 12 else "PM"
            display = f"{display_hour}:{minute:02d} {am_pm}"  # shown to user
            times.append((value, display))
    return times

# ------------------ CREATE EXAM ------------------
@login_required
def create_exam(request):
    # 🔐 PERMISSION CHECK
    if not can_manage_exams(request.user):
        msg = "You do not have permission to create exams."
        if request.headers.get('x-requested-with') == 'XMLHttpRequest':
            return JsonResponse({"status": "error", "message": msg})
        messages.error(request, msg)
        return redirect('dashboard:router')

    staff = getattr(request.user, 'staff', None)
    is_admin = request.user.is_superuser or (staff and staff.role == 'ADMIN')

    # BASE DATA
    sessions = AcademicSession.objects.all()
    terms = Term.objects.all()

    # SUBJECTS & CLASSES ACCESS CONTROL
    if is_admin:
        subjects = Subject.objects.all()
        classes = Class.objects.all()
    else:
        assignments = StaffSubjectClass.objects.filter(staff=staff)
        subjects = Subject.objects.filter(id__in=assignments.values('subject_id'))
        classes = Class.objects.filter(id__in=assignments.values('school_class_id'))

    # ----------------- TIME OPTIONS -----------------
    hours = generate_times()  # 06:00 → 21:30 in 30-min intervals

    # ----------------- POST: CREATE EXAM -----------------
    if request.method == "POST":
        required_fields = [
            'title', 'exam_type', 'subject', 'session',
            'term', 'duration_minutes', 'start_time', 'end_time'
        ]

        # 🚨 VALIDATION
        missing_fields = [f for f in required_fields if not request.POST.get(f)]
        if missing_fields:
            msg = "Please fill in all required fields: " + ", ".join(missing_fields)
            if request.headers.get('x-requested-with') == 'XMLHttpRequest':
                return JsonResponse({"status": "error", "message": msg})
            messages.error(request, msg)
            return redirect('cbt:cbt_create_exam')

        from datetime import datetime

        # The exam window: start date + time to end date + time (it may span several days).
        # `exam_date` is the old single-date field, still accepted.
        start_date = request.POST.get('start_date') or request.POST.get('exam_date')
        end_date = request.POST.get('end_date') or start_date
        start_time_str = request.POST.get('start_time')  # "HH:MM"
        end_time_str = request.POST.get('end_time')      # "HH:MM"

        def _window_error(msg):
            if request.headers.get('x-requested-with') == 'XMLHttpRequest':
                return JsonResponse({"status": "error", "message": msg})
            messages.error(request, msg)
            return redirect('cbt:cbt_create_exam')

        try:
            start_datetime = timezone.make_aware(datetime.strptime(f"{start_date} {start_time_str}", "%Y-%m-%d %H:%M"))
            end_datetime = timezone.make_aware(datetime.strptime(f"{end_date} {end_time_str}", "%Y-%m-%d %H:%M"))
        except (TypeError, ValueError):
            return _window_error("Choose the start date and time and the end date and time.")
        if end_datetime <= start_datetime:
            return _window_error("The exam must end after it starts — check the end date and time.")
        try:
            duration = int(request.POST.get('duration_minutes'))
        except (TypeError, ValueError):
            return _window_error("Enter the exam duration in minutes.")
        window = int((end_datetime - start_datetime).total_seconds() // 60)
        if duration > window:
            return _window_error(f"The exam lasts {duration} minutes but the window from start to end is only "
                                 f"{window} minutes. Move the end later or shorten the exam.")


        # CREATE EXAM
        exam = Exam.objects.create(
            title=request.POST.get('title'),
            exam_type=request.POST.get('exam_type'),
            subject_id=request.POST.get('subject'),
            session_id=request.POST.get('session'),
            term_id=request.POST.get('term'),
            duration_minutes=request.POST.get('duration_minutes'),
            start_time=start_datetime,
            end_time=end_datetime,
            shuffle_questions=bool(request.POST.get('shuffle_questions')),
            shuffle_options=bool(request.POST.get('shuffle_options')),
            lockdown=bool(request.POST.get('lockdown')),
            answer_review=request.POST.get('answer_review') if request.POST.get('answer_review') in dict(Exam.REVIEW_CHOICES) else 'NEVER',
            allow_retake=bool(request.POST.get('allow_retake')),
            created_by=staff if staff else None
        )


        selected_classes = request.POST.getlist('classes')
        if not selected_classes:
            exam.delete()
            msg = "Please select at least one class for the exam."
            if request.headers.get('x-requested-with') == 'XMLHttpRequest':
                return JsonResponse({"status": "error", "message": msg})
            messages.error(request, msg)
            return redirect('cbt:cbt_create_exam')

        exam.classes.set(selected_classes)

        # ----------------- SUCCESS MESSAGE -----------------
        success_msg = f"Exam '{exam.title}' created successfully. You can now add CBT or Essay sections."
        if request.headers.get('x-requested-with') == 'XMLHttpRequest':
            return JsonResponse({"status": "success", "message": success_msg, "exam_id": exam.id})

        messages.success(request, success_msg)
        return redirect('cbt:add_exam_part_redirect', exam_id=exam.id)

    # ----------------- GET REQUEST -----------------
    return render(request, 'cbt/create_exam.html', {
        'subjects': subjects,
        'classes': classes,
        'sessions': sessions,
        'terms': terms,
        'is_admin': is_admin,
        'hours': hours,
    })



from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from .models import Question, Option, ExamPart
from PIL import Image
from io import BytesIO
from django.core.files.uploadedfile import InMemoryUploadedFile
import sys

@login_required
def add_question(request):
    """
    Add a single question with optional passage and image support
    """
    # 🔐 PERMISSION CHECK
    if not can_manage_exams(request.user):
        messages.error(request, "You are not authorized to add questions.")
        return redirect('dashboard:router')
 
    staff_profile = getattr(request.user, 'staff', None)
 
    # 🔎 FETCH EXAM PARTS BASED ON ROLE
    if request.user.is_superuser or (staff_profile and staff_profile.role == 'ADMIN'):
        exam_parts = (
            ExamPart.objects
            .select_related('exam', 'exam__subject')
            .all()
            .order_by('-exam__created_at')
        )
    else:
        exam_parts = (
            ExamPart.objects
            .select_related('exam', 'exam__subject')
            .filter(exam__created_by=staff_profile)
            .order_by('-exam__created_at')
        )
 
    # 🚫 NO EXAM PARTS SAFETY
    if not exam_parts.exists():
        messages.warning(
            request,
            "No exam parts available. Please create an exam and add CBT or Essay sections first."
        )
        return redirect('cbt:cbt_create_exam')
 
    # POST: ADD QUESTION
    if request.method == 'POST':
        exam_part_id = request.POST.get('exam_part')
        question_text = request.POST.get('question_text', '').strip()
        marks = request.POST.get('marks')
        question_type = request.POST.get('question_type')
        option_texts = request.POST.getlist('option_text')
        correct_index = request.POST.get('correct_option')
        
        # 🆕 NEW FIELDS - Passage and Image
        passage = request.POST.get('passage', '').strip()
        passage_title = request.POST.get('passage_title', '').strip()
        passage_group = request.POST.get('passage_group', '').strip()
        image_caption = request.POST.get('image_caption', '').strip()
        image_file = request.FILES.get('image')
 
        # 🚨 BASIC VALIDATION
        if not exam_part_id or not question_text:
            messages.error(request, "Exam part and question text are required.")
            return redirect('cbt:cbt_add_question')
 
        try:
            marks = int(marks) if marks else 1
        except ValueError:
            messages.error(request, "Marks must be a valid number.")
            return redirect('cbt:cbt_add_question')
 
        exam_part = get_object_or_404(ExamPart, id=exam_part_id, exam__tenant=request.tenant)
        if exam_part.part_type == 'CBT' and question_type == 'SA':
            exam_part = exam_part.section_for('SA')
            messages.info(request, "Short-answer questions need a teacher to mark them, so this one was added "
                                   "to the exam's Essay section, not the objective (CBT) section.")
 
        # 🖼️ PROCESS IMAGE IF UPLOADED
        processed_image = None
        if image_file:
            try:
                # Validate file size (5MB max)
                if image_file.size > 5242880:
                    messages.error(request, "Image file size must be less than 5MB.")
                    return redirect('cbt:cbt_add_question')
                
                # Validate file type
                allowed_extensions = ['jpg', 'jpeg', 'png', 'gif']
                file_ext = image_file.name.split('.')[-1].lower()
                if file_ext not in allowed_extensions:
                    messages.error(request, "Invalid image format. Allowed: JPG, PNG, GIF")
                    return redirect('cbt:cbt_add_question')
                
                # Process and optimize image
                processed_image = process_image(image_file)
                
            except Exception as e:
                messages.error(request, f"Error processing image: {str(e)}")
                return redirect('cbt:cbt_add_question')
 
        # ✍️ ESSAY QUESTION
        if exam_part.part_type == 'ESSAY':
            question = Question.objects.create(
                exam_part=exam_part,
                question_text=question_text,
                marks=marks,
                question_type='SA',  # Essay questions are Short Answer type
                passage=passage if passage else None,
                passage_title=passage_title if passage_title else None,
                passage_group=passage_group if passage_group else None,
                image_caption=image_caption if image_caption else None
            )
            
            # Attach image if present
            if processed_image:
                question.image = processed_image
                question.save()
 
        # 🧠 CBT QUESTION
        else:
            if question_type not in ['MCQ', 'TF', 'SA']:
                messages.error(request, "Invalid question type selected.")
                return redirect('cbt:cbt_add_question')
 
            # Validate options for MCQ/TF
            if question_type in ['MCQ', 'TF']:
                if not option_texts:
                    messages.error(request, "Please provide options for the question.")
                    return redirect('cbt:cbt_add_question')
 
                try:
                    correct_index = int(correct_index)
                except (TypeError, ValueError):
                    messages.error(request, "Please select a correct option.")
                    return redirect('cbt:cbt_add_question')
 
            # Create question
            question = Question.objects.create(
                exam_part=exam_part,
                question_text=question_text,
                marks=marks,
                question_type=question_type,
                passage=passage if passage else None,
                passage_title=passage_title if passage_title else None,
                passage_group=passage_group if passage_group else None,
                image_caption=image_caption if image_caption else None,
                explanation=(request.POST.get('explanation') or '').strip(),
            )
            
            # Attach image if present
            if processed_image:
                question.image = processed_image
                question.save()
 
            # Create options for MCQ/TF
            if question_type in ['MCQ', 'TF']:
                correct_option_letter = chr(65 + correct_index)  # A, B, C, D...
                
                for idx, text in enumerate(option_texts):
                    if text.strip():
                        Option.objects.create(
                            question=question,
                            text=text.strip(),
                            is_correct=(idx == correct_index)
                        )
                
                # Store correct answer letter
                question.correct_answer = correct_option_letter
                question.save()
 
        # ✅ SUCCESS MESSAGE
        success_msg = f"✓ Question added successfully to {exam_part.exam.title} ({exam_part.get_part_type_display()})."
        
        if passage:
            success_msg += " [With passage]"
        if processed_image:
            success_msg += " [With image]"
        if passage_group:
            success_msg += f" [Group: {passage_group}]"
        
        messages.success(request, success_msg)
 
        return redirect('cbt:questions_overview')
 
    return render(request, "cbt/add_question.html", {
        "exam_parts": exam_parts
    })

def process_image(image_file):
    """
    Process and optimize uploaded image
    - Converts to RGB if needed
    - Resizes if too large
    - Compresses to reduce file size
    - Returns optimized image file
    """
    try:
        # Open image
        img = Image.open(image_file)
        
        # Convert RGBA/LA/P to RGB if necessary
        if img.mode in ('RGBA', 'LA', 'P'):
            # Create white background
            background = Image.new('RGB', img.size, (255, 255, 255))
            # Paste image with alpha channel as mask if RGBA
            if img.mode == 'RGBA':
                background.paste(img, mask=img.split()[-1])
            else:
                background.paste(img)
            img = background
        
        # Resize if too large (max 1200px width)
        max_width = 1200
        if img.width > max_width:
            ratio = max_width / img.width
            new_height = int(img.height * ratio)
            img = img.resize((max_width, new_height), Image.Resampling.LANCZOS)
        
        # Resize if too tall (max 1200px height)
        max_height = 1200
        if img.height > max_height:
            ratio = max_height / img.height
            new_width = int(img.width * ratio)
            img = img.resize((new_width, max_height), Image.Resampling.LANCZOS)
        
        # Save to BytesIO with optimization
        output = BytesIO()
        
        # Use original format or default to JPEG
        format_to_use = 'JPEG'
        file_extension = 'jpg'
        
        # Save as JPEG with quality optimization
        img.save(output, format=format_to_use, quality=85, optimize=True)
        output.seek(0)
        
        # Get original filename without extension
        original_name = image_file.name.rsplit('.', 1)[0] if '.' in image_file.name else image_file.name
        
        # Create InMemoryUploadedFile
        return InMemoryUploadedFile(
            output,
            'ImageField',
            f"{original_name}.{file_extension}",
            f'image/{format_to_use.lower()}',
            sys.getsizeof(output),
            None
        )
        
    except Exception as e:
        print(f"Image processing error: {e}")
        # Return original file if processing fails
        return image_file

 
@login_required
def admin_exam_list(request):
    """
    Display all exams for admin/teachers with statistics and filters.
    """
    from django.utils import timezone
 
    if not can_manage_exams(request.user):
        messages.error(request, "Unauthorized access")
        return redirect('dashboard:router')
 
    staff_profile = getattr(request.user, 'staff', None)
 
    # Base queryset
    if can_view_all_exams(request.user):
        base_qs = Exam.objects.all().select_related(
            'subject', 'created_by', 'session', 'term'
        ).prefetch_related('classes').annotate(
            question_count=Count('parts__questions', distinct=True)
        ).order_by('-created_at')
    else:
        base_qs = Exam.objects.filter(created_by=staff_profile).select_related(
            'subject', 'session', 'term'
        ).prefetch_related('classes').annotate(
            question_count=Count('parts__questions', distinct=True)
        ).order_by('-created_at')
 
    f_subject = request.GET.get('subject', '').strip()
    f_class   = request.GET.get('class_id', '').strip()
    f_term    = request.GET.get('term', '').strip()
    f_session = request.GET.get('session', '').strip()
    f_status  = request.GET.get('status', '').strip()
    f_search  = request.GET.get('q', '').strip()
 
    exams = base_qs
    if f_subject:
        exams = exams.filter(subject_id=f_subject)
    if f_class:
        exams = exams.filter(classes__id=f_class).distinct()
    if f_term:
        exams = exams.filter(term_id=f_term)
    if f_session:
        exams = exams.filter(session_id=f_session)
    if f_search:
        exams = exams.filter(title__icontains=f_search)
 
    now = timezone.now()
    if f_status == 'published':
        exams = exams.filter(published=True)
    elif f_status == 'draft':
        exams = exams.filter(published=False)
    elif f_status == 'active':
        exams = exams.filter(published=True, start_time__lte=now, end_time__gte=now)
    elif f_status == 'upcoming':
        exams = exams.filter(published=True, start_time__gt=now)
    elif f_status == 'past':
        exams = exams.filter(end_time__lt=now)
 
    total_exams     = base_qs.count()
    published_exams = base_qs.filter(published=True).count()
    draft_exams     = base_qs.filter(published=False).count()
    active_exams    = base_qs.filter(published=True, start_time__lte=now, end_time__gte=now).count()
    upcoming_exams  = base_qs.filter(published=True, start_time__gt=now).count()
    past_exams      = base_qs.filter(end_time__lt=now).count()
 
    all_subjects = Subject.objects.filter(
        id__in=base_qs.values_list('subject_id', flat=True)
    ).order_by('name')
    all_classes  = Class.objects.all().order_by('name')
    all_terms    = Term.objects.all().order_by('-start_date')
    all_sessions = AcademicSession.objects.all().order_by('-start_date')
 
    context = {
        'exams':           exams,
        'total_exams':     total_exams,
        'published_exams': published_exams,
        'draft_exams':     draft_exams,
        'active_exams':    active_exams,
        'upcoming_exams':  upcoming_exams,
        'past_exams':      past_exams,
        'all_subjects':    all_subjects,
        'all_classes':     all_classes,
        'all_terms':       all_terms,
        'all_sessions':    all_sessions,
        'f_subject':       f_subject,
        'f_class':         f_class,
        'f_term':          f_term,
        'f_session':       f_session,
        'f_status':        f_status,
        'f_search':        f_search,
        'filtered_count':  exams.count(),
    }
 
    return render(request, "cbt/admin_exam_list.html", context)

@login_required
def edit_exam(request, exam_id):
    # Fetch the exam
    exam = get_object_or_404(Exam, id=exam_id)

    # Permission check
    if not can_manage_exams(request.user):
        messages.error(request, "You do not have permission to edit exams.")
        return redirect('dashboard:router')

    # POST: Update exam
    if request.method == 'POST':
        form = ExamForm(request.POST, instance=exam)
        if form.is_valid():
            form.save()
            messages.success(request, f"Exam '{exam.title}' updated successfully.")
            return redirect('cbt:admin_exam_list')
        else:
            messages.error(request, "Please correct the errors in the form.")
    else:
        form = ExamForm(instance=exam)

    # Render form with context
    return render(request, 'cbt/edit_exam.html', {
        'form': form,
        'exam': exam
    })

@login_required
def delete_exam(request, exam_id):
    if not can_manage_exams(request.user):
        messages.error(request, "You do not have permission to delete exams.")
        return redirect('dashboard:router')

    exam = get_object_or_404(Exam, id=exam_id)
    exam.delete()
    messages.success(request, f"Exam '{exam.title}' deleted successfully.")
    return redirect('cbt:admin_exam_list')


@login_required
def bulk_delete_exams(request):
    if not can_manage_exams(request.user):
        messages.error(request, "Unauthorized action.")
        return redirect('dashboard:router')
 
    if request.method != 'POST':
        return redirect('cbt:admin_exam_list')
 
    exam_ids = [eid for eid in request.POST.getlist('exam_ids') if eid]
    if not exam_ids:
        messages.error(request, "No exams selected.")
        return redirect('cbt:admin_exam_list')
 
    exams = Exam.objects.filter(id__in=exam_ids)
    count = exams.count()
    exams.delete()
    messages.success(request, f"{count} exam(s) deleted successfully.")
    return redirect('cbt:admin_exam_list')



@login_required
def bulk_publish_exams(request):
    if not can_manage_exams(request.user):
        messages.error(request, "Unauthorized action.")
        return redirect('dashboard:router')
 
    if request.method == "POST":
        exam_ids = request.POST.getlist('exam_ids')
        action = request.POST.get('action')
 
        if not exam_ids:
            messages.warning(request, "No exams selected.")
            return redirect('cbt:admin_exam_list')
 
        exams = Exam.objects.filter(id__in=exam_ids)
        if action == "publish":
            exams.update(published=True)
            messages.success(request, f"{exams.count()} exam(s) published successfully.")
        elif action == "unpublish":
            exams.update(published=False)
            messages.success(request, f"{exams.count()} exam(s) unpublished successfully.")
        else:
            messages.error(request, "Invalid action.")
 
    return redirect('cbt:admin_exam_list')

@login_required
def ajax_get_classes_by_subject(request):
    subject_id = request.GET.get('subject_id')
    if not subject_id:
        return JsonResponse({"classes": []})

    if request.user.is_superuser or (getattr(request.user, 'staff', None) and request.user.staff.role == 'ADMIN'):
        classes = Class.objects.filter(class_subjects__subject_id=subject_id).distinct()
    else:
        staff = request.user.staff
        classes = Class.objects.filter(
            subject_classes__subject_id=subject_id,
            subject_classes__staff=staff
        ).distinct()

    data = [{"id": cls.id, "name": cls.name} for cls in classes]
    return JsonResponse({"classes": data})


# ------------------------
# Student Views
# ------------------------

@login_required
def available_exams(request):
    student = getattr(request.user, 'student', None)
    if not student:
        messages.error(request, "Only students can access exams")
        return redirect('dashboard:router')
 
    now = timezone.now()
    active_session = get_active_session()
    active_term = get_active_term()
 
    # --- Fetch exams for this student's class ---
    exams_qs = Exam.objects.filter(
        published=True,
        classes=student.class_assigned,
        session=active_session,
        term=active_term,
        start_time__lte=now,
        end_time__gte=now
    ).distinct().prefetch_related('parts', 'student_restrictions').order_by('start_time')
 
    # --- Apply per-student restrictions ---
    # If an exam has ANY restriction records, only allow listed students through.
    # Exams with NO restriction records are visible to all class members.
    allowed_exams = []
    for exam in exams_qs:
        restriction_ids = exam.student_restrictions.values_list('student_id', flat=True)
        if restriction_ids.exists():
            if student.id in list(restriction_ids):
                allowed_exams.append(exam)
        else:
            allowed_exams.append(exam)
    exams_qs = allowed_exams
 
    # --- Track completed attempts per exam part ---
    from . import engine
    done = {a.exam_part_id: a for a in ExamAttempt.objects.filter(student=student, completed=True)
            .select_related('exam_part__exam')}
    completed_attempts = set(done)
 
    from .models import Seat
    seats = {s.exam_id: s for s in Seat.objects.filter(student=student).select_related('hall')}
    exam_list = []
    for exam in exams_qs:
        parts_info = []
        for part in exam.parts.all():
            has_started = now >= exam.start_time
            has_ended = now > exam.end_time
            is_active = has_started and not has_ended
            is_completed = part.id in completed_attempts
 
            parts_info.append({
                "id": part.id,
                "part_type": part.part_type,
                "total_marks": part.total_marks,
                "duration_minutes": part.duration_minutes or exam.duration_minutes,
                "is_completed": is_completed,
                "allow_retake": exam.allow_retake,
                "is_active": is_active,
                "has_started": has_started,
                "has_ended": has_ended,
                "review_attempt": done[part.id].id if part.id in done and engine.review_open(done[part.id]) else None,
            })
 
        exam_list.append({
            "seat": seats.get(exam.id),
            "id": exam.id,
            "title": exam.title,
            "subject": exam.subject.name,
            "start_time": exam.start_time,
            "end_time": exam.end_time,
            "is_active": any(p['is_active'] for p in parts_info),
            "parts": parts_info
        })
 
    # Debugging: log the number of exams found
    if not exam_list:
        print(f"[DEBUG] No exams found for student {student.id}, class {student.class_assigned}, session {active_session}, term {active_term}")
 
    context = {
        "student": student,
        "exams": exam_list,
        "now": now,
    }
 
    return render(request, "cbt/available_exams.html", context)




from django.db import transaction
from django.utils import timezone
from django.shortcuts import get_object_or_404, redirect, render
from django.contrib import messages
from .models import Exam, ExamAttempt, Question, Option, StudentAnswer


from django.db import transaction

@login_required
def take_exam(request, exam_id, part_type='CBT'):
    """
    Enhanced exam taking view with passage grouping support
    """
    student = getattr(request.user, 'student', None)
    if not student:
        messages.error(request, "Unauthorized access.")
        return redirect('dashboard:router')
 
    exam = get_object_or_404(
        Exam,
        id=exam_id,
        published=True,
        session=get_active_session(),
        term=get_active_term(),
        classes=student.class_assigned
    )
 
    exam_part = exam.parts.filter(part_type=part_type).first()
    if not exam_part:
        messages.error(request, f"{part_type} section not configured.")
        return redirect('cbt:available_exams')

    # A student with extra time who has already started may finish after the
    # scheduled end; nobody may start once the window has closed.
    from . import engine
    current = ExamAttempt.objects.filter(student=student, exam_part=exam_part, completed=False).first()
    if not (exam_open_for(exam, student, bool(current), exam_part.duration_minutes or exam.duration_minutes)
            or (current and engine.may_continue(current))):
        messages.warning(request, "This exam is not currently active.")
        return redirect('cbt:available_exams')
 
    # -----------------------------
    # GET OR CREATE ATTEMPT (with proper handling)
    # -----------------------------
    try:
        # Try to get existing active attempt
        attempt = ExamAttempt.objects.get(
            student=student,
            exam_part=exam_part,
            completed=False
        )
        messages.info(request, "Continuing your ongoing exam session.")
        
    except ExamAttempt.DoesNotExist:
        # Check if there's a completed attempt
        completed_attempt = ExamAttempt.objects.filter(
            student=student,
            exam_part=exam_part,
            completed=True
        ).first()
        
        if completed_attempt:
            # Handle retake
            if not exam.allow_retake:
                messages.info(request, "You have already submitted this exam.")
                return redirect('cbt:available_exams')
            
            # Allow retake - update existing attempt
            with transaction.atomic():
                attempt = completed_attempt
                attempt.retake_number += 1
                attempt.score = 0
                attempt.completed = False
                attempt.started_at = timezone.now()
                attempt.submitted_at = None
                attempt.elapsed_seconds = attempt.extra_seconds = 0
                attempt.question_order, attempt.option_order = [], {}   # a fresh order for the retake
                attempt.integrity_events.all().delete()
                attempt.answers.all().delete()  # Clear old answers
                attempt.save()
                messages.info(request, "You are now retaking the exam.")
        else:
            # Create new attempt
            with transaction.atomic():
                attempt = ExamAttempt.objects.create(
                    student=student,
                    exam_part=exam_part,
                    retake_number=1,
                    started_at=timezone.now(),
                    completed=False,
                    score=0
                )
    
    except ExamAttempt.MultipleObjectsReturned:
        # Handle edge case of multiple active attempts (shouldn't happen but just in case)
        messages.error(request, "Multiple active exam sessions detected. Please contact administrator.")
        return redirect('cbt:available_exams')
 
    # -----------------------------
    # TIMER CALCULATION
    # -----------------------------
    remaining_seconds = engine.seconds_left(attempt)   # includes extra time and any extension
    if remaining_seconds == 0:
        # Time ran out while away (power cut, closed browser): mark what was autosaved.
        engine.finish(attempt)
        messages.warning(request, "Time is up. Your saved answers have been submitted.")
        return redirect('cbt:available_exams')

    # -----------------------------
    # FETCH QUESTIONS WITH PASSAGE GROUPING
    # -----------------------------
    questions = engine.paper(attempt)     # this candidate's fixed question and option order
 
    # Process questions for passage display
    questions_list = []
    displayed_passages = set()  # Track which passage groups have been shown
    
    for question in questions:
        question_dict = {
            'question': question,
            'show_passage': False
        }
        
        # Check if this question has a passage
        if question.passage:
            # Determine passage key (use passage_group if available, otherwise use question ID)
            passage_key = question.passage_group if question.passage_group else f"passage_{question.id}"
            
            # Show passage only if not already displayed
            if passage_key not in displayed_passages:
                question_dict['show_passage'] = True
                displayed_passages.add(passage_key)
        
        questions_list.append(question_dict)
 
    # -----------------------------
    # SUBMISSION
    # -----------------------------
    if request.method == "POST":
        # FINAL SUBMIT
        if "submit_exam" in request.POST:
            if attempt.completed:
                messages.warning(request, "This attempt is already submitted.")
                return redirect('cbt:available_exams')
 
            with transaction.atomic():
                # Save what was posted, then mark from the saved answers.
                for item in questions_list:
                    question = item['question']
                    
                    answer, _ = StudentAnswer.objects.get_or_create(
                        attempt=attempt,
                        question=question
                    )
                    key = f"question_{question.id}"
 
                    if question.question_type in ['MCQ', 'TF']:
                        option_id = request.POST.get(key)
                        answer.selected_option = Option.objects.filter(
                            id=option_id,
                            question=question
                        ).first()
                        answer.text_answer = None
                    else:
                        answer.text_answer = request.POST.get(key, "").strip()
                        answer.selected_option = None
 
                    answer.save()

                engine.finish(attempt)
            
            messages.success(request, "Exam submitted successfully.")
            return redirect('cbt:available_exams')
 
        else:
            # TEMP SAVE WITHOUT SUBMIT
            for item in questions_list:
                question = item['question']
                
                answer, _ = StudentAnswer.objects.get_or_create(
                    attempt=attempt,
                    question=question
                )
                key = f"question_{question.id}"
 
                if question.question_type in ['MCQ', 'TF']:
                    option_id = request.POST.get(key)
                    answer.selected_option = Option.objects.filter(
                        id=option_id,
                        question=question
                    ).first()
                    answer.text_answer = None
                else:
                    answer.text_answer = request.POST.get(key, "").strip()
                    answer.selected_option = None
                answer.save()
            
            messages.success(request, "Your answers have been saved. Continue or submit when ready.")
 
    # -----------------------------
    # Already selected answers - no need for dict
    # -----------------------------
    # We'll handle this in the template with JavaScript
    
    return render(request, "cbt/take_exam.html", {
        "exam":                 exam,
        "exam_part":            exam_part,
        "attempt":              attempt,
        "questions_list":       questions_list,
        "remaining_seconds":    remaining_seconds,
        "student":              student,
        "resume_question_index": getattr(attempt, 'last_question_index', 0) or 0,
        "extra_time_percent":   extra_time_percent(student),
        "large_print":          needs_large_print(student),
        "lockdown":             exam.lockdown,
    })


@login_required

def autosave_exam_answers(request):
    """
    Called every 30 seconds from the exam page via fetch().
    Saves individual answers + updates elapsed time + last question index.
    Returns JSON so the client can show a save indicator.
    Does NOT submit the exam — only preserves state.
    """
    import json as _json
 
    student = getattr(request.user, 'student', None)
    if not student:
        return JsonResponse({'ok': False, 'error': 'Not a student'}, status=403)
 
    try:
        data        = _json.loads(request.body)
        attempt_id  = data.get('attempt_id')
        answers     = data.get('answers', {})       # {question_id: option_id_or_text}
        elapsed     = int(data.get('elapsed', 0))   # seconds elapsed client-side
        question_idx= int(data.get('question_index', 0))
    except (ValueError, TypeError, _json.JSONDecodeError):
        return JsonResponse({'ok': False, 'error': 'Bad request'}, status=400)
 
    try:
        attempt = ExamAttempt.objects.select_related(
            'exam_part__exam'
        ).get(id=attempt_id, student=student, completed=False)
    except ExamAttempt.DoesNotExist:
        return JsonResponse({'ok': False, 'error': 'Attempt not found'}, status=404)
 
    # Verify exam is still open to this student (extra time can run past the end)
    exam = attempt.exam_part.exam
    from . import engine
    if not engine.may_continue(attempt):
        return JsonResponse({'ok': False, 'error': 'Exam no longer active'}, status=403)
 
    saved_count = 0
    with transaction.atomic():
        for q_id_str, value in answers.items():
            try:
                question = Question.objects.get(
                    id=int(q_id_str),
                    exam_part=attempt.exam_part
                )
            except (Question.DoesNotExist, ValueError):
                continue
 
            answer, _ = StudentAnswer.objects.get_or_create(
                attempt=attempt, question=question
            )
 
            if question.question_type in ['MCQ', 'TF']:
                try:
                    opt = Option.objects.get(id=int(value), question=question)
                    answer.selected_option = opt
                    answer.text_answer = None
                except (Option.DoesNotExist, ValueError, TypeError):
                    pass
            else:
                answer.text_answer = str(value).strip()
                answer.selected_option = None
 
            answer.save()
            saved_count += 1
 
        # Update resume state on the attempt
        # Only advance elapsed if it's larger — never let a client send a smaller
        # value to "gain" extra time
        if elapsed > (attempt.elapsed_seconds or 0):
            attempt.elapsed_seconds = elapsed
 
        attempt.last_question_index = max(0, question_idx)
        attempt.last_autosave_at    = timezone.now()
        attempt.save(update_fields=[
            'elapsed_seconds', 'last_question_index', 'last_autosave_at'
        ])
 
    return JsonResponse({
        'ok':          True,
        'saved':       saved_count,
        'elapsed':     attempt.elapsed_seconds,
        'question_idx': attempt.last_question_index,
        'timestamp':   attempt.last_autosave_at.strftime('%H:%M:%S'),
    })
 


from django.http import JsonResponse
from django.views.decorators.http import require_POST
import json

@login_required
@require_POST
def bulk_delete_questions(request):
    """
    Bulk delete multiple questions
    """
    if not can_manage_exams(request.user):
        return JsonResponse({'success': False, 'message': 'Unauthorized'}, status=403)
    
    try:
        data = json.loads(request.body)
        question_ids = data.get('question_ids', [])
        
        if not question_ids:
            return JsonResponse({'success': False, 'message': 'No questions selected'}, status=400)
        
        # Get staff profile
        staff_profile = getattr(request.user, 'staff', None)
        
        # Filter questions based on role
        if request.user.is_superuser or (staff_profile and staff_profile.role == 'ADMIN'):
            questions = Question.objects.filter(
                id__in=question_ids, exam_part__exam__tenant=request.tenant)
        else:
            # Teachers can only delete their own questions
            questions = Question.objects.filter(
                id__in=question_ids,
                exam_part__exam__created_by=staff_profile
            )
        
        deleted_count = questions.count()
        
        if deleted_count == 0:
            return JsonResponse({
                'success': False, 
                'message': 'No questions found or you do not have permission to delete them'
            }, status=404)
        
        # Delete the questions
        questions.delete()
        
        return JsonResponse({
            'success': True, 
            'message': f'Successfully deleted {deleted_count} question(s)',
            'deleted_count': deleted_count
        })
        
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'message': 'Invalid request data'}, status=400)
    except Exception as e:
        return JsonResponse({'success': False, 'message': str(e)}, status=500)

@login_required
def exam_result(request, attempt_id):
    attempt = get_object_or_404(ExamAttempt, id=attempt_id, student=request.user.student)
    answers = attempt.answers.select_related('question', 'selected_option')
    return render(request, "cbt/exam_result.html", {"attempt": attempt, "answers": answers})



@login_required
def questions_overview(request):
    """
    Enhanced questions overview with passage and image indicators
    """
    if not can_manage_exams(request.user):
        messages.error(request, "Unauthorized access")
        return redirect('dashboard:router')
 
    staff_profile = getattr(request.user, 'staff', None)
 
    # Fetch exams with parts, questions, and options
    if request.user.is_superuser or (staff_profile and staff_profile.role == 'ADMIN'):
        exams_qs = Exam.objects.prefetch_related('parts__questions__options').all()
    else:
        exams_qs = Exam.objects.prefetch_related('parts__questions__options').filter(created_by=staff_profile)
 
    # Get filter parameters from GET request
    selected_exam    = request.GET.get('exam', '')
    selected_subject = request.GET.get('subject', '')
    selected_class   = request.GET.get('class_id', '')
 
    # Apply filters
    if selected_exam:
        exams_qs = exams_qs.filter(id=selected_exam)
    if selected_subject:
        exams_qs = exams_qs.filter(subject_id=selected_subject)
    if selected_class:
        exams_qs = exams_qs.filter(classes__id=selected_class).distinct()
 
    # Build a list of all questions with related exam, part, and options
    questions_data = []
    for exam in exams_qs:
        for part in exam.parts.all():
            for question in part.questions.all():
                correct_option = question.options.filter(is_correct=True).first()
                
                questions_data.append({
                    "id": question.id,
                    "exam": exam.title,
                    "exam_id": exam.id,
                    "subject": exam.subject.name,
                    "subject_id": exam.subject.id,
                    "part_type": part.part_type,
                    "question_text": question.question_text,
                    "question_type": question.question_type,
                    "marks": question.marks,
                    "num_options": question.options.count(),
                    "correct_option": correct_option.text if correct_option else None,
                    # NEW: Passage information
                    "has_passage": bool(question.passage),
                    "passage_title": question.passage_title if question.passage else None,
                    "passage_group": question.passage_group if question.passage_group else None,
                    # NEW: Image information
                    "has_image": bool(question.image),
                    "image_caption": question.image_caption if question.image else None,
                })
 
    # For the dropdown filters
    all_exams    = Exam.objects.all()
    all_subjects = Subject.objects.all()
    all_classes  = Class.objects.all().order_by('name')
 
    # Calculate statistics
    stats = {
        'total': len(questions_data),
        'with_passage': sum(1 for q in questions_data if q['has_passage']),
        'with_image': sum(1 for q in questions_data if q['has_image']),
        'mcq': sum(1 for q in questions_data if q['question_type'] == 'MCQ'),
        'tf': sum(1 for q in questions_data if q['question_type'] == 'TF'),
        'sa': sum(1 for q in questions_data if q['question_type'] == 'SA'),
    }
 
    return render(
        request,
        "cbt/questions_overview.html",
        {
            "questions":        questions_data,
            "all_exams":        all_exams,
            "all_subjects":     all_subjects,
            "all_classes":      all_classes,
            "selected_exam":    selected_exam,
            "selected_subject": selected_subject,
            "selected_class":   selected_class,
            "stats":            stats,
        }
    )
 

@login_required
def edit_question(request, question_id):
    question = get_object_or_404(Question, id=question_id, exam_part__exam__tenant=request.tenant)
    exam_part = question.exam_part

    if not can_manage_exams(request.user):
        messages.error(request, "Unauthorized access")
        return redirect('dashboard:router')

    if request.method == "POST":
        question_text = request.POST.get("question_text", "").strip()
        try:
            marks = int(float(request.POST.get("marks", 1)))
        except (ValueError, TypeError):
            marks = 1

        if not question_text:
            messages.error(request, "Question text cannot be empty.")
            return redirect('cbt:edit_question', question_id=question.id)

        question.question_text = question_text
        question.marks = marks
        question.explanation = (request.POST.get('explanation') or '').strip()

        # Only update question_type for CBT part
        if exam_part.part_type == 'CBT':
            question_type = request.POST.get("question_type")
            if question_type not in ['MCQ', 'TF', 'SA']:
                messages.error(request, "Invalid question type for CBT part.")
                return redirect('cbt:edit_question', question_id=question.id)
            question.question_type = question_type
            if question_type == 'SA':
                # Typed answers are marked by a teacher: move it to the exam's Essay section
                question.exam_part = exam_part.section_for('SA')
                question.options.all().delete()
                messages.info(request, "Short-answer questions are marked by a teacher, so this one moved to "
                                       "the exam's Essay section.")
        else:
            question.question_type = None  # Essay questions do not have type

        question.save()

        # Handle options for CBT MCQ/TF
        if exam_part.part_type == 'CBT' and question.question_type in ['MCQ', 'TF']:
            option_texts = request.POST.getlist("option_text")
            try:
                correct_index = int(float(request.POST.get("correct_index", 0)))
            except (ValueError, TypeError):
                correct_index = 0

            if not option_texts or len(option_texts) < 2:
                messages.error(request, "MCQ/TF questions require at least 2 options.")
                return redirect('cbt:edit_question', question_id=question.id)

            # Delete existing options and recreate
            question.options.all().delete()
            for idx, text in enumerate(option_texts):
                Option.objects.create(
                    question=question,
                    text=text.strip(),
                    is_correct=(idx == correct_index)
                )
        else:
            # Remove all options for Essay or SA
            question.options.all().delete()

        messages.success(request, "Question updated successfully!")
        return redirect('cbt:questions_overview')

    # Prepare options for the form
    options = question.options.all() if exam_part.part_type == 'CBT' else []

    return render(request, "cbt/edit_question.html", {
        "question": question,
        "options": options
    })



@login_required
def delete_question(request, question_id):
    question = get_object_or_404(Question, id=question_id, exam_part__exam__tenant=request.tenant)
    if not can_manage_exams(request.user):
        messages.error(request, "Unauthorized access")
        return redirect('dashboard:router')
 
    question.delete()
    messages.success(request, "Question deleted successfully!")
    return redirect('cbt:questions_overview')




@login_required
def ajax_exams_by_subject(request):
    if not (request.user.is_superuser or getattr(request.user, 'staff', None) is not None):
        return JsonResponse({'exams': []}, status=403)
    subject_id = request.GET.get('subject_id')
    if not subject_id:
        return JsonResponse({'exams': []})

    exams = Exam.objects.filter(subject_id=subject_id).prefetch_related('parts')
    data = []

    for exam in exams:
        parts_list = []
        for part in exam.parts.all():
            parts_list.append({
                'id': part.id,
                'part_type': part.part_type,
                'total_marks': part.total_marks,
            })
        data.append({
            'id': exam.id,
            'title': exam.title,
            'parts': parts_list
        })

    return JsonResponse({'exams': data})


@login_required
def add_exam_part_redirect(request, exam_id):
    exam = get_object_or_404(Exam, id=exam_id)
    if not can_manage_exams(request.user):
        messages.error(request, "Unauthorized")
        return redirect('dashboard:router')

    # Check existing parts
    existing_parts = exam.parts.values_list('part_type', flat=True)

    # Automatically add CBT if not exists
    if 'CBT' not in existing_parts:
        ExamPart.objects.create(exam=exam, part_type='CBT', duration_minutes=exam.duration_minutes)
        messages.success(request, "CBT part added automatically. You can now add Essay part.")
        return redirect('cbt:add_exam_part_redirect', exam_id=exam.id)

    # Automatically add ESSAY if not exists
    if 'ESSAY' not in existing_parts:
        ExamPart.objects.create(exam=exam, part_type='ESSAY', duration_minutes=exam.duration_minutes)
        messages.success(request, "Essay part added automatically.")
        return redirect('cbt:add_exam_part_redirect', exam_id=exam.id)

    # If both exist, show the page with buttons
    return render(request, "cbt/add_exam_part_redirect.html", {
        "exam": exam,
        "existing_parts": existing_parts
    })



@login_required
def add_exam_part(request, exam_id, part_type):
    exam = get_object_or_404(Exam, id=exam_id)
    if not can_manage_exams(request.user):
        messages.error(request, "Unauthorized")
        return redirect('dashboard:router')

    if part_type not in ['CBT', 'ESSAY']:
        messages.error(request, "Invalid exam part type.")
        return redirect('cbt:admin_exam_list')

    part_exists = exam.parts.filter(part_type=part_type).exists()
    if not part_exists:
        ExamPart.objects.create(
            exam=exam,
            part_type=part_type,
            duration_minutes=exam.duration_minutes
        )
        messages.success(request, f"{part_type} part added successfully to exam '{exam.title}'.")
    else:
        messages.warning(request, f"{part_type} part already exists for this exam.")

    return redirect('cbt:add_exam_part_redirect', exam_id=exam.id)

import zipfile
import os
from django.core.files.base import ContentFile
from pathlib import Path

@login_required
def bulk_question_upload(request):
    """
    Enhanced version supporting both Excel/CSV and Word document uploads
    Preserves mathematical formulas and scientific notation from Word
    """
    if not can_manage_exams(request.user):
        messages.error(request, "You do not have permission to upload questions.")
        return redirect('dashboard:router')
    from tenants.middleware import get_current_tenant

    # ── Scope ExamPart to the current tenant via its parent Exam ──────────────
    # ExamPart has no tenant FK of its own, but Exam does (via TenantModelMixin).
    # Exam.objects already filters by tenant; filtering ExamPart through
    # exam__in ensures we only show parts belonging to this school.
    current_tenant = get_current_tenant()
    if current_tenant:
        tenant_exams = Exam.objects.filter(tenant=current_tenant).values_list('id', flat=True)
        exam_parts   = ExamPart.objects.select_related('exam').filter(
            part_type='CBT',
            exam__id__in=tenant_exams,
        )
    else:
        # Fallback (management commands / tests with no tenant context)
        exam_parts = ExamPart.objects.select_related('exam').filter(part_type='CBT')

    if request.method == 'POST':
        exam_part_id = request.POST.get('exam_part')

        # ---------- EXAM PART VALIDATION ----------
        if not exam_part_id:
            messages.error(request, "Please select an exam part/section.")
            return redirect(request.path)

        # Also scope the POST lookup to the current tenant to prevent cross-school submission
        if current_tenant:
            exam_part = get_object_or_404(
                ExamPart,
                id=exam_part_id,
                exam__tenant=current_tenant,
            )
        else:
            exam_part = get_object_or_404(ExamPart, id=exam_part_id)

        # ---------- TEXT PASTE MODE ----------
        input_mode = request.POST.get('input_mode', 'file')
        if input_mode == 'text':
            return _process_text_upload(request, exam_part)

        # ---------- FILE UPLOAD MODE ----------
        upload_file = request.FILES.get('file')

        if not upload_file:
            messages.error(request, "Please upload a file (CSV, Excel, or Word).")
            return redirect(request.path)

        if upload_file.size == 0:
            messages.error(request, "Uploaded file is empty.")
            return redirect(request.path)

        file_extension = upload_file.name.lower().split('.')[-1]

        # ---------- HANDLE WORD DOCUMENTS ----------
        if file_extension in ['docx', 'doc', 'odt', 'rtf']:
            return _process_word_upload(request, upload_file, exam_part)

        # ---------- HANDLE EXCEL/CSV ----------
        elif file_extension in ['csv', 'xlsx', 'xls', 'ods', 'tsv']:
            return _process_excel_upload(request, upload_file, exam_part)

        else:
            # Unknown extension — try Word parser as a fallback
            try:
                return _process_word_upload(request, upload_file, exam_part)
            except Exception:
                messages.error(request,
                    f"Unsupported file format (.{file_extension}). "
                    "Accepted formats: Word (.docx, .doc, .odt, .rtf), "
                    "Excel (.xlsx, .xls, .ods), CSV (.csv, .tsv).")
                return redirect(request.path)

    # GET request → render upload page
    return render(request, 'cbt/bulk_upload_questions.html', {
        'exam_parts': exam_parts
    })



import zipfile
import os
from django.core.files.base import ContentFile
from pathlib import Path

def _essay_question_count(exam_part):
    return Question.objects.filter(exam_part__exam=exam_part.exam, exam_part__part_type='ESSAY').count()


def _note_essay_moves(request, exam_part, essay_before):
    """After an upload into an objective section: say how many short-answer questions went to Essay."""
    moved = _essay_question_count(exam_part) - essay_before if exam_part.part_type == 'CBT' else 0
    if moved > 0:
        messages.info(request, f"{moved} short-answer question{'s' if moved != 1 else ''} "
                               f"{'were' if moved != 1 else 'was'} put in the exam's Essay section — typed "
                               f"answers are marked by a teacher, not in the objective (CBT) section.")


def _process_word_upload(request, upload_file, exam_part):
    """
    Process Word document uploads with passages and images.
    Supports both embedded images (pasted in Word) and ZIP-referenced images.
    """
    essay_before = _essay_question_count(exam_part)
    try:
        # ✅ Pass image_zip directly to parser — it handles everything internally
        images_zip = request.FILES.get('images_zip')
        parser = WordQuestionParser(upload_file, image_zip=images_zip)
        questions_data = parser.parse()

        # Validate
        validation_errors = parser.validate_questions()
        if validation_errors:
            error_msg = "Validation errors found:\n" + "\n".join(validation_errors)
            messages.error(request, error_msg)
            return redirect('cbt:bulk_question_upload')

        if not questions_data:
            messages.warning(request, "No questions found in the document.")
            return redirect('cbt:bulk_question_upload')

        stats = parser.get_statistics()
        created_count = 0
        skipped_count = 0
        passage_cache = {}

        for q_data in questions_data:
            try:
                # ── Passage handling ──────────────────────────────────────
                passage       = q_data.get('passage')
                passage_title = q_data.get('passage_title')
                passage_group = q_data.get('passage_group')

                if passage_group and passage_group in passage_cache:
                    passage       = None
                    passage_title = None
                elif passage_group and passage:
                    passage_cache[passage_group] = {
                        'passage': passage,
                        'passage_title': passage_title,
                    }

                # ── Image handling ────────────────────────────────────────
                # ✅ Parser already resolved both embedded + ZIP images into image_bytes
                image_bytes     = q_data.get('image_bytes')       # raw bytes or None
                image_reference = q_data.get('image_reference')   # filename string or None

                # ── Create Question ───────────────────────────────────────
                # ✅ question_text is already clean — parser stripped IMAGE:/CAPTION:
                question = Question.objects.create(
                    exam_part     = exam_part.section_for(q_data['question_type']),
                    question_text = q_data['question_text'],
                    question_type = q_data['question_type'],
                    marks         = float(q_data.get('marks', 1.0)),
                    passage       = passage,
                    passage_title = passage_title,
                    passage_group = passage_group,
                    image_caption = q_data.get('image_caption'),
                )

                # ── Attach image if bytes available ───────────────────────
                # ✅ Both embedded and ZIP images are now just raw bytes
                if image_bytes and image_reference:
                    question.image.save(
                        image_reference,
                        ContentFile(image_bytes),  # bytes → Django file
                        save=True
                    )
                elif image_bytes and not image_reference:
                    # Embedded image with auto-generated filename
                    fname = f"q{q_data.get('question_number', 'x')}_embed.png"
                    question.image.save(
                        fname,
                        ContentFile(image_bytes),
                        save=True
                    )

                # ── Options ───────────────────────────────────────────────
                if q_data['question_type'] in ['MCQ', 'TF']:
                    correct_option = q_data.get('correct_option', '').upper()
                    for letter, option_html in q_data.get('options', {}).items():
                        Option.objects.create(
                            question   = question,
                            text       = option_html,
                            is_correct = (letter == correct_option),
                        )
                    if correct_option:
                        question.correct_answer = correct_option
                        question.save()

                elif q_data['question_type'] == 'SA':
                    correct_answer = q_data.get('correct_answer', '')
                    if correct_answer:
                        question.correct_answer = correct_answer
                        question.save()

                created_count += 1

            except Exception as e:
                skipped_count += 1
                print(f"Error creating question {q_data.get('question_number')}: {e}")
                continue

        # ── Success message ───────────────────────────────────────────────
        if created_count == 0:
            messages.warning(request, "No questions were uploaded successfully.")
        else:
            msg = f"✓ Successfully uploaded {created_count} questions!\n"
            msg += f"  • MCQ: {stats['mcq']}, True/False: {stats['tf']}, Short Answer: {stats['sa']}\n"
            msg += f"  • Total Marks: {stats['total_marks']}\n"
            if stats['with_passage'] > 0:
                msg += f"  • Questions with passages: {stats['with_passage']}\n"
                msg += f"  • Passage groups: {stats['passage_groups']}\n"
            if stats['with_image'] > 0:
                msg += f"  • Questions with images: {stats['with_image']}\n"
            if skipped_count > 0:
                msg += f"  • {skipped_count} question(s) skipped due to errors"
            messages.success(request, msg)
            _note_essay_moves(request, exam_part, essay_before)

        return redirect('cbt:admin_exam_list')

    except Exception as e:
        messages.error(request, f"Error processing Word document: {str(e)}")
        return redirect('cbt:bulk_question_upload')

def _process_text_upload(request, exam_part):
    """
    Handle plain-text question pasting from the textarea tab.
    Uses plain_text_parser to parse the raw text into question dicts,
    then saves them to the database exactly like the Word/Excel handlers.
    """
    essay_before = _essay_question_count(exam_part)
    raw_text = request.POST.get('plain_text', '').strip()

    if not raw_text:
        messages.error(request, "No questions found. Please paste your questions in the text box.")
        return redirect('cbt:bulk_question_upload')

    questions_data = parse_plain_text(raw_text)

    if not questions_data:
        messages.warning(
            request,
            "No questions could be parsed. Make sure each question starts with a number "
            "(e.g. '1.' or '1)') and includes options A–D and an ANSWER line."
        )
        return redirect('cbt:bulk_question_upload')

    validation_errors = validate_plain_text_questions(questions_data)
    if validation_errors:
        messages.error(request, "Validation errors:\n" + "\n".join(validation_errors))
        return redirect('cbt:bulk_question_upload')

    created_count = 0
    skipped_count = 0

    for q_data in questions_data:
        try:
            q_type  = q_data.get('question_type', 'MCQ')
            options = q_data.get('options', {})

            question = Question.objects.create(
                exam_part     = exam_part.section_for(q_type),
                question_text = q_data['question_text'],
                question_type = q_type,
                marks         = float(q_data.get('marks', 1.0)),
            )

            if q_type in ['MCQ', 'TF']:
                correct_option = q_data.get('correct_option', '').upper()
                for letter, text in options.items():
                    Option.objects.create(
                        question   = question,
                        text       = text,
                        is_correct = (letter == correct_option),
                    )
                if correct_option:
                    question.correct_answer = correct_option
                    question.save()

            elif q_type == 'SA':
                correct_answer = q_data.get('correct_answer', '')
                if correct_answer:
                    question.correct_answer = correct_answer
                    question.save()

            created_count += 1

        except Exception as e:
            skipped_count += 1
            print(f"[plain_text_upload] Error saving question {q_data.get('question_number')}: {e}")
            continue

    if created_count:
        msg = f"✅ Successfully imported {created_count} question(s) into {exam_part}."
        if skipped_count:
            msg += f" ({skipped_count} skipped due to errors.)"
        messages.success(request, msg)
        _note_essay_moves(request, exam_part, essay_before)
    else:
        messages.error(request, "No questions were saved. Check your format and try again.")

    return redirect('cbt:admin_exam_list')


def extract_images_from_zip(zip_file):
    """
    Extract images from uploaded ZIP file
    Returns dict of {filename: file_content}
    """
    image_files = {}
    
    try:
        with zipfile.ZipFile(zip_file, 'r') as zip_ref:
            for file_info in zip_ref.filelist:
                filename = os.path.basename(file_info.filename)
                
                # Skip directories and hidden files
                if file_info.is_dir() or filename.startswith('.') or filename.startswith('__'):
                    continue
                
                # Check if it's an image file
                ext = os.path.splitext(filename)[1].lower()
                if ext in ['.jpg', '.jpeg', '.png', '.gif']:
                    image_data = zip_ref.read(file_info.filename)
                    image_files[filename] = image_data
        
        return image_files
        
    except Exception as e:
        print(f"Error extracting images from ZIP: {e}")
        return {}


def _process_excel_upload(request, upload_file, exam_part):
    """
    Process Excel/CSV uploads (original logic preserved)
    """
    essay_before = _essay_question_count(exam_part)
    try:
        file_extension = upload_file.name.lower().split('.')[-1]
        
        # Read file
        if file_extension == 'csv':
            try:
                file_data = upload_file.read().decode('utf-8')
            except UnicodeDecodeError:
                upload_file.seek(0)
                file_data = upload_file.read().decode('latin1')
            df = pd.read_csv(io.StringIO(file_data))
        
        elif file_extension in ['xlsx', 'xls']:
            df = pd.read_excel(upload_file)
        
        else:
            messages.error(request, "Unsupported file format.")
            return redirect('cbt:bulk_question_upload')

        if df.empty:
            messages.error(request, "The uploaded file contains no data.")
            return redirect('cbt:bulk_question_upload')

    except Exception as e:
        messages.error(request, f"File processing error: {e}")
        return redirect('cbt:bulk_question_upload')

    # Clean headers
    df.columns = df.columns.str.strip()

    # Required columns (option_e is optional — only option_a to option_d are required)
    required_columns = [
        'question_text',
        'question_type',
        'marks',
        'option_a',
        'option_b',
        'option_c',
        'option_d',
        'correct_option',
        'correct_answer'
    ]
    missing = [col for col in required_columns if col not in df.columns]
    if missing:
        messages.error(request, f"Missing required columns: {', '.join(missing)}")
        return redirect('cbt:bulk_question_upload')

    # Question type mapping
    q_type_map = {
        'MULTIPLE CHOICE': 'MCQ',
        'TRUE / FALSE': 'TF',
        'SHORT ANSWER': 'SA',
        'MCQ': 'MCQ',
        'TF': 'TF',
        'SA': 'SA'
    }

    # Process rows
    created_count = 0
    skipped_count = 0

    for index, row in df.iterrows():
        question_text = str(row.get('question_text', '')).strip()
        q_type_raw = str(row.get('question_type', '')).strip().upper()
        question_type = q_type_map.get(q_type_raw)

        # Skip invalid rows
        if not question_text or not question_type:
            skipped_count += 1
            continue

        marks = int(row.get('marks', 1)) if pd.notna(row.get('marks')) else 1

        # Create Question
        question = Question.objects.create(
            exam_part=exam_part.section_for(question_type),
            question_text=question_text,
            question_type=question_type,
            marks=marks
        )

        # MCQ / TF
        if question_type in ['MCQ', 'TF']:
            correct_letter = str(row.get('correct_option', '')).strip().upper()
            options_map = {
                'A': row.get('option_a'),
                'B': row.get('option_b'),
                'C': row.get('option_c'),
                'D': row.get('option_d'),
                'E': row.get('option_e'),
            }

            for key, text in options_map.items():
                if pd.notna(text) and str(text).strip():
                    Option.objects.create(
                        question=question,
                        text=str(text).strip(),
                        is_correct=(key == correct_letter)
                    )

            # Save correct answer as A/B/C/D
            if correct_letter in options_map:
                question.correct_answer = correct_letter
                question.save()

        # Short Answer
        elif question_type == 'SA':
            correct_answer = row.get('correct_answer')
            if pd.notna(correct_answer):
                question.correct_answer = str(correct_answer).strip()
                question.save()

        created_count += 1

    # Success message
    if created_count == 0:
        messages.warning(request, "No valid questions were uploaded. Check question types and data.")
    else:
        msg = f"{created_count} question(s) uploaded successfully."
        if skipped_count > 0:
            msg += f" ({skipped_count} row(s) skipped due to invalid data.)"
        messages.success(request, msg)
        _note_essay_moves(request, exam_part, essay_before)

    return redirect('cbt:admin_exam_list')


def download_word_template(request):
    """
    NEW: Provides a Word template for teachers with examples
    Shows proper formatting for math/science questions
    """
    from docx import Document
    from docx.shared import Pt
    from .word_parser import create_word_template
    
    # Create the template
    doc = create_word_template()
    
    # Prepare response
    response = HttpResponse(
        content_type='application/vnd.openxmlformats-officedocument.wordprocessingml.document'
    )
    response['Content-Disposition'] = 'attachment; filename="exam_questions_template.docx"'
    
    # Save document to response
    doc.save(response)
    
    return response


def download_question_csv_template(request):
    """
    Provides a CSV template for bulk question upload.
    Includes headers and example rows for MCQ, TF, and SA questions.
    """
    response = HttpResponse(
        content_type='text/csv'
    )
    response['Content-Disposition'] = 'attachment; filename="bulk_questions_template.csv"'

    writer = csv.writer(response)

    # Write CSV headers
    writer.writerow([
        'question_text',
        'question_type',
        'marks',
        'option_a',
        'option_b',
        'option_c',
        'option_d',
        'option_e',
        'correct_option',
        'correct_answer'
    ])

    # Example MCQ row (5 options)
    writer.writerow([
        'Simplify: 3(2x + 5) - 4x',
        'MCQ',
        '2',
        '2x + 15',
        '2x + 5',
        '6x + 15',
        '14x + 5',
        '10x + 3',
        'A',
        ''
    ])

    # Example True/False row
    writer.writerow([
        'The earth is flat.',
        'TF',
        '1',
        'True',
        'False',
        '',
        '',
        '',
        'B',
        ''
    ])

    # Example Short Answer row
    writer.writerow([
        'What is the capital of France?',
        'SA',
        '1',
        '',
        '',
        '',
        '',
        '',
        '',
        'Paris'
    ])

    return response


def download_question_csv_template(request):
    """
    Provides a CSV template for bulk question upload.
    Includes headers and example rows for MCQ, TF, and SA questions.
    """
    response = HttpResponse(
        content_type='text/csv'
    )
    response['Content-Disposition'] = 'attachment; filename="bulk_questions_template.csv"'

    writer = csv.writer(response)

    # Write CSV headers
    writer.writerow([
        'question_text',
        'question_type',
        'marks',
        'option_a',
        'option_b',
        'option_c',
        'option_d',
        'option_e',
        'correct_option',
        'correct_answer'
    ])

    # Example MCQ row (5 options)
    writer.writerow([
        'Simplify: 3(2x + 5) - 4x',
        'MCQ',
        '2',
        '2x + 15',
        '2x + 5',
        '6x + 15',
        '14x + 5',
        '10x + 3',
        'A',
        ''  # For MCQ, correct_answer will be auto-filled from correct_option
    ])

    # Example True/False row
    writer.writerow([
        'The earth is flat.',
        'TF',
        '1',
        'True',
        'False',
        '',
        '',
        '',
        'B',
        ''
    ])

    # Example Short Answer row
    writer.writerow([
        'What is the capital of France?',
        'SA',
        '1',
        '',
        '',
        '',
        '',
        '',
        '',
        'Paris'
    ])

    return response



@login_required
def exam_results_view(request):
    # Every student's exam scores: staff only.
    if not (request.user.is_superuser or getattr(request.user, 'staff', None) is not None):
        messages.error(request, "Unauthorized access")
        return redirect('dashboard:router')
    classes = Class.objects.all()
    subjects = Subject.objects.all()
    sessions = AcademicSession.objects.all()
    terms = Term.objects.all()

    # Filter parameters
    selected_class   = request.GET.get('class')
    selected_student = request.GET.get('student')
    selected_subject = request.GET.get('subject')
    selected_session = request.GET.get('session')
    selected_term    = request.GET.get('term')
    selected_exam_id = request.GET.get('exam_id')   # pre-filter when coming from edit_exam

    # Students dropdown depends on selected class
    if selected_class:
        students = Student.objects.filter(class_assigned_id=selected_class)
    else:
        students = Student.objects.all()

    # Base queryset for exam attempts
    attempts = ExamAttempt.objects.select_related(
        'student', 
        'student__class_assigned',
        'exam_part', 
        'exam_part__exam'
    ).prefetch_related(
        'answers',
        'answers__selected_option'
    )

    # Apply filters
    if selected_class:
        attempts = attempts.filter(student__class_assigned_id=selected_class)
    if selected_student:
        attempts = attempts.filter(student_id=selected_student)
    if selected_subject:
        attempts = attempts.filter(exam_part__exam__subject_id=selected_subject)
    if selected_session:
        attempts = attempts.filter(exam_part__exam__session_id=selected_session)
    if selected_term:
        attempts = attempts.filter(exam_part__exam__term_id=selected_term)
    if selected_exam_id:
        attempts = attempts.filter(exam_part__exam_id=selected_exam_id)

    context = {
        'classes': classes,
        'students': students,
        'subjects': subjects,
        'sessions': sessions,
        'terms': terms,
        'attempts': attempts,
        'selected_class': selected_class,
        'selected_student': selected_student,
        'selected_subject': selected_subject,
        'selected_session': selected_session,
        'selected_term':    selected_term,
        'selected_exam_id': selected_exam_id,
    }

    return render(request, 'cbt/exam_results_detail.html', context)


# ── Exam Access Control ────────────────────────────────────────────────────────
 
@login_required
def exam_access_control(request):
    """
    Page to restrict specific exams to specific students.
    Supports class filter + multi-select students.
    """
    from users.models import Class, Student as StudentModel
    from academics.models import AcademicSession
 
    if not can_manage_exams(request.user):
        messages.error(request, "Unauthorized access")
        return redirect('dashboard:router')
 
    # All published exams (admin sees all; teacher sees own)
    staff_profile = getattr(request.user, 'staff', None)
    if request.user.is_superuser or (staff_profile and staff_profile.role == 'ADMIN'):
        exams_qs = Exam.objects.all()
    else:
        exams_qs = Exam.objects.filter(created_by=staff_profile)
 
    exams_qs = exams_qs.select_related('subject', 'session', 'term')                        .prefetch_related('classes', 'student_restrictions')                        .order_by('-created_at')
 
    all_classes  = Class.objects.all().order_by('name')
    all_sessions = AcademicSession.objects.all().order_by('-name')
 
    # --- GET filters ---
    sel_exam_id  = request.GET.get('exam', '').strip()
    sel_class_ids = request.GET.getlist('classes')   # multi-select
    sel_session  = request.GET.get('session', '').strip()
    sel_status   = request.GET.get('status', '').strip()
 
    if sel_session:
        exams_qs = exams_qs.filter(session_id=sel_session)
    if sel_status == 'published':
        exams_qs = exams_qs.filter(published=True)
    elif sel_status == 'draft':
        exams_qs = exams_qs.filter(published=False)
 
    # Students for the selected exam (shown in right panel)
    selected_exam      = None
    exam_classes       = []
    class_student_data = []  # [{class, students, restricted_ids}]
    filter_class_ids   = [int(c) for c in sel_class_ids if c.isdigit()]
 
    if sel_exam_id:
        try:
            selected_exam = Exam.objects.prefetch_related(
                'classes', 'student_restrictions__student'
            ).get(id=sel_exam_id)
            exam_classes = selected_exam.classes.all().order_by('name')
            restricted_ids = set(
                selected_exam.student_restrictions.values_list('student_id', flat=True)
            )
            has_any_restriction = bool(restricted_ids)
 
            display_classes = exam_classes.filter(id__in=filter_class_ids)                               if filter_class_ids else exam_classes
 
            for cls in display_classes:
                students = StudentModel.objects.filter(
                    class_assigned=cls, status__in=ENROLLED_STATUSES
                ).order_by('full_name')
                class_student_data.append({
                    'cls':            cls,
                    'students':       students,
                    'restricted_ids': restricted_ids,
                    'has_restriction': has_any_restriction,
                })
        except Exam.DoesNotExist:
            pass
 
    # --- POST: save restriction choices ---
    if request.method == 'POST':
        exam_id      = request.POST.get('exam_id', '').strip()
        action       = request.POST.get('action', '')   # 'restrict' | 'allow_all' | 'clear_all'
        student_ids  = request.POST.getlist('student_ids')
 
        try:
            target_exam = Exam.objects.get(id=exam_id)
        except Exam.DoesNotExist:
            messages.error(request, "Exam not found.")
            return redirect(request.path + f'?exam={exam_id}')
 
        if action == 'allow_all':
            # Remove all restrictions → everyone in the class can see it
            count = target_exam.student_restrictions.count()
            target_exam.student_restrictions.all().delete()
            messages.success(request, f"Removed all restrictions — all students in assigned classes can now see this exam.")
 
        elif action == 'clear_all':
            target_exam.student_restrictions.all().delete()
            messages.info(request, "All access restrictions cleared.")
 
        elif action == 'restrict':
            if not student_ids:
                messages.warning(request, "No students selected. Tick students to restrict access to, then save.")
            else:
                # Replace current restrictions with the new selection
                target_exam.student_restrictions.all().delete()
                added = 0
                for sid in student_ids:
                    try:
                        stu = StudentModel.objects.get(id=sid)
                        ExamStudentRestriction.objects.create(
                            exam=target_exam,
                            student=stu,
                            added_by=staff_profile,
                        )
                        added += 1
                    except (StudentModel.DoesNotExist, Exception):
                        continue
                messages.success(
                    request,
                    f"Access restricted to {added} student(s). Only these students will see this exam."
                )
 
        # Preserve exam + class filters on redirect
        params = f'?exam={exam_id}'
        if sel_class_ids:
            params += '&' + '&'.join(f'classes={c}' for c in sel_class_ids)
        return redirect(request.path + params)
 
    context = {
        'exams':             exams_qs,
        'selected_exam':     selected_exam,
        'exam_classes':      exam_classes,
        'class_student_data': class_student_data,
        'all_classes':       all_classes,
        'all_sessions':      all_sessions,
        'sel_exam_id':       sel_exam_id,
        'sel_class_ids':     [int(c) for c in sel_class_ids if c.isdigit()],
        'sel_session':       sel_session,
        'sel_status':        sel_status,
    }
    return render(request, 'cbt/exam_access_control.html', context)
 






# ── AJAX: Auto-save answers during exam ───────────────────────────────────────
 
@login_required
@require_POST
def autosave_exam_answers(request):
    """
    Called every 30 seconds from the exam page via fetch().
    Saves individual answers + updates elapsed time + last question index.
    Returns JSON so the client can show a save indicator.
    Does NOT submit the exam — only preserves state.
    """
    import json as _json
 
    student = getattr(request.user, 'student', None)
    if not student:
        return JsonResponse({'ok': False, 'error': 'Not a student'}, status=403)
 
    try:
        data        = _json.loads(request.body)
        attempt_id  = data.get('attempt_id')
        answers     = data.get('answers', {})       # {question_id: option_id_or_text}
        elapsed     = int(data.get('elapsed', 0))   # seconds elapsed client-side
        question_idx= int(data.get('question_index', 0))
    except (ValueError, TypeError, _json.JSONDecodeError):
        return JsonResponse({'ok': False, 'error': 'Bad request'}, status=400)
 
    try:
        attempt = ExamAttempt.objects.select_related(
            'exam_part__exam'
        ).get(id=attempt_id, student=student, completed=False)
    except ExamAttempt.DoesNotExist:
        return JsonResponse({'ok': False, 'error': 'Attempt not found'}, status=404)
 
    # Verify exam is still open to this student (extra time can run past the end)
    exam = attempt.exam_part.exam
    from . import engine
    if not engine.may_continue(attempt):
        return JsonResponse({'ok': False, 'error': 'Exam no longer active'}, status=403)
 
    saved_count = 0
    with transaction.atomic():
        for q_id_str, value in answers.items():
            try:
                question = Question.objects.get(
                    id=int(q_id_str),
                    exam_part=attempt.exam_part
                )
            except (Question.DoesNotExist, ValueError):
                continue
 
            answer, _ = StudentAnswer.objects.get_or_create(
                attempt=attempt, question=question
            )
 
            if question.question_type in ['MCQ', 'TF']:
                try:
                    opt = Option.objects.get(id=int(value), question=question)
                    answer.selected_option = opt
                    answer.text_answer = None
                except (Option.DoesNotExist, ValueError, TypeError):
                    pass
            else:
                answer.text_answer = str(value).strip()
                answer.selected_option = None
 
            answer.save()
            saved_count += 1
 
        # Update resume state on the attempt
        # Only advance elapsed if it's larger — never let a client send a smaller
        # value to "gain" extra time
        if elapsed > (attempt.elapsed_seconds or 0):
            attempt.elapsed_seconds = elapsed
 
        attempt.last_question_index = max(0, question_idx)
        attempt.last_autosave_at    = timezone.now()
        attempt.save(update_fields=[
            'elapsed_seconds', 'last_question_index', 'last_autosave_at'
        ])
 
    return JsonResponse({
        'ok':          True,
        'saved':       saved_count,
        'elapsed':     attempt.elapsed_seconds,
        'question_idx': attempt.last_question_index,
        'timestamp':   attempt.last_autosave_at.strftime('%H:%M:%S'),
    })