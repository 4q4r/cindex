"""Sync façade driving PERELMAN quote extraction for a batch of search results.

:class:`QuoteExtractionService.enrich` is the single entry point the celery
``run_search_job`` task calls between the index search and the result update.
It is **query-agnostic** (the search query is deliberately not passed to the
LLM — quotes capture the article's own salient passages and are cached
per-article, re-used across every search) and **cache-aware**:

* published articles (``Article.is_not_preprint_or_author_manuscript == True``)
  are processed exactly once — the extracted quotes are persisted to
  ``ArticleQuotes`` (``status="done"``) and the article is frozen to a local
  ``.md`` via :class:`ArticleMarkdownService` (stamping ``local_md_path``);
  every subsequent search reads the quotes from the cache with no LLM call;
* preprints are never cached or frozen (they are volatile) — their quotes are
  extracted fresh on every search and written only into the in-memory result;
* a concurrent-extraction claim (``ArticleQuotes.status="pending"`` created via
  ``get_or_create``) prevents parallel celery jobs from duplicate LLM calls.

The whole method is wrapped so it **never raises**: a single article's failure
yields ``quotes: []`` for that result (logged), and the search job always
completes. When the LLM endpoint is not configured (missing
``CINDEX_LLM_*`` env), one warning is logged and every result keeps ``[]`` —
the frontend then falls back to the real abstract preview (no fake quotes).
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import TYPE_CHECKING

import structlog
from django.db import transaction

from apps.articles.models import Article
from apps.ingestion.connectors.base import BrowserTransport

from .config import LLMConfig, load_config
from .content_fetcher import ArticleContentFetcher
from .llm_client import OpenAICompatibleClient
from .local_store import ArticleMarkdownService
from .models import (
    STATUS_DONE,
    STATUS_PENDING,
    ArticleQuotes,
)
from .perelman import PerelmanExtractor

if TYPE_CHECKING:
    from .perelman import ExtractionResult

logger = structlog.get_logger(__name__)


class QuoteExtractionService:
    """Cache-aware, query-agnostic, never-raises PERELMAN enrichment façade."""

    @classmethod
    def enrich(cls, results: list[dict]) -> None:
        """Fill ``results[i]["quotes"]`` for every result (never raises).

        Cache hits are served from ``ArticleQuotes``; uncached articles are
        extracted via the PERELMAN agent loop (published → frozen + cached,
        preprint → fresh, in-memory only). Any failure is logged and leaves
        the result's quotes as ``[]`` so the search job stays stable.
        """
        if not results:
            return
        try:
            cls._enrich(results)
        except Exception as exc:
            logger.exception("perelman: enrich failed", error=str(exc))

    @classmethod
    def _enrich(cls, results: list[dict]) -> None:
        """Probe config, serve cache hits, then extract uncached articles."""
        cfg = load_config()
        if not cfg.is_configured():
            logger.warning(
                "perelman: LLM not configured, skipping quote extraction "
                "(results fall back to the abstract preview)",
            )
            return

        indexed = cls._indexed_results(results)
        if not indexed:
            return
        ids = [article_id for _, article_id in indexed]
        articles = cls._load_articles(ids)
        cache = cls._load_cache(ids)

        to_extract = cls._claim_uncached(indexed, results, articles, cache)
        if not to_extract:
            return

        batch = cls._run_extraction([item.article for item in to_extract], cfg)
        cls._persist_results(to_extract, batch, results, cfg)

    @staticmethod
    def _indexed_results(results: list[dict]) -> list[tuple[int, int]]:
        """Return ``(result_index, article_id)`` pairs for results with an id."""
        indexed: list[tuple[int, int]] = []
        for i, result in enumerate(results):
            result.setdefault("quotes", [])
            article_id = result.get("id")
            if article_id is None:
                continue
            indexed.append((i, article_id))
        return indexed

    @staticmethod
    def _load_articles(ids: list[int]) -> dict[int, Article]:
        """Bulk-fetch articles by id, keyed by id (with source/journal/authors)."""
        qs = (
            Article.objects.filter(id__in=ids)
            .select_related("source", "journal")
            .prefetch_related("article_authors__author")
        )
        return {article.id: article for article in qs}

    @staticmethod
    def _load_cache(ids: list[int]) -> dict[int, list]:
        """Bulk-read done-cache rows, keyed by article id."""
        rows = ArticleQuotes.objects.filter(article_id__in=ids, status=STATUS_DONE)
        return {row.article_id: (row.quotes or []) for row in rows}

    @staticmethod
    def _claim_uncached(
        indexed: list[tuple[int, int]],
        results: list[dict],
        articles: dict[int, Article],
        cache: dict[int, list],
    ) -> list[_PendingItem]:
        """Serve cache hits; claim published rows; collect items to extract.

        Published uncached rows are claimed via ``get_or_create`` (anti-dup
        across concurrent jobs). A row already ``pending`` is another job's
        in-progress claim → skipped (quotes stay ``[]``). A ``failed`` /
        ``no_text`` row is re-claimed (``pending``) for a retry. Preprints are
        collected for fresh extraction with no row and no persistence.
        """
        to_extract: list[_PendingItem] = []
        for i, article_id in indexed:
            if article_id in cache:
                results[i]["quotes"] = list(cache[article_id])
                continue
            article = articles.get(article_id)
            if article is None:
                continue
            if article.is_not_preprint_or_author_manuscript:
                row = QuoteExtractionService._claim_published(article)
                if row is None:
                    continue  # another job is processing this article
                to_extract.append(_PendingItem(i, article, row, is_published=True))
            else:
                to_extract.append(_PendingItem(i, article, None, is_published=False))
        return to_extract

    @staticmethod
    def _claim_published(article: Article) -> ArticleQuotes | None:
        """Claim a published article's row; return ``None`` if already pending.

        ``get_or_create`` with ``status="pending"`` defaults claims a fresh
        row atomically. An existing ``pending`` row is another job's claim
        (skip → ``None``). An existing ``failed`` / ``no_text`` row is reset
        to ``pending`` for a retry.
        """
        row, created = ArticleQuotes.objects.get_or_create(
            article=article,
            defaults={"status": STATUS_PENDING},
        )
        if created:
            return row
        if row.status == STATUS_PENDING:
            return None
        row.status = STATUS_PENDING
        row.error = ""
        row.save(update_fields=["status", "error", "updated_at"])
        return row

    @staticmethod
    def _run_extraction(
        articles: list[Article],
        cfg: LLMConfig,
    ) -> list[ExtractionResult]:
        """Build the extractor stack and run ``extract_batch`` (asyncio.run)."""
        transport = BrowserTransport()
        fetcher = ArticleContentFetcher(transport, cfg)
        client = OpenAICompatibleClient(cfg)
        extractor = PerelmanExtractor(client, cfg, fetcher)
        return asyncio.run(extractor.extract_batch(articles))

    @staticmethod
    def _persist_results(
        items: list[_PendingItem],
        batch: list[ExtractionResult],
        results: list[dict],
        cfg: LLMConfig,
    ) -> None:
        """Write extracted quotes back to results and persist published rows.

        Published: non-empty → freeze to ``.md`` + mark ``done``; empty →
        ``no_text`` (retryable next search); a persistence exception →
        ``failed`` (retryable, error recorded). Preprint: quotes written
        in-memory only, nothing persisted.
        """
        for item, result in zip(items, batch, strict=True):
            quotes_dicts = [asdict(q) for q in result.quotes]
            results[item.result_index]["quotes"] = quotes_dicts
            if not item.is_published:
                continue
            try:
                QuoteExtractionService._persist_published(item, result, cfg)
            except Exception as exc:
                logger.exception(
                    "perelman: persist failed",
                    article_id=item.article.id,
                    error=str(exc),
                )
                try:
                    item.row.mark_failed(str(exc))
                except Exception:
                    logger.exception("perelman: mark_failed raised")

    @staticmethod
    def _persist_published(
        item: _PendingItem,
        result: ExtractionResult,
        cfg: LLMConfig,
    ) -> None:
        """Freeze + cache a published article's successful extraction."""
        if result.is_empty:
            item.row.mark_no_text()
            return
        formulas_dicts = [asdict(f) for f in result.formulas]
        figures_dicts = [asdict(f) for f in result.figures]
        quotes_dicts = [asdict(q) for q in result.quotes]
        with transaction.atomic():
            ArticleMarkdownService.save(
                item.article,
                quotes_dicts,
                formulas_dicts,
                figures_dicts,
            )
            item.row.mark_done(quotes_dicts, model=cfg.model)


class _PendingItem:
    """One article awaiting extraction + post-extraction persistence.

    ``row`` is the claimed ``ArticleQuotes`` row for published articles
    (``None`` for preprints, which are never persisted). ``result_index`` is
    this item's slot in the caller's ``results`` list, used to write the
    extracted quotes back into that live dict after the batch.
    """

    __slots__ = ("article", "is_published", "result_index", "row")

    def __init__(
        self,
        result_index: int,
        article: Article,
        row: ArticleQuotes | None,
        *,
        is_published: bool,
    ) -> None:
        """Bind the result slot, article, optional claim row, and pub flag."""
        self.result_index = result_index
        self.article = article
        self.row = row
        self.is_published = is_published
