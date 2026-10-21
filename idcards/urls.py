from django.urls import path
from . import views

app_name = 'idcards'

urlpatterns = [
    # My own card
    path('my-card/', views.my_id_card, name='my_id_card'),

    # Admin: view/print a specific person's card
    path('student/<int:student_id>/', views.print_student_card, name='print_student_card'),
    path('staff/<int:staff_id>/', views.print_staff_card, name='print_staff_card'),
    path('bulk-print/', views.bulk_print_cards, name='bulk_print_cards'),
    path('directory/', views.id_card_directory, name='id_card_directory'),
    path('reissue/<uuid:uid>/', views.reissue_card, name='reissue_card'),

    # QR images embedded in the printed cards
    path('qr/<uuid:uid>.png', views.qr_image, name='qr_image'),
    path('reception-qr/<uuid:uid>.png', views.reception_qr_image, name='reception_qr_image'),

    # Gate scan (student arrival/departure)
    # The camera scanner opened from the top bar
    path('scanner/', views.scanner, name='scanner'),
    path('scan/<uuid:uid>/', views.gate_scan, name='gate_scan'),
    path('gate-log/', views.gate_scan_log, name='gate_scan_log'),

    # Staff reception clock-in/out
    path('reception/', views.reception_qr_page, name='reception_qr_page'),
    path('reception/scan/<uuid:uid>/', views.reception_clock, name='reception_clock'),
    path('staff-attendance/', views.staff_attendance_log, name='staff_attendance_log'),
]
