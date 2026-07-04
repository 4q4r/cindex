from __future__ import annotations

from typing import Never
from uuid import uuid4

from apps.search import tasks as search_tasks
from apps.search.models import SearchJob


def test_run_search_job_resumes_interrupted_live_scan(monkeypatch, db) -> None:
    """A live-scan job should continue from completed source checkpoints."""
    job = SearchJob.objects.create(
        id=uuid4(),
        query="AI in neuroscience",
        expression="",
        status="running",
        stage="live_scan",
        substage="source_collection",
        substage_label="Собираем статьи",
        message="Собираем статьи · сейчас jstage",
        source_total=22,
        source_done=2,
        source_live=20,
        source_failed=[],
        source_timings={
            "jstage": {"status": "completed"},
            "cinii": {"status": "completed"},
        },
        index_hits_before=136,
    )
    captured: dict[str, object] = {}

    def fake_ingest_query(
        *,
        query: str,
        source_keys=None,
        per_source_limit: int = 5,
        progress_callback=None,
        profile_callback=None,
        initial_done: int = 0,
        initial_failed=None,
        resume_completed_source_keys=None,
    ):
        captured["query"] = query
        captured["source_keys"] = list(source_keys or [])
        captured["per_source_limit"] = per_source_limit
        captured["initial_done"] = initial_done
        captured["initial_failed"] = list(initial_failed or [])
        captured["resume_completed_source_keys"] = list(
            resume_completed_source_keys or []
        )
        return []

    monkeypatch.setattr(
        search_tasks.SearchService,
        "index_hit_count",
        lambda *_args, **_kwargs: 136,
    )
    monkeypatch.setattr(search_tasks.SearchService, "run", lambda **_kwargs: [])
    monkeypatch.setattr(
        search_tasks.IngestionService, "ingest_query", fake_ingest_query
    )

    search_tasks.run_search_job(str(job.id))

    assert captured["query"] == "AI in neuroscience"
    assert captured["initial_done"] == 2
    assert captured["initial_failed"] == []
    assert captured["resume_completed_source_keys"] == ["jstage", "cinii"]


def test_run_search_job_skips_ingestion_after_completed_live_scan(
    monkeypatch, db
) -> None:
    """A job interrupted after ingestion should resume directly at ranking."""
    job = SearchJob.objects.create(
        id=uuid4(),
        query="AI in neuroscience",
        expression="",
        status="running",
        stage="searching_index",
        substage="relevance_refresh",
        substage_label="Обновляем релевантность",
        message="Ранжируем найденные статьи и собираем выдачу",
        source_total=22,
        source_done=22,
        source_live=22,
        source_failed=[],
        source_timings={
            f"source-{index}": {"status": "completed"} for index in range(22)
        },
        index_hits_before=136,
    )

    def fake_ingest_query(**_kwargs) -> Never:
        msg = "ingestion should not run after a completed live scan"
        raise AssertionError(msg)

    monkeypatch.setattr(
        search_tasks,
        "_is_fresh_recent_scan",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        search_tasks.SearchService,
        "index_hit_count",
        lambda *_args, **_kwargs: 136,
    )
    monkeypatch.setattr(search_tasks.SearchService, "run", lambda **_kwargs: [])
    monkeypatch.setattr(
        search_tasks.IngestionService, "ingest_query", fake_ingest_query
    )

    search_tasks.run_search_job(str(job.id))

    job.refresh_from_db()
    assert job.status == "completed"
    assert job.stage == "completed"
