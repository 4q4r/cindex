# Archive

This directory holds historical design and progress documents that describe an
**abandoned** pre-migration architecture. They are kept for provenance only and
do **not** reflect the current system.

## Why these were archived

The current `cindex` implementation is a **PostgreSQL-only** scholarly citation
search engine (see the repository root `README.md`). The documents below were
written against an earlier stack that has since been removed:

- **Vector store:** Chroma / Qdrant multivector — removed.
- **Search index:** Elasticsearch — removed; search now runs entirely against
  PostgreSQL via `apps/search/services.py`.
- **Frontend:** Next.js — replaced by the current React + Vite + Tailwind
  application under `frontend/`.
- **Sources:** J-STAGE and Unpaywall connectors, plus the standalone SciBot
  pipeline — removed from the connector registry
  (`apps/ingestion/connectors/registry.py`).
- **HTTP transport:** `httpx` — replaced by `cloudscraper` + `aiohttp`.

## Archived files

| File                         | Historical content                                                                                                    |
| ---------------------------- | --------------------------------------------------------------------------------------------------------------------- |
| `progress.md`                | Day-by-day dev log from the Chroma/Elasticsearch/Next.js era.                                                         |
| `findings.md`                | Design findings from the same era.                                                                                    |
| `task_plan.md`               | Original task card referencing Qdrant + Elasticsearch.                                                                |
| `SOURCE_ENDPOINTS_REPORT.md` | Source endpoint notes referencing the removed single-file `apps/ingestion/connectors.py` and J-STAGE / SciOpen hooks. |

## Live documents that are still current

The following root-level documents are **not** archived and remain authoritative:

- `LIVE_QUERY_POLICY.md` — governs `apps/ingestion/live_queries.py` and the
  `SOURCE_QUERY_MATRIX` stability gate used by strict live CI checks.
- `README.md` — current architecture and operational guide.

Do not use the archived files to understand the running system. Read the source
under `apps/` and `frontend/` instead.
