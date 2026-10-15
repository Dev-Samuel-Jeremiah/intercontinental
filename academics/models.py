from django.db import models
from django.core.exceptions import ValidationError
from datetime import date, datetime, timedelta
from tenants.managers import TenantModelMixin, TenantManager
from tenants.middleware import get_current_tenant


class AcademicSession(TenantModelMixin, models.Model):
    name      = models.CharField(max_length=20)
    is_active = models.BooleanField(default=False)
    start_date = models.DateField(null=True, blank=True)
    end_date   = models.DateField(null=True, blank=True)

    objects = TenantManager()

    class Meta:
        ordering = ['-name']
        unique_together = ('tenant', 'name')

    def clean(self):
        if self.is_active:
            qs = AcademicSession.objects.filter(is_active=True).exclude(id=self.id)
            if qs.exists():
                raise ValidationError("Only one academic session can be active per school.")
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValidationError("Session start date must be before end date.")

    def save(self, *args, **kwargs):
        if not self.tenant_id:
            tenant = get_current_tenant()
            if tenant:
                self.tenant = tenant
        if self.is_active:
            AcademicSession.objects.filter(tenant=self.tenant).exclude(id=self.id).update(is_active=False)
        super(TenantModelMixin, self).save(*args, **kwargs)

    def __str__(self):
        return self.name


class PublicHoliday(TenantModelMixin, models.Model):
    name = models.CharField(max_length=100)
    date = models.DateField()

    objects = TenantManager()

    class Meta:
        unique_together = ('tenant', 'date')

    def __str__(self):
        return f"{self.name} ({self.date})"


class Term(TenantModelMixin, models.Model):
    TERM_CHOICES = (
        ('1st Term', '1st Term'),
        ('2nd Term', '2nd Term'),
        ('3rd Term', '3rd Term'),
    )

    session  = models.ForeignKey(AcademicSession, on_delete=models.CASCADE, related_name='terms')
    name     = models.CharField(max_length=20, choices=TERM_CHOICES)
    is_active = models.BooleanField(default=False)
    start_date = models.DateField(null=True, blank=True)
    end_date   = models.DateField(null=True, blank=True)
    number_of_school_days    = models.PositiveIntegerField(default=0)
    manual_school_days       = models.PositiveIntegerField(default=0,
        help_text="Override auto-calculated days. Set 0 to use auto-calculation.")
    resumption_date_next_term = models.DateField(null=True, blank=True)

    objects = TenantManager()

    class Meta:
        unique_together = ('tenant', 'session', 'name')
        ordering = ['start_date']

    def calculate_school_days(self):
        if not self.start_date or not self.end_date:
            return 0
        holidays = set(
            PublicHoliday.objects.filter(
                tenant_id=self.tenant_id,
                date__range=(self.start_date, self.end_date)
            ).values_list('date', flat=True)
        )
        day_count = 0
        current_day = self.start_date
        while current_day <= self.end_date:
            if current_day.weekday() < 5 and current_day not in holidays:
                day_count += 1
            current_day += timedelta(days=1)
        return day_count

    def save(self, *args, **kwargs):
        if not self.tenant_id:
            tenant = get_current_tenant()
            if tenant:
                self.tenant = tenant
        if isinstance(self.start_date, str):
            self.start_date = datetime.strptime(self.start_date, "%Y-%m-%d").date()
        if isinstance(self.end_date, str):
            self.end_date = datetime.strptime(self.end_date, "%Y-%m-%d").date()
        auto_days = self.calculate_school_days()
        self.number_of_school_days = self.manual_school_days if self.manual_school_days > 0 else auto_days
        if self.is_active:
            Term.objects.filter(tenant=self.tenant, session=self.session).exclude(id=self.id).update(is_active=False)
        super(TenantModelMixin, self).save(*args, **kwargs)

    def clean(self):
        if self.is_active and not self.session.is_active:
            raise ValidationError("Cannot activate a term under an inactive session.")
        qs = Term.objects.filter(tenant=self.tenant, session=self.session, is_active=True).exclude(id=self.id)
        if self.is_active and qs.exists():
            raise ValidationError("Only one term can be active per session.")
        if self.start_date and self.end_date and self.start_date >= self.end_date:
            raise ValidationError("Term start date must be before end date.")
        if self.resumption_date_next_term and self.end_date and self.resumption_date_next_term <= self.end_date:
            raise ValidationError("Next term resumption date must be after this term's end date.")

    @property
    def closing_date(self):
        return self.end_date

    def __str__(self):
        return f"{self.session} - {self.name}"