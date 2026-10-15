from django import forms

from tenants.forms import TenantScopedFormMixin
from .models import Announcement


class AnnouncementForm(TenantScopedFormMixin, forms.ModelForm):
    class Meta:
        model = Announcement
        fields = [
            'title',
            'message',
            'audience',
            'school_class',
            'term',
            'session',
            'published',
            'expires_at',
            'notify_parents',
            'notify_channel',
        ]
        widgets = {
            'expires_at': forms.DateTimeInput(attrs={'type': 'datetime-local'}),
        }
        labels = {
            'notify_parents': 'Send Email/SMS notification to parents',
            'notify_channel': 'Notification channel',
        }
        help_texts = {
            'notify_parents': 'Parents will receive an email and/or SMS about this announcement',
        }
