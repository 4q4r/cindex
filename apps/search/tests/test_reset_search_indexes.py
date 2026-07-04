from __future__ import annotations

from django.core.management import call_command


def test_reset_search_indexes_reports_postgres_only_mode(capsys, db) -> None:
    """Reset search indexes should report that no external indexes exist."""

    call_command("reset_search_indexes", verbosity=0)

    captured = capsys.readouterr()
    assert "postgres-only search has no derived state" in captured.out
