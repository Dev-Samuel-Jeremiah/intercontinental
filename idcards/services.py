"""
idcards/services.py — scan-recording + parent notification logic.
"""
from datetime import timedelta

from django.utils import timezone

from communications.services import send_email
from tenants.middleware import get_current_tenant

from .models import GateScanLog, StaffClockLog, IDCard


def get_or_create_student_card(student):
    """Fetch (or lazily create) the ID card for a student. Safe to call from
    any view/template context that already has a Student instance."""
    card, _ = IDCard.objects.get_or_create(
        student=student,
        defaults={'card_type': 'STUDENT', 'card_number': student.admission_number},
    )
    return card


def get_or_create_staff_card(staff):
    """Fetch (or lazily create) the ID card for a staff member."""
    card, _ = IDCard.objects.get_or_create(
        staff=staff,
        defaults={'card_type': 'STAFF', 'card_number': f"STAFF-{staff.id:04d}"},
    )
    return card


def _next_direction(last_log):
    """IN if there's no log yet today or the last one was OUT, else OUT."""
    if last_log is None or last_log.direction == 'OUT':
        return 'IN'
    return 'OUT'


def _school_name():
    tenant = get_current_tenant()
    return tenant.name if tenant else 'Your School'


def record_gate_scan(card, operator_user=None):
    """
    Record a gate scan for a student's ID card, figure out whether it's an
    arrival or a departure, save the log, and email the parent.
    Returns the created GateScanLog.
    """
    student = card.student
    today = timezone.localdate()
    last_log = (GateScanLog.objects.filter(student=student, scanned_at__date=today)
                .order_by('scanned_at').last())
    if repeat_scan(last_log):
        # The same card read again moments later (a camera reads a code many times a second):
        # keep the first record — no flip from IN to OUT, no second email to the parent.
        last_log.repeat = True
        return last_log
    direction = _next_direction(last_log)

    log = GateScanLog.objects.create(
        student=student,
        card=card,
        direction=direction,
        scanned_by=operator_user if (operator_user and operator_user.is_authenticated) else None,
    )
    if direction == 'IN':
        # The first arrival of the day marks the register, with any lateness.
        from users.attendance import record_arrival
        record_arrival(student, log.scanned_at)
    _notify_parent(log)
    return log


def _notify_parent(log):
    student = log.student
    parent_email = student.parent_email
    if not parent_email:
        log.notify_error = 'No parent email on file'
        log.save(update_fields=['notify_error'])
        return

    school = _school_name()
    when = timezone.localtime(log.scanned_at)
    action = 'arrived safely at' if log.direction == 'IN' else 'left'
    color = '#35c58b' if log.direction == 'IN' else '#5ea2ff'
    label = 'Arrival' if log.direction == 'IN' else 'Departure'
    class_name = student.class_assigned.name if student.class_assigned else ''

    subject = f"{label} Notice — {student.full_name}"
    html_body = f"""
    <div style="font-family:Arial,Helvetica,sans-serif;max-width:480px;margin:0 auto;
                border:1px solid #e5e5e5;border-radius:10px;overflow:hidden">
      <div style="background:{color};color:#fff;padding:16px 20px;font-size:18px;font-weight:bold;">
        {school} — {label} Notice
      </div>
      <div style="padding:20px;color:#222;">
        <p style="margin:0 0 12px;">Dear {student.parent_name or 'Parent/Guardian'},</p>
        <p style="margin:0 0 12px;">
          This is to confirm that <strong>{student.full_name}</strong>
          {'(' + class_name + ')' if class_name else ''}
          has just {action} school.
        </p>
        <table style="width:100%;border-collapse:collapse;margin:12px 0;">
          <tr><td style="padding:6px 0;color:#666;">Status</td>
              <td style="padding:6px 0;font-weight:bold;">{log.get_direction_display()}</td></tr>
          <tr><td style="padding:6px 0;color:#666;">Date</td>
              <td style="padding:6px 0;">{when:%A, %d %B %Y}</td></tr>
          <tr><td style="padding:6px 0;color:#666;">Time</td>
              <td style="padding:6px 0;">{when:%I:%M %p}</td></tr>
        </table>
        <p style="margin:12px 0 0;font-size:13px;color:#888;">
          This is an automated message from the {school} gate attendance system.
        </p>
      </div>
    </div>
    """
    ok, ref = send_email(parent_email, subject, html_body, student=student)
    log.parent_notified = ok
    log.notify_error = '' if ok else ref
    log.save(update_fields=['parent_notified', 'notify_error'])


# A second scan this soon is a slip of the thumb, not a departure
DOUBLE_SCAN_WINDOW = timedelta(minutes=2)


def repeat_scan(last_log):
    """True when last_log was recorded moments ago, so a new scan should not flip IN/OUT."""
    return last_log is not None and timezone.now() - last_log.scanned_at < DOUBLE_SCAN_WINDOW


def record_staff_clock(staff, station=None):
    """Record a staff clock in/out scan at the reception QR station. A repeat scan
    within DOUBLE_SCAN_WINDOW returns the existing record marked `repeat`."""
    today = timezone.localdate()
    last_log = (StaffClockLog.objects.filter(staff=staff, scanned_at__date=today)
                .order_by('scanned_at').last())
    if repeat_scan(last_log):
        last_log.repeat = True
        return last_log
    direction = _next_direction(last_log)
    from safety.staff_attendance import check_clock_in
    check_clock_in(staff, direction)            # raises ClockClosed on a day off the school does not take
    return StaffClockLog.objects.create(staff=staff, station=station, direction=direction)
