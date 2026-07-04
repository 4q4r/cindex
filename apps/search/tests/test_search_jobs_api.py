from __future__ import annotations

from uuid import UUID

from rest_framework.test import APIClient

from apps.search.models import SearchJob


def test_create_search_job_enqueues_task(monkeypatch, db):
    """Test create search job enqueues task helper."""
    captured: dict[str, str] = {}

    def fake_delay(job_id: str):
        captured["job_id"] = job_id
        return None

    monkeypatch.setattr("apps.search.views.run_search_job.delay", fake_delay)
    client = APIClient()
    response = client.post(
        "/api/v1/search/jobs",
        {
            "query": "machine learning diagnosis",
            "expression": '"machine learning" AND diagnosis',
        },
        format="json",
    )
    assert response.status_code == 202
    payload = response.data
    assert "id" in payload
    UUID(str(payload["id"]))
    assert payload["status"] == "queued"
    assert payload["stage"] == "queued"
    assert payload["substage_label"] == "Запрос принят"
    assert captured["job_id"] == str(payload["id"])


def test_create_search_job_attaches_to_running_job(monkeypatch, db):
    """Test create search job attaches to running job helper."""
    SearchJob.objects.create(
        query="machine learning diagnosis",
        expression='"machine learning" AND diagnosis',
        status="running",
        stage="live_scan",
        message="Сканирование источников: 4/23",
        source_total=23,
        source_done=4,
        source_live=23,
        source_failed=[],
        substage="source_collection",
        substage_label="Собираем статьи",
        results=[],
    )

    def fake_delay(job_id: str):
        raise AssertionError(f"delay should not be called for attached job: {job_id}")

    monkeypatch.setattr("apps.search.views.run_search_job.delay", fake_delay)
    client = APIClient()
    response = client.post(
        "/api/v1/search/jobs",
        {
            "query": "machine learning diagnosis",
            "expression": '"machine learning" AND diagnosis',
        },
        format="json",
    )
    assert response.status_code == 200
    payload = response.data
    assert payload["status"] == "running"
    assert payload["stage"] == "live_scan"
    assert payload["substage_label"] == "Собираем статьи"
    assert payload["attached_to_existing"] is True


def test_get_search_job_returns_payload(db):
    """Test get search job returns payload helper."""
    job = SearchJob.objects.create(
        query="deep learning",
        expression="",
        status="completed",
        stage="completed",
        substage="done",
        substage_label="Выдача готова",
        message="Готово",
        results=[],
    )
    client = APIClient()
    response = client.get(f"/api/v1/search/jobs/{job.id}")
    assert response.status_code == 200
    assert response.data["id"] == str(job.id)
    assert response.data["status"] == "completed"
    assert response.data["substage_label"] == "Выдача готова"
    assert "progress_percent" in response.data
    assert "source_stats" in response.data
    assert "average_wait_without_enrichment_seconds" in response.data
    assert "average_wait_with_enrichment_seconds" in response.data
