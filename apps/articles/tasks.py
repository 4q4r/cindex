"""Celery tasks for article eligibility backfill."""

from __future__ import annotations

import structlog
from celery import shared_task
from django.db import OperationalError, close_old_connections

from .models import Article
from .services import ArticleEligibilityService

logger = structlog.get_logger(__name__)

_CHUNK_SIZE = 500


@shared_task(
    autoretry_for=(OperationalError,),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=3,
)
def reapply_eligibility(
    source_keys: list[str] | None = None,
    chunk_size: int = _CHUNK_SIZE,
) -> dict[str, int]:
    """
    Re-run ``ArticleEligibilityService.apply`` over the persisted corpus.

    Re-applies the tiered eligibility classifier to existing articles without
    re-fetching from sources. Used to backfill articles ingested before the
    connectors emitted peer-review / preprint / indexing tier evidence: the
    classifier's source-reputation default (``PEER_REVIEWED_BY_DEFAULT``) and
    keyword scan reclassify them from ``article.source.key`` and the stored
    title / abstract / full_text.

    Optionally scope to a subset of source keys. Per-article failures are
    logged and skipped so one bad record cannot abort the whole backfill;
    a transient DB ``OperationalError`` propagates so celery retries the task.
    """
    qs = Article.objects.select_related("source").all()
    if source_keys:
        qs = qs.filter(source__key__in=source_keys)

    total = 0
    peer_reviewed = 0
    indexed = 0
    preprint = 0
    eligible = 0
    failed = 0
    for article in qs.iterator(chunk_size=chunk_size):
        try:
            ArticleEligibilityService.apply(article)
        except OperationalError:
            # Transient DB issue -- let celery autoretry the whole task.
            raise
        except Exception:  # log + continue per-article
            logger.exception(
                "articles.reapply_eligibility.article_failed",
                article_id=article.pk,
            )
            failed += 1
            continue
        total += 1
        if article.is_peer_reviewed_or_refereed:
            peer_reviewed += 1
        if article.is_indexed_in_reputable_db:
            indexed += 1
        if not article.is_not_preprint_or_author_manuscript:
            preprint += 1
        if article.is_eligible:
            eligible += 1

    logger.info(
        "articles.reapply_eligibility.done",
        total=total,
        peer_reviewed=peer_reviewed,
        indexed=indexed,
        preprint=preprint,
        eligible=eligible,
        failed=failed,
    )
    close_old_connections()
    return {
        "total": total,
        "peer_reviewed": peer_reviewed,
        "indexed": indexed,
        "preprint": preprint,
        "eligible": eligible,
        "failed": failed,
    }
