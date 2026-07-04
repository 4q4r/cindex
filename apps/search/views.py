"""DRF views exposing the search, source-stats, and reindex HTTP endpoints."""

from __future__ import annotations

import hashlib
import uuid
from math import ceil
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

from django.core.cache import cache
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.permissions import AllowAny, IsAdminUser
from rest_framework.request import Request  # noqa: TC002  # used only in annotations
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.ingestion.tasks import ingest_search_query

from .filters import SORT_RELEVANCE, SearchFilters
from .models import SearchJob
from .progress import get_search_wait_stats, get_source_stats
from .serializers import (
    SearchJobCreateSerializer,
    SearchJobDetailQuerySerializer,
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


def _filters_from_validated(data: Mapping[str, object]) -> SearchFilters:
    """Build a :class:`SearchFilters` from a serializer's validated data."""
    return SearchFilters(
        peer_reviewed_only=bool(data.get("peer_reviewed_only", False)),
        indexed_only=bool(data.get("indexed_only", False)),
        exclude_preprints=bool(data.get("exclude_preprints", False)),
        year_from=_coerce_optional_int(data.get("year_from")),
        year_to=_coerce_optional_int(data.get("year_to")),
        sort_by=str(data.get("sort_by", SORT_RELEVANCE)),
    )


def _coerce_optional_int(value: object) -> int | None:
    """Coerce a serializer value into an optional int."""
    if value is None or value == "":
        return None
    return int(value)  # type: ignore[arg-type]


def _filters_from_job(job: SearchJob) -> SearchFilters:
    """Build a :class:`SearchFilters` from a persisted search job."""
    return SearchFilters(
        peer_reviewed_only=job.peer_reviewed_only,
        indexed_only=job.indexed_only,
        exclude_preprints=job.exclude_preprints,
        year_from=job.year_from,
        year_to=job.year_to,
        sort_by=job.sort_by,
    )


def _serialize_job(
    job: SearchJob,
    *,
    results: list[dict] | None = None,
    pagination: Mapping[str, int] | None = None,
) -> dict:
    """Serialize a search job into the public API payload shape.

    Args:
        job: The persisted (or in-memory placeholder) search job.
        results: Optional override for the serialized results slice; when
            ``None`` the full ``job.results`` list is serialized.
        pagination: Optional pagination metadata (``page``, ``per_page``,
            ``total_pages``, ``total_results``) merged into the payload.

    """
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
            "peer_reviewed_only": job.peer_reviewed_only,
            "indexed_only": job.indexed_only,
            "exclude_preprints": job.exclude_preprints,
            "year_from": job.year_from,
            "year_to": job.year_to,
            "sort_by": job.sort_by,
            "page": pagination.get("page") if pagination else None,
            "per_page": pagination.get("per_page") if pagination else None,
            "total_pages": pagination.get("total_pages") if pagination else None,
            "total_results": pagination.get("total_results") if pagination else None,
            "created_at": job.created_at,
            "updated_at": job.updated_at,
            "finished_at": job.finished_at,
            "error": job.error,
            "results": results if results is not None else job.results,
        },
    )
    return serializer.data


def _normalize_job_text(value: str) -> str:
    """Normalize job matching text for deduplication and comparisons."""
    return " ".join(value.split()).casefold()


def _search_job_key_material(
    query: str,
    expression: str,
    force_refresh: bool,  # noqa: FBT001  # internal helper
    filters: SearchFilters,
) -> str:
    """Build the normalized search-job key material.

    The filter signature participates in the key so that two jobs sharing a
    query/expression but requesting different filters do not attach to each
    other or share a creation lock.
    """
    return "|".join(
        (
            _normalize_job_text(query),
            _normalize_job_text(expression),
            str(int(force_refresh)),
            filters.signature(),
        ),
    )


def _search_job_lock_key(
    query: str,
    expression: str,
    force_refresh: bool,  # noqa: FBT001  # internal helper
    filters: SearchFilters,
) -> str:
    """Build a stable cache key for search-job creation locking."""
    digest = hashlib.sha256(
        _search_job_key_material(query, expression, force_refresh, filters).encode(
            "utf-8",
        ),
    ).hexdigest()
    return f"search-job-create:{digest}"


def _search_job_pending_key(
    query: str,
    expression: str,
    force_refresh: bool,  # noqa: FBT001  # internal helper
    filters: SearchFilters,
) -> str:
    """Build a cache key that temporarily reserves a job id during creation."""
    digest = hashlib.sha256(
        _search_job_key_material(query, expression, force_refresh, filters).encode(
            "utf-8",
        ),
    ).hexdigest()
    return f"search-job-pending:{digest}"


def _find_active_search_job(
    query: str,
    expression: str,
    force_refresh: bool,  # noqa: FBT001  # internal helper
    filters: SearchFilters,
) -> SearchJob | None:
    """Return the latest matching active search job if one exists.

    A match requires the same normalized query/expression, the same
    ``force_refresh`` flag, and the same filter signature -- different filters
    produce different jobs even for an identical query.
    """
    normalized_query = _normalize_job_text(query)
    normalized_expression = _normalize_job_text(expression)
    target_signature = filters.signature()
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
            and _filters_from_job(job).signature() == target_signature
        ):
            return job
    return None


def _build_pending_search_job(
    query: str,
    expression: str,
    force_refresh: bool,  # noqa: FBT001  # internal helper
    filters: SearchFilters,
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
        peer_reviewed_only=filters.peer_reviewed_only,
        indexed_only=filters.indexed_only,
        exclude_preprints=filters.exclude_preprints,
        year_from=filters.year_from,
        year_to=filters.year_to,
        sort_by=filters.normalized_sort(),
        created_at=now,
        updated_at=now,
        finished_at=None,
    )


def _paginate_results(
    results: list[dict],
    page: int,
    per_page: int,
) -> tuple[list[dict], dict[str, int]]:
    """Slice a stored results list into a single page plus pagination metadata."""
    total_results = len(results)
    total_pages = ceil(total_results / per_page) if per_page else 0
    start = (page - 1) * per_page
    end = start + per_page
    return results[start:end], {
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
        "total_results": total_results,
    }


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
        filters = _filters_from_validated(serializer.validated_data)
        page = int(serializer.validated_data["page"])
        per_page = int(serializer.validated_data["per_page"])

        if force_refresh:
            ingest_search_query.delay(query)

        results = SearchService.run(
            query=query,
            expression=expression,
            force_refresh=False,
            filters=filters,
        )
        page_results, pagination = _paginate_results(results, page, per_page)
        stats = self._source_stats()

        return Response(
            {
                "query": query,
                "count": pagination["total_results"],
                "page": pagination["page"],
                "per_page": pagination["per_page"],
                "total_pages": pagination["total_pages"],
                "source_stats": stats,
                "results": SearchResultSerializer(page_results, many=True).data,
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
        filters = _filters_from_validated(serializer.validated_data)
        pending_key = _search_job_pending_key(query, expression, force_refresh, filters)

        job: SearchJob | None = _find_active_search_job(
            query,
            expression,
            force_refresh,
            filters,
        )
        attached_to_existing = job is not None
        lock_key = _search_job_lock_key(query, expression, force_refresh, filters)

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
                        peer_reviewed_only=filters.peer_reviewed_only,
                        indexed_only=filters.indexed_only,
                        exclude_preprints=filters.exclude_preprints,
                        year_from=filters.year_from,
                        year_to=filters.year_to,
                        sort_by=filters.normalized_sort(),
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
                        filters=filters,
                        job_id=reserved_job_id,
                    )
                    attached_to_existing = True
            if job is None:
                with transaction.atomic():
                    job = SearchJob.objects.create(
                        query=query,
                        expression=expression,
                        force_refresh_requested=force_refresh,
                        peer_reviewed_only=filters.peer_reviewed_only,
                        indexed_only=filters.indexed_only,
                        exclude_preprints=filters.exclude_preprints,
                        year_from=filters.year_from,
                        year_to=filters.year_to,
                        sort_by=filters.normalized_sort(),
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
    """Return the current state of a search job, paginated server-side."""

    permission_classes = (AllowAny,)
    authentication_classes = ()

    def get(self, request: Request, job_id: uuid.UUID) -> Response:
        """Handle the GET request."""
        job = get_object_or_404(SearchJob, id=job_id)
        query_serializer = SearchJobDetailQuerySerializer(data=request.query_params)
        query_serializer.is_valid(raise_exception=True)
        page = int(query_serializer.validated_data["page"])
        per_page = int(query_serializer.validated_data["per_page"])

        page_results, pagination = _paginate_results(job.results, page, per_page)
        serialized_page = SearchResultSerializer(page_results, many=True).data
        payload = _serialize_job(job, results=serialized_page, pagination=pagination)
        payload["source_stats"] = get_source_stats()
        payload["count"] = pagination["total_results"]
        return Response(payload)


class ReindexView(APIView):
    """Trigger a privileged reindex operation for administrators."""

    permission_classes = (IsAdminUser,)

    def post(self, request: Request) -> Response:
        """Handle the POST request."""
        query = request.data.get("query", "scientific research")
        task = ingest_search_query.delay(query)
        return Response({"task_id": task.id, "status": "queued"})
