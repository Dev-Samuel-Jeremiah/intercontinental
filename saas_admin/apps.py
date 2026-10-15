from django.apps import AppConfig


class SaasAdminConfig(AppConfig):
    # The app label stays 'saas_admin' so existing migrations and data are untouched.
    name = 'saas_admin'
    verbose_name = 'School administration'
    default_auto_field = 'django.db.models.BigAutoField'
