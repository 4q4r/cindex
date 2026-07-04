from __future__ import annotations

import os

import pytest

from apps.ingestion.connectors import CONNECTORS
from apps.ingestion.live_queries import REQUIRED_SOURCES, SOURCE_QUERY_MATRIX


@pytest.mark.live_quality
def test_live_quality_nonempty_on_multiple_queries() -> None:
    """Test live quality nonempty on multiple queries helper."""
    if os.getenv("RUN_LIVE_QUALITY") != "1":
        pytest.skip("Set RUN_LIVE_QUALITY=1 to run live quality gate")

    per_source_limit = 3
    failures: list[str] = []
    for source_key in REQUIRED_SOURCES:
        connector = CONNECTORS[source_key]()
        queries = SOURCE_QUERY_MATRIX[source_key]
        non_empty = 0
        for query in queries:
            try:
                items = connector.fetch(query, limit=per_source_limit)
            except (
                ValueError,
                RuntimeError,
                ConnectionError,
            ) as exc:  # pragma: no cover - network dependent
                failures.append(f"{source_key}:{query}: {exc}")
                continue
            if items:
                non_empty += 1
        if non_empty < 2:
            failures.append(f"{source_key}: non-empty={non_empty}/{len(queries)}")

    assert not failures, f"Quality gate failures: {failures}"
