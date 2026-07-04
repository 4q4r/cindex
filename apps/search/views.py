"""DRF views exposing the search, source-stats, and reindex HTTP endpoints."""

from __future__ import annotations

import hashlib
import uuid

from django.core.cache import cache
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.permissions import AllowAny, IsAdminUser
from rest_framework.request import Request  # noqa: TC002  # used only in annotations
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.ingestion.tasks import ingest_search_query

from .models import SearchJob
from .progress import get_search_wait_stats, get_source_stats
from .serializers import (
    SearchJobCreateSerializer,
    SearchJobSerializer,
    SearchRequestSerializer,
    SearchResultSerializer,
)
from .services import SearchService
from .tasks import LIVE_SCAN_PHASE_RATIO, STAGE_PROGRESS, run_search_job

ACTIVE_JOB_STATUSES = {"queued", "running"}
ACTIVE_JOB_LOCK_TIMEOUT_SECONDS = 30


def _job_progress_percent(job: SearchJob) -> int:
    """Convert job state into a user-facing progress percentage."""
    if job.status in {"completed", "partial", "failed"}:
        return 100
    if job.stage == "live_scan" and job.source_total > 0:
        base = STAGE_PROGRESS["checking_index"]
        span = STAGE_PROGRESS["live_scan"] - STAGE_PROGRESS["checking_index"]
        phase_ratio = LIVE_SCAN_PHASE_RATIO.get(job.substage, 0.0)
        return min(
            80,
            base + int(((job.source_done + phase_ratio) / job.source_total) * span),
        )
    return STAGE_PROGRESS.get(job.stage, 10)


def _serialize_job(job: SearchJob) -> dict:
    """Serialize a search job into the public API payload shape."""
    average_wait_stats = get_search_wait_stats()

    serializer = SearchJobSerializer(
        {
            "id": job.id,
            "query": job.query,
            "expression": job.expression,
            "status": job.status,
            "stage": job.stage,
            "substage": job.substage,
            "substage_label": job.substage_label,
            "message": job.message,
            "progress_percent": _job_progress_percent(job),
            "source_total": job.source_total,
            "source_done": job.source_done,
            "source_live": job.source_live,
            "source_failed": job.source_failed,
            "source_timings": job.source_timings,
            "average_wait_without_enrichment_seconds": average_wait_stats[
                "without_enrichment_seconds"
            ],
            "average_wait_with_enrichment_seconds": average_wait_stats[
                "with_enrichment_seconds"
            ],
            "index_hits_before": job.index_hits_before,
            "index_hits_after": job.index_hits_after,
            "rescan_triggered": job.rescan_triggered,
            "rescan_reason": job.rescan_reason,
            "freshness_days_used": job.freshness_days_used,
            "created_at": job.created_at,
            "updated_at": job.updated_at,
            "finished_at": job.finished_at,
            "error": job.error,
            "results": job.results,
        },
    )
    return serializer.data


def _normalize_job_text(value: str) -> str:
    """Normalize job matching text for deduplication and comparisons."""
    return " ".join(value.split()).casefold()


def _search_job_lock_key(query: str, expression: str, force_refresh: bool) -> str:  # noqa: FBT001  # internal helper
    """Build a stable cache key for search-job creation locking."""
    digest = hashlib.sha256(
        _search_job_key_material(query, expression, force_refresh).encode("utf-8"),
    ).hexdigest()
    return f"search-job-create:{digest}"


def _search_job_pending_key(
    query: str,
    expression: str,
    force_refresh: bool,  # noqa: FBT001  # internal helper
) -> str:
    """Build a cache key that temporarily reserves a job id during creation."""
    digest = hashlib.sha256(
        _search_job_key_material(query, expression, force_refresh).encode("utf-8"),
    ).hexdigest()
    return f"search-job-pending:{digest}"


def _search_job_key_material(
    query: str,
    expression: str,
    force_refresh: bool,  # noqa: FBT001  # internal helper
) -> str:
    """Build the normalized search-job key material."""
    return "|".join(
        (
            _normalize_job_text(query),
            _normalize_job_text(expression),
            str(int(force_refresh)),
        ),
    )


def _find_active_search_job(
    query: str,
    expression: str,
    force_refresh: bool,  # noqa: FBT001  # internal helper
) -> SearchJob | None:
    """Return the latest matching active search job if one exists."""
    normalized_query = _normalize_job_text(query)
    normalized_expression = _normalize_job_text(expression)
    for job in (
        SearchJob.objects.filter(
            force_refresh_requested=force_refresh,
            status__in=ACTIVE_JOB_STATUSES,
        )
        .order_by("-created_at")
        .iterator()
    ):
        if (
            _normalize_job_text(job.query) == normalized_query
            and _normalize_job_text(job.expression) == normalized_expression
        ):
            return job
    return None


def _build_pending_search_job(
    query: str,
    expression: str,
    force_refresh: bool,  # noqa: FBT001  # internal helper
    job_id: uuid.UUID,
) -> SearchJob:
    """Build an in-memory search job placeholder for a creation race."""
    now = timezone.now()
    return SearchJob(
        id=job_id,
        query=query,
        expression=expression,
        force_refresh_requested=force_refresh,
        freshness_days_used=14,
        status="queued",
        stage="queued",
        substage="queued",
        substage_label="Запрос принят",
        message="Задача поставлена в очередь",
        source_total=0,
        source_done=0,
        source_live=0,
        source_failed=[],
        source_timings={},
        index_hits_before=0,
        index_hits_after=0,
        rescan_triggered=False,
        rescan_reason="",
        results=[],
        error="",
        created_at=now,
        updated_at=now,
        finished_at=None,
    )


class SourceStatsView(APIView):
    """Expose aggregated source health and coverage statistics."""

    permission_classes = (AllowAny,)
    authentication_classes = ()

    def get(self, request: Request) -> Response:  # noqa: ARG002  # DRF view method signature
        """Handle the GET request."""
        return Response(get_source_stats())


class SearchView(APIView):
    """Run an immediate search against the current article corpus."""

    permission_classes = (AllowAny,)
    authentication_classes = ()

    @staticmethod
    def _source_stats() -> dict:
        """Source stats."""
        return get_source_stats()

    def post(self, request: Request) -> Response:
        """Handle the POST request."""
        serializer = SearchRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        query = serializer.validated_data["query"]
        expression = serializer.validated_data.get("expression", "")
        force_refresh = serializer.validated_data["force_refresh"]

        if force_refresh:
            ingest_search_query.delay(query)

        results = SearchService.run(
            query=query,
            expression=expression,
            force_refresh=False,
        )
        stats = self._source_stats()

        return Response(
            {
                "query": query,
                "count": len(results),
                "source_stats": stats,
                "results": SearchResultSerializer(results, many=True).data,
            },
        )


class SearchJobCreateView(APIView):
    """Create or attach to an asynchronous search job."""

    permission_classes = (AllowAny,)
    authentication_classes = ()

    def post(self, request: Request) -> Response:
        """Handle the POST request."""
        serializer = SearchJobCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        query = serializer.validated_data["query"]
        expression = serializer.validated_data.get("expression", "")
        force_refresh = serializer.validated_data.get("force_refresh", False)
        pending_key = _search_job_pending_key(query, expression, force_refresh)

        job: SearchJob | None = _find_active_search_job(
            query,
            expression,
            force_refresh,
        )
        attached_to_existing = job is not None
        lock_key = _search_job_lock_key(query, expression, force_refresh)

        if job is None and cache.add(
            lock_key,
            "1",
            timeout=ACTIVE_JOB_LOCK_TIMEOUT_SECONDS,
        ):
            job_id = uuid.uuid4()
            cache.set(
                pending_key,
                str(job_id),
                timeout=ACTIVE_JOB_LOCK_TIMEOUT_SECONDS,
            )
            try:
                with transaction.atomic():
                    job = SearchJob.objects.create(
                        id=job_id,
                        query=query,
                        expression=expression,
                        force_refresh_requested=force_refresh,
                        source_failed=[],
                        source_timings={},
                        results=[],
                        status="queued",
                        stage="queued",
                        substage="queued",
                        substage_label="Запрос принят",
                        message="Задача поставлена в очередь",
                    )
                run_search_job.delay(str(job.id))
            finally:
                cache.delete(lock_key)
                cache.delete(pending_key)
        elif job is None:
            pending_job_id = cache.get(pending_key)
            if pending_job_id is not None:
                try:
                    reserved_job_id = uuid.UUID(str(pending_job_id))
                except ValueError:
                    reserved_job_id = None
                if reserved_job_id is not None:
                    job = _build_pending_search_job(
                        query=query,
                        expression=expression,
                        force_refresh=force_refresh,
                        job_id=reserved_job_id,
                    )
                    attached_to_existing = True
            if job is None:
                with transaction.atomic():
                    job = SearchJob.objects.create(
                        query=query,
                        expression=expression,
                        force_refresh_requested=force_refresh,
                        source_failed=[],
                        source_timings={},
                        results=[],
                        status="queued",
                        stage="queued",
                        substage="queued",
                        substage_label="Запрос принят",
                        message="Задача поставлена в очередь",
                    )
                run_search_job.delay(str(job.id))

        if job is None:
            msg = "Failed to create or locate search job"
            raise RuntimeError(msg)
        payload = _serialize_job(job)
        payload["attached_to_existing"] = attached_to_existing
        payload["source_stats"] = get_source_stats()
        return Response(payload, status=200 if attached_to_existing else 202)


class SearchJobDetailView(APIView):
    """Return the current state of a search job."""

    permission_classes = (AllowAny,)
    authentication_classes = ()

    def get(self, request: Request, job_id: uuid.UUID) -> Response:  # noqa: ARG002  # DRF view method signature
        """Handle the GET request."""
        job = get_object_or_404(SearchJob, id=job_id)
        payload = _serialize_job(job)
        payload["source_stats"] = get_source_stats()
        payload["count"] = len(payload["results"])
        return Response(payload)


class ReindexView(APIView):
    """Trigger a privileged reindex operation for administrators."""

    permission_classes = (IsAdminUser,)

    def post(self, request: Request) -> Response:
        """Handle the POST request."""
        query = request.data.get("query", "scientific research")
        task = ingest_search_query.delay(query)
        return Response({"task_id": task.id, "status": "queued"})
