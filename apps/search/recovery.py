"""Recovery helpers that requeue interrupted search jobs after a worker restart."""

from __future__ import annotations

from typing import Final

import structlog
from django.core.cache import cache

from apps.search.models import SearchJob

LOGGER = structlog.get_logger(__name__)

RESUME_LOCK_TIMEOUT_SECONDS: Final[int] = 300


def resume_running_search_jobs() -> list[str]:
    """
    Requeue interrupted search jobs after a worker restart.

    Returns:
        The list of job IDs that were requeued for resumption.

    """
    from apps.search.tasks import (  # noqa: PLC0415  # lazy import to avoid circular dependency
        run_search_job,
    )

    resumed_job_ids: list[str] = []
    running_jobs = SearchJob.objects.filter(status="running", finished_at__isnull=True)
    for job in running_jobs.order_by("created_at"):
        lock_key = f"search-job-resume:{job.id}"
        if not cache.add(lock_key, "1", timeout=RESUME_LOCK_TIMEOUT_SECONDS):
            continue
        try:
            run_search_job.delay(str(job.id))
            resumed_job_ids.append(str(job.id))
            LOGGER.info("Requeued interrupted search job %s", job.id)
        except Exception:
            cache.delete(lock_key)
            raise
    return resumed_job_ids
