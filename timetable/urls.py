from django.urls import path
from . import views, views_edit, views_extras, views_lessons, views_more

app_name = 'timetable'

urlpatterns = [
    # Public / student / teacher views
    path('', views.timetable_index, name='index'),
    path('my-timetable/', views.teacher_timetable, name='teacher_timetable'),

    # Admin: Configurations
    path('admin/', views.admin_index, name='admin_index'),
    path('admin/config/create/', views.config_create, name='config_create'),
    path('admin/config/<int:pk>/', views.config_detail, name='config_detail'),
    path('admin/config/<int:pk>/edit/', views.config_edit, name='config_edit'),
    path('admin/config/<int:pk>/delete/', views.config_delete, name='config_delete'),
    path('admin/config/<int:config_pk>/generate/', views.generate, name='generate'),

    # Admin: Time Slots
    path('admin/slots/', views.slot_list, name='slot_list'),
    path('admin/slots/create/', views.slot_create, name='slot_create'),
    path('admin/slots/<int:pk>/edit/', views.slot_edit, name='slot_edit'),
    path('admin/slots/<int:pk>/delete/', views.slot_delete, name='slot_delete'),

    # Admin: Teacher-Subject-Class Assignments
    path('admin/config/<int:config_pk>/assignments/add/', views.assignment_create, name='assignment_create'),
    path('admin/assignments/<int:pk>/edit/', views.assignment_edit, name='assignment_edit'),
    path('admin/assignments/<int:pk>/delete/', views.assignment_delete, name='assignment_delete'),

    # Admin: Preview, Publish & Export
    path('admin/generated/<int:pk>/', views.preview, name='preview'),
    path('admin/generated/<int:pk>/publish/', views.publish, name='publish'),
    path('admin/generated/<int:pk>/delete/', views.delete_generated, name='delete_generated'),
    path('admin/generated/<int:pk>/export/', views.export_excel, name='export_excel'),

    # Admin: Day Overrides (e.g. custom Friday schedule)
    path('admin/config/<int:config_pk>/overrides/', views.day_override_list, name='day_override_list'),
    path('admin/config/<int:config_pk>/overrides/create/', views.day_override_create, name='day_override_create'),
    path('admin/config/<int:config_pk>/overrides/<int:pk>/', views.day_override_detail, name='day_override_detail'),
    path('admin/config/<int:config_pk>/overrides/<int:pk>/edit/', views.day_override_edit, name='day_override_edit'),
    path('admin/config/<int:config_pk>/overrides/<int:pk>/delete/', views.day_override_delete, name='day_override_delete'),

    # Admin: Special Slots (inside a day override)
    path('admin/overrides/<int:override_pk>/slots/add/', views.special_slot_create, name='special_slot_create'),
    path('admin/special-slots/<int:pk>/edit/', views.special_slot_edit, name='special_slot_edit'),
    path('admin/special-slots/<int:pk>/delete/', views.special_slot_delete, name='special_slot_delete'),
    path('admin/generated/<int:pk>/edit/', views_edit.edit, name='edit'),
    path('admin/generated/<int:pk>/reports/', views_edit.reports, name='reports'),
    path('admin/generated/<int:pk>/print/', views_edit.print_all, name='print_all'),
    path('admin/lessons/<int:entry_id>/targets/', views_edit.targets, name='targets'),
    path('admin/lessons/<int:entry_id>/move/', views_edit.move, name='move'),
    path('admin/rooms/', views_edit.rooms, name='rooms'),
    path('admin/config/<int:config_pk>/availability/', views_edit.availability, name='availability'),
    path('lessons/', views_lessons.my_lessons, name='my_lessons'),
    path('lessons/overview/', views_lessons.lessons_overview, name='lessons_overview'),
    path('lessons/subjects/', views_lessons.subject_attendance, name='subject_attendance'),
    path('lessons/<int:slot_id>/register/', views_lessons.take_register, name='take_register'),

    # Part 2: rooms, free periods, versions, parent meetings, calendar sync, bells, printing
    path('rooms/bookings/', views_more.room_bookings, name='room_bookings'),
    path('free-periods/', views_more.free_finder, name='free_finder'),
    path('admin/config/<int:config_pk>/versions/', views_more.versions, name='versions'),
    path('admin/generated/<int:pk>/save-version/', views_more.save_version_now, name='save_version'),
    path('admin/generated/<int:pk>/print/teachers/', views_more.print_teachers, name='print_teachers'),
    path('admin/meetings/', views_more.meetings, name='meetings'),
    path('admin/meetings/<int:pk>/', views_more.meeting_detail, name='meeting_detail'),
    path('meetings/mine/', views_more.my_meetings, name='my_meetings'),
    path('meetings/book/', views_more.parent_meetings, name='parent_meetings'),
    path('my-timetable/print/', views_more.print_mine, name='print_mine'),
    path('calendar/', views_more.calendar_sync, name='calendar_sync'),
    path('calendar/download.ics', views_more.calendar_download, name='calendar_download'),
    path('calendar/<str:token>.ics', views_more.calendar_feed, name='calendar_feed'),
    path('bells/', views_more.bell_display, name='bell_display'),

    # After-school lessons and coaching classes
    path('extras/', views_extras.extras, name='extras'),
    path('extras/<int:pk>/', views_extras.extra_detail, name='extra_detail'),
    path('extras/family/', views_extras.family_extras, name='family_extras'),
]
