"""Persistence models for PERELMAN quote extraction (per-article quote cache).

``ArticleQuotes`` is a OneToOne cache off :class:`apps.articles.models.Article`.
A published article is processed exactly once (``status="done"``) and read from
the cache on every subsequent search; preprints are never cached (volatile —
they may change, so they stay fully refreshable and are re-extracted fresh).
"""

from __future__ import annotations

from django.db import models
from django.utils import timezone

STATUS_PENDING = "pending"
STATUS_DONE = "done"
STATUS_NO_TEXT = "no_text"
STATUS_FAILED = "failed"

STATUS_CHOICES = (
    (STATUS_PENDING, "pending"),
    (STATUS_DONE, "done"),
    (STATUS_NO_TEXT, "no_text"),
    (STATUS_FAILED, "failed"),
)


class ArticleQuotes(models.Model):
    """Per-article cache of PERELMAN-extracted verbatim quotes.

    The ``quotes`` JSONField holds a list of
    ``{"text", "location", "relevance", "rationale"}`` dicts. A row with
    ``status="pending"`` is a concurrent-extraction claim
    (``get_or_create(defaults={"status": "pending"})``) so parallel celery jobs
    do not duplicate LLM calls for the same article.
    """

    article = models.OneToOneField(
        "articles.Article",
        on_delete=models.CASCADE,
        related_name="quotes",
    )
    quotes = models.JSONField(default=list)
    status = models.CharField(
        max_length=16,
        choices=STATUS_CHOICES,
        default=STATUS_PENDING,
        db_index=True,
    )
    extracted_at = models.DateTimeField(null=True, blank=True)
    model = models.CharField(max_length=128, blank=True)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """Django model metadata and options."""

        indexes = (models.Index(fields=["status"]),)

    def __str__(self) -> str:
        """Return the human-readable representation of the object."""
        return f"{self.article_id}:{self.status}:{len(self.quotes or [])}q"

    def mark_done(self, quotes: list, *, model: str = "") -> None:
        """Persist a successful extraction and stamp ``extracted_at``."""
        self.quotes = quotes
        self.status = STATUS_DONE
        self.model = model
        self.extracted_at = timezone.now()
        self.error = ""
        self.save(
            update_fields=[
                "quotes",
                "status",
                "model",
                "extracted_at",
                "error",
                "updated_at",
            ],
        )

    def mark_no_text(self) -> None:
        """Record that the article had no extractable text."""
        self.quotes = []
        self.status = STATUS_NO_TEXT
        self.extracted_at = timezone.now()
        self.save(
            update_fields=["quotes", "status", "extracted_at", "updated_at"],
        )

    def mark_failed(self, error: str) -> None:
        """Record a failed extraction (the next job may retry)."""
        self.status = STATUS_FAILED
        self.error = (error or "")[:500]
        self.save(update_fields=["status", "error", "updated_at"])
