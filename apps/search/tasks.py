"""Celery tasks and helpers that drive asynchronous search job execution."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import structlog
from celery import shared_task
from django.conf import settings
from django.utils import timezone

from apps.ingestion.services import IngestionService

from .models import SearchJob, SearchWaitStat
from .services import SearchService

logger = structlog.get_logger(__name__)

STAGE_PROGRESS: dict[str, int] = {
    "queued": 5,
    "checking_index": 20,
    "live_scan": 55,
    "searching_index": 85,
    "completed": 100,
    "partial": 100,
    "failed": 100,
}

LIVE_SCAN_PHASE_RATIO: dict[str, float] = {
    "fetching": 0.10,
    "enriching": 0.45,
    "indexing": 0.75,
    "completed": 1.0,
    "failed": 1.0,
    "skipped": 1.0,
}

STAGE_SUBSTAGE: dict[str, tuple[str, str]] = {
    "queued": ("queued", "Запрос принят"),
    "checking_index": ("index_checking", "Проверяем корпус"),
    "live_scan": ("source_collection", "Собираем статьи"),
    "searching_index": ("relevance_refresh", "Ранжируем статьи"),
    "completed": ("done", "Выдача готова"),
    "failed": ("failed", "Поиск остановлен"),
}

# Threshold separating Russian "few" plural form ("источника недоступно")
# from the "many" form ("источников недоступно") in the partial-result message.
FAILED_SOURCES_MANY_THRESHOLD = 5


def _update_job(job: SearchJob, **fields: Any) -> None:  # noqa: ANN401  # dynamic model field update
    """Update the search job with the given fields and persist them."""
    for key, value in fields.items():
        setattr(job, key, value)
    if "stage" in fields:
        job.message = fields.get("message", job.message)
    update_fields = list(fields.keys())
    if "updated_at" not in update_fields:
        update_fields.append("updated_at")
    job.save(update_fields=update_fields)


def _stage_snapshot(stage: str) -> tuple[str, str]:
    """Return the stable substage key and label for a job stage."""
    return STAGE_SUBSTAGE.get(stage, ("", ""))


def _is_fresh_recent_scan(query: str, freshness_days: int, exclude_job_id: str) -> bool:
    """Return whether a recent completed scan for the query is still fresh."""
    recent = (
        SearchJob.objects.filter(
            query=query,
            status__in={"completed", "partial"},
        )
        .exclude(id=exclude_job_id)
        .order_by("-finished_at")
        .first()
    )
    if not recent or not recent.finished_at:
        return False
    return recent.finished_at >= timezone.now() - timedelta(days=freshness_days)


def _determine_rescan(
    job: SearchJob,
    hits_before: int,
    freshness_days: int,
) -> tuple[bool, str]:
    """Determine whether a live rescan is needed and why.

    Returns (rescan_triggered, rescan_reason).
    """
    completed_source_keys = list((job.source_timings or {}).keys())
    finished_live_scan = (
        int(job.source_total or 0) > 0
        and int(job.source_done or 0) >= int(job.source_total or 0)
        and bool(job.source_timings)
    )
    resume_live_scan = bool(completed_source_keys) and not finished_live_scan
    has_fresh_recent = _is_fresh_recent_scan(job.query, freshness_days, str(job.id))
    stale_scan = not has_fresh_recent

    if resume_live_scan:
        return True, "resumed_after_restart"
    if job.force_refresh_requested:
        return True, "forced_by_user"
    if hits_before <= 0:
        return True, "empty_index_hits"
    if stale_scan:
        return True, "stale_query_scan"
    return False, ""


def _build_progress_callback(job: SearchJob) -> Any:  # noqa: ANN401  # callback type is dynamic
    """Build the progress callback for live scan ingestion."""

    def _progress(event: dict) -> None:
        """Progress."""
        total = int(event.get("total", 0))
        done = int(event.get("done", 0))
        failed = [str(x) for x in event.get("failed", [])]
        live = max(0, total - len(failed))
        current_source = str(event.get("current_source", "")).strip()
        status = str(event.get("status", "")).strip()
        substage = str(event.get("substage", "")).strip()
        substage_label = str(event.get("substage_label", "")).strip()
        if status in {"fetching", "enriching", "indexing"} and current_source:
            message = f"{substage_label} · сейчас {current_source}"
        elif status == "skipped":
            message = (
                f"Источник пропущен: {current_source}"
                if current_source
                else "Источник пропущен"
            )
        elif status == "failed":
            message = (
                f"Источник временно недоступен: {current_source}"
                if current_source
                else "Источник временно недоступен"
            )
        elif status == "completed":
            message = f"Проверили {done} из {total} источников"
        else:
            message = "Проверяем корпус"
            if current_source:
                message = f"{message} · сейчас {current_source}"
        _update_job(
            job,
            stage="live_scan",
            substage=substage or _stage_snapshot("live_scan")[0],
            substage_label=substage_label or _stage_snapshot("live_scan")[1],
            message=message,
            source_total=total,
            source_done=done,
            source_live=live,
            source_failed=failed,
        )
        job.refresh_from_db(fields=["stage"])
        if status == "running" and message:
            _update_job(job, message=message)

    return _progress


def _build_profile_callback(job: SearchJob) -> Any:  # noqa: ANN401  # callback type is dynamic
    """Build the profile callback for live scan ingestion."""
    source_timings: dict[str, dict[str, Any]] = dict(job.source_timings or {})

    def _profile(event: dict) -> None:
        """Profile."""
        source_key = str(event.get("source_key", "")).strip()
        if not source_key:
            return
        source_timings[source_key] = {
            "status": str(event.get("status", "")),
            "fetch_seconds": float(event.get("fetch_seconds", 0.0)),
            "enrich_seconds": float(event.get("enrich_seconds", 0.0)),
            "save_seconds": float(event.get("save_seconds", 0.0)),
            "total_seconds": float(event.get("total_seconds", 0.0)),
            "articles_count": int(event.get("articles_count", 0)),
        }
        _update_job(job, source_timings=source_timings)

    return _profile


def _run_supplemental_enrichment(job: SearchJob, stale_keys: list[str]) -> None:
    """Run supplemental enrichment for stale or failed sources."""
    _update_job(
        job,
        stage="live_scan",
        substage="enrichment_supplement",
        substage_label="Дополняем недостающие источники",
        message=f"Проверяем {len(stale_keys)} недоступных источников",
    )

    def _supplement_progress(event: dict) -> None:
        """Report progress for supplemental enrichment."""
        total = int(event.get("total", 0))
        done = int(event.get("done", 0))
        failed_list = [str(x) for x in event.get("failed", [])]
        current_source = str(event.get("current_source", "")).strip()
        status = str(event.get("status", "")).strip()
        if current_source and status in {
            "fetching",
            "enriching",
            "indexing",
        }:
            message = f"Дополняем: {current_source}"
        elif status == "failed" and current_source:
            message = f"Источник недоступен: {current_source}"
        elif status == "skipped" and current_source:
            message = f"Источник пропущен: {current_source}"
        elif status == "completed":
            message = f"Проверили {done} из {total} источников"
        else:
            message = "Дополняем недостающие источники"
        _update_job(
            job,
            message=message,
            source_total=total,
            source_done=done,
            source_live=max(0, total - len(failed_list)),
            source_failed=failed_list,
        )

    def _supplement_profile(event: dict) -> None:
        """Record profile data for supplemental enrichment."""
        source_key = str(event.get("source_key", "")).strip()
        if not source_key:
            return
        timings = dict(job.source_timings or {})
        timings[source_key] = {
            "status": str(event.get("status", "")),
            "fetch_seconds": float(event.get("fetch_seconds", 0.0)),
            "enrich_seconds": float(event.get("enrich_seconds", 0.0)),
            "save_seconds": float(event.get("save_seconds", 0.0)),
            "total_seconds": float(event.get("total_seconds", 0.0)),
            "articles_count": int(event.get("articles_count", 0)),
        }
        _update_job(job, source_timings=timings)

    try:
        IngestionService.ingest_query(
            query=job.query,
            source_keys=stale_keys,
            per_source_limit=5,
            progress_callback=_supplement_progress,
            profile_callback=_supplement_profile,
        )
    except (ValueError, RuntimeError, ConnectionError):
        logger.warning(
            "Supplemental enrichment failed for job %s",
            job.id,
            exc_info=True,
        )


def _record_source_health(job: SearchJob) -> None:
    """Run supplemental enrichment or record source health when no rescan needed."""
    stale_keys = IngestionService.get_stale_or_failed_source_keys()
    if stale_keys:
        _run_supplemental_enrichment(job, stale_keys)
        return
    source_health = IngestionService.get_source_health_map()
    total = len(source_health)
    failed_names = [
        IngestionService._upsert_source(k).name or k.upper()  # noqa: SLF001  # internal API
        for k, health_status in source_health.items()
        if health_status != "healthy"
    ]
    _update_job(
        job,
        source_total=total,
        source_done=total,
        source_live=max(0, total - len(failed_names)),
        source_failed=failed_names,
    )


@shared_task
def run_search_job(job_id: str) -> None:
    """Run search job."""
    job = SearchJob.objects.get(id=job_id)
    if job.status in {"completed", "partial", "failed"} or job.finished_at is not None:
        return
    try:
        _update_job(
            job,
            status="running",
            stage="checking_index",
            substage=_stage_snapshot("checking_index")[0],
            substage_label=_stage_snapshot("checking_index")[1],
            message="Проверяем, есть ли уже подходящие статьи в корпусе",
        )
        hits_before = SearchService.index_hit_count(job.query, job.expression)
        freshness_days = max(
            1,
            int(getattr(settings.APP, "search_query_freshness_days", 14)),
        )

        rescan_triggered, rescan_reason = _determine_rescan(
            job,
            hits_before,
            freshness_days,
        )

        completed_source_keys = list((job.source_timings or {}).keys())
        finished_live_scan = (
            int(job.source_total or 0) > 0
            and int(job.source_done or 0) >= int(job.source_total or 0)
            and bool(job.source_timings)
        )

        _update_job(
            job,
            index_hits_before=hits_before,
            freshness_days_used=freshness_days,
            rescan_triggered=rescan_triggered,
            rescan_reason=rescan_reason,
            source_total=job.source_total or 0,
            source_done=job.source_done or 0,
            source_live=job.source_live or 0,
            source_failed=job.source_failed or [],
            source_timings=job.source_timings or {},
            substage=_stage_snapshot("checking_index")[0],
            substage_label=_stage_snapshot("checking_index")[1],
        )

        if rescan_triggered:
            IngestionService.ingest_query(
                query=job.query,
                progress_callback=_build_progress_callback(job),
                profile_callback=_build_profile_callback(job),
                initial_done=int(job.source_done or 0),
                initial_failed=list(job.source_failed or []),
                resume_completed_source_keys=completed_source_keys,
            )
        elif finished_live_scan:
            _update_job(
                job,
                stage="searching_index",
                substage=_stage_snapshot("searching_index")[0],
                substage_label=_stage_snapshot("searching_index")[1],
                message="Ранжируем найденные статьи и собираем выдачу",
            )
        elif not rescan_triggered:
            _record_source_health(job)

        if job.stage != "searching_index":
            _update_job(
                job,
                stage="searching_index",
                substage=_stage_snapshot("searching_index")[0],
                substage_label=_stage_snapshot("searching_index")[1],
                message="Ранжируем найденные статьи и собираем выдачу",
            )

        results = SearchService.run(
            query=job.query,
            expression=job.expression,
            force_refresh=False,
            fallback_to_recent=False,
        )
        hits_after = SearchService.index_hit_count(job.query, job.expression)

        final_status = "completed"
        final_message = "Готово"
        current_failed: list[str] = list(job.source_failed or [])
        if current_failed:
            final_status = "partial"
            if len(current_failed) == 1:
                unavailable_text = "источник недоступен"
            elif len(current_failed) < FAILED_SOURCES_MANY_THRESHOLD:
                unavailable_text = "источника недоступно"
            else:
                unavailable_text = "источников недоступно"
            final_message = f"Готово, но {len(current_failed)} {unavailable_text}"

        completion_time = timezone.now()
        _update_job(
            job,
            status=final_status,
            stage="completed",
            substage=_stage_snapshot("completed")[0],
            substage_label=_stage_snapshot("completed")[1],
            message=final_message,
            index_hits_after=hits_after,
            results=results,
            finished_at=completion_time,
        )
        SearchWaitStat.record_completion(
            job.rescan_triggered,
            (completion_time - job.created_at).total_seconds(),
            exclude_job_id=str(job.id),
        )
    except Exception as exc:
        _update_job(
            job,
            status="failed",
            stage="failed",
            substage=_stage_snapshot("failed")[0],
            substage_label=_stage_snapshot("failed")[1],
            message="Ошибка выполнения поиска",
            error=str(exc)[:4000],
            finished_at=timezone.now(),
        )
        raise
