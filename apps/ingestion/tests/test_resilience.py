from datetime import timedelta
from typing import Never

from django.utils import timezone

from apps.articles.models import Source
from apps.ingestion.connectors import RawArticle
from apps.ingestion.services import IngestionService


class AlwaysFailConnector:
    """Always Fail source connector."""

    def fetch(self, query: str, limit: int = 5) -> Never:
        msg = "temporary source failure"
        raise RuntimeError(msg)


class CountingConnector:
    """Counting source connector."""

    calls = 0

    def fetch(self, query: str, limit: int = 5):
        type(self).calls += 1
        return []


class SuccessConnector:
    """Success source connector."""

    def fetch(self, query: str, limit: int = 5):
        return [
            RawArticle(
                source_key="res_success",
                title="Reliable indexed article",
                url="https://example.org/res1",
                abstract="peer reviewed indexed in scopus",
                full_text="journal article DOI 10.8888/success.1",
                language="en",
                year=2024,
                doi="10.8888/success.1",
                journal="Reliability Journal",
                peer_review_evidence="peer reviewed",
                indexing_evidence="scopus web of science",
                preprint_evidence="journal article",
            )
        ]


def test_circuit_breaker_opens_after_threshold(monkeypatch, db) -> None:
    """Test circuit breaker opens after threshold helper."""
    monkeypatch.setattr(
        "apps.ingestion.services.CONNECTORS", {"res_fail": AlwaysFailConnector}
    )

    IngestionService.ingest_query("topic", source_keys=["res_fail"])
    IngestionService.ingest_query("topic", source_keys=["res_fail"])
    IngestionService.ingest_query("topic", source_keys=["res_fail"])

    source = Source.objects.get(key="res_fail")
    assert source.consecutive_failures >= 3
    assert source.circuit_open_until is not None
    assert source.is_circuit_open() is True


def test_circuit_open_source_is_skipped(monkeypatch, db) -> None:
    """Test circuit open source is skipped helper."""
    CountingConnector.calls = 0
    source = Source.objects.create(
        key="res_skip",
        name="RES_SKIP",
        base_url="https://res_skip.org",
        circuit_open_until=timezone.now() + timedelta(minutes=5),
    )
    monkeypatch.setattr(
        "apps.ingestion.services.CONNECTORS", {"res_skip": CountingConnector}
    )

    IngestionService.ingest_query("topic", source_keys=["res_skip"])

    source.refresh_from_db()
    assert CountingConnector.calls == 0
    assert source.total_runs == 0


def test_success_resets_consecutive_failures(monkeypatch, db) -> None:
    """Test success resets consecutive failures helper."""
    source = Source.objects.create(
        key="res_success",
        name="RES_SUCCESS",
        base_url="https://res_success.org",
        consecutive_failures=2,
        total_runs=2,
        total_failures=2,
    )
    monkeypatch.setattr(
        "apps.ingestion.services.CONNECTORS", {"res_success": SuccessConnector}
    )

    IngestionService.ingest_query("topic", source_keys=["res_success"])

    source.refresh_from_db()
    assert source.consecutive_failures == 0
    assert source.total_successes == 1
    assert source.last_success_at is not None
