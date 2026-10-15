"""When a teacher gives behaviour points, check the merit/demerit thresholds straight away."""
import logging

from django.db.models.signals import post_save
from django.dispatch import receiver

from teaching.models import BehaviourEntry

log = logging.getLogger(__name__)


@receiver(post_save, sender=BehaviourEntry, dispatch_uid='welfare_behaviour_thresholds')
def behaviour_saved(sender, instance, created, **kwargs):
    if not created:
        return
    from .models import Threshold
    if not Threshold.objects.filter(tenant_id=instance.tenant_id).exists():
        return
    from .services import check_thresholds
    try:
        check_thresholds(instance.student)
    except Exception:   # a threshold email must never stop a teacher saving points
        log.exception('Threshold check failed for student %s', instance.student_id)
