from __future__ import annotations

import pytest
from django.db import connections

from apps.articles.models import Article, Source
from apps.search.services import SearchService

pytestmark = pytest.mark.skipif(
    connections["default"].vendor != "postgresql",
    reason="requires the PostgreSQL full-text search backend",
)


def _make_article(
    source: Source,
    *,
    title: str,
    doi: str,
    abstract: str = "",
    full_text: str = "",
) -> Article:
    return Article.objects.create(
        source=source,
        title=title,
        abstract=abstract,
        full_text=full_text,
        url=f"https://example.org/{doi}",
        doi=doi,
        publication_year=2024,
    )


def test_pg_fulltext_search_finds_article_by_title_term(db) -> None:
    """PostgreSQL FTS matches an article by a distinctive title term."""
    source = Source.objects.create(
        key="pgftstest",
        name="PG FTS TEST",
        base_url="https://x",
    )
    _make_article(
        source,
        title="Quantum entanglement in superconducting circuits",
        abstract="We study entanglement dynamics in transmon qubits.",
        full_text="The experiment measures entanglement entropy over time.",
        doi="10.9999/pgfts.title.1",
    )
    results = SearchService.run("entanglement", "")
    assert any(r["doi"] == "10.9999/pgfts.title.1" for r in results)


def test_pg_fulltext_search_finds_article_by_abstract_term(db) -> None:
    """PostgreSQL FTS matches an article by a term present only in abstract."""
    source = Source.objects.create(
        key="pgftstest2",
        name="PG FTS TEST 2",
        base_url="https://x",
    )
    _make_article(
        source,
        title="Unrelated title about ocean currents",
        abstract="Rare kryssartoken marker appears only in the abstract body.",
        full_text="No matching terms here.",
        doi="10.9999/pgfts.abstract.1",
    )
    results = SearchService.run("kryssartoken", "")
    assert any(r["doi"] == "10.9999/pgfts.abstract.1" for r in results)


def test_pg_fulltext_search_excludes_non_matching(db) -> None:
    """PostgreSQL FTS does not return articles lacking the search term."""
    source = Source.objects.create(
        key="pgftstest3",
        name="PG FTS TEST 3",
        base_url="https://x",
    )
    _make_article(
        source,
        title="Completely unrelated subject matter",
        abstract="Nothing relevant here at all.",
        full_text="Lorem ipsum dolor sit amet.",
        doi="10.9999/pgfts.exclude.1",
    )
    results = SearchService.run("entanglement", "")
    assert not any(r["doi"] == "10.9999/pgfts.exclude.1" for r in results)
