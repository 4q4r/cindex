from django.apps import AppConfig


class IngestionConfig(AppConfig):
    """Ingestion application config."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.ingestion"
