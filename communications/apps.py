from django.apps import AppConfig


class CommunicationsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'communications'
    verbose_name = 'Communications'

    def ready(self):
        """Wire up signals when Django starts."""
        try:
            import communications.signals  # noqa: F401
        except Exception:
            pass
