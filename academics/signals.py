from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .models import PublicHoliday, Term


@receiver(post_save, sender=PublicHoliday)
@receiver(post_delete, sender=PublicHoliday)
def recalculate_term_days(sender, instance, **kwargs):
    """
    A holiday was added, changed or removed: recount school days for this
    school's terms that cover it. Term.save() does the counting (weekdays
    that are not holidays, or the school's manual figure), so this is
    correct however many times it runs — unlike subtracting one per save,
    which also hit other schools' terms and weekend holidays.
    """
    for term in Term.objects.filter(tenant_id=instance.tenant_id, start_date__lte=instance.date,
                                    end_date__gte=instance.date):
        term.save()
