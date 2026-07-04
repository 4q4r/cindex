"""Query selectors for the articles app."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .models import Article

if TYPE_CHECKING:
    from django.db.models import QuerySet


def eligible_articles() -> QuerySet[Article]:
    """Eligible articles helper."""
    return (
        Article.objects.filter(is_eligible=True, doi__startswith="10.")
        .select_related("source", "journal")
        .prefetch_related("article_authors__author", "identifiers")
    )
