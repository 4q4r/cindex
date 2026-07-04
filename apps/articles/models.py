"""Database models for scholarly articles."""

from __future__ import annotations

from django.db import models
from django.utils import timezone


class Source(models.Model):
    """A source platform that provides scholarly records."""

    key = models.CharField(max_length=64, unique=True)
    name = models.CharField(max_length=128)
    base_url = models.URLField()
    active = models.BooleanField(default=True)
    total_runs = models.PositiveIntegerField(default=0)
    total_successes = models.PositiveIntegerField(default=0)
    total_failures = models.PositiveIntegerField(default=0)
    consecutive_failures = models.PositiveIntegerField(default=0)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    last_success_at = models.DateTimeField(null=True, blank=True)
    circuit_open_until = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)

    def __str__(self) -> str:
        """Return the human-readable representation of the object."""
        return self.name

    def is_circuit_open(self) -> bool:
        """Return whether circuit open."""
        return bool(
            self.circuit_open_until and self.circuit_open_until > timezone.now(),
        )


class Journal(models.Model):
    """A scholarly journal or publication venue."""

    name = models.CharField(max_length=300, db_index=True)
    issn = models.CharField(max_length=32, blank=True)
    eissn = models.CharField(max_length=32, blank=True)
    publisher = models.CharField(max_length=255, blank=True)

    def __str__(self) -> str:
        """Return the human-readable representation of the object."""
        return self.name


class Article(models.Model):
    """A normalized scholarly article record."""

    source = models.ForeignKey(
        Source,
        on_delete=models.CASCADE,
        related_name="articles",
    )
    journal = models.ForeignKey(
        Journal,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    external_id = models.CharField(max_length=255, blank=True)
    title = models.CharField(max_length=1000)
    abstract = models.TextField(blank=True)
    full_text = models.TextField(blank=True)
    language = models.CharField(max_length=32, blank=True)
    publication_year = models.IntegerField(null=True, blank=True)
    publication_date = models.DateField(null=True, blank=True)
    url = models.URLField(max_length=1000, db_index=True)
    doi = models.CharField(max_length=256)
    volume = models.CharField(max_length=32, blank=True)
    issue = models.CharField(max_length=32, blank=True)
    pages = models.CharField(max_length=32, blank=True)
    is_open_access = models.BooleanField(default=True)

    is_peer_reviewed_or_refereed = models.BooleanField(default=False)
    is_indexed_in_reputable_db = models.BooleanField(default=False)
    has_doi_and_journal_card = models.BooleanField(default=False)
    is_not_preprint_or_author_manuscript = models.BooleanField(default=False)
    search_vector = models.TextField(blank=True, default="")
    is_eligible = models.BooleanField(default=False)
    peer_review_confidence = models.FloatField(default=0.0)
    indexing_confidence = models.FloatField(default=0.0)
    doi_and_card_confidence = models.FloatField(default=0.0)
    not_preprint_confidence = models.FloatField(default=0.0)
    eligibility_confidence = models.FloatField(default=0.0)

    peer_review_evidence = models.TextField(blank=True)
    indexing_evidence = models.TextField(blank=True)
    doi_journal_evidence = models.TextField(blank=True)
    preprint_evidence = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """Django model metadata and options."""

        indexes = (
            models.Index(fields=["is_eligible", "publication_year"]),
            models.Index(fields=["doi"]),
        )

    def __str__(self) -> str:
        """Return the human-readable representation of the object."""
        return self.title


class Author(models.Model):
    """A normalized author entity."""

    full_name = models.CharField(max_length=255, db_index=True)

    def __str__(self) -> str:
        """Return the human-readable representation of the object."""
        return self.full_name


class ArticleAuthor(models.Model):
    """Join table storing article-author ordering."""

    article = models.ForeignKey(
        Article,
        on_delete=models.CASCADE,
        related_name="article_authors",
    )
    author = models.ForeignKey(Author, on_delete=models.CASCADE)
    order = models.PositiveIntegerField(default=1)

    class Meta:
        """Django model metadata and options."""

        unique_together = ("article", "author", "order")
        ordering = ("order",)

    def __str__(self) -> str:
        """Return the human-readable representation of the object."""
        return f"{self.article_id}:{self.author_id}:{self.order}"


class Identifier(models.Model):
    """An external identifier associated with an article."""

    article = models.ForeignKey(
        Article,
        on_delete=models.CASCADE,
        related_name="identifiers",
    )
    kind = models.CharField(max_length=64)
    value = models.CharField(max_length=255)

    class Meta:
        """Django model metadata and options."""

        unique_together = ("article", "kind", "value")

    def __str__(self) -> str:
        """Return the human-readable representation of the object."""
        return f"{self.kind}:{self.value}"


# Full-text search uses an ad-hoc SearchVector expression (title, abstract,
# full_text) in the search service rather than a stored tsvector column. A GIN
# expression index is created on PostgreSQL only (see migration 0010) so the
# ``vector @@ query`` filter is index-accelerated in production. The
# ``search_vector`` TextField above is retained as an unused placeholder for
# compatibility with existing migrations and is not part of the FTS path.
