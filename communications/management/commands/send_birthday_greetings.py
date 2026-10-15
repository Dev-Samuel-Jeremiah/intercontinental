"""
Birthday greetings for students and staff, for schools that switched them on
(Communications → Settings). Each person is greeted once a year. Run daily:

    0 7 * * * cd /path/to/project && venv/bin/python manage.py send_birthday_greetings
"""
from django.core.management.base import BaseCommand

from communications.engage import run_birthdays


class Command(BaseCommand):
    help = 'Send today\'s birthday greetings.'

    def handle(self, *args, **options):
        result = run_birthdays()
        students = sum(v['students'] for v in result.values())
        staff = sum(v['staff'] for v in result.values())
        self.stdout.write(self.style.SUCCESS(f'Greeted {students} student(s) and {staff} member(s) of staff.'))
