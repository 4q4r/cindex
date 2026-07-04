from __future__ import annotations

import os

import pytest

from apps.ingestion.connectors import CONNECTORS
from apps.ingestion.live_queries import REQUIRED_SOURCES, SOURCE_QUERY_MATRIX


@pytest.mark.live_smoke
def test_live_smoke_connectors() -> None:
    """Test live smoke connectors helper."""
    if os.getenv("RUN_LIVE_SMOKE") != "1":
        pytest.skip("Set RUN_LIVE_SMOKE=1 to run live connector smoke tests")

    per_source_limit = 2
    failures: list[str] = []
    counts: dict[str, int] = {}
    for source_key in REQUIRED_SOURCES:
        connector_cls = CONNECTORS[source_key]
        connector = connector_cls()
        query = SOURCE_QUERY_MATRIX[source_key][0]
        try:
            items = connector.fetch(query, limit=per_source_limit)
            counts[source_key] = len(items)
        except (
            ValueError,
            RuntimeError,
            ConnectionError,
        ) as exc:  # pragma: no cover - network dependent
            failures.append(f"{source_key}: {exc}")

    assert not failures, f"Live connector failures: {failures}"
