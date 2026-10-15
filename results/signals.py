# results/signals.py
from django.db.models.signals import post_save
from django.dispatch import receiver
from cbt.models import ExamAttempt
from .services import update_term_result

@receiver(post_save, sender=ExamAttempt)
def handle_exam_submission(sender, instance, created, **kwargs):
    # Only update when attempt is completed
    if instance.completed:
        update_term_result(instance)
