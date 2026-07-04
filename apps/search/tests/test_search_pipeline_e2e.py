from __future__ import annotations

from apps.articles.models import Article, Journal, Source
from apps.ingestion.connectors import RawArticle
from apps.search.services import SearchService


class E2EConnector:
    """E2E source connector."""

    def __init__(self, events: list[str]):
        self.events = events

    def fetch(self, query: str, limit: int = 5):
        self.events.append(f"fetch:{query}:{limit}")
        return [
            RawArticle(
                source_key="e2e",
                title="Clinical trial ranking with graph methods 2024",
                url="https://example.org/e2e-article",
                abstract="Peer reviewed and indexed in scopus. Clinical trial ranking methods.",
                full_text="Journal article discussing clinical trial ranking methods.",
                language="en",
                year=2024,
                doi="",
                journal="E2E Journal",
                peer_review_evidence="peer reviewed",
                indexing_evidence="scopus",
                preprint_evidence="journal article",
            )
        ]

    def enrich_raw(self, raw: RawArticle) -> RawArticle:
        self.events.append(f"enrich:{raw.url}")
        raw.doi = "10.1234/e2e.2024.1"
        raw.full_text = f"{raw.full_text} DOI 10.1234/e2e.2024.1"
        return raw


def test_search_pipeline_end_to_end_article_ranking(monkeypatch, db):
    """Search should ingest fresh articles and rank article-level matches."""

    events: list[str] = []

    monkeypatch.setattr(
        "apps.ingestion.services.CONNECTORS", {"e2e": lambda: E2EConnector(events)}
    )

    results = SearchService.run(
        query="clinical trial",
        expression="",
        force_refresh=True,
    )

    assert results, "search results should not be empty"
    assert "fetch:clinical trial:5" in events
    assert any(x.startswith("enrich:https://example.org/e2e-article") for x in events)

    first = results[0]
    assert first["title"].startswith("Clinical trial ranking")
    assert first["doi"] == "10.1234/e2e.2024.1"
    assert "<em>" not in first["preview"]
    assert "clinical trial" in first["preview"].lower()
    assert first["identifiers"].get("doi") == "10.1234/e2e.2024.1"


def test_search_pipeline_deduplicates_same_doi_articles(db):
    """Search should collapse duplicate article records with the same DOI."""

    source_a = Source.objects.create(
        key="src-a", name="SRC A", base_url="https://a.example"
    )
    source_b = Source.objects.create(
        key="src-b", name="SRC B", base_url="https://b.example"
    )
    journal = Journal.objects.create(name="Duplicate Journal")
    Article.objects.create(
        source=source_a,
        journal=journal,
        title="Shared article title",
        abstract="",
        full_text="",
        language="en",
        publication_year=2024,
        url="https://example.org/a",
        doi="10.1234/shared.doi",
        is_eligible=True,
    )
    Article.objects.create(
        source=source_b,
        journal=journal,
        title="Shared article title",
        abstract="",
        full_text="",
        language="en",
        publication_year=2024,
        url="https://example.org/b",
        doi="10.1234/shared.doi",
        is_eligible=True,
    )

    results = SearchService._run_index_search(
        query="shared article", expression="", size=10
    )

    assert len(results) == 1
    assert results[0]["doi"] == "10.1234/shared.doi"
    assert "<em>" not in results[0]["preview"]


def test_search_pipeline_keeps_ineligible_articles(db):
    """Search should not filter out ineligible articles from the result set."""

    source = Source.objects.create(
        key="ineligible-src", name="INELIGIBLE SRC", base_url="https://i.example"
    )
    journal = Journal.objects.create(name="Ineligible Journal")
    Article.objects.create(
        source=source,
        journal=journal,
        title="Neural parsing for mixed signal corpora",
        abstract="",
        full_text="Neural parsing for mixed signal corpora and retrieval.",
        language="en",
        publication_year=2024,
        url="https://example.org/ineligible",
        doi="10.1234/ineligible.1",
        is_eligible=False,
    )

    results = SearchService._run_index_search(
        query="neural parsing", expression="", size=10
    )

    assert results
    assert results[0]["doi"] == "10.1234/ineligible.1"


def test_search_pipeline_excludes_articles_without_doi(db):
    """Search should exclude articles that lack a valid DOI."""

    source = Source.objects.create(
        key="no-doi-src", name="NO DOI SRC", base_url="https://nd.example"
    )
    journal = Journal.objects.create(name="No DOI Journal")
    Article.objects.create(
        source=source,
        journal=journal,
        title="Article without DOI must not appear",
        abstract="",
        full_text="Article without DOI must not appear in search results.",
        language="en",
        publication_year=2024,
        url="https://example.org/no-doi",
        doi="",
    )

    results = SearchService._run_index_search(
        query="Article without DOI", expression="", size=10
    )

    assert len(results) == 0
