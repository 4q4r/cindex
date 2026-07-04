from __future__ import annotations

import os

import structlog
from django.apps import AppConfig

from apps.search.warmup import start_background_warmup

LOGGER = structlog.get_logger(__name__)


class SearchConfig(AppConfig):
    """Search application config."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.search"

    def ready(self) -> None:
        """Kick off search model warmup when runtime containers enable it."""
        if os.getenv("CINDEX_WARMUP_SEARCH_MODELS", "").lower() not in {
            "1",
            "true",
            "yes",
        }:
            return
        start_background_warmup()
