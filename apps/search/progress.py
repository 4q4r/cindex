"""Source health and search wait-time statistics helpers for the search app."""

from __future__ import annotations

from django.core.cache import cache

from apps.articles.models import Source
from apps.ingestion.connectors import CONNECTORS
from apps.search.models import SearchWaitStat

# Source health (circuit-breaker state) changes slowly, and ``get_source_stats``
# is called on every search-job poll (apps/search/views.py). Caching collapses
# a per-poll DB query (~1/s/user) into one query per TTL — a meaningful cut of
# DB load under concurrent polling, which was a contributor to the connection
# exhaustion that caused HTTP 500 on the poll endpoint.
_SOURCE_STATS_CACHE_KEY = "search:source_stats"
_SOURCE_STATS_TTL_SECONDS = 15


def _round_half_up(value: float) -> int:
    """Round a non-negative float to the nearest int, half away from zero.

    Python's built-in ``round`` uses banker's rounding (half to even), so
    ``round(32.5)`` returns ``32``. Search wait times are display values and
    should round half up, which is what users expect.
    """
    return int(value + 0.5)


def _compute_source_stats() -> dict:
    """Query the DB for the shared source stats (uncached)."""
    total = len(CONNECTORS)
    indexed = {
        item.key: item for item in Source.objects.filter(key__in=CONNECTORS.keys())
    }

    failed_names: list[str] = []
    for key in CONNECTORS:
        src = indexed.get(key)
        if src and (not src.active or src.is_circuit_open()):
            failed_names.append(src.name or key.upper())

    live = max(0, total - len(failed_names))
    return {
        "total": total,
        "live": live,
        "failed": failed_names,
    }


def get_source_stats() -> dict:
    """Return the shared source stats, cached for a short TTL.

    Source health changes slowly (circuit-breaker cooldown is minutes), so a
    15s TTL is safe and removes a per-poll DB query. Backed by Redis in prod
    (``django_redis``) and LocMem in tests (``USE_LOCAL_CACHE``).
    """
    return cache.get_or_set(
        _SOURCE_STATS_CACHE_KEY,
        _compute_source_stats,
        _SOURCE_STATS_TTL_SECONDS,
    )


def get_search_wait_stats() -> dict[str, int | None]:
    """Return rolling average completed search durations grouped by scan mode."""
    without_enrichment, _ = SearchWaitStat.objects.get_or_create(
        kind=SearchWaitStat.KIND_WITHOUT_ENRICHMENT,
        defaults={"average_seconds": 0.0, "sample_count": 0},
    )
    with_enrichment, _ = SearchWaitStat.objects.get_or_create(
        kind=SearchWaitStat.KIND_WITH_ENRICHMENT,
        defaults={"average_seconds": 0.0, "sample_count": 0},
    )

    return {
        "without_enrichment_seconds": (
            _round_half_up(without_enrichment.average_seconds)
            if without_enrichment.sample_count > 0
            else None
        ),
        "with_enrichment_seconds": (
            _round_half_up(with_enrichment.average_seconds)
            if with_enrichment.sample_count > 0
            else None
        ),
    }
