"""Unit tests for :class:`apps.extraction.models.ArticleQuotes` (real ORM).

Exercises the per-article quote cache against the real Django ORM with the
SQLite test database: default ``status``/``quotes`` values, the OneToOne link
to :class:`apps.articles.models.Article` (cascade on article deletion), and
the ``mark_done`` / ``mark_no_text`` / ``mark_failed`` state transitions that
``QuoteExtractionService.enrich`` relies on for cache persistence and the
concurrent-extraction ``pending`` claim. The migration that creates the
``ArticleQuotes`` table applies cleanly in the pytest setup (``migrate``).
"""

from __future__ import annotations

import pytest
from django.db import IntegrityError

from apps.articles.models import Article, Journal, Source
from apps.extraction.models import STATUS_CHOICES, ArticleQuotes

_QUOTES = [
    {
        "text": "A verbatim passage.",
        "location": "abstract",
        "relevance": 0.9,
        "rationale": "core claim",
    },
]


def _make_article(doi: str = "10.1000/models-test") -> Article:
    source = Source.objects.create(
        key="test",
        name="Test",
        base_url="https://example.org",
    )
    journal = Journal.objects.create(name="Journal of Tests")
    return Article.objects.create(
        source=source,
        journal=journal,
        title="A Study of Quotes",
        abstract="A verbatim passage.",
        full_text="Body text.",
        doi=doi,
        url=f"https://example.org/{doi}",
        publication_year=2024,
    )


@pytest.fixture
def article(db) -> Article:
    """Create a persisted article for the OneToOne cache."""
    return _make_article()


class TestDefaults:
    def test_new_row_defaults_pending_empty_quotes(self, article) -> None:
        cache = ArticleQuotes.objects.create(article=article)

        assert cache.status == "pending"
        assert cache.quotes == []
        assert cache.model == ""
        assert cache.error == ""
        assert cache.extracted_at is None
        assert cache.created_at is not None
        assert cache.updated_at is not None

    def test_status_choices_cover_lifecycle(self) -> None:
        choices = {value for value, _label in STATUS_CHOICES}

        assert choices == {"pending", "done", "no_text", "failed"}


class TestMarkDone:
    def test_persists_quotes_and_stamps_extracted_at(self, article) -> None:
        cache = ArticleQuotes.objects.create(article=article)

        cache.mark_done(_QUOTES, model="test-model")

        cache.refresh_from_db()
        assert cache.status == "done"
        assert cache.quotes == _QUOTES
        assert cache.model == "test-model"
        assert cache.extracted_at is not None
        assert cache.error == ""

    def test_related_name_quotes_on_article(self, article) -> None:
        ArticleQuotes.objects.create(article=article)

        assert article.quotes is not None
        assert isinstance(article.quotes, ArticleQuotes)


class TestMarkNoText:
    def test_clears_quotes_and_sets_status(self, article) -> None:
        cache = ArticleQuotes.objects.create(article=article, quotes=_QUOTES)

        cache.mark_no_text()

        cache.refresh_from_db()
        assert cache.status == "no_text"
        assert cache.quotes == []
        assert cache.extracted_at is not None


class TestMarkFailed:
    def test_truncates_error_and_keeps_quotes(self, article) -> None:
        cache = ArticleQuotes.objects.create(article=article, quotes=_QUOTES)

        long_error = "x" * 900
        cache.mark_failed(long_error)

        cache.refresh_from_db()
        assert cache.status == "failed"
        # error field is truncated to 500 chars by ``mark_failed``.
        assert cache.error == "x" * 500
        # quotes are not wiped on failure (next job may retry).
        assert cache.quotes == _QUOTES

    def test_empty_error_stored_as_blank(self, article) -> None:
        cache = ArticleQuotes.objects.create(article=article)

        cache.mark_failed("")

        cache.refresh_from_db()
        assert cache.status == "failed"
        assert cache.error == ""


class TestOneToOne:
    def test_one_cache_per_article(self, article) -> None:
        ArticleQuotes.objects.create(article=article)

        with pytest.raises(IntegrityError):
            ArticleQuotes.objects.create(article=article)

    def test_cascade_delete_with_article(self, article) -> None:
        cache_id = ArticleQuotes.objects.create(article=article).id

        article.delete()

        assert not ArticleQuotes.objects.filter(id=cache_id).exists()


class TestMigration:
    def test_table_created_for_test_db(self, article) -> None:
        """The ``ArticleQuotes`` migration applied (table + status index)."""

        cache = ArticleQuotes.objects.create(article=article)

        assert cache.id is not None
        # ``status`` is indexed (Meta.indexes) — exercised by the query planner
        # implicitly; here we only assert the row is queryable by status.
        assert ArticleQuotes.objects.filter(status="pending").exists()
