from django.urls import path
from . import views
from django.contrib.auth import views as auth_views 
from django.urls import path, reverse_lazy

app_name = "cbt"
# ------------------------
# Staff/Admin URLs
# ------------------------
urlpatterns = [
    path('admin/exams/', views.admin_exam_list, name='admin_exam_list'),
    path('admin/exams/access-control/', views.exam_access_control, name='exam_access_control'),
    path('student/exam/autosave/', views.autosave_exam_answers, name='autosave_exam_answers'),
    path('admin/exams/create/', views.create_exam, name='cbt_create_exam'),
    path('admin/exams/<int:exam_id>/edit/', views.edit_exam, name='edit_exam'),
    path('admin/exams/<int:exam_id>/delete/', views.delete_exam, name='delete_exam'),

    # Question management
    path('admin/exams/questions/add/', views.add_question, name='cbt_add_question'),

    #path('admin/exams/<int:exam_id>/questions/add/', views.add_question, name='cbt_add_question'),


    # Bulk actions
    path('admin/exams/bulk-publish/', views.bulk_publish_exams, name='cbt_bulk_publish'),

    # ------------------------
    # Student URLs
    # ------------------------
   
    
    path('student/exam/result/<int:attempt_id>/', views.exam_result, name='cbt_exam_result'),


    path('ajax/classes-by-subject/', views.ajax_get_classes_by_subject, name='ajax_classes_by_subject'),



    # Questions overview
    path(
        'admin/questions/overview/',
        views.questions_overview,
        name='questions_overview'
    ),
    path(
        'student/exams/',
        views.available_exams,
        name='available_exams'
    ),
# urls.py
    path('student/exams/<int:exam_id>/take/<str:part_type>/', views.take_exam, name='take_exam'),


    # Edit question
    path('admin/questions/<int:question_id>/edit/', views.edit_question, name='cbt_edit_question'),
    # Delete question
    path('admin/questions/<int:question_id>/delete/', views.delete_question, name='cbt_delete_question'),
    path('ajax/exams-by-subject/', views.ajax_exams_by_subject, name='ajax_exams_by_subject'),
    path('admin/exams/<int:exam_id>/add-part/<str:part_type>/', views.add_exam_part, name='add_exam_part'),


# urls.py

    path('admin/exams/<int:exam_id>/add-part/', views.add_exam_part_redirect, name='add_exam_part_redirect'),
    path('admin/exams/<int:exam_id>/add-part/<str:part_type>/', views.add_exam_part, name='add_exam_part'),

    path('questions/bulk-upload/', views.bulk_question_upload, name='bulk_question_upload'),

    path(
        'questions/download-template/',
        views.download_question_csv_template,
        name='download_question_csv_template'
    ),

    path('exam-results/', views.exam_results_view, name='exam_results'),

    path('download-word-template/', views.download_word_template, name='download_word_template'),


    # Password Reset URLs - WITH success_url ADDED
    path(
        'password-reset/',
        auth_views.PasswordResetView.as_view(
            template_name='registration/password_reset_form.html',

            # TEXT VERSION (required)
            email_template_name='registration/password_reset_email.txt',

            # HTML VERSION (this fixes your issue)
            html_email_template_name='registration/password_reset_email.html',

            subject_template_name='registration/password_reset_subject.txt',
            success_url=reverse_lazy('cbt:password_reset_done'),
        ),
        name='password_reset'
    ),

    
    path('password-reset/done/', 
         auth_views.PasswordResetDoneView.as_view(
             template_name='registration/password_reset_done.html'
         ), 
         name='password_reset_done'),
    
    path('password-reset-confirm/<uidb64>/<token>/', 
         auth_views.PasswordResetConfirmView.as_view(
             template_name='registration/password_reset_confirm.html',
             success_url=reverse_lazy('cbt:password_reset_complete'),  # ← ADD THIS LINE
         ), 
         name='password_reset_confirm'),
    
    path('password-reset-complete/', 
         auth_views.PasswordResetCompleteView.as_view(
             template_name='registration/password_reset_complete.html'
         ), 
         name='password_reset_complete'),




    path('questions/bulk-delete/', views.bulk_delete_questions, name='bulk_delete_questions'),

]