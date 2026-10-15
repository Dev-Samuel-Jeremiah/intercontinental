from django.apps import AppConfig


class CbtConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'cbt'

    def ready(self):
        # Import signals when app is ready
        import cbt.signals
