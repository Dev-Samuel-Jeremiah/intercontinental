"""
dashboard/access.py
───────────────────
Sidebar links a member of staff would only be turned away from are not shown
to them at all. Each rule below asks the same question the page itself asks
(using the same checks), so a teacher who is also the school nurse still sees
Sick Bay, and one who heads a department still sees Review Lesson Notes.

Administrators are never limited here. tests/test_sidebar_access.py opens
every link a staff member is shown and fails if any of them turns them away,
so a new page with its own rule must get a rule here too.
"""
from django.core.cache import cache

CACHE_SECONDS = 120          # a new job (e.g. made a store keeper) shows in the menu within two minutes

# Links for administrators only: the page refuses everyone else
ADMIN_ONLY = {
    'safety:staff_attendance', 'safety:staff_hours', 'idcards:reception_qr_page', 'idcards:staff_attendance_log',
    'safety:settings', 'users:attendance_settings',
}

# Links opened by one of the staff member's permission switches
NEEDS_SWITCH = {
    'cbt:cbt_create_exam': 'can_manage_exams', 'cbt:admin_exam_list': 'can_manage_exams',
    'cbt:timetables': 'can_manage_exams', 'cbt:halls': 'can_manage_exams', 'cbt:questions_overview': 'can_manage_exams',
    'results:cbt_scores_reference': 'can_enter_scores', 'results:manual_score_entry': 'can_enter_scores',
    'results:bundles': 'can_view_all_results',
    'results:manual_term_attendance': 'can_mark_attendance', 'idcards:gate_scan_log': 'can_mark_attendance',
    'safety:pickup_desk': 'can_mark_attendance', 'users:attendance_home': 'can_mark_attendance',
    'idcards:id_card_directory': 'can_manage_students', 'idcards:bulk_print_cards': 'can_manage_students',
}


def _form_teacher(user, tenant, staff):
    return staff.form_classes.filter(tenant=tenant).exists()


def _head_of_department(user, tenant, staff):
    from curriculum.services import hod_subject_ids
    return bool(hod_subject_ids(staff))


def _broadsheet(user, tenant, staff):
    return getattr(staff, 'can_view_all_results', False) or _form_teacher(user, tenant, staff)


def _nurse(user, tenant, staff):
    from welfare.services import is_nurse
    return is_nurse(user, tenant)


def _librarian(user, tenant, staff):
    from library.services import is_librarian
    return is_librarian(user, tenant)


def _transport_manager(user, tenant, staff):
    from transport.services import is_manager
    return is_manager(user, tenant)


def _bus_crew(user, tenant, staff):
    from transport.services import crew_routes
    return _transport_manager(user, tenant, staff) or crew_routes(user, tenant).exists()


def _store_keeper(user, tenant, staff):
    from facilities.services import is_store
    return is_store(user, tenant)


def _maintenance(user, tenant, staff):
    from facilities.services import is_maint
    return is_maint(user, tenant)


# Links that depend on the person's job at the school
NEEDS_JOB = {
    'teaching:class_dashboard': _form_teacher,
    'results:broadsheet': _broadsheet,
    'curriculum:review': _head_of_department, 'results:approvals': _head_of_department,
    'welfare:sick_bay': _nurse,
    'library:home': _librarian, 'library:desk': _librarian, 'library:catalogue': _librarian, 'library:loans': _librarian,
    'transport:home': _transport_manager, 'transport:fleet': _transport_manager, 'transport:crew': _transport_manager,
    'transport:fees': _transport_manager, 'transport:field_trips': _transport_manager,
    'transport:changes': _transport_manager, 'transport:incidents': _transport_manager,
    'transport:reports': _transport_manager, 'transport:my_trips': _bus_crew,
    'facilities:orders': _store_keeper, 'facilities:assets': _store_keeper, 'facilities:utilities': _store_keeper,
    'facilities:hire': _store_keeper, 'facilities:generators': _maintenance,
}


def closed_to(user, tenant, staff):
    """The sidebar links this member of staff cannot open."""
    key = f'sidebar-closed:{tenant.pk}:{user.pk}'
    closed = cache.get(key)
    if closed is None:
        closed = set(ADMIN_ONLY)
        closed |= {name for name, flag in NEEDS_SWITCH.items() if not getattr(staff, flag, False)}
        for name, rule in NEEDS_JOB.items():
            try:
                if not rule(user, tenant, staff):
                    closed.add(name)
            except Exception:                      # a rule that fails must not break every page
                pass
        cache.set(key, closed, CACHE_SECONDS)
    return closed


def forget(user, tenant):
    cache.delete(f'sidebar-closed:{tenant.pk}:{user.pk}')


# ─────────────────────────────────────────────────────────────────────────────
# Words for the Module Permissions page
# ─────────────────────────────────────────────────────────────────────────────

SWITCHES = [
    ('Students', [('can_manage_students', 'Manage students (records, ID cards)'),
                  ('can_generate_password', 'Make student and parent passwords'), ('can_create_staff', 'Create staff')]),
    ('Attendance', [('can_mark_attendance', 'Mark attendance (gate, registers, pickup desk)')]),
    ('Exams & results', [('can_manage_exams', 'Manage exams'), ('can_view_all_exams', 'See all exams'),
                         ('can_enter_scores', 'Enter scores'), ('can_view_all_results', 'See all results'),
                         ('can_publish_results', 'Publish results')]),
    ('Money', [('can_access_finance', 'Finance'), ('can_approve_payments', 'Approve payments')]),
    ('Boarding & stores', [('can_access_hostel', 'Hostel'), ('can_manage_boarders', 'Manage boarders'),
                           ('can_access_inventory', 'Inventory')]),
    ('Messages', [('can_send_messages', 'Send messages to parents')]),
]
SWITCH_LABEL = {name: label for _, rows in SWITCHES for name, label in rows}

JOB_LABEL = {
    _form_teacher: 'form teachers', _head_of_department: 'heads of department',
    _broadsheet: 'form teachers, or “See all results”', _nurse: 'nurses (Health & Welfare settings)',
    _librarian: 'librarians', _transport_manager: 'transport managers (Transport settings)',
    _bus_crew: 'bus drivers and attendants', _store_keeper: 'store keepers (Facilities settings)',
    _maintenance: 'the maintenance team (Facilities settings)',
}


def needs(name):
    """What else a link needs besides being ticked, in words, or ''."""
    if name in NEEDS_SWITCH:
        return f'needs the “{SWITCH_LABEL.get(NEEDS_SWITCH[name], NEEDS_SWITCH[name])}” switch'
    if name in NEEDS_JOB:
        return f'only for {JOB_LABEL.get(NEEDS_JOB[name], "staff with that job")}'
    return ''
