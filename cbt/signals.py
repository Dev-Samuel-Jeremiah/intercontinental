from django.db.models.signals import post_save
from django.dispatch import receiver
from .models import ExamAttempt
from results.services import update_term_result


@receiver(post_save, sender=ExamAttempt)
def update_term_result_on_attempt(sender, instance, created, **kwargs):
    """
    Update the student's term result automatically
    whenever an ExamAttempt is completed.
    """
    if instance.completed:
        update_term_result(instance)
