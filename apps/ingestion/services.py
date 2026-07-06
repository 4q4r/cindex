"""Scholarly source ingestion: fetch, enrich, validate, and index articles."""

from __future__ import annotations

import time
from datetime import timedelta
from typing import TYPE_CHECKING

import aiohttp
import structlog
from django.utils import timezone
from requests.exceptions import RequestException

from apps.articles.models import Article, ArticleAuthor, Author, Journal, Source
from apps.articles.services import ArticleEligibilityService, IdentifierService
from apps.core.translate import translate_query_for_source

from .connectors import CONNECTORS, BaseConnector, ConnectorFetchError, RawArticle
from .doi_enrichment import DoiEnrichmentService

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

logger = structlog.get_logger(__name__)


class IngestionService:
    """Fetch, enrich, validate, and index scholarly source records."""

    MAX_CONSECUTIVE_FAILURES = 3
    CIRCUIT_SECONDS = 15 * 60

    @classmethod
    def get_stale_or_failed_source_keys(cls) -> list[str]:
        """Return source keys that are circuit-open or have never succeeded."""
        stale_keys: list[str] = []
        for source_key in CONNECTORS:
            try:
                source = Source.objects.get(key=source_key)
            except Source.DoesNotExist:
                stale_keys.append(source_key)
                continue
            if source.is_circuit_open():
                stale_keys.append(source_key)
                continue
            if source.last_success_at is None and source.total_runs > 0:
                stale_keys.append(source_key)
        return stale_keys

    @classmethod
    def get_source_health_map(cls) -> dict[str, str]:
        """Return a map of source_key -> health status for all known sources.

        Status values: ``healthy``, ``circuit_open``, ``never_succeeded``,
        ``never_queried``.
        """
        health: dict[str, str] = {}
        for source_key in CONNECTORS:
            try:
                source = Source.objects.get(key=source_key)
            except Source.DoesNotExist:
                health[source_key] = "never_queried"
                continue
            if source.is_circuit_open():
                health[source_key] = "circuit_open"
            elif source.last_success_at is None and source.total_runs > 0:
                health[source_key] = "never_succeeded"
            else:
                health[source_key] = "healthy"
        return health

    @staticmethod
    def _upsert_source(source_key: str) -> Source:
        """Get or create the Source row for the given source key."""
        if source_key == "local_import":
            defaults = {
                "name": "LOCAL IMPORT",
                "base_url": "https://local-import.invalid",
                "active": True,
            }
            return Source.objects.get_or_create(key=source_key, defaults=defaults)[0]
        return Source.objects.get_or_create(
            key=source_key,
            defaults={
                "name": source_key.upper(),
                "base_url": f"https://{source_key}.org",
                "active": True,
            },
        )[0]

    @classmethod
    def _should_skip_source(cls, source: Source) -> bool:
        """Return True if the source circuit breaker is open."""
        return source.is_circuit_open()

    @classmethod
    def _mark_success(cls, source: Source) -> None:
        """Record a successful run and reset the circuit breaker."""
        now = timezone.now()
        source.total_runs += 1
        source.total_successes += 1
        source.consecutive_failures = 0
        source.last_checked_at = now
        source.last_success_at = now
        source.last_error = ""
        source.circuit_open_until = None
        source.save(
            update_fields=[
                "total_runs",
                "total_successes",
                "consecutive_failures",
                "last_checked_at",
                "last_success_at",
                "last_error",
                "circuit_open_until",
            ],
        )

    @classmethod
    def _mark_failure(cls, source: Source, error_message: str) -> None:
        """Record a failure and open the circuit breaker after consecutive failures."""
        now = timezone.now()
        source.total_runs += 1
        source.total_failures += 1
        source.consecutive_failures += 1
        source.last_checked_at = now
        source.last_error = error_message[:2000]
        if source.consecutive_failures >= cls.MAX_CONSECUTIVE_FAILURES:
            source.circuit_open_until = now + timedelta(seconds=cls.CIRCUIT_SECONDS)
        source.save(
            update_fields=[
                "total_runs",
                "total_failures",
                "consecutive_failures",
                "last_checked_at",
                "last_error",
                "circuit_open_until",
            ],
        )

    @classmethod
    def _save_article(cls, raw: RawArticle) -> Article:
        """Persist a RawArticle as an Article with authors and eligibility."""
        source = cls._upsert_source(raw.source_key)
        journal, _ = Journal.objects.get_or_create(name=raw.journal or raw.source_key)
        article, _ = Article.objects.update_or_create(
            doi=raw.doi,
            defaults={
                "source": source,
                "url": raw.url,
                "title": raw.title,
                "abstract": raw.abstract,
                "full_text": raw.full_text,
                "language": raw.language,
                "publication_year": raw.year,
                "journal": journal,
                "volume": raw.volume,
                "issue": raw.issue,
                "pages": raw.pages,
                "peer_review_evidence": raw.peer_review_evidence,
                "indexing_evidence": raw.indexing_evidence,
                "preprint_evidence": raw.preprint_evidence,
            },
        )
        if raw.doi:
            IdentifierService.upsert(article, [("doi", raw.doi)])
        parsed_authors = [name.strip() for name in raw.authors if name.strip()]
        if parsed_authors:
            article.article_authors.all().delete()
            for order, full_name in enumerate(parsed_authors, start=1):
                author, _ = Author.objects.get_or_create(full_name=full_name)
                ArticleAuthor.objects.get_or_create(
                    article=article,
                    author=author,
                    order=order,
                )
        elif not article.article_authors.exists():
            author, _ = Author.objects.get_or_create(full_name="Unknown author")
            ArticleAuthor.objects.get_or_create(article=article, author=author, order=1)
        return ArticleEligibilityService.apply(article)

    @staticmethod
    def _emit_progress(  # noqa: PLR0913
        progress_callback: Callable[[dict], None] | None,
        *,
        total: int,
        done: int,
        failed: list[str],
        current_source: str,
        status: str,
        substage: str,
        substage_label: str,
    ) -> None:
        """Send a normalized progress event if progress reporting is enabled."""
        if progress_callback is None:
            return
        progress_callback(
            {
                "total": total,
                "done": done,
                "failed": list(failed),
                "current_source": current_source,
                "status": status,
                "substage": substage,
                "substage_label": substage_label,
            },
        )

    @classmethod
    def ingest_query(  # noqa: PLR0913
        cls,
        query: str,
        source_keys: Iterable[str] | None = None,
        per_source_limit: int = 5,
        progress_callback: Callable[[dict], None] | None = None,
        profile_callback: Callable[[dict], None] | None = None,
        initial_done: int = 0,
        initial_failed: Iterable[str] | None = None,
        resume_completed_source_keys: Iterable[str] | None = None,
    ) -> list[Article]:
        """Ingest articles for *query* from selected sources.

        Translates the query to each source's primary language before fetching.
        """
        selected = list(source_keys or CONNECTORS.keys())
        resumed_sources = {str(key) for key in resume_completed_source_keys or []}
        saved: list[Article] = []
        failed_sources: list[str] = [str(item) for item in initial_failed or []]
        done = max(0, int(initial_done))

        if progress_callback:
            cls._emit_progress(
                progress_callback,
                total=len(selected),
                done=done,
                failed=failed_sources,
                current_source="",
                status="running",
                substage="resuming" if done > 0 else "queued",
                substage_label="Возобновляем поиск после рестарта"
                if done > 0
                else "Запрос принят",
            )

        for source_key in selected:
            if source_key in resumed_sources:
                continue
            connector_cls = CONNECTORS.get(source_key)
            if not connector_cls:
                done += 1
                continue
            source = cls._upsert_source(source_key)
            if cls._should_skip_source(source):
                failed_sources.append(source.name or source_key.upper())
                done += 1
                if profile_callback:
                    profile_callback(
                        {
                            "source_key": source_key,
                            "status": "skipped",
                            "fetch_seconds": 0.0,
                            "enrich_seconds": 0.0,
                            "save_seconds": 0.0,
                            "total_seconds": 0.0,
                            "articles_count": 0,
                        },
                    )
                cls._emit_progress(
                    progress_callback,
                    total=len(selected),
                    done=done,
                    failed=failed_sources,
                    current_source=source_key,
                    status="skipped",
                    substage="source_skipped",
                    substage_label="Источник пропущен",
                )
                continue
            result = cls._process_single_source(
                source_key=source_key,
                query=query,
                connector=connector_cls(),
                per_source_limit=per_source_limit,
                progress_callback=progress_callback,
                profile_callback=profile_callback,
                total=len(selected),
                done=done,
                failed_sources=failed_sources,
            )
            saved.extend(result["articles"])
            done = result["done"]
            failed_sources = result["failed_sources"]

        # Post-ingestion: backfill missing metadata via DOI
        if saved:
            DoiEnrichmentService.enrich_sync(saved)

        return saved

    @classmethod
    def _process_single_source(  # noqa: PLR0913
        cls,
        *,
        source_key: str,
        query: str,
        connector: BaseConnector,
        per_source_limit: int,
        progress_callback: Callable[[dict], None] | None,
        profile_callback: Callable[[dict], None] | None,
        total: int,
        done: int,
        failed_sources: list[str],
    ) -> dict:
        """Fetch, enrich, and index a single source, returning result state."""
        source = cls._upsert_source(source_key)
        source_started = time.perf_counter()
        try:
            cls._emit_progress(
                progress_callback,
                total=total,
                done=done,
                failed=failed_sources,
                current_source=source_key,
                status="fetching",
                substage="fetching",
                substage_label="Собираем статьи",
            )
            fetch_started = time.perf_counter()
            source_query = translate_query_for_source(query, source_key)
            raws = connector.fetch(source_query, limit=per_source_limit)
            fetch_seconds = time.perf_counter() - fetch_started
            cls._emit_progress(
                progress_callback,
                total=total,
                done=done,
                failed=failed_sources,
                current_source=source_key,
                status="enriching",
                substage="enriching",
                substage_label="Обогащаем карточки",
            )
            enrich_started = time.perf_counter()
            enriched_raws: list[RawArticle] = []
            for raw in raws:
                try:
                    enriched_raws.append(connector.enrich_raw(raw))
                except (ValueError, RuntimeError, ConnectorFetchError):
                    # A single article's enrichment must not abort the whole
                    # source: a sidecar 502/403 or a residual challenge page on
                    # one landing page degrades to the raw payload (already
                    # fetched) instead of discarding every article. A fetch-
                    # level ConnectorFetchError (no articles fetched at all) is
                    # still surfaced by the outer handler below.
                    logger.warning(
                        "%s: enrich_raw failed for %s, keeping raw payload",
                        source_key,
                        raw.url,
                        exc_info=True,
                    )
                    enriched_raws.append(raw)
            enrich_seconds = time.perf_counter() - enrich_started
            cls._mark_success(source)
        except (
            ValueError,
            RuntimeError,
            ConnectionError,
            ConnectorFetchError,
            RequestException,
            aiohttp.ClientError,
        ) as exc:
            total_seconds = time.perf_counter() - source_started
            cls._mark_failure(source, str(exc))
            failed_sources.append(source.name or source_key.upper())
            done += 1
            if profile_callback:
                profile_callback(
                    {
                        "source_key": source_key,
                        "status": "failed",
                        "fetch_seconds": 0.0,
                        "enrich_seconds": 0.0,
                        "save_seconds": 0.0,
                        "total_seconds": total_seconds,
                        "articles_count": 0,
                    },
                )
            cls._emit_progress(
                progress_callback,
                total=total,
                done=done,
                failed=failed_sources,
                current_source=source_key,
                status="failed",
                substage="failed",
                substage_label="Источник не ответил",
            )
            return {"articles": [], "done": done, "failed_sources": failed_sources}

        cls._emit_progress(
            progress_callback,
            total=total,
            done=done,
            failed=failed_sources,
            current_source=source_key,
            status="indexing",
            substage="indexing",
            substage_label="Индексируем статьи",
        )
        save_started = time.perf_counter()
        articles: list[Article] = []
        for raw in enriched_raws:
            if not raw.doi or not raw.doi.startswith("10."):
                logger.warning(
                    "ingestion: dropping article without valid DOI",
                    source_key=source_key,
                    url=raw.url,
                    title=raw.title[:120],
                )
                continue
            article = cls._save_article(raw)
            articles.append(article)
        save_seconds = time.perf_counter() - save_started
        total_seconds = time.perf_counter() - source_started
        done += 1
        if profile_callback:
            profile_callback(
                {
                    "source_key": source_key,
                    "status": "completed",
                    "fetch_seconds": fetch_seconds,
                    "enrich_seconds": enrich_seconds,
                    "save_seconds": save_seconds,
                    "total_seconds": total_seconds,
                    "articles_count": len(articles),
                },
            )
        cls._emit_progress(
            progress_callback,
            total=total,
            done=done,
            failed=failed_sources,
            current_source=source_key,
            status="completed",
            substage="completed",
            substage_label="Источник обработан",
        )
        return {"articles": articles, "done": done, "failed_sources": failed_sources}
