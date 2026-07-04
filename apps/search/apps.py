"""App configuration for the search app."""

from __future__ import annotations

from django.apps import AppConfig


class SearchConfig(AppConfig):
    """Search application config."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.search"
