"""Tests for the short-TTL cache on ``get_source_stats``.

``get_source_stats`` is called on every search-job poll (apps/search/views.py).
Without a cache it issues a ``Source.objects.filter`` query per poll (~1/s/user),
which contributed to the DB-connection exhaustion that caused HTTP 500 on the
poll endpoint. The cache collapses that to one query per TTL.
"""

from __future__ import annotations

import pytest
from django.core.cache import cache

from apps.search.progress import get_source_stats


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    """Ensure each test starts with a cold cache."""
    cache.clear()
    yield
    cache.clear()


def test_get_source_stats_queries_once_then_cache_hit(
    db,
    django_assert_num_queries,
) -> None:
    """First call hits the DB; the second within TTL is served from cache."""
    with django_assert_num_queries(1):
        first = get_source_stats()
    with django_assert_num_queries(0):
        second = get_source_stats()
    assert second == first
    assert {"total", "live", "failed"} <= set(first)


def test_get_source_stats_recomputes_after_cache_clear(
    db,
    django_assert_num_queries,
) -> None:
    """After ``cache.clear()`` the next call hits the DB again."""
    with django_assert_num_queries(1):
        get_source_stats()
    cache.clear()
    with django_assert_num_queries(1):
        get_source_stats()


def test_get_source_stats_key_isolation(db) -> None:
    """An unrelated cache key is not touched by source-stats caching."""
    cache.set("search:unrelated", {"marker": True})
    get_source_stats()
    assert cache.get("search:unrelated") == {"marker": True}
