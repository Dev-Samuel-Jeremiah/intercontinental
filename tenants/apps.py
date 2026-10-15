from django.apps import AppConfig


class TenantsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'tenants'
    verbose_name = 'Multi-Tenancy'

    def ready(self):
        # FIX: lock down /admin/ so only superusers can log in or access it.
        # By default Django admin allows any user with is_staff=True (e.g.
        # school admins, if that flag is ever set on them). Overriding
        # has_permission on the global admin.site enforces is_superuser
        # everywhere admin checks permission — login, every page view, and
        # already-active sessions.
        from django.contrib import admin

        def has_permission(self, request):
            return bool(
                request.user
                and request.user.is_active
                and request.user.is_superuser
            )

        admin.site.has_permission = has_permission.__get__(admin.site, admin.site.__class__)

        # Import every app's forms module now, while no school is active.
        #
        # A ModelChoiceField's queryset is built once, when its module is
        # first imported. If that first import happened during a request for
        # one school, the queryset would be filtered to that school for the
        # life of the process, and every other school would get an empty
        # dropdown. Importing here, at startup, keeps the base queryset
        # unscoped; TenantScopedFormMixin then scopes it per request.
        import importlib
        from django.apps import apps as django_apps

        for app_config in django_apps.get_app_configs():
            if app_config.name.startswith('django.'):
                continue
            try:
                importlib.import_module(f'{app_config.name}.forms')
            except ModuleNotFoundError:
                pass
            except Exception:  # a broken forms module must not stop startup
                import logging
                logging.getLogger(__name__).exception(
                    'Could not preload %s.forms', app_config.name)
