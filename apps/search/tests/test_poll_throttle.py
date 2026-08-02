"""Tests that the search-job poll endpoint is exempt from throttling.

The frontend polls ``GET /api/v1/search/jobs/{id}`` ~1/s while a job runs.
The default ``anon: 60/min`` throttle equals that cadence and produced
intermittent 429s mid-poll. ``SearchJobDetailView`` now sets
``throttle_classes = ()`` so a status read is never throttled; job creation
(``SearchJobCreateView``) keeps the throttle for abuse protection.
"""

from __future__ import annotations

from django.core.cache import cache
from rest_framework.test import APIClient

from apps.search.models import SearchJob


def _make_completed_job() -> SearchJob:
    return SearchJob.objects.create(
        query="deep learning",
        expression="",
        status="completed",
        stage="completed",
        substage="done",
        substage_label="Выдача готова",
        message="Готово",
        results=[],
    )


def test_poll_endpoint_not_throttled_on_burst(db) -> None:
    """A burst well over ``anon: 60/min`` must not produce a 429 on polling."""
    cache.clear()
    job = _make_completed_job()
    client = APIClient()
    # 70 GETs in the same minute — exceeds the anon rate, must all be 200.
    for _ in range(70):
        response = client.get(f"/api/v1/search/jobs/{job.id}")
        assert response.status_code == 200


def test_poll_view_has_no_throttle_classes() -> None:
    """Static guard: the poll view must declare an empty throttle list."""
    from apps.search.views import SearchJobDetailView

    assert tuple(SearchJobDetailView.throttle_classes) == ()
