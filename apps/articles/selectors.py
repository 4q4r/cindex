from __future__ import annotations

from django.db.models import QuerySet

from .models import Article


def eligible_articles() -> QuerySet[Article]:
    """Eligible articles helper."""
    return (
        Article.objects.filter(is_eligible=True, doi__startswith="10.")
        .select_related("source", "journal")
        .prefetch_related("article_authors__author", "identifiers")
    )
