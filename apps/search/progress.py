"""Source health and search wait-time statistics helpers for the search app."""

from __future__ import annotations

from apps.articles.models import Source
from apps.ingestion.connectors import CONNECTORS
from apps.search.models import SearchWaitStat


def _round_half_up(value: float) -> int:
    """Round a non-negative float to the nearest int, half away from zero.

    Python's built-in ``round`` uses banker's rounding (half to even), so
    ``round(32.5)`` returns ``32``. Search wait times are display values and
    should round half up, which is what users expect.
    """
    return int(value + 0.5)


def get_source_stats() -> dict:
    """Return the shared source stats."""
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
