"""Celery tasks for ingestion, local-import scanning, and DOI enrichment backfill."""

from __future__ import annotations

from pathlib import Path

from celery import shared_task
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from .doi_enrichment import DoiEnrichmentService
from .exa_usage import sync_exa_usage_snapshots
from .local_imports import SCANNED_LOCK_SECONDS, LocalImportService
from .models import IngestionRun
from .services import IngestionService


@shared_task(
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=5,
)
def ingest_search_query(
    query: str,
    source_keys: list[str] | None = None,
) -> dict[str, int]:
    """Ingest search query helper."""
    run = IngestionRun.objects.create(
        query=query,
        source_key=",".join(source_keys or ["all"]),
    )
    try:
        articles = IngestionService.ingest_query(query=query, source_keys=source_keys)
        run.status = "completed"
        run.fetched = len(articles)
        run.eligible = sum(1 for x in articles if x.is_eligible)
        run.finished_at = timezone.now()
        run.save(update_fields=["status", "fetched", "eligible", "finished_at"])
    except Exception as exc:
        run.status = "failed"
        run.error = str(exc)
        run.finished_at = timezone.now()
        run.save(update_fields=["status", "error", "finished_at"])
        raise
    else:
        return {"fetched": run.fetched, "eligible": run.eligible}


@shared_task
def nightly_ingestion() -> None:
    """Nightly ingestion helper."""
    default_queries = [
        "machine learning",
        "biomedical signal processing",
        "scientometrics",
        "applied mathematics",
    ]
    for query in default_queries:
        ingest_search_query.delay(query)


@shared_task
def scan_local_imports() -> dict[str, int]:
    """Scan the configured local import folder for new scholarly files."""
    drop_dir = Path(getattr(settings.APP, "local_import_directory", "local_imports"))
    lock_key = f"local-import-scan:{drop_dir}"
    if not cache.add(lock_key, "1", timeout=SCANNED_LOCK_SECONDS):
        return {"scanned": 0, "imported": 0, "skipped": 0, "failed": 0}
    try:
        return LocalImportService.scan_drop_dir(drop_dir)
    finally:
        cache.delete(lock_key)


@shared_task
def sync_exa_quota_snapshots() -> dict[str, int]:
    """Sync Exa team-management usage snapshots for admin visibility."""
    synced, failed = sync_exa_usage_snapshots()
    return {"synced": synced, "failed": failed}


@shared_task(autoretry_for=(Exception,), retry_backoff=True, max_retries=3)
def doi_enrichment_backfill(batch_size: int = 200) -> dict[str, int]:
    """Backfill missing metadata for existing articles with DOIs."""
    candidates = DoiEnrichmentService._select_doi_candidates()[:batch_size]  # noqa: SLF001
    if not candidates:
        return {"enriched": 0, "candidates": 0}
    enriched = DoiEnrichmentService.enrich_sync(candidates)
    return {"enriched": enriched, "candidates": len(candidates)}
