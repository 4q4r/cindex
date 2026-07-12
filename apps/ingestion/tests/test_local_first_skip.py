"""Ingestion local-first skip: frozen articles are read from local md.

A published article frozen by PERELMAN (a ``.md`` file exists in
``CINDEX_ARTICLES_DIR``) must NOT be re-fetched from the network on refresh:
``IngestionService._process_single_source`` probes
:meth:`LocalArticleStore.exists` before calling ``connector.enrich_raw`` and,
on a hit, builds the :class:`RawArticle` from the local md via
:meth:`LocalArticleStore.to_raw`. The DB upsert (``_save_article``) then takes
its content from the md, so ``connector.enrich_raw`` is bypassed entirely. An
unfrozen article (no md) is enriched from the network as before. A frozen md
that fails to parse falls back to network enrichment.
"""

from __future__ import annotations

from apps.articles.models import Article
from apps.extraction.local_store import LocalArticleStore
from apps.ingestion.connectors import RawArticle
from apps.ingestion.services import IngestionService

_DOI = "10.9999/local.1"


def _network_raw() -> RawArticle:
    """Return the raw article the connector would fetch from the network."""
    return RawArticle(
        source_key="dummy",
        title="Network Article",
        url="https://example.org/a1",
        abstract="network abstract",
        full_text="network full text NETWORK_MARKER",
        language="en",
        year=2024,
        doi=_DOI,
        journal="Dummy Journal",
        peer_review_evidence="peer reviewed",
        indexing_evidence="scopus",
        preprint_evidence="journal article",
    )


def _make_spy_connector() -> tuple[type, list[RawArticle]]:
    """Build a connector class that records every ``enrich_raw`` call."""

    enrich_calls: list[RawArticle] = []

    class _SpyConnector:
        def fetch(self, query: str, limit: int = 5):
            return [_network_raw()]

        def enrich_raw(self, raw: RawArticle) -> RawArticle:
            enrich_calls.append(raw)
            return raw

    return _SpyConnector, enrich_calls


def _frozen_md() -> str:
    """Return a parseable frozen-md document distinct from the network raw."""
    return (
        "---\n"
        "title: Frozen Article\n"
        "authors:\n"
        "  - Jane Doe\n"
        "year: 2023\n"
        "journal: Frozen Journal\n"
        f"doi: {_DOI}\n"
        "url: https://example.org/a1\n"
        "source_key: dummy\n"
        "is_preprint: false\n"
        "---\n\n"
        "## Аннотация\n\n"
        "Frozen abstract from local md.\n\n"
        "## Полный текст\n\n"
        "Frozen full text from local md FROZEN_MARKER.\n"
    )


def _malformed_md() -> str:
    """Return an md with an unclosed front-matter block (parse failure)."""
    return "---\ntitle: Broken\nkey: value\n"


def test_frozen_article_skips_network_enrich(monkeypatch, tmp_path, db) -> None:
    """A frozen article is read from local md; ``enrich_raw`` is not called."""
    monkeypatch.setenv("CINDEX_ARTICLES_DIR", str(tmp_path))
    LocalArticleStore._path(_DOI).write_text(_frozen_md(), encoding="utf-8")

    spy_cls, enrich_calls = _make_spy_connector()
    monkeypatch.setattr("apps.ingestion.services.CONNECTORS", {"dummy": spy_cls})

    articles = IngestionService.ingest_query(
        "test",
        source_keys=["dummy"],
        per_source_limit=1,
    )

    assert len(articles) == 1
    # The network enrich path was bypassed entirely.
    assert enrich_calls == []
    article = Article.objects.get(doi=_DOI)
    # The DB upsert took its content from the local md, not the network raw.
    assert "FROZEN_MARKER" in article.full_text
    assert "NETWORK_MARKER" not in article.full_text
    assert article.abstract == "Frozen abstract from local md."
    assert article.title == "Frozen Article"
    assert article.journal.name == "Frozen Journal"
    assert article.publication_year == 2023


def test_unfrozen_article_is_enriched_from_network(
    monkeypatch,
    tmp_path,
    db,
) -> None:
    """An unfrozen article (no local md) is enriched from the network."""
    monkeypatch.setenv("CINDEX_ARTICLES_DIR", str(tmp_path))

    spy_cls, enrich_calls = _make_spy_connector()
    monkeypatch.setattr("apps.ingestion.services.CONNECTORS", {"dummy": spy_cls})

    articles = IngestionService.ingest_query(
        "test",
        source_keys=["dummy"],
        per_source_limit=1,
    )

    assert len(articles) == 1
    assert len(enrich_calls) == 1
    article = Article.objects.get(doi=_DOI)
    assert "NETWORK_MARKER" in article.full_text
    assert "FROZEN_MARKER" not in article.full_text


def test_frozen_md_parse_failure_falls_back_to_network(
    monkeypatch,
    tmp_path,
    db,
) -> None:
    """A frozen md that fails to parse falls back to network enrichment."""
    monkeypatch.setenv("CINDEX_ARTICLES_DIR", str(tmp_path))
    LocalArticleStore._path(_DOI).write_text(_malformed_md(), encoding="utf-8")

    spy_cls, enrich_calls = _make_spy_connector()
    monkeypatch.setattr("apps.ingestion.services.CONNECTORS", {"dummy": spy_cls})

    articles = IngestionService.ingest_query(
        "test",
        source_keys=["dummy"],
        per_source_limit=1,
    )

    assert len(articles) == 1
    assert len(enrich_calls) == 1
    article = Article.objects.get(doi=_DOI)
    assert "NETWORK_MARKER" in article.full_text
