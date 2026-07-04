from apps.articles.models import Source
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
            )
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
            )
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
            )
        ]

    def enrich_raw(self, raw: RawArticle) -> RawArticle:
        return raw


def test_ingestion_emits_russian_progress_phases(monkeypatch, db) -> None:
    """Test ingestion emits russian progress phases helper."""
    events: list[dict] = []

    monkeypatch.setattr(
        "apps.ingestion.services.CONNECTORS", {"dummy": ProgressConnector}
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
        "test", source_keys=["dummy"], per_source_limit=2
    )

    assert len(articles) == 1
    assert articles[0].is_eligible is True
    assert articles[0].full_text.startswith("journal article doi")


def test_ingestion_applies_enrichment_hook(monkeypatch, db) -> None:
    """Test ingestion applies enrichment hook helper."""
    monkeypatch.setattr(
        "apps.ingestion.services.CONNECTORS", {"dummy": EnrichingConnector}
    )

    articles = IngestionService.ingest_query(
        "test", source_keys=["dummy"], per_source_limit=1
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
            )
        ]

    def enrich_raw(self, raw: RawArticle) -> RawArticle:
        return raw


def test_ingestion_skips_articles_without_doi(monkeypatch, db) -> None:
    """Articles without a valid DOI should not be saved."""
    monkeypatch.setattr(
        "apps.ingestion.services.CONNECTORS", {"no-doi": NoDoiConnector}
    )

    articles = IngestionService.ingest_query(
        "test", source_keys=["no-doi"], per_source_limit=1
    )

    assert len(articles) == 0


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
    monkeypatch, db
) -> None:
    """A fetch ConnectorFetchError must mark the source failed.

    The error must propagate instead of being swallowed into an empty
    list reported as a successful zero-article result.
    """
    monkeypatch.setattr(
        "apps.ingestion.services.CONNECTORS", {"failing": FailingConnector}
    )

    articles = IngestionService.ingest_query(
        "test", source_keys=["failing"], per_source_limit=1
    )

    assert articles == []
    source = Source.objects.get(key="failing")
    assert source.total_failures == 1
    assert source.last_error
