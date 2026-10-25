from django.apps import AppConfig


class DashboardConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'dashboard'

    def ready(self):
        from django.db.models.signals import post_save

        from users.models import Staff

        def staff_changed(sender, instance, **kwargs):
            # New permission switches or role: rebuild this person's sidebar straight away
            from django.core.cache import cache
            cache.delete(f'sidebar-closed:{instance.tenant_id}:{instance.user_id}')

        post_save.connect(staff_changed, sender=Staff, dispatch_uid='dashboard-sidebar-staff-changed')
