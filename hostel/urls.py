from django.urls import path
from hostel import views, views_boarding as vb

app_name = 'hostel'

urlpatterns = [
    # Dashboard
    path('',                                views.dashboard,           name='dashboard'),

    # Hostel buildings
    path('hostels/',                        views.hostel_list,         name='hostel_list'),
    path('hostels/new/',                    views.hostel_create_edit,  name='hostel_create'),
    path('hostels/<int:hostel_id>/',        views.hostel_detail,       name='hostel_detail'),
    path('hostels/<int:hostel_id>/edit/',   views.hostel_create_edit,  name='hostel_edit'),

    # Floors
    path('hostels/<int:hostel_id>/floors/', views.manage_floors, name='manage_floors'),

    # Rooms
    path('hostels/<int:hostel_id>/rooms/new/',           views.room_create_edit, name='room_create'),
    path('hostels/<int:hostel_id>/rooms/<int:room_id>/', views.room_create_edit, name='room_edit'),

    # Boarders
    path('boarders/',                             views.boarder_list,     name='boarder_list'),
    path('boarders/<int:student_id>/',            views.boarder_profile,  name='boarder_profile'),
    path('boarders/<int:student_id>/update/',     views.boarder_update,   name='boarder_update'),
    path('boarders/<int:student_id>/checkout/',   views.checkout_boarder, name='checkout'),
    path('boarders/<int:student_id>/exeat/',      views.grant_exeat_view, name='grant_exeat'),
    path('movements/<int:movement_id>/return/',   views.return_from_exeat,name='return_exeat'),

    # Bed assignment
    path('assign-bed/',                           views.assign_bed_view,  name='assign_bed'),

    # Billing
    path('billing/',                              views.billing_overview, name='billing'),
    path('billing/generate/',                     views.generate_bills,   name='generate_bills'),
    path('billing/<int:billing_id>/pay/',         views.pay_bill_view,    name='pay_bill'),
    path('billing/fee-structure/',                views.save_fee_structure, name='save_fee_structure'),

    # Visitors
    path('visitors/',                             views.visitor_log,      name='visitors'),
    path('visitors/log/',                         views.log_visitor,      name='log_visitor'),
    path('visitors/<int:visitor_id>/review/',     views.approve_visitor,  name='approve_visitor'),

    # Incidents
    path('incidents/',                            views.incidents,        name='incidents'),
    path('incidents/log/',                        views.log_incident,     name='log_incident'),
    path('incidents/<int:incident_id>/resolve/',  views.resolve_incident, name='resolve_incident'),

    # Notices
    path('notices/',                              views.notices,          name='notices'),
    path('notices/<int:notice_id>/delete/',       views.delete_notice,    name='delete_notice'),

    # Analytics & Reports
    path('analytics/',           views.analytics,          name='analytics'),
    path('payment-reminders/',  views.payment_reminders,  name='payment_reminders'),

    # Meal plans
    path('meals/',                                views.meal_plans,       name='meal_plans'),
    # Boarding life
    path('bed-map/',                              vb.bed_map,         name='bed_map'),
    path('roll-call/',                            vb.roll_calls,      name='roll_calls'),
    path('exeats/',                               vb.exeats,          name='exeats'),
    path('visiting-days/',                        vb.visiting,        name='visiting'),
    path('inspections/',                          vb.inspections,     name='inspections'),
    path('sick-bay/',                             vb.sick_bay,        name='sick_bay'),
    path('room-changes/',                         vb.room_changes,    name='room_changes'),
    path('reports/',                              vb.reports,         name='boarding_reports'),
    path('reports/<int:boarder_id>/',             vb.report_edit,     name='boarding_report'),
    path('kitchen/',                              vb.kitchen,         name='kitchen'),
    path('night-sheet/',                          vb.night_sheet,     name='night_sheet'),
    path('family/',                               vb.family_boarding, name='family_boarding'),
]
