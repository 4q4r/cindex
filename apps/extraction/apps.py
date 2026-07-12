"""App configuration for the extraction app."""

from django.apps import AppConfig


class ExtractionConfig(AppConfig):
    """Extraction application config (PERELMAN LLM quote extraction)."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.extraction"
