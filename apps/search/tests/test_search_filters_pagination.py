from __future__ import annotations

import uuid
from uuid import UUID

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from apps.articles.models import Article, Journal, Source
from apps.ingestion.services import IngestionService
from apps.search.filters import (
    DEFAULT_FILTERS,
    SORT_METADATA,
    SORT_NEWEST,
    SORT_RELEVANCE,
    SearchFilters,
)
from apps.search.models import SearchJob
from apps.search.services import SearchService
from apps.search.tasks import _determine_rescan, run_search_job

_SOURCE_KEY = "filter-test-src"


def _source() -> Source:
    source, _ = Source.objects.get_or_create(
        key=_SOURCE_KEY,
        defaults={"name": "Filter Test SRC", "base_url": "https://filter.example"},
    )
    return source


def _journal(name: str = "Filter Test Journal") -> Journal:
    journal, _ = Journal.objects.get_or_create(name=name)
    return journal


def _make_article(  # test fixture builder
    *,
    title: str,
    year: int | None,
    doi: str,
    full_text: str = "",
    peer: bool = False,
    indexed: bool = False,
    not_preprint: bool = False,
    doi_card: bool = True,
    with_journal: bool = True,
) -> Article:
    return Article.objects.create(
        source=_source(),
        journal=_journal() if with_journal else None,
        title=title,
        abstract="",
        full_text=full_text or title,
        language="en",
        publication_year=year,
        url=f"https://example.org/{doi}",
        doi=doi,
        is_peer_reviewed_or_refereed=peer,
        is_indexed_in_reputable_db=indexed,
        is_not_preprint_or_author_manuscript=not_preprint,
        has_doi_and_journal_card=doi_card,
    )


# --- SearchFilters dataclass ---


def test_filters_signature_and_default_checks() -> None:
    """Filter signatures distinguish params and ``is_default`` reports overrides."""
    assert DEFAULT_FILTERS.is_default() is True
    assert SearchFilters().signature() == SearchFilters().signature()

    assert (
        SearchFilters(peer_reviewed_only=True).signature()
        != SearchFilters().signature()
    )
    assert SearchFilters(year_from=2010).signature() != SearchFilters().signature()
    assert SearchFilters(sort_by=SORT_NEWEST).signature() != SearchFilters().signature()
    assert SearchFilters(peer_reviewed_only=True).is_default() is False
    assert SearchFilters(sort_by=SORT_NEWEST).is_default() is False

    assert SearchFilters(sort_by="bogus").normalized_sort() == SORT_RELEVANCE
    assert SearchFilters(sort_by=SORT_METADATA).normalized_sort() == SORT_METADATA


# --- SearchService applies filters before top-K ---


def test_search_service_peer_reviewed_filter(db) -> None:
    """peer_reviewed_only must drop non-peer-reviewed matches before top-K."""
    _make_article(
        title="Quantum lattice models overview",
        year=2024,
        doi="10.1234/peer.1",
        peer=True,
    )
    _make_article(
        title="Quantum lattice models preprint",
        year=2024,
        doi="10.1234/peer.2",
        peer=False,
    )

    default_results = SearchService._run_index_search(
        query="quantum lattice",
        expression="",
        size=10,
    )
    assert {r["doi"] for r in default_results} == {
        "10.1234/peer.1",
        "10.1234/peer.2",
    }

    filtered = SearchService._run_index_search(
        query="quantum lattice",
        expression="",
        size=10,
        filters=SearchFilters(peer_reviewed_only=True),
    )
    assert [r["doi"] for r in filtered] == ["10.1234/peer.1"]


def test_search_service_year_range_filter(db) -> None:
    """year_from / year_to must bound publication_year inclusively."""
    _make_article(title="Historical graph methods", year=2005, doi="10.1234/y.1")
    _make_article(title="Graph methods revisited", year=2015, doi="10.1234/y.2")
    _make_article(title="Graph methods today", year=2024, doi="10.1234/y.3")

    from_2010 = SearchService._run_index_search(
        query="graph methods",
        expression="",
        size=10,
        filters=SearchFilters(year_from=2010),
    )
    assert {r["doi"] for r in from_2010} == {"10.1234/y.2", "10.1234/y.3"}

    to_2020 = SearchService._run_index_search(
        query="graph methods",
        expression="",
        size=10,
        filters=SearchFilters(year_to=2020),
    )
    assert {r["doi"] for r in to_2020} == {"10.1234/y.1", "10.1234/y.2"}


def test_search_service_exclude_preprints_filter(db) -> None:
    """exclude_preprints must keep only non-preprint articles."""
    _make_article(
        title="CRISPR editing review",
        year=2024,
        doi="10.1234/pre.1",
        not_preprint=True,
    )
    _make_article(
        title="CRISPR editing draft",
        year=2024,
        doi="10.1234/pre.2",
        not_preprint=False,
    )

    filtered = SearchService._run_index_search(
        query="crispr editing",
        expression="",
        size=10,
        filters=SearchFilters(exclude_preprints=True),
    )
    assert [r["doi"] for r in filtered] == ["10.1234/pre.1"]


def test_search_service_newest_sort(db) -> None:
    """newest sort must order by descending publication_year."""
    _make_article(title="Sorting networks old", year=2010, doi="10.1234/s.1")
    _make_article(title="Sorting networks new", year=2024, doi="10.1234/s.2")
    _make_article(title="Sorting networks mid", year=2018, doi="10.1234/s.3")

    results = SearchService._run_index_search(
        query="sorting networks",
        expression="",
        size=10,
        filters=SearchFilters(sort_by=SORT_NEWEST),
    )
    assert [r["doi"] for r in results] == [
        "10.1234/s.2",
        "10.1234/s.3",
        "10.1234/s.1",
    ]


def test_search_service_metadata_sort_ranks_eligible_first(db) -> None:
    """metadata sort must rank eligibility-flagged articles above unflagged."""
    _make_article(
        title="Metadata completeness study",
        year=2020,
        doi="10.1234/m.1",
        peer=True,
        indexed=True,
        not_preprint=True,
        doi_card=True,
        with_journal=True,
    )
    _make_article(
        title="Metadata completeness note",
        year=2020,
        doi="10.1234/m.2",
        peer=False,
        indexed=False,
        not_preprint=False,
        doi_card=False,
        with_journal=False,
    )

    results = SearchService._run_index_search(
        query="metadata completeness",
        expression="",
        size=10,
        filters=SearchFilters(sort_by=SORT_METADATA),
    )
    assert [r["doi"] for r in results] == ["10.1234/m.1", "10.1234/m.2"]


# --- SearchJob create/detail API ---


def test_create_search_job_persists_filters(monkeypatch, db) -> None:
    """Create endpoint must persist filter/sort params onto the SearchJob."""
    captured: dict[str, str] = {}

    def fake_delay(job_id: str) -> None:
        captured["job_id"] = job_id

    monkeypatch.setattr("apps.search.views.run_search_job.delay", fake_delay)
    client = APIClient()
    response = client.post(
        "/api/v1/search/jobs",
        {
            "query": "cancer immunotherapy",
            "expression": "",
            "peer_reviewed_only": True,
            "indexed_only": True,
            "exclude_preprints": True,
            "year_from": 2015,
            "year_to": 2024,
            "sort_by": "newest",
        },
        format="json",
    )
    assert response.status_code == 202
    payload = response.data
    UUID(str(payload["id"]))
    assert payload["peer_reviewed_only"] is True
    assert payload["indexed_only"] is True
    assert payload["exclude_preprints"] is True
    assert payload["year_from"] == 2015
    assert payload["year_to"] == 2024
    assert payload["sort_by"] == "newest"

    job = SearchJob.objects.get(id=payload["id"])
    assert job.peer_reviewed_only is True
    assert job.indexed_only is True
    assert job.exclude_preprints is True
    assert job.year_from == 2015
    assert job.year_to == 2024
    assert job.sort_by == "newest"


def test_create_search_job_different_filters_not_attached(monkeypatch, db) -> None:
    """Different filter signatures must not attach to an existing running job."""
    SearchJob.objects.create(
        query="cancer immunotherapy",
        expression="",
        status="running",
        stage="live_scan",
        message="Сканирование",
        source_total=5,
        source_done=1,
        source_live=5,
        source_failed=[],
        substage="source_collection",
        substage_label="Собираем статьи",
        results=[],
        peer_reviewed_only=False,
    )

    captured: dict[str, str] = {}

    def fake_delay(job_id: str) -> None:
        captured["job_id"] = job_id

    monkeypatch.setattr("apps.search.views.run_search_job.delay", fake_delay)
    client = APIClient()
    response = client.post(
        "/api/v1/search/jobs",
        {
            "query": "cancer immunotherapy",
            "expression": "",
            "peer_reviewed_only": True,
        },
        format="json",
    )
    assert response.status_code == 202
    assert response.data["attached_to_existing"] is False
    assert response.data["peer_reviewed_only"] is True
    assert response.data["status"] == "queued"
    # A brand-new job was created and enqueued (not attached to the running
    # default-filter job).
    assert captured["job_id"] == str(response.data["id"])
    assert SearchJob.objects.filter(
        peer_reviewed_only=True,
        status="queued",
    ).exists()


def test_search_job_detail_paginates_results(db) -> None:
    """Detail endpoint must slice stored results server-side by page/per_page."""
    _make_article(title="Pagination result one", year=2024, doi="10.1234/p.1")
    _make_article(title="Pagination result two", year=2024, doi="10.1234/p.2")
    _make_article(title="Pagination result three", year=2024, doi="10.1234/p.3")
    _make_article(title="Pagination result four", year=2024, doi="10.1234/p.4")
    _make_article(title="Pagination result five", year=2024, doi="10.1234/p.5")

    stored = SearchService._run_index_search(
        query="pagination result",
        expression="",
        size=10,
    )
    assert len(stored) == 5

    job = SearchJob.objects.create(
        query="pagination result",
        expression="",
        status="completed",
        stage="completed",
        substage="done",
        substage_label="Выдача готова",
        message="Готово",
        results=stored,
    )

    client = APIClient()
    response = client.get(
        f"/api/v1/search/jobs/{job.id}?page=2&per_page=2",
    )
    assert response.status_code == 200
    payload = response.data
    assert payload["count"] == 5
    assert payload["total_results"] == 5
    assert payload["total_pages"] == 3
    assert payload["page"] == 2
    assert payload["per_page"] == 2
    expected_page = [r["doi"] for r in stored[2:4]]
    assert [r["doi"] for r in payload["results"]] == expected_page


def test_search_job_detail_default_page(db) -> None:
    """Detail endpoint defaults to page 1 with per_page 5."""
    _make_article(title="Default page one", year=2024, doi="10.1234/d.1")
    stored = SearchService._run_index_search(
        query="default page",
        expression="",
        size=10,
    )
    job = SearchJob.objects.create(
        query="default page",
        expression="",
        status="completed",
        stage="completed",
        substage="done",
        substage_label="Выдача готова",
        message="Готово",
        results=stored,
    )
    client = APIClient()
    response = client.get(f"/api/v1/search/jobs/{job.id}")
    assert response.status_code == 200
    assert response.data["page"] == 1
    assert response.data["per_page"] == 5
    assert response.data["total_results"] == 1
    assert len(response.data["results"]) == 1


# --- index_hit_count stays unfiltered (rescan semantics) ---


def test_index_hit_count_ignores_filters(db) -> None:
    """index_hit_count must reflect the corpus, not the client's filters.

    A non-peer-reviewed matching article must still count toward
    ``index_hit_count`` (which drives the rescan decision), even though the
    same article is dropped by a ``peer_reviewed_only`` filter in
    ``_run_index_search``. A filtered count of 0 would wrongly trigger a live
    rescan; the unfiltered count of 1 correctly suppresses it.
    """
    _make_article(
        title="Rescan corpus article",
        year=2024,
        doi="10.1234/r.1",
        peer=False,
    )
    assert SearchService.index_hit_count("rescan corpus", "") == 1
    filtered_results = SearchService._run_index_search(
        query="rescan corpus",
        expression="",
        size=10,
        filters=SearchFilters(peer_reviewed_only=True),
    )
    assert filtered_results == []


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])


def _index_job(*, query: str, status: str = "queued", **kwargs) -> SearchJob:
    """Create a search job with sensible defaults for integration tests."""
    defaults = {
        "query": query,
        "expression": "",
        "status": status,
        "stage": status,
        "finished_at": timezone.now() if status in {"completed", "partial"} else None,
    }
    defaults.update(kwargs)
    return SearchJob.objects.create(**defaults)


def test_run_search_job_no_rescan_applies_filters(monkeypatch, db) -> None:
    """run_search_job ranks the index without a rescan and applies persisted filters."""
    _make_article(
        title="Immunotherapy peer reviewed",
        year=2024,
        doi="10.1234/rsj.peer",
        full_text="immunotherapy",
        peer=True,
        not_preprint=True,
    )
    _make_article(
        title="Immunotherapy preprint",
        year=2024,
        doi="10.1234/rsj.pre",
        full_text="immunotherapy",
        peer=False,
        not_preprint=False,
    )
    # A fresh completed scan for the same query suppresses the stale-rescan branch.
    _index_job(query="immunotherapy", status="completed")

    monkeypatch.setattr(
        IngestionService,
        "get_stale_or_failed_source_keys",
        classmethod(lambda cls: []),
    )
    monkeypatch.setattr(
        IngestionService,
        "get_source_health_map",
        classmethod(lambda cls: {}),
    )

    job = _index_job(
        query="immunotherapy",
        status="queued",
        peer_reviewed_only=True,
        sort_by=SORT_NEWEST,
    )
    # Invoke the task body directly (bypassing the celery.local proxy) so
    # coverage.py traces the function and the no-rescan path is exercised.
    run_search_job.run(str(job.id))
    job.refresh_from_db()

    assert job.status == "completed"
    assert job.stage == "completed"
    assert job.rescan_triggered is False
    assert job.index_hits_before == 2
    assert job.index_hits_after == 2
    assert [r["doi"] for r in job.results] == ["10.1234/rsj.peer"]
    assert job.finished_at is not None


def test_create_search_job_attaches_to_pending_reserved(monkeypatch, db) -> None:
    """When the creation lock is held, attach to the pending-reserved job id."""
    reserved = uuid.uuid4()
    monkeypatch.setattr("apps.search.views.cache.add", lambda *a, **k: False)

    def fake_get(key, default=None, **kwargs):
        if isinstance(key, str) and "pending" in key:
            return str(reserved)
        return default

    monkeypatch.setattr("apps.search.views.cache.get", fake_get)
    client = APIClient()
    response = client.post(
        "/api/v1/search/jobs",
        {"query": "locked query"},
        format="json",
    )
    assert response.status_code == 200
    assert response.data["attached_to_existing"] is True
    assert response.data["id"] == str(reserved)
    assert response.data["status"] == "queued"
    assert not SearchJob.objects.filter(id=reserved).exists()


def test_create_search_job_creates_when_lock_held_invalid_pending(
    monkeypatch,
    db,
) -> None:
    """An unparseable pending id falls back to creating a fresh job."""
    monkeypatch.setattr("apps.search.views.cache.add", lambda *a, **k: False)

    def fake_get(key, default=None, **kwargs):
        if isinstance(key, str) and "pending" in key:
            return "not-a-uuid"
        return default

    monkeypatch.setattr("apps.search.views.cache.get", fake_get)
    captured: dict[str, str] = {}

    def fake_delay(job_id: str) -> None:
        captured["job_id"] = job_id

    monkeypatch.setattr("apps.search.views.run_search_job.delay", fake_delay)
    client = APIClient()
    response = client.post(
        "/api/v1/search/jobs",
        {"query": "locked query two"},
        format="json",
    )
    assert response.status_code == 202
    assert response.data["attached_to_existing"] is False
    assert captured["job_id"] == str(response.data["id"])
    assert SearchJob.objects.filter(id=response.data["id"], status="queued").exists()


def test_create_search_job_invalid_sort_returns_400(db) -> None:
    """An unknown sort_by value is rejected by the serializer."""
    client = APIClient()
    response = client.post(
        "/api/v1/search/jobs",
        {"query": "bad sort", "sort_by": "bogus"},
        format="json",
    )
    assert response.status_code == 400
    assert any(e.get("attr") == "sort_by" for e in response.data.get("errors", []))


def test_search_endpoint_per_page_over_cap_returns_400(db) -> None:
    """per_page above the server cap is rejected."""
    client = APIClient()
    response = client.post(
        "/api/v1/search",
        {"query": "cap", "per_page": 999},
        format="json",
    )
    assert response.status_code == 400
    assert any(e.get("attr") == "per_page" for e in response.data.get("errors", []))


def test_reindex_view_requires_admin(db) -> None:
    """Anonymous users cannot trigger a reindex."""
    client = APIClient()
    response = client.post(
        "/api/v1/admin/reindex",
        {"query": "quantum"},
        format="json",
    )
    assert response.status_code in {401, 403}


def test_reindex_view_admin_enqueues(monkeypatch, db) -> None:
    """An admin user enqueues an ingestion task and receives its id."""
    user_model = get_user_model()
    admin = user_model.objects.create_superuser(
        username="admin",
        password="p",  # noqa: S106  # throwaway test-only superuser credential
        email="admin@example.com",
    )

    class _FakeTask:
        id = "task-1"

    captured: dict[str, str] = {}

    def fake_delay(query: str) -> _FakeTask:
        captured["query"] = query
        return _FakeTask()

    monkeypatch.setattr("apps.search.views.ingest_search_query.delay", fake_delay)
    client = APIClient()
    client.force_authenticate(user=admin)
    response = client.post(
        "/api/v1/admin/reindex",
        {"query": "quantum"},
        format="json",
    )
    assert response.status_code == 200
    assert response.data["status"] == "queued"
    assert response.data["task_id"] == "task-1"
    assert captured["query"] == "quantum"


# --- run_search_job rescan / callback / supplemental / failure branches ---


def test_run_search_job_rescan_drives_callbacks_and_partial_status(
    monkeypatch,
    db,
) -> None:
    """A stale-query rescan drives progress/profile callbacks and partial status."""
    _make_article(
        title="Clinical trial ranking methods",
        year=2024,
        doi="10.1234/ct.1",
        full_text="clinical trial ranking",
        peer=True,
        not_preprint=True,
    )
    # No fresh recent completed job for this query -> stale_scan -> rescan triggered.

    def fake_ingest(cls, **kwargs):  # classmethod-style fake
        pc = kwargs.get("progress_callback")
        prc = kwargs.get("profile_callback")
        if pc:
            pc(
                {
                    "total": 3,
                    "done": 1,
                    "failed": [],
                    "current_source": "src1",
                    "status": "fetching",
                    "substage": "fetch",
                    "substage_label": "Загружаем",
                },
            )
            pc(
                {
                    "total": 3,
                    "done": 1,
                    "failed": [],
                    "current_source": "src2",
                    "status": "skipped",
                },
            )
            pc({"total": 3, "done": 1, "failed": [], "status": "skipped"})
            pc(
                {
                    "total": 3,
                    "done": 1,
                    "failed": ["src3"],
                    "current_source": "src3",
                    "status": "failed",
                },
            )
            pc({"total": 3, "done": 1, "failed": ["src3"], "status": "failed"})
            pc({"total": 3, "done": 3, "failed": ["src3"], "status": "completed"})
            pc(
                {
                    "total": 3,
                    "done": 3,
                    "failed": ["src3"],
                    "status": "running",
                    "current_source": "src4",
                },
            )
            pc({"total": 3, "done": 3, "failed": ["src3"], "status": "running"})
        if prc:
            prc({"source_key": ""})
            prc(
                {
                    "source_key": "src1",
                    "status": "completed",
                    "fetch_seconds": 1.0,
                    "enrich_seconds": 0.5,
                    "save_seconds": 0.2,
                    "total_seconds": 1.7,
                    "articles_count": 5,
                },
            )

    monkeypatch.setattr(IngestionService, "ingest_query", classmethod(fake_ingest))

    job = _index_job(query="clinical trial ranking", status="queued")
    run_search_job.run(str(job.id))
    job.refresh_from_db()

    assert job.rescan_triggered is True
    assert job.rescan_reason == "stale_query_scan"
    assert job.source_failed == ["src3"]
    assert job.status == "partial"
    assert "недоступен" in (job.message or "")
    assert job.finished_at is not None


def test_run_search_job_failure_sets_failed_status(monkeypatch, db) -> None:
    """An exception during index lookup must mark the job failed and re-raise."""

    def _raising_hit_count(cls, query, expression):  # classmethod-style fake
        msg = "index lookup boom"
        raise KeyError(msg)

    monkeypatch.setattr(
        SearchService,
        "index_hit_count",
        classmethod(_raising_hit_count),
    )

    job = _index_job(query="exception case", status="queued")
    with pytest.raises(KeyError):
        run_search_job.run(str(job.id))
    job.refresh_from_db()

    assert job.status == "failed"
    assert job.stage == "failed"
    assert "index lookup boom" in (job.error or "")
    assert job.finished_at is not None


def test_run_search_job_supplemental_enrichment_when_stale_sources(
    monkeypatch,
    db,
) -> None:
    """Stale sources without a rescan trigger supplemental enrichment."""
    _make_article(
        title="Supplemental enrichment corpus",
        year=2024,
        doi="10.1234/sup.1",
        full_text="supplemental enrichment",
        peer=True,
        not_preprint=True,
    )
    # A fresh completed scan suppresses the stale-rescan branch so the job takes
    # the _record_source_health path with stale sources.
    _index_job(query="supplemental enrichment", status="completed")

    def fake_supplement(cls, **kwargs):  # classmethod-style fake
        pc = kwargs.get("progress_callback")
        prc = kwargs.get("profile_callback")
        if pc:
            pc(
                {
                    "total": 1,
                    "done": 0,
                    "failed": [],
                    "current_source": "stale-src",
                    "status": "fetching",
                },
            )
            pc(
                {
                    "total": 1,
                    "done": 0,
                    "failed": ["stale-src"],
                    "current_source": "stale-src",
                    "status": "failed",
                },
            )
            pc(
                {
                    "total": 1,
                    "done": 0,
                    "failed": ["stale-src"],
                    "current_source": "stale-src",
                    "status": "skipped",
                },
            )
            pc({"total": 1, "done": 1, "failed": ["stale-src"], "status": "completed"})
            pc({"total": 1, "done": 1, "failed": ["stale-src"], "status": "running"})
        if prc:
            prc({"source_key": ""})
            prc(
                {
                    "source_key": "stale-src",
                    "status": "completed",
                    "fetch_seconds": 0.1,
                    "enrich_seconds": 0.1,
                    "save_seconds": 0.1,
                    "total_seconds": 0.3,
                    "articles_count": 2,
                },
            )
        msg = "supplement connection lost"
        raise ConnectionError(msg)

    monkeypatch.setattr(
        IngestionService,
        "get_stale_or_failed_source_keys",
        classmethod(lambda cls: ["stale-src"]),
    )
    monkeypatch.setattr(IngestionService, "ingest_query", classmethod(fake_supplement))

    job = _index_job(query="supplemental enrichment", status="queued")
    run_search_job.run(str(job.id))
    job.refresh_from_db()

    assert job.rescan_triggered is False
    assert job.source_failed == ["stale-src"]
    assert job.status == "partial"
    assert job.finished_at is not None


def test_run_search_job_skips_already_finished_job(db) -> None:
    """A job that is already finished must not be re-executed."""
    job = _index_job(query="already done query", status="completed")
    run_search_job.run(str(job.id))
    job.refresh_from_db()
    assert job.status == "completed"
    assert job.stage == "completed"


def test_determine_rescan_forced_and_empty_branches(db) -> None:
    """_determine_rescan must honor force_refresh and empty-index branches."""
    forced = SearchJob(query="forced-rescan-q", force_refresh_requested=True)
    assert _determine_rescan(forced, hits_before=10, freshness_days=14) == (
        True,
        "forced_by_user",
    )

    empty = SearchJob(query="empty-rescan-q", force_refresh_requested=False)
    assert _determine_rescan(empty, hits_before=0, freshness_days=14) == (
        True,
        "empty_index_hits",
    )
