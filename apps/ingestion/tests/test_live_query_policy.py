from __future__ import annotations

from apps.ingestion.live_queries import REQUIRED_SOURCES, SOURCE_QUERY_MATRIX


def test_live_query_matrix_has_all_required_sources() -> None:
    """Test live query matrix has all required sources helper."""
    assert set(SOURCE_QUERY_MATRIX.keys()) == set(REQUIRED_SOURCES)


def test_live_query_matrix_has_two_nonempty_queries_per_source() -> None:
    """Test live query matrix has two nonempty queries per source helper."""
    for source_key, queries in SOURCE_QUERY_MATRIX.items():
        assert len(queries) >= 2, f"{source_key}: expected at least 2 queries"
        for idx, query in enumerate(queries):
            assert query and query.strip(), f"{source_key}[{idx}] is empty"


def test_live_query_matrix_duplicates_only_for_allowlist() -> None:
    """Test live query matrix duplicates only for allowlist helper."""
    allow_duplicate_sources = {"mathnet"}
    for source_key, queries in SOURCE_QUERY_MATRIX.items():
        if len(set(queries)) == len(queries):
            continue
        assert source_key in allow_duplicate_sources, (
            f"{source_key}: duplicate queries are not allowed without allowlist entry"
        )
