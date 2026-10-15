from django.apps import AppConfig


class WelfareConfig(AppConfig):
    name = 'welfare'
    verbose_name = 'Health, welfare & discipline'
    default_auto_field = 'django.db.models.BigAutoField'

    def ready(self):
        from . import signals  # noqa: F401
