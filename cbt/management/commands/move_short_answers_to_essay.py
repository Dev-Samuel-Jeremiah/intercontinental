"""
Move short-answer (typed) questions out of objective (CBT) sections into the
exam's Essay section, where a teacher marks them. In a CBT section only
chosen options are marked, so a typed answer there always scores 0.

    python manage.py move_short_answers_to_essay            # show what would move
    python manage.py move_short_answers_to_essay --apply    # move them

New uploads already put short-answer questions in the Essay section; this
tidies questions added before that.
"""
from collections import defaultdict

from django.core.management.base import BaseCommand
from django.db import transaction

from cbt.models import Question


class Command(BaseCommand):
    help = "Move short-answer questions from objective (CBT) sections to each exam's Essay section."

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='Actually move them (otherwise just report).')

    def handle(self, *args, **options):
        found = list(Question.objects.filter(exam_part__part_type='CBT', question_type='SA')
                     .select_related('exam_part__exam'))
        if not found:
            self.stdout.write(self.style.SUCCESS('No short-answer questions in objective sections.'))
            return
        by_exam = defaultdict(list)
        for q in found:
            by_exam[q.exam_part.exam].append(q)
        for exam, questions in by_exam.items():
            self.stdout.write(f'{exam.title}: {len(questions)} short-answer question(s)')
        if not options['apply']:
            self.stdout.write(self.style.WARNING('Nothing moved — run again with --apply.'))
            return
        with transaction.atomic():
            for exam, questions in by_exam.items():
                essay = questions[0].exam_part.section_for('SA')
                for q in questions:
                    q.options.all().delete()
                    q.exam_part = essay
                    q.save()                     # an Essay question has no objective type
        self.stdout.write(self.style.SUCCESS(f'Moved {len(found)} question(s) to the Essay section.'))
