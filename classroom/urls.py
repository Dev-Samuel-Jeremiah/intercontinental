# classroom/urls.py
from django.urls import path
from . import views, views_extra, views_more

app_name = 'classroom'

urlpatterns = [
    # ------------------ TEACHER / STAFF URLS ------------------
    path('teacher/courses/', views.teacher_courses, name='teacher_courses'),
    path('course/<int:course_id>/hub/', views_extra.course_hub, name='course_hub'),
    path('course/<int:course_id>/discussion/', views_more.discussion, name='discussion'),
    path('discussion/<int:thread_id>/', views_more.thread, name='thread'),
    path('assignment/<int:assignment_id>/rubric/', views_more.rubric, name='rubric'),
    path('submission/<int:submission_id>/voice/', views_more.feedback_audio, name='feedback_audio'),
    path('course/<int:course_id>/certificate/', views_more.certificate, name='certificate'),
    path('course/<int:course_id>/completion/', views_more.completion, name='completion'),
    path('course/<int:course_id>/board-photo/', views_more.board_photo, name='board_photo'),
    path('certificate/verify/<str:ref>/', views_more.verify_certificate, name='verify_certificate'),
    path('certificate/verify/<str:ref>/qr.png', views_more.certificate_qr, name='certificate_qr'),
    path('course/<int:course_id>/copy/', views_extra.clone_course, name='clone_course'),
    path('live/<int:live_id>/join/', views_extra.join_live, name='join_live'),
    path('live/<int:live_id>/attendance/', views_extra.live_attendance, name='live_attendance'),
    path('teacher/courses/add/', views.add_course, name='add_course'),
    path('teacher/course/<int:course_id>/', views.teacher_course_detail, name='teacher_course_detail'),
    # urls.py
    path('teacher/section/<int:section_id>/add_material/', views.add_material, name='add_material'),

   path(
    'teacher/section/<int:section_id>/add_assignment/',
    views.add_assignment,
    name='add_assignment'
    ),

    # Add Quiz to a Section
    path(
        'teacher/section/<int:section_id>/add_quiz/',
        views.add_quiz,
        name='add_quiz'
    ),

         
    path('teacher/course/<int:course_id>/add_section/', views.add_section, name='add_section'),


    path(
        'teacher/section/<int:section_id>/add_lesson/',
        views.add_lesson,
        name='add_lesson'
    ),
    
    path(
        'teacher/lesson/<int:lesson_id>/edit/',
        views.edit_lesson,
        name='edit_lesson'
    ),


    # ------------------ QUIZ QUESTIONS ------------------
    path(
        'teacher/quiz/<int:quiz_id>/add_question/', 
        views.add_quiz_question, 
        name='add_quiz_question'
    ),

    # ------------------ ASSIGNMENT QUESTIONS ------------------
    path(
        'teacher/assignment/<int:assignment_id>/add_question/', 
        views.add_assignment_question, 
        name='add_assignment_question'
    ),

    # Assignments
    path('teacher/assignment/<int:assignment_id>/', views.assignment_detail, name='assignment_detail'),
    # Quizzes
    path('teacher/quiz/<int:quiz_id>/', views.quiz_detail, name='quiz_detail'),



    # ------------------ STUDENT COURSE ------------------
    path('student/courses/', views.student_courses, name='student_courses'),
    path('student/course/<int:course_id>/', views.student_course_content, name='student_course_content'),

    # ------------------ LESSONS ------------------
    path('student/lesson/<int:lesson_id>/watch/', views.watch_lesson, name='watch_lesson'),

    # assignments
    path('student/assignment/<int:assignment_id>/', views.view_assignment, name='view_assignment'),
    # classroom/urls.py
    path('assignment/<int:assignment_id>/grade/', views.grade_assignment, name='grade_assignment'),

    path('assignment/<int:assignment_id>/result/', views.view_assignment_result, name='view_assignment_result'),

    path('course/<int:course_id>/delete/', views.delete_course, name='delete_course'),






    # quizzes
    path('student/quiz/<int:quiz_id>/take/', views.take_quiz, name='take_quiz'),

    path(
        'student/quizzes/results/',
        views.my_quiz_results,
        name='my_quiz_results'
    ),

    # Teacher quiz results per course
    path(
        'teacher/course/<int:course_id>/quiz-results/',
        views.teacher_course_quiz_results,
        name='teacher_course_quiz_results'
    ),




]
