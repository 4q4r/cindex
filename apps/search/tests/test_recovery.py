from __future__ import annotations

from uuid import uuid4

from apps.search.models import SearchJob
from apps.search.recovery import resume_running_search_jobs


def test_resume_running_search_jobs_requeues_running_job(monkeypatch, db) -> None:
    """Interrupted jobs should be requeued on worker startup."""
    job = SearchJob.objects.create(
        id=uuid4(),
        query="AI in neuroscience",
        expression="",
        status="running",
        stage="live_scan",
        substage="source_collection",
        substage_label="Собираем статьи",
        source_total=22,
        source_done=9,
        source_failed=[],
        source_timings={"jstage": {"status": "completed"}},
    )
    captured: list[str] = []

    def fake_delay(job_id: str) -> None:
        captured.append(job_id)

    monkeypatch.setattr("apps.search.tasks.run_search_job.delay", fake_delay)

    resumed = resume_running_search_jobs()

    assert resumed == [str(job.id)]
    assert captured == [str(job.id)]
