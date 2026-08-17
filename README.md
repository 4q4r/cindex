# CIndex

[![Go backend](https://github.com/4q4r/cindex/actions/workflows/backend.yml/badge.svg)](https://github.com/4q4r/cindex/actions/workflows/backend.yml)
[![Go](https://img.shields.io/badge/Go-1.26.6-00ADD8.svg)](https://go.dev/)
[![React](https://img.shields.io/badge/React-19-61DAFB.svg)](https://react.dev/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-18.3-336791.svg)](https://www.postgresql.org/)
[![Docker](https://img.shields.io/badge/runtime-distroless-2496ED.svg)](https://github.com/GoogleContainerTools/distroless)

CIndex is a cross-regional scholarly citation search engine. It searches a
PostgreSQL article corpus, refreshes stale results through 24 source
connectors, evaluates publication trust evidence, and returns ranked articles
with copy-ready citations. TLDR summaries and verbatim quotes are included when
cached or when LLM extraction is configured.

The production backend is written in Go. Python remains only in the isolated
browser/PDF/OCR sidecar. The frontend is Russian-language; source code and API
contracts are English.

## Table of contents

- [Architecture](#architecture)
- [Stack](#stack)
- [Project structure](#project-structure)
- [Search and ingestion](#search-and-ingestion)
- [Eligibility](#eligibility)
- [PERELMAN extraction](#perelman-extraction)
- [API](#api)
- [Configuration](#configuration)
- [Docker deployment](#docker-deployment)
- [Local development](#local-development)
- [Quality gate](#quality-gate)
- [Documentation](#documentation)

## Architecture

```mermaid
flowchart LR
    UI["React 19 + Vite\nBun-built SPA"] --> NGINX["nginx :80"]
    NGINX -- "/api/*" --> API["Go API\nnet/http :8000"]
    API --> PG[("PostgreSQL 18.3\ncorpus + River jobs")]
    API --> REDIS[("Redis 8.6\nrate limits + locks + cache")]
    API -- "enqueue River job" --> PG
    PG -- "dequeue" --> WORKER["Go worker"]
    WORKER --> SEARCH["Search + eligibility + PERELMAN"]
    SEARCH --> PG
    WORKER --> CONN["24 Go connectors"]
    CONN -- "HTTP" --> SOURCES["Scholarly sources"]
    CONN -- "fetch / pdf-text" --> BROWSER["Python sidecar :8081"]
    BROWSER --> SOURCES
```

Request flow:

1. The frontend submits `POST /api/v1/search/jobs`.
2. The Go API atomically creates the `SearchJob` and River queue row in
   PostgreSQL.
3. The frontend polls `GET /api/v1/search/jobs/{id}`. Polling is not rate
   limited.
4. The worker checks the local corpus. Missing or stale results trigger the 24
   source connectors; fresh corpus hits skip the network scan.
5. PostgreSQL performs matching and ranking. The worker enriches ranked
   results with cached or newly extracted PERELMAN output and persists a
   terminal `completed`, `partial`, or `failed` state.
6. On startup, the worker idempotently re-enqueues unfinished search rows that
   do not already have an active River job.

## Stack

| Layer | Technology |
| --- | --- |
| API | Go 1.26.6, `net/http`, generated OpenAPI handlers |
| Async jobs | River with PostgreSQL-backed queue state |
| Database/search | PostgreSQL 18.3 with deterministic SQL scoring |
| Cache/control | Redis 8.6.2 for rate limits, cache, and locks |
| Ingestion | 24 Go connectors using direct HTTP or the browser sidecar |
| Browser sidecar | Python 3.13, FastAPI, cloakbrowser, PDF/OCR |
| Frontend | React 19, Vite 7, TypeScript 5.9, Tailwind 4, Bun |
| Runtime | Multi-stage Go build to `distroless/static-debian13:nonroot` |
| Edge | nginx serves `frontend/dist` and proxies `/api/` to `app:8000` |

PostgreSQL is the only durable application store. No external vector database
or search service is required. Redis is not the job broker; River queue state
is stored in PostgreSQL.

## Project structure

```text
backend/
  api/openapi.yaml           API contract
  cmd/server/                public HTTP API
  cmd/worker/                River worker and interrupted-job recovery
  cmd/cli/                   schema and River migrations
  cmd/healthcheck/           distroless-compatible health probe
  internal/config/           environment configuration
  internal/connector/        24 source connectors and transports
  internal/domain/           entities and eligibility rules
  internal/httpapi/          generated contract bindings and handlers
  internal/jobs/             asynchronous search workflow
  internal/repository/       PostgreSQL persistence and search queries
  internal/service/          search, ingestion, enrichment, local store, PERELMAN
  migrations/                embedded SQL migrations
  Dockerfile                 active Go production image
browser_service/             Python browser, PDF text, and OCR sidecar
frontend/                    React/Vite SPA, managed with Bun
nginx/                       static serving and reverse proxy
scripts/compose_up.sh        reproducible full-stack rebuild
docker-compose.yml           production-like local topology
docs/archive/                historical implementation notes
```

The root `apps/`, `config/`, `manage.py`, Python Dockerfile, and legacy Python
CI describe the pre-Go implementation. They remain for migration provenance
but are not started by the current Compose stack.

## Search and ingestion

The search entry point is `backend/internal/service/search.go`; SQL matching is
implemented in `backend/internal/repository/articles.go`.

- PostgreSQL matches title, abstract, and full text with
  `to_tsvector('simple', ...) @@ websearch_to_tsquery('simple', ...)`.
- Weighted scoring prioritizes DOI, title, abstract, full text, and journal
  matches. Zenodo receives a `0.3` source penalty.
- Sort modes are `relevance`, `newest`, and `metadata`.
- The server-side result cap defaults to 30.
- Results are deduplicated by normalized DOI, then by canonical
  title/year/journal.
- Public search results require a normalized DOI beginning with `10.`.
- Article, author, identifier, and eligibility writes are grouped in a
  transaction. Job creation and its River row are also atomic.

The canonical connector registry contains:

`Europe PMC`, `OpenAlex`, `Crossref`, `PubMed`, `arXiv`, `DOAJ`, `PMC`, `CORE`,
`DBLP`, `HAL`, `Zenodo`, `IACR ePrint`, `Exa`, `CiNii`, `SciEngine`,
`CyberLeninka`, `MathNet.Ru`, `SciELO`, `Persée`, `OpenEdition`, `Medknow`,
`DergiPark`, `Hrčak`, and `AJOL`.

Each source has retry and circuit-breaker state. Connector failures are
isolated: a job can complete as `partial` while preserving successful results.
The lawful full-text resolver uses Unpaywall when `UNPAYWALL_EMAIL` is set and
then Europe PMC. PDFs are sent to the sidecar for native extraction with
bounded OCR fallback.

The sidecar exposes these internal endpoints:

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/fetch` | Fetch data in a persistent stealth context |
| `POST` | `/screenshot` | Available sidecar page capture endpoint |
| `POST` | `/pdf-text` | Extract PDF text with bounded OCR fallback |
| `GET` | `/healthz` | Sidecar readiness |

The sidecar is reachable at `http://browser:8081` inside Compose and is not
published to the host.

## Eligibility

Every article carries four evidence-backed criteria:

- `peer_reviewed`
- `indexed`
- `doi_and_journal_card`
- `not_preprint`

Each criterion has a persisted confidence score. Peer-review confidence also
drives the trust tier (`A`, `B`, `source-default`, `keyword`, or `none`), while
`eligibility_confidence.overall` summarizes the four signals. Retraction state
is irreversible once observed. The frontend renders the evidence, confidence,
tier, citation count, and twelve client-side citation formats.

## PERELMAN extraction

The Go PERELMAN implementation is text-first. It receives normalized title,
abstract, and available full text and requests strict JSON containing:

- a Russian TLDR;
- verbatim quotes with location, relevance, and rationale;
- formulas represented as LaTeX when they are present in the source text;
- table and figure descriptions already recoverable from text.

It does not send screenshots or image payloads and does not expose a
zoom/crop/rotate tool loop.

Extraction runs only in asynchronous River jobs. Immediate
`POST /api/v1/search` requests read the cache and never invoke the LLM. Published
articles use a single-winner claim, cache the result, and freeze normalized
content under `CINDEX_ARTICLES_DIR`; local Markdown is preferred on later
refreshes. Preprints are not frozen.

The LLM client supports OpenAI-compatible `chat/completions`, bounded input and
output, provider-specific extra request fields, request pacing, context
cancellation, transient overload retries, and `response_format=json_object`
with a compatibility retry for providers that reject it.

If `CINDEX_LLM_BASE_URL`, `CINDEX_LLM_API_KEY`, and `CINDEX_LLM_MODEL` are not
all configured, the worker uses existing cache entries only. It does not
fabricate quotes or summaries.

Example Z.AI configuration:

```env
CINDEX_LLM_BASE_URL=https://api.z.ai/api/paas/v4
CINDEX_LLM_API_KEY=YOUR_API_KEY
CINDEX_LLM_MODEL=glm-4.6v-flash
CINDEX_LLM_CONCURRENCY=1
CINDEX_LLM_MIN_REQUEST_INTERVAL=1
CINDEX_LLM_TIMEOUT=120s
```

## API

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/healthz` | Internal PostgreSQL and Redis readiness check |
| `GET` | `/api/health/` | Health route exposed through nginx |
| `POST` | `/api/v1/search` | Immediate corpus search |
| `POST` | `/api/v1/search/jobs` | Create or attach to a River job |
| `GET` | `/api/v1/search/jobs/{job_id}` | Poll progress and results |
| `GET` | `/api/v1/source-stats` | Cached source health |
| `POST` | `/api/v1/admin/reindex` | Protected ingestion trigger |

The two search POST routes default to 10 requests per 60 seconds per client IP.
Job polling and source statistics are not throttled. Pagination defaults to
page 1 with 5 results per page and permits at most 50 results per page.
`force_refresh=true` on immediate search performs live ingestion synchronously
before querying the corpus.

The admin reindex request also blocks until ingestion completes. Its response
retains the legacy `status: "queued"` shape, but the returned task ID is not a
pollable background job.

`backend/api/openapi.yaml` is the source for generated search handlers and
types. The compatibility health routes are wired separately in the Go router.

## Configuration

Copy `.env.example` to `.env`. `DATABASE_URL` and `REDIS_URL` are required.
The Go loader reads these variable groups:

- Runtime: `CINDEX_ENV`, `CINDEX_HTTP_ADDR`, `CINDEX_SHUTDOWN_TIMEOUT`, and
  `CINDEX_ARTICLES_DIR`.
- Storage: `DATABASE_URL` and `REDIS_URL`.
- Search: `CINDEX_SEARCH_DEFAULT_FRESHNESS_DAYS`,
  `CINDEX_SEARCH_FINAL_TOP_K`, `CINDEX_SEARCH_RATE_LIMIT_PER_IP`, and
  `CINDEX_SEARCH_RATE_LIMIT_WINDOW`.
- LLM connection: `CINDEX_LLM_BASE_URL`, `CINDEX_LLM_API_KEY`,
  `CINDEX_LLM_MODEL`, and `CINDEX_LLM_EXTRA_BODY`.
- LLM tuning: `CINDEX_LLM_TIMEOUT`, `CINDEX_LLM_TEMPERATURE`,
  `CINDEX_LLM_MAX_QUOTES`, `CINDEX_LLM_CONCURRENCY`,
  `CINDEX_LLM_MIN_REQUEST_INTERVAL`, and `CINDEX_LLM_MAX_INPUT_CHARS`.
- Sources: `CORE_API_KEY`, `EXA_API_KEY`, `CROSSREF_MAILTO`,
  `OPENALEX_API_KEY`, `UNPAYWALL_EMAIL`, and `CINDEX_BROWSER_URL`.
- Administration: `CINDEX_ADMIN_API_KEY`.

Never commit real credentials. Example values use `YOUR_API_KEY` placeholders.

## Docker deployment

Prepare `.env` and a database password file. The password in `DATABASE_URL`
must match `secrets/postgres_password.txt`.
Set `CINDEX_HOST_UID` and `CINDEX_HOST_GID` to the host account that should own
the bind-mounted frontend build output; both default to `1000`.

```bash
cp .env.example .env
mkdir -p secrets
printf '%s' 'YOUR_DB_PASSWORD' > secrets/postgres_password.txt
./scripts/compose_up.sh
```

`compose_up.sh` pulls runtime images, rebuilds the Go app/migration/worker image
and browser sidecar, force-recreates the stack, removes legacy orphan
containers, waits for readiness, and preserves PostgreSQL and browser-profile
volumes.

The one-shot `migrate` service runs `cindex-cli migrate`, including River
migrations. Both `app` and `worker` wait for it. The Go containers are
distroless and run as `nonroot:nonroot`.

Only nginx is published to the host:

```text
http://127.0.0.1:80     frontend and /api proxy
app:8000                internal Go API
browser:8081            internal browser/PDF sidecar
db:5432                 internal PostgreSQL
redis:6379              internal Redis
```

Check the running stack:

```bash
docker compose ps -a
curl --fail http://127.0.0.1/api/health/
docker compose logs --since=5m app worker browser
```

## Local development

Go does not load `.env` automatically. For host-native development, export
externally reachable PostgreSQL and Redis URLs first; Compose service names
such as `db` and `redis` resolve only inside the Compose network.

```bash
cd backend
export DATABASE_URL='postgresql://postgres:YOUR_DB_PASSWORD@127.0.0.1:5432/cindex'
export REDIS_URL='redis://127.0.0.1:6379/0'
make migrate-up
```

Run the API and worker in separate terminals with the same environment:

```bash
# terminal 1
cd backend
make run-server

# terminal 2
cd backend
make run-worker
```

The non-Compose API default is `127.0.0.1:8001`.

Frontend commands use Bun only:

```bash
cd frontend
bun install --frozen-lockfile
bun run dev
bun run build
bun run lint:check
```

The browser sidecar uses uv:

```bash
cd browser_service
uv sync
uv run uvicorn cindex_browser_sidecar.main:app --port 8081
```

## Quality gate

Go backend:

The race suite includes Testcontainers integration tests and requires access
to Docker for complete coverage.

```bash
cd backend
test -z "$(gofmt -l .)"
go test -race ./...
go build ./...
go vet ./...
golangci-lint run ./...
```

Frontend:

```bash
cd frontend
bun run lint:check
bun run build
```

Browser sidecar:

```bash
cd browser_service
uv run ruff format --check .
uv run ruff check .
uv run pytest
```

Container configuration:

`hadolint` and `shellcheck` are optional local prerequisites for the final two
commands.

```bash
docker compose config --quiet
hadolint --failure-threshold style backend/Dockerfile
shellcheck -x -S style scripts/compose_up.sh nginx/entrypoint.sh
```

`.github/workflows/backend.yml` runs Go lint, unit tests, migration tests, and
repository Testcontainers tests. The root Python workflow remains active for
legacy connector regression history; it is not the production backend gate.

## Documentation

- `README.md` describes the current Go runtime.
- `backend/api/openapi.yaml` defines the HTTP contract.
- `browser_service/README.md` defines the sidecar payloads and limits.
- `docs/archive/` contains historical implementation and planning records.
