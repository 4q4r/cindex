import pytest
from django.db import IntegrityError, transaction

from apps.articles.models import Article, Source
from apps.ingestion.connectors import ConnectorFetchError, RawArticle
from apps.ingestion.services import IngestionService


class DummyConnector:
    """Dummy source connector."""

    def fetch(self, query: str, limit: int = 5):
        return [
            RawArticle(
                source_key="dummy",
                title="Peer reviewed indexed article",
                url="https://example.org/a1",
                abstract="peer reviewed and indexed in scopus",
                full_text="journal article doi 10.9999/dummy.1",
                language="en",
                year=2024,
                doi="10.9999/dummy.1",
                journal="Dummy Journal",
                peer_review_evidence="peer reviewed",
                indexing_evidence="scopus web of science",
                preprint_evidence="journal article",
            ),
        ]

    def enrich_raw(self, raw: RawArticle) -> RawArticle:
        return raw


class EnrichingConnector:
    """Enriching source connector."""

    def fetch(self, query: str, limit: int = 5):
        return [
            RawArticle(
                source_key="dummy",
                title="Evidence article",
                url="https://example.org/a2",
                abstract="peer reviewed",
                full_text="journal article",
                language="en",
                year=2024,
                doi="",
                journal="Dummy Journal",
                peer_review_evidence="peer reviewed",
                indexing_evidence="scopus",
                preprint_evidence="journal article",
            ),
        ]

    def enrich_raw(self, raw: RawArticle) -> RawArticle:
        raw.doi = "10.7777/enriched.1"
        raw.full_text = f"{raw.full_text} doi 10.7777/enriched.1"
        return raw


class ProgressConnector:
    """Progress source connector."""

    def fetch(self, query: str, limit: int = 5):
        return [
            RawArticle(
                source_key="dummy",
                title="Progress article",
                url="https://example.org/a3",
                abstract="peer reviewed",
                full_text="journal article",
                language="en",
                year=2024,
                doi="10.8888/progress.1",
                journal="Dummy Journal",
                peer_review_evidence="peer reviewed",
                indexing_evidence="scopus",
                preprint_evidence="journal article",
            ),
        ]

    def enrich_raw(self, raw: RawArticle) -> RawArticle:
        return raw


def test_ingestion_emits_russian_progress_phases(monkeypatch, db) -> None:
    """Test ingestion emits russian progress phases helper."""
    events: list[dict] = []

    monkeypatch.setattr(
        "apps.ingestion.services.CONNECTORS",
        {"dummy": ProgressConnector},
    )

    articles = IngestionService.ingest_query(
        "test",
        source_keys=["dummy"],
        per_source_limit=1,
        progress_callback=events.append,
    )

    assert len(articles) == 1
    labels = [
        event["substage_label"] for event in events if event.get("substage_label")
    ]
    assert labels[0] == "Запрос принят"
    assert "Собираем статьи" in labels
    assert "Обогащаем карточки" in labels
    assert "Индексируем статьи" in labels
    assert "Источник обработан" in labels


def test_ingestion_indexes_eligible_article(monkeypatch, db) -> None:
    """Test ingestion persists eligible article records."""
    monkeypatch.setattr("apps.ingestion.services.CONNECTORS", {"dummy": DummyConnector})

    articles = IngestionService.ingest_query(
        "test",
        source_keys=["dummy"],
        per_source_limit=2,
    )

    assert len(articles) == 1
    assert articles[0].is_eligible is True
    assert articles[0].full_text.startswith("journal article doi")


def test_ingestion_applies_enrichment_hook(monkeypatch, db) -> None:
    """Test ingestion applies enrichment hook helper."""
    monkeypatch.setattr(
        "apps.ingestion.services.CONNECTORS",
        {"dummy": EnrichingConnector},
    )

    articles = IngestionService.ingest_query(
        "test",
        source_keys=["dummy"],
        per_source_limit=1,
    )

    assert len(articles) == 1
    assert articles[0].doi == "10.7777/enriched.1"


class NoDoiConnector:
    """Connector that returns an article without DOI and no enrichment."""

    def fetch(self, query: str, limit: int = 5):
        return [
            RawArticle(
                source_key="no-doi",
                title="Article without DOI",
                url="https://example.org/no-doi",
                abstract="",
                full_text="This article has no DOI",
                language="en",
                year=2024,
                doi="",
                journal="No DOI Journal",
            ),
        ]

    def enrich_raw(self, raw: RawArticle) -> RawArticle:
        return raw


def test_ingestion_skips_articles_without_doi(monkeypatch, db) -> None:
    """Articles without a valid DOI should not be saved."""
    monkeypatch.setattr(
        "apps.ingestion.services.CONNECTORS",
        {"no-doi": NoDoiConnector},
    )

    articles = IngestionService.ingest_query(
        "test",
        source_keys=["no-doi"],
        per_source_limit=1,
    )

    assert len(articles) == 0


def test_article_doi_is_unique(db) -> None:
    """Article.doi must enforce uniqueness at the database level."""
    source = Source.objects.create(key="s1", name="S1", base_url="https://x")
    Article.objects.create(source=source, title="t1", url="https://x/1", doi="10.1/a")
    with pytest.raises(IntegrityError), transaction.atomic():
        Article.objects.create(
            source=source,
            title="t2",
            url="https://x/2",
            doi="10.1/a",
        )


def test_save_article_upserts_by_doi(db) -> None:
    """_save_article upserts by DOI across sources.

    A second ingestion of the same DOI from a different source must update
    the existing row instead of creating a duplicate Article.
    """
    raw_a = RawArticle(
        source_key="src-a",
        title="From A",
        url="https://a/1",
        abstract="",
        full_text="a",
        language="en",
        year=2024,
        doi="10.5555/shared",
        journal="J",
    )
    raw_b = RawArticle(
        source_key="src-b",
        title="From B",
        url="https://b/1",
        abstract="",
        full_text="b",
        language="en",
        year=2024,
        doi="10.5555/shared",
        journal="J",
    )
    first = IngestionService._save_article(raw_a)
    second = IngestionService._save_article(raw_b)
    assert first.pk == second.pk
    assert second.title == "From B"
    assert second.source.key == "src-b"
    assert Article.objects.filter(doi="10.5555/shared").count() == 1


def test_save_article_retraction_flag_is_monotonic(db) -> None:
    """_save_article never clears a retraction flag set by another source.

    A retraction is irreversible: a connector unaware of it must not reset
    ``is_retracted`` from True back to False on re-ingest.
    """
    raw_flagged = RawArticle(
        source_key="src-a",
        title="Retracted work",
        url="https://a/2",
        abstract="",
        full_text="a",
        language="en",
        year=2024,
        doi="10.5555/retracted",
        journal="J",
        is_retracted=True,
        retraction_note="https://doi.org/notice",
    )
    raw_clean = RawArticle(
        source_key="src-b",
        title="Retracted work",
        url="https://b/2",
        abstract="",
        full_text="b",
        language="en",
        year=2024,
        doi="10.5555/retracted",
        journal="J",
        is_retracted=False,
    )
    first = IngestionService._save_article(raw_flagged)
    assert first.is_retracted is True
    assert first.retraction_note == "https://doi.org/notice"
    second = IngestionService._save_article(raw_clean)
    assert second.is_retracted is True
    assert second.retraction_note == "https://doi.org/notice"
    assert second.pk == first.pk


class FailingConnector:
    """Connector whose fetch raises ConnectorFetchError.

    Models the SciEngine / Medknow openalex paths before the swallow fix:
    fetch must surface the error so the ingestion service marks the source
    failed instead of silently reporting zero articles as a success.
    """

    def fetch(self, query: str, limit: int = 5):
        raise ConnectorFetchError("failing: simulated upstream failure")

    def enrich_raw(self, raw: RawArticle) -> RawArticle:
        return raw


def test_ingestion_surfaces_connector_fetch_error_as_failed_source(
    monkeypatch,
    db,
) -> None:
    """A fetch ConnectorFetchError must mark the source failed.

    The error must propagate instead of being swallowed into an empty
    list reported as a successful zero-article result.
    """
    monkeypatch.setattr(
        "apps.ingestion.services.CONNECTORS",
        {"failing": FailingConnector},
    )

    articles = IngestionService.ingest_query(
        "test",
        source_keys=["failing"],
        per_source_limit=1,
    )

    assert articles == []
    source = Source.objects.get(key="failing")
    assert source.total_failures == 1
    assert source.last_error


class EnrichFailingConnector:
    """Connector whose ``enrich_raw`` raises ``ConnectorFetchError``.

    Models a sidecar 502/403 or a residual challenge page on one article
    landing page: fetch succeeds (articles are returned), but enrichment
    of each article's landing page fails. The raw payload is already fetched
    and eligible, so a single-article enrichment failure must degrade to the
    raw record instead of aborting the whole source.
    """

    def fetch(self, query: str, limit: int = 5):
        return [
            RawArticle(
                source_key="enrich-failing",
                title="Peer reviewed indexed article",
                url="https://example.org/a1",
                abstract="peer reviewed and indexed in scopus",
                full_text="journal article doi 10.9999/enrichfail.1",
                language="en",
                year=2024,
                doi="10.9999/enrichfail.1",
                journal="Dummy Journal",
                peer_review_evidence="peer reviewed",
                indexing_evidence="scopus web of science",
                preprint_evidence="journal article",
            ),
        ]

    def enrich_raw(self, raw: RawArticle) -> RawArticle:
        raise ConnectorFetchError("enrich-failing: sidecar 502 on landing page")


def test_ingestion_enrich_connector_fetch_error_keeps_raw_source(
    monkeypatch,
    db,
) -> None:
    """An enrich-level ``ConnectorFetchError`` must not abort the source.

    A fetch-level error (no articles fetched) still marks the source failed
    (see ``test_ingestion_surfaces_connector_fetch_error_as_failed_source``).
    But once articles are fetched, a per-article enrichment failure (sidecar
    502/403 or residual challenge page on the landing page) must degrade to
    the already-fetched raw payload, keep the source marked successful, and
    still index the article — not discard every article for that source.
    """
    monkeypatch.setattr(
        "apps.ingestion.services.CONNECTORS",
        {"enrich-failing": EnrichFailingConnector},
    )

    articles = IngestionService.ingest_query(
        "test",
        source_keys=["enrich-failing"],
        per_source_limit=1,
    )

    assert len(articles) == 1
    assert articles[0].doi == "10.9999/enrichfail.1"
    source = Source.objects.get(key="enrich-failing")
    assert source.total_failures == 0
    assert not source.last_error
