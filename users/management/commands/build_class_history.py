"""
Build class histories for students who were on the roll before class history
was kept (from the change log). Safe to run more than once.

    python manage.py build_class_history
"""
from django.core.management.base import BaseCommand

from users import history
from users.models import ClassHistory, Student


class Command(BaseCommand):
    help = "Rebuild class histories from the student change log, for students who don't have one yet."

    def handle(self, *args, **options):
        done = 0
        have = set(ClassHistory._base_manager.values_list('student_id', flat=True).distinct())
        for st in Student._base_manager.exclude(pk__in=have).select_related('class_assigned').iterator():
            if history.ensure(st):
                done += 1
        self.stdout.write(self.style.SUCCESS(f'Class history built for {done} student(s).'))
