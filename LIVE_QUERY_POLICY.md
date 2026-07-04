# Live Query Matrix Policy

Updated: 2026-04-22 (Europe/Moscow)

This policy governs [`apps/ingestion/live_queries.py`](/home/whoami/projects/cindex/apps/ingestion/live_queries.py) used by strict live CI checks.

## Purpose

`SOURCE_QUERY_MATRIX` is a **stability gate**, not a benchmark corpus.  
Its goal is to detect source outages, parser regressions, and endpoint breakages early.

## Rules

1. Each required source must have exactly one matrix entry.
2. Each source must define at least 2 non-empty queries.
3. Queries should be:
   - source-appropriate,
   - language-appropriate for that source,
   - historically stable (avoid rapidly trending/ephemeral terms).
4. Prefer unique queries per source.
5. Duplicate queries are allowed only with explicit justification in this file and in commit message.

## Current Exception

- `mathnet`: currently uses duplicated query (`probability`, `probability`) to reduce transient network/timeouts while preserving strict all-source gate reliability.

## Change Process

When changing any source query pair:

1. Run targeted live check for that source (both queries).
2. Run strict full gate:
   - `RUN_LIVE_QUALITY=1 uv run pytest -q -m live_quality`
3. Update this policy file if adding/removing exceptions.
4. Mention rationale in changelog/PR notes.
