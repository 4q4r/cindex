from __future__ import annotations

from apps.search.models import SearchWaitStat
from apps.search.progress import get_search_wait_stats


def test_get_search_wait_stats_empty(db):
    """With no completions, stats must return None for both averages."""
    stats = get_search_wait_stats()

    assert stats["without_enrichment_seconds"] is None
    assert stats["with_enrichment_seconds"] is None


def test_record_completion_updates_running_average(db):
    """Each completion averages with the previous value: new = (old + dur) / 2."""
    SearchWaitStat.record_completion(False, 30.0)
    SearchWaitStat.record_completion(False, 50.0)

    stat = SearchWaitStat.objects.get(kind=SearchWaitStat.KIND_WITHOUT_ENRICHMENT)
    # (0 + 30) / 2 = 15, then (15 + 50) / 2 = 32.5
    assert stat.average_seconds == 32.5
    assert stat.sample_count == 2

    stats = get_search_wait_stats()
    assert stats["without_enrichment_seconds"] == 33


def test_record_completion_separates_kinds(db):
    """With and without enrichment must be tracked separately."""
    SearchWaitStat.record_completion(False, 20.0)
    SearchWaitStat.record_completion(True, 100.0)
    SearchWaitStat.record_completion(True, 140.0)

    stats = get_search_wait_stats()
    assert stats["without_enrichment_seconds"] == 10
    # (0 + 100) / 2 = 50, then (50 + 140) / 2 = 95
    assert stats["with_enrichment_seconds"] == 95


def test_record_completion_ignores_negative_duration(db):
    """Negative durations must be ignored."""
    SearchWaitStat.record_completion(False, 10.0)
    SearchWaitStat.record_completion(False, -5.0)

    stat = SearchWaitStat.objects.get(kind=SearchWaitStat.KIND_WITHOUT_ENRICHMENT)
    assert stat.average_seconds == 5.0
    assert stat.sample_count == 1