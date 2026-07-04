"""App configuration for the articles app."""

from django.apps import AppConfig


class ArticlesConfig(AppConfig):
    """Articles application config."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.articles"
