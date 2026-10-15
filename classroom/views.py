from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.utils import timezone
from django.db import models, transaction
from django.db.models import Q, Max
from django.forms import modelformset_factory

from .access import require_enrolled, require_teacher
from .models import (
    Course,
    Section,
    Lesson,
    Material,
    Assignment,
    AssignmentQuestion,
    Submission,
    SubmissionAnswer,
    Quiz,
    Question,
    QuizSubmission,
    StudentSectionProgress,
    LessonProgress,
)

from classroom.models import Quiz as ClassroomQuiz, Question as ClassroomQuestion, QuizSubmission as ClassroomQuizSubmission, QuizAnswer

from .forms import (
    CourseForm,
    SectionForm,
    LessonForm,
    MaterialForm,
    AssignmentForm,
    QuizForm,
    QuestionForm,
    AssignmentQuestionForm,
)

from .decorators import staff_required, student_required



# ------------------ STAFF / TEACHER VIEWS ------------------

@login_required
@staff_required
def teacher_courses(request):
    """List all courses created by the logged-in staff"""
    courses = Course.objects.filter(teacher=request.user)
    return render(request, 'classroom/teacher_courses.html', {'courses': courses})



@login_required
@staff_required
def teacher_course_detail(request, course_id):
    course = get_object_or_404(Course, id=course_id)
    require_teacher(request.user, course)

    sections = course.sections.prefetch_related('lessons', 'materials', 'assignments', 'quizzes')


    # 🔑 CRITICAL PART
    for section in sections:
        section.lessons_ordered = section.lessons.all().order_by('order')

    context = {
        'course': course,
        'sections': sections,
    }

    return render(request, 'classroom/teacher_course_detail.html', context)




@login_required
@staff_required
def add_course(request):
    """Allow staff to create a new course and assign it to a class"""
    if request.method == 'POST':
        form = CourseForm(request.POST)
        if form.is_valid():
            course = form.save(commit=False)
            course.teacher = request.user
            course.save()

            # Auto-assign students if a class is selected
            if course.class_assigned:
                # Make sure we only assign the student user objects
                student_users = [student.user for student in course.class_assigned.student_set.all()]
                course.students.set(student_users)

            messages.success(request, f'Course "{course.name}" created successfully.')
            return redirect('classroom:teacher_courses')
    else:
        form = CourseForm()
    return render(request, 'classroom/add_course.html', {'form': form})



@login_required
@staff_required
def delete_course(request, course_id):
    course = get_object_or_404(Course, id=course_id, teacher=request.user)
    course_name = course.name
    course.delete()
    messages.success(request, f'Course "{course_name}" has been deleted successfully.')
    return redirect('classroom:teacher_courses')




# ------------------ ADD SECTION ------------------
@login_required
@staff_required
def add_section(request, course_id):
    course = get_object_or_404(
        Course,
        id=course_id,
        teacher=request.user
    )

    if request.method == 'POST':
        title = request.POST.get('title')

        last_order = (
            course.sections
            .aggregate(max_order=models.Max('order'))
            ['max_order'] or 0
        )

        Section.objects.create(
            course=course,   # ✅ THIS IS REQUIRED
            title=title,
            order=last_order + 1
        )

        return redirect(
            'classroom:teacher_course_detail',
            course_id=course.id
        )

    return render(
        request,
        'classroom/add_section.html',
        {'course': course}
    )



@login_required
@staff_required
def add_lesson(request, section_id):
    section = get_object_or_404(Section, id=section_id)
    require_teacher(request.user, section.course)

    # Auto-order lessons
    max_order = section.lessons.aggregate(Max('order'))['order__max'] or 0

    if request.method == 'POST':
        form = LessonForm(request.POST, request.FILES)
        if form.is_valid():
            lesson = form.save(commit=False)
            lesson.section = section
            lesson.order = max_order + 1
            lesson.save()

            messages.success(request, 'Lesson (video) added successfully.')
            return redirect(
                'classroom:teacher_course_detail',
                course_id=section.course.id
            )
        else:
            # Debugging (temporary)
            print(form.errors)
    else:
        form = LessonForm()  # ✅ ALWAYS define form for GET

    return render(request, 'classroom/add_lesson.html', {
        'form': form,
        'section': section
    })



@login_required
@staff_required
def edit_lesson(request, lesson_id):
    lesson = get_object_or_404(Lesson, id=lesson_id)
    require_teacher(request.user, lesson.section.course)
    course_id = lesson.section.course.id 
    

    if request.method == 'POST':
        form = LessonForm(request.POST, request.FILES, instance=lesson)
        if form.is_valid():
            form.save()
            messages.success(request, 'Lesson updated successfully.')
            return redirect(
                'classroom:teacher_course_detail',
                course_id=lesson.section.course.id
            )
    else:
        form = LessonForm(instance=lesson)

    return render(request, 'classroom/edit_lesson.html', {
        'form': form,
        'lesson': lesson,
        'course_id': course_id,
    })




@login_required
@staff_required
def add_material(request, section_id):
    section = get_object_or_404(Section, id=section_id)
    require_teacher(request.user, section.course)
    course = section.course

    if request.method == 'POST':
        form = MaterialForm(request.POST, request.FILES)
        if form.is_valid():
            material = form.save(commit=False)
            material.section = section  # set the section automatically
            material.save()
            messages.success(request, 'Material added successfully.')
            return redirect('classroom:teacher_course_detail', course_id=course.id)
    else:
        form = MaterialForm()

    context = {
        'form': form,
        'section': section,
        'course': course
    }
    return render(request, 'classroom/add_material.html', context)




@login_required
@staff_required
def add_assignment(request, section_id):
    section = get_object_or_404(Section, id=section_id)
    require_teacher(request.user, section.course)
    course = section.course  # get course for redirect/back

    if request.method == 'POST':
        form = AssignmentForm(request.POST)

        if form.is_valid():
            assignment = form.save(commit=False)
            assignment.section = section  # assign the section
            assignment.save()

            messages.success(request, 'Assignment created successfully.')
            return redirect('classroom:teacher_course_detail', course_id=course.id)
        else:
            messages.error(request, f"Form error: {form.errors}")

    else:
        form = AssignmentForm()

    return render(
        request,
        'classroom/add_assignment.html',
        {'form': form, 'section': section, 'course': course}
    )


@login_required
@staff_required
def add_quiz(request, section_id):
    section = get_object_or_404(Section, id=section_id)
    require_teacher(request.user, section.course)
    course = section.course  # get course for redirect/back

    if request.method == 'POST':
        form = QuizForm(request.POST)

        if form.is_valid():
            quiz = form.save(commit=False)
            quiz.section = section  # assign the section
            quiz.save()

            messages.success(request, 'Quiz created successfully.')
            return redirect('classroom:teacher_course_detail', course_id=course.id)
        else:
            messages.error(request, f"Form error: {form.errors}")

    else:
        form = QuizForm()

    return render(
        request,
        'classroom/add_quiz.html',
        {'form': form, 'section': section, 'course': course}
    )




# -------------------- QUIZ QUESTIONS --------------------
@login_required
@staff_required
def add_quiz_question(request, quiz_id):
    quiz = get_object_or_404(Quiz, id=quiz_id)
    require_teacher(request.user, quiz.section.course)
    section = quiz.section  # Get the section the quiz belongs to
    course = section.course  # Get the course from the section

    if request.method == 'POST':
        form = QuestionForm(request.POST)
        if form.is_valid():
            question = form.save(commit=False)
            question.quiz = quiz
            question.save()
            messages.success(request, 'Question added successfully.')

            # Redirect to teacher course dashboard
            return redirect('classroom:teacher_course_detail', course_id=course.id)
    else:
        form = QuestionForm()

    return render(request, 'classroom/add_quiz_question.html', {
        'form': form,
        'quiz': quiz,
        'section': section,
        'course': course
    })




@login_required
@staff_required
def add_assignment_question(request, assignment_id):
    assignment = get_object_or_404(Assignment, id=assignment_id)
    require_teacher(request.user, assignment.section.course)
    section = assignment.section
    course = section.course

    # Create a FormSet for multiple questions (extra=3 shows 3 empty forms initially)
    QuestionFormSet = modelformset_factory(
        AssignmentQuestion,
        form=AssignmentQuestionForm,
        extra=3,  # You can change the number of empty forms displayed
        can_delete=True  # Allow teacher to remove questions in the formset
    )

    if request.method == 'POST':
        formset = QuestionFormSet(request.POST, queryset=AssignmentQuestion.objects.none())
        if formset.is_valid():
            questions = formset.save(commit=False)
            for question in questions:
                question.assignment = assignment
                question.save()
            messages.success(request, f"{len(questions)} questions added successfully!")
            return redirect('classroom:teacher_course_detail', course_id=course.id)
    else:
        formset = QuestionFormSet(queryset=AssignmentQuestion.objects.none())

    return render(request, 'classroom/add_assignment_question.html', {
        'formset': formset,
        'assignment': assignment,
        'section': section,
        'course': course
    })




# ------------------ ASSIGNMENT DETAIL ------------------
@login_required
@staff_required
def assignment_detail(request, assignment_id):
    assignment = get_object_or_404(Assignment, id=assignment_id)
    require_teacher(request.user, assignment.section.course)
    questions = assignment.questions.all()  # Related AssignmentQuestion objects
    section = assignment.section           # Get the section of the assignment
    course = section.course                # Get the course via section

    return render(request, 'classroom/assignment_detail.html', {
        'assignment': assignment,
        'questions': questions,
        'section': section,
        'course': course
    })


# ------------------ QUIZ DETAIL ------------------
@login_required
@staff_required
def quiz_detail(request, quiz_id):
    quiz = get_object_or_404(Quiz, id=quiz_id)
    require_teacher(request.user, quiz.section.course)
    questions = quiz.questions.all()       # Related Question objects
    section = quiz.section                 # Get the section of the quiz
    course = section.course                # Get the course via section

    return render(request, 'classroom/quiz_detail.html', {
        'quiz': quiz,
        'questions': questions,
        'section': section,
        'course': course
    })



# ------------------ STUDENT VIEWS ------------------

@login_required
@student_required
def student_courses(request):
    """Courses the student is enrolled in, or that are for their class."""
    student = getattr(request.user, 'student', None)
    courses = Course.objects.filter(Q(students=request.user) |
                                    Q(class_assigned_id=getattr(student, 'class_assigned_id', None))).distinct()
    return render(request, 'classroom/student_courses.html', {'courses': courses})



@login_required
@student_required
def student_course_content(request, course_id):
    user = request.user
    student = user.student  # related Student object

    # Fetch course: student explicitly enrolled OR course assigned to student's class
    course_qs = Course.objects.prefetch_related(
        'sections__lessons',
        'sections__assignments',
        'sections__quizzes',
        'sections__materials'
    ).filter(
        Q(students=user) | Q(class_assigned=student.class_assigned)
    )

    course = get_object_or_404(course_qs, id=course_id)

    # Initialize trackers
    section_progress = {}
    unlocked_sections = {}
    lesson_progress = {}
    assignment_submissions = {}
    quiz_submissions = {}  # <-- add this

    sections = course.sections.all().order_by('order')

    # Strict sequential unlock: only first accessible section can unlock next
    previous_unlocked = True
    previous_completed = True

    for section in sections:
        lessons = section.lessons.all().order_by('order')

        # Count completed lessons
        completed_lessons = lessons.filter(
            lessonprogress__student=user,
            lessonprogress__watched=True
        ).count()
        total_lessons = lessons.count()

        # Section completed if all lessons done or empty
        completed = total_lessons == 0 or completed_lessons == total_lessons
        section_progress[section.id] = {
            'total_lessons': total_lessons,
            'completed_lessons': completed_lessons,
            'completed': completed
        }

        # Unlock logic: only unlock if previous section was unlocked AND completed
        unlocked_sections[section.id] = previous_unlocked and previous_completed

        # Update trackers for next iteration
        previous_unlocked = unlocked_sections[section.id]
        previous_completed = completed

        # Track lesson progress
        for lesson in lessons:
            lesson_progress[lesson.id] = lesson.lessonprogress_set.filter(
                student=user,
                watched=True
            ).exists()

        # Track assignment submissions
        for assignment in section.assignments.all():
            submission = assignment.submissions.filter(student=user).first()
            if submission:
                assignment_submissions[assignment.id] = submission

        # Track quiz submissions
        for quiz in section.quizzes.all():
            quiz_sub = quiz.submissions.filter(student=user).first()
            if quiz_sub:
                quiz_submissions[quiz.id] = quiz_sub


    context = {
        'course': course,
        'section_progress': section_progress,
        'unlocked_sections': unlocked_sections,
        'lesson_progress': lesson_progress,
        'assignment_submissions': assignment_submissions,
        'quiz_submissions': quiz_submissions,  # <-- pass to template
    }

    return render(request, 'classroom/student_course_content.html', context)


@login_required
@student_required
def watch_lesson(request, lesson_id):
    lesson = get_object_or_404(Lesson, id=lesson_id)
    require_enrolled(request.user, lesson.section.course)
    section = lesson.section
    course = section.course

    # Mark lesson as watched (can be marked again, no restriction)
    lesson_progress, _ = LessonProgress.objects.get_or_create(
        student=request.user,
        lesson=lesson
    )
    lesson_progress.watched = True
    lesson_progress.save()

    # Lessons in section (already ordered by Meta)
    lessons = list(section.lessons.all())

    # Lesson watched map
    watched_map = {
        lp.lesson_id: lp.watched
        for lp in LessonProgress.objects.filter(
            student=request.user,
            lesson__section=section,
            watched=True
        )
    }

    # Next lesson logic
    next_lesson = None
    idx = lessons.index(lesson)
    if idx + 1 < len(lessons):
        next_lesson = lessons[idx + 1]

    # Check section completion (still tracks progress)
    if all(watched_map.get(l.id) for l in lessons):
        StudentSectionProgress.objects.update_or_create(
            student=request.user,
            section=section,
            defaults={"completed": True, "completed_at": timezone.now()}
        )

    # Next section
    next_section = course.sections.filter(order__gt=section.order).first()

    context = {
        "lesson": lesson,
        "lessons": lessons,
        "watched_map": watched_map,
        "next_lesson": next_lesson,
        "next_section": next_section,
        "course": course,  # needed for back button
    }

    return render(request, "classroom/watch_lesson.html", context)





@login_required
@student_required
def view_assignment(request, assignment_id):
    """
    Display assignment details and allow a student to submit once.
    After final submission, resubmission is blocked.
    """

    assignment = get_object_or_404(Assignment, id=assignment_id)
    require_enrolled(request.user, assignment.section.course)
    course = assignment.section.course

    # Get existing submission if any (DO NOT create here)
    submission = Submission.objects.filter(
        assignment=assignment,
        student=request.user
    ).first()

    # 🚫 Block access if already submitted
    if submission and submission.submitted_at:
        messages.warning(
            request,
            "You have already submitted this assignment."
        )
        return redirect(
            "classroom:student_course_content",
            course_id=course.id
        )

    # Build answers map for prefilling (draft only)
    answers_map = {}
    if submission:
        answers_map = {
            ans.question.id: ans.answer_text
            for ans in submission.answers.all()
        }

    from .services import late_status
    late = late_status(assignment)

    if request.method == "POST":
        if not late['open']:
            messages.error(request, late['message'])
            return redirect("classroom:student_course_content", course_id=course.id)
        # Create submission ONLY on submit
        if not submission:
            submission = Submission.objects.create(
                assignment=assignment,
                student=request.user
            )

        # Save answers
        for question in assignment.questions.all():
            answer_text = request.POST.get(
                f"answer_{question.id}", ""
            ).strip()

            if answer_text:
                SubmissionAnswer.objects.update_or_create(
                    submission=submission,
                    question=question,
                    defaults={
                        "student": request.user,
                        "answer_text": answer_text
                    }
                )

        # Save optional file
        uploaded_file = request.FILES.get("file")
        if uploaded_file:
            submission.file = uploaded_file

        # 🔒 Final submission lock
        submission.submitted_at = timezone.now()
        submission.late_days, submission.penalty_percent = late['late_days'], late['penalty']
        submission.save()

        messages.success(
            request,
            "Assignment submitted successfully."
        )
        return redirect(
            "classroom:student_course_content",
            course_id=course.id
        )

    context = {
        "assignment": assignment,
        "course": course,
        "submission": submission,
        "answers_map": answers_map,
        "late": late,
    }

    return render(
        request,
        "classroom/view_assignment.html",
        context
    )



@login_required
@staff_required
def grade_assignment(request, assignment_id):
    assignment = get_object_or_404(Assignment, id=assignment_id)
    require_teacher(request.user, assignment.section.course)
    submissions = assignment.submissions.filter(submitted_at__isnull=False)

    grade_options = [0, 1, 1.5, 2, 2.5, 3, 3.5, 4, 4.5, 5,
                     5.5, 6, 6.5, 7, 7.5, 8, 8.5, 9, 9.5, 10]

    # Safely get the course from assignment.section
    course = getattr(getattr(assignment, 'section', None), 'course', None)

    submission_id = request.GET.get('submission_id')
    if submission_id:
        submission = get_object_or_404(Submission, id=submission_id, assignment=assignment)

        if request.method == "POST":
            from .extras import ExtrasError, mark_with_rubric, set_voice_feedback
            feedback = request.POST.get('feedback')
            try:
                if assignment.criteria.exists():
                    choices = {k[3:]: v for k, v in request.POST.items() if k.startswith('rc_')}
                    mark_with_rubric(submission, choices, feedback)
                else:
                    submission.grade = float(request.POST.get('grade'))
                    submission.feedback = feedback
                    submission.save()
                if request.FILES.get('feedback_audio'):
                    set_voice_feedback(submission, request.FILES['feedback_audio'])
                messages.success(request, f"{submission.student} graded successfully!")
            except (ExtrasError, TypeError, ValueError) as problem:
                messages.error(request, str(problem) or 'Choose a grade.')
            return redirect(f"{request.path}?submission_id={submission.id}")

        answers = submission.answers.all()
        chosen = {r.criterion_id: r.level_id for r in submission.rubric_scores.all()}
        criteria = list(assignment.criteria.prefetch_related('levels'))
        for c in criteria:
            c.chosen = chosen.get(c.id)
        context = {
            'criteria': criteria,
            'assignment': assignment,
            'submission': submission,
            'answers': answers,
            'grade_options': grade_options,
            'course': course,  # pass course safely
        }
        return render(request, 'classroom/grade_assignment.html', context)

    from .extras import similar_pairs
    context = {
        'assignment': assignment,
        'submissions': submissions,
        'course': course,  # pass course safely
        'similar': similar_pairs(assignment),
    }
    return render(request, 'classroom/grade_assignment_list.html', context)


@login_required
@student_required
def view_assignment_result(request, assignment_id):
    assignment = get_object_or_404(Assignment, id=assignment_id)
    require_enrolled(request.user, assignment.section.course)
    # Get the student's submission
    submission = assignment.submissions.filter(student=request.user, submitted_at__isnull=False).first()

    if not submission:
        messages.info(request, "You have not submitted this assignment yet.")
        return redirect('classroom:student_course_content', course_id=assignment.section.course.id)

    answers = submission.answers.select_related('question').all()

    context = {
        'assignment': assignment,
        'submission': submission,
        'answers': answers
    }
    return render(request, 'classroom/view_assignment_result.html', context)




@login_required
@student_required
def take_quiz(request, quiz_id):
    quiz = get_object_or_404(Quiz, id=quiz_id)
    require_enrolled(request.user, quiz.section.course)
    course = quiz.section.course
    user = request.user

    # Check if the student already submitted this quiz
    existing_submission = QuizSubmission.objects.filter(
        quiz=quiz,
        student=user,
        submitted_at__isnull=False
    ).first()

    if existing_submission:
        messages.info(request, "You have already submitted this quiz. You cannot retake it.")
        return redirect('classroom:my_quiz_results')  # ✅ Redirect to results page

    questions = quiz.questions.all()
    total_questions = questions.count()

    if request.method == 'POST':
        correct_count = 0

        # Create submission
        quiz_submission = QuizSubmission.objects.create(
            quiz=quiz,
            student=user,
            total_questions=total_questions
        )

        for q in questions:
            selected = request.POST.get(str(q.id))

            if selected and selected.upper() == q.correct_option:
                correct_count += 1

            QuizAnswer.objects.create(
                submission=quiz_submission,
                question=q,
                selected_option=selected.upper() if selected else ""
            )

        # 1 mark per question
        quiz_submission.score = correct_count
        quiz_submission.submitted_at = timezone.now()
        quiz_submission.save()

        messages.success(
            request,
            f'You scored {correct_count} out of {total_questions}'
        )

        return redirect('classroom:my_quiz_results')  # ✅ Redirect to results page

    return render(request, 'classroom/take_quiz.html', {
        'quiz': quiz,
        'questions': questions,
        'course': course
    })





@login_required
@student_required
def my_quiz_results(request):
    """
    Display all quizzes the student has submitted along with answers,
    score, submission date, and a link back to the course content.
    """
    user = request.user

    # Fetch submissions with related quiz, section, and course
    submissions = (
        QuizSubmission.objects
        .filter(student=user, submitted_at__isnull=False)
        .select_related('quiz__section__course')  # Grab course via quiz->section->course
        .prefetch_related('answers__question')
        .order_by('-submitted_at')
    )

    # Build a dictionary mapping submission id -> course
    submission_courses = {sub.id: sub.quiz.section.course for sub in submissions}

    context = {
        'submissions': submissions,
        'submission_courses': submission_courses,
    }

    return render(request, 'classroom/my_quiz_results.html', context)



@login_required
@staff_required
def teacher_course_quiz_results(request, course_id):
    course = get_object_or_404(Course, id=course_id, teacher=request.user)

    section_id = request.GET.get("section_id")

    # 🔒 Enforce section filter
    section = get_object_or_404(
        Section,
        id=section_id,
        course=course
    )

    quizzes = section.quizzes.all()

    quiz_results = {}
    for quiz in quizzes:
        quiz_results[quiz.id] = quiz.submissions.filter(
            submitted_at__isnull=False
        ).select_related("student")

    context = {
        "course": course,
        "section": section,      # SINGLE section
        "quizzes": quizzes,      # ONLY quizzes in this section
        "quiz_results": quiz_results,
    }

    return render(
        request,
        "classroom/teacher_course_quiz_results.html",
        context
    )
