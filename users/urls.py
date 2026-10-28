from django.urls import path
from . import views, views_achievements, views_attendance, views_import, views_me, views_profile, views_records, views_roster
from users.views import parent_login, parent_dashboard

app_name = "users"

urlpatterns = [
    # Home page
    path('', views.home, name='home'),

    # Authentication
    path('staff/login/', views.staff_login, name='staff_login'),
    path('student/login/', views.student_login, name='student_login'),
    path('logout/', views.user_logout, name='user_logout'),
    path('change-password/', views.change_password, name='change_password'),
    path('my-profile/', views_me.my_profile, name='my_profile'),

    # Staff Management
    path('staff/create/', views.create_staff, name='create_staff'),

    # Student Management
    path('students/register/', views.register_student, name='register_student'),
    # Bulk registration from Excel
    path('students/import/', views_import.import_page, {'kind': 'students'}, name='import_students'),
    path('students/import/confirm/', views_import.import_confirm, {'kind': 'students'}, name='import_students_confirm'),
    path('students/import/done/', views_import.import_done, {'kind': 'students'}, name='import_students_done'),
    path('staff/import/', views_import.import_page, {'kind': 'staff'}, name='import_staff'),
    path('staff/import/confirm/', views_import.import_confirm, {'kind': 'staff'}, name='import_staff_confirm'),
    path('staff/import/done/', views_import.import_done, {'kind': 'staff'}, name='import_staff_done'),
    path('import/<str:kind>/template.xlsx', views_import.import_template, name='import_template'),
    path('import/<str:kind>/logins.xlsx', views_import.import_credentials, name='import_credentials'),
    path('students/generate_password/', views.generate_student_password_view, name='generate_student_password'),
    path('students/all/', views.all_students, name='all_students'),
    path('students/export/', views.export_students_excel, name='export_students_excel'),
    path('students/promote/', views.promote_students, name='promote_students'),

    # Class Management
    path('classes/manage/', views.manage_classes, name='manage_classes'),
    path('classes/delete/<int:class_id>/', views.delete_class, name='delete_class'),

    # Subject Assignment
    path('subjects/assign/', views.assign_subjects, name='assign_subjects'),

    # Advanced Subject-Teacher-Class Assignment
    path('subjects/advanced-assignment/', views.advanced_subject_assignment, name='advanced_subject_assignment'),

    # Delete assigned subject from class
    path('subjects/remove-class/<int:class_id>/<int:subject_id>/', views.remove_class_subject, name='remove_class_subject'),
    # Remove subject from student
    path('subjects/remove-student/<int:student_id>/<int:subject_id>/', views.remove_student_subject, name='remove_student_subject'),
    path('subjects/student-manager/', views.student_subject_manager, name='student_subject_manager'),
    # Attendance
    path("home/", views.attendance_home, name="attendance_home"),
    path("mark/", views.select_class_mark, name="select_class_mark"),
    path("view/", views.select_class_view, name="select_class_view"),
    path("mark/<int:class_id>/", views.mark_attendance, name="mark_attendance"),
    path("view/<int:class_id>/", views.view_attendance, name="view_attendance"),
    path('edit/<int:class_id>/<str:date>/', views.edit_attendance, name='edit_attendance'),
    path('delete/<int:class_id>/<int:student_id>/<str:date>/', views.delete_attendance, name='delete_attendance'),

    path('summary/', views.attendance_summary, name='attendance_summary'),

    path("parent/login/", parent_login, name="parent_login"),
    path("parent/dashboard/", parent_dashboard, name="parent_dashboard"),




    # Staff Management
    path('staff/permissions/', views.manage_staff_permissions, name='manage_staff_permissions'),
    path('staff/<int:staff_id>/toggle-restriction/', views.toggle_staff_restriction, name='toggle_staff_restriction'),
    path('staff/<int:staff_id>/delete/', views.delete_staff, name='delete_staff'),


    # Student Management
    path('students/<int:student_id>/', views_profile.student_profile_view, name='student_profile'),
    path('students/<int:student_id>/record-card/', views_records.record_card, name='record_card'),
    path('students/records/', views_records.records_health, name='records_health'),
    path('students/<int:student_id>/status/', views_profile.change_student_status,
         name='change_student_status'),
    path('students/<int:student_id>/guardians/', views_profile.student_guardians,
         name='student_guardians'),
    path('students/<int:student_id>/documents/', views_profile.student_documents,
         name='student_documents'),
    path('students/<int:student_id>/details/', views_profile.student_details,
         name='student_details'),
    path('students/documents/<int:document_id>/file/', views_profile.document_file,
         name='document_file'),
    path('students/documents/expiring/', views_profile.expiring_documents,
         name='expiring_documents'),
    path('students/fields/', views_profile.student_fields, name='student_fields'),
    path('students/<int:student_id>/photo/', views_roster.student_photo, name='student_photo'),
    path('students/photos/', views_roster.bulk_photos, name='bulk_photos'),
    path('students/bulk/', views_roster.bulk_edit, name='bulk_edit'),
    path('students/duplicates/', views_roster.duplicates, name='duplicates'),
    path('students/houses/', views_roster.houses, name='houses'),
    path('students/registers/', views_roster.class_register, name='class_register'),
    path('classes/arms/', views_roster.class_arms, name='class_arms'),
    path('students/<int:student_id>/awards/', views_achievements.student_awards, name='student_awards'),
    path('students/<int:student_id>/roles/', views_achievements.student_roles, name='student_roles'),
    path('students/<int:student_id>/support/', views_achievements.student_support, name='student_support'),
    path('students/<int:student_id>/export/', views_achievements.student_export, name='student_export'),
    path('students/achievements/', views_achievements.achievements, name='achievements'),
    path('students/support/', views_achievements.support_register, name='support_register'),
    path('students/corrections/', views_achievements.change_request_queue, name='change_requests'),
    path('parent/corrections/', views_achievements.parent_corrections, name='parent_corrections'),
    path('attendance/settings/', views_attendance.attendance_settings, name='attendance_settings'),
    path('attendance/lateness/', views_attendance.lateness, name='lateness'),
    path('attendance/follow-up/', views_attendance.follow_up, name='attendance_follow_up'),
    path('attendance/heatmap/', views_attendance.class_heatmap, name='attendance_heatmap'),
    path('attendance/reasons/', views_attendance.absence_reasons, name='absence_reasons'),
    path('parent/absences/', views_attendance.parent_absences, name='parent_absences'),
    path('students/<int:student_id>/attendance-certificate/', views_attendance.attendance_certificate,
         name='attendance_certificate'),
    path('attendance/verify/<str:reference>/', views_attendance.verify_attendance, name='verify_attendance'),
    path('attendance/verify/<str:reference>/qr.png', views_attendance.certificate_qr, name='certificate_qr'),
    path('students/alumni/', views_profile.alumni_register, name='alumni_register'),
    path('students/alumni/export/', views_profile.alumni_export, name='alumni_export'),
    path('students/edit/<int:student_id>/', views.edit_student, name='edit_student'),
    path('students/delete/<int:student_id>/', views.delete_student, name='delete_student'),
    path('students/reset-password/<int:student_id>/', views.reset_student_password, name='reset_student_password'),


]