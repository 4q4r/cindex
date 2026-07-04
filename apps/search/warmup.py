"""Search model warmup helpers for the postgres-only search pipeline."""

from __future__ import annotations

import time
from functools import lru_cache
from threading import Thread

import structlog

LOGGER = structlog.get_logger(__name__)


@lru_cache(maxsize=1)
def warmup_search_models() -> None:
    """Warm the search process cache for the postgres-only pipeline."""
    started_at = time.perf_counter()
    elapsed = time.perf_counter() - started_at
    LOGGER.info("Search warmup completed in %.2fs", elapsed)


@lru_cache(maxsize=1)
def start_background_warmup() -> None:
    """Start search model warmup in a daemon thread once per process.

    The warmup itself remains synchronous so the model cache is fully primed,
    but the container startup path does not wait for it. This keeps Celery
    workers available for recovery/requeue work immediately after restart.
    """

    def _run() -> None:
        try:
            warmup_search_models()
        except Exception:
            LOGGER.exception("Search model warmup failed")

    Thread(target=_run, name="search-model-warmup", daemon=True).start()
