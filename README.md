# 📚 CIndex

<div align="center">

**Scholarly citation search engine**

Cross-regional literature search over a PostgreSQL corpus with 24 live source connectors, trust-tiered eligibility evidence, and copy-ready citations.

[![Go](https://img.shields.io/badge/Go-1.26-00ADD8?style=for-the-badge&logo=go&logoColor=white)](https://go.dev/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-18-336791?style=for-the-badge&logo=postgresql&logoColor=white)](https://www.postgresql.org/)
[![Redis](https://img.shields.io/badge/Redis-8.6-DC382D?style=for-the-badge&logo=redis&logoColor=white)](https://redis.io/)
[![React](https://img.shields.io/badge/React-19-61DAFB?style=for-the-badge&logo=react&logoColor=black)](https://react.dev/)
[![Vite](https://img.shields.io/badge/Vite-7-646CFF?style=for-the-badge&logo=vite&logoColor=white)](https://vite.dev/)
[![Bun](https://img.shields.io/badge/Bun-1.3-FBF0DF?style=for-the-badge&logo=bun&logoColor=black)](https://bun.sh/)
[![Docker](https://img.shields.io/badge/Docker-Compose-2496ED?style=for-the-badge&logo=docker&logoColor=white)](https://www.docker.com/)

[![CI](https://img.shields.io/github/actions/workflow/status/4q4r/cindex/backend.yml?branch=main&style=flat-square&logo=githubactions&logoColor=white)](https://github.com/4q4r/cindex/actions/workflows/backend.yml)
[![Last commit](https://img.shields.io/github/last-commit/4q4r/cindex?style=flat-square&logo=github&logoColor=white)](https://github.com/4q4r/cindex/commits/main)
[![Repo size](https://img.shields.io/github/repo-size/4q4r/cindex?style=flat-square&logo=github&logoColor=white)](https://github.com/4q4r/cindex)
[![TypeScript](https://img.shields.io/badge/TypeScript-5.9-3178C6?style=flat-square&logo=typescript&logoColor=white)](https://www.typescriptlang.org/)
[![Tailwind](https://img.shields.io/badge/Tailwind-4-38BDF8?style=flat-square&logo=tailwindcss&logoColor=black)](https://tailwindcss.com/)
[![River](https://img.shields.io/badge/River-0.43-2A9D8F?style=flat-square)](https://riverqueue.com/)
[![FastAPI](https://img.shields.io/badge/FastAPI-Sidecar-009688?style=flat-square&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Python](https://img.shields.io/badge/Python-3.13-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![OpenAPI](https://img.shields.io/badge/OpenAPI-3-85EA2D?style=flat-square&logo=swagger&logoColor=black)](https://swagger.io/specification/)
[![Runtime](https://img.shields.io/badge/Runtime-Distroless-2496ED?style=flat-square&logo=docker&logoColor=white)](https://github.com/GoogleContainerTools/distroless)
[![nginx](https://img.shields.io/badge/nginx-latest-009639?style=flat-square&logo=nginx&logoColor=white)](https://nginx.org/)

[Architecture](#-system-architecture) · [Quick Start](#-quick-start) · [API](#-api-surface) · [Security](#-security) · [Testing](#-testing)

</div>

---

## 📑 Table of Contents

- [System Architecture](#-system-architecture)
- [Project Structure](#-project-structure)
- [Core Modules](#-core-modules)
- [Search & Ingestion](#-search--ingestion)
- [Eligibility & Trust](#-eligibility--trust)
- [PERELMAN Extraction](#-perelman-extraction)
- [API Surface](#-api-surface)
- [Configuration](#-configuration)
- [Security](#-security)
- [Quick Start](#-quick-start)
- [Local Development](#-local-development)
- [Testing](#-testing)
- [Access Points](#-access-points)

---

## 🗺️ System Architecture

```mermaid
flowchart TB
    UI[React SPA\nBun-built] -->|"HTTP :80"| Nginx[Nginx\nstatic + /api proxy]

    subgraph App Runtime
      API[Go API\nnet/http :8000]
      Worker[Go Worker\nRiver jobs]
    end

    subgraph Data
      PG[(PostgreSQL 18.3\ncorpus + River queue)]
      Redis[(Redis 8.6\nlimits + cache + locks)]
    end

    subgraph Ingestion
      Conn[24 Go connectors]
      Browser[Python sidecar :8081\nfetch / pdf-text / OCR]
    end

    Nginx -->|"/api/*"| API
    API --> PG
    API --> Redis
    API -->|"enqueue"| PG
    PG -->|"dequeue"| Worker
    Worker --> PG
    Worker --> Conn
    Conn -->|"HTTP"| Sources[Scholarly sources]
    Conn --> Browser
    Browser --> Sources
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

---

## 📂 Project Structure

```text
cindex/
├── backend/
│   ├── api/openapi.yaml           API contract
│   ├── cmd/                       server, worker, cli, healthcheck
│   ├── internal/
│   │   ├── connector/             24 source connectors + transports
│   │   ├── domain/                entities and eligibility rules
│   │   ├── httpapi/               generated contract bindings
│   │   ├── jobs/                  River search workflow
│   │   ├── repository/            PostgreSQL persistence + search
│   │   └── service/               search, ingestion, PERELMAN, local store
│   ├── migrations/                embedded SQL migrations
│   └── Dockerfile                 distroless Go image
├── browser_service/               Python browser/PDF/OCR sidecar
├── frontend/                      React/Vite SPA (Bun)
├── nginx/                         static serving + reverse proxy
├── scripts/compose_up.sh          reproducible full-stack rebuild
├── docker-compose.yml             production-like local topology
└── docs/archive/                  historical implementation notes
```

---

## 🧩 Core Modules

| Module | Purpose | Key Endpoints |
| :-- | :-- | :-- |
| `httpapi` | Public HTTP API, rate limits, generated handlers | `/api/v1/search`, `/api/v1/search/jobs` |
| `jobs` | River-based async search workflow | internal `search_job` |
| `repository` | PostgreSQL persistence, FTS matching, eligibility writes | internal |
| `connector` | 24 scholarly source connectors with circuit breakers | internal |
| `service` | Search orchestration, live ingestion, DOI enrichment, PERELMAN | internal |
| `browser_service` | Stealth browser, PDF text, OCR, vision sidecar | `/fetch`, `/pdf-text`, `/pdf-pages`, `/screenshot`, `/healthz` |
| `frontend` | Russian-language research SPA | `/` |
| `nginx` | Static assets + `/api/` reverse proxy | `/api/*` |

---

## 🔎 Search & Ingestion

The search entry point is `backend/internal/service/search.go`; SQL matching is
implemented in `backend/internal/repository/articles.go`.

- PostgreSQL matches title, abstract, and full text with
  `to_tsvector('simple', ...) @@ websearch_to_tsquery('simple', ...)`.
- Weighted scoring prioritizes DOI, title, abstract, full text, and journal
  matches. Zenodo receives a `0.3` source penalty.
- Sort modes are `relevance`, `newest`, and `metadata`; result cap defaults to 30.
- Results are deduplicated by normalized DOI, then by canonical
  title/year/journal. Public results require a DOI beginning with `10.`.
- Article, author, identifier, and eligibility writes are grouped in a
  transaction; job creation and its River row are atomic.

The canonical connector registry contains: `Europe PMC`, `OpenAlex`,
`Crossref`, `PubMed`, `arXiv`, `DOAJ`, `PMC`, `CORE`, `DBLP`, `HAL`, `Zenodo`,
`IACR ePrint`, `Exa`, `CiNii`, `SciEngine`, `CyberLeninka`, `MathNet.Ru`,
`SciELO`, `Persée`, `OpenEdition`, `Medknow`, `DergiPark`, `Hrčak`, and `AJOL`.

Each source has retry and circuit-breaker state; connector failures are
isolated, so a job can complete as `partial` while preserving successful
results. The lawful full-text resolver uses Unpaywall when `UNPAYWALL_EMAIL`
is set, then Europe PMC. PDFs are sent to the sidecar for native extraction
with bounded OCR fallback.

---

## 🎯 Eligibility & Trust

Every article carries four evidence-backed criteria, each with a persisted
confidence score:

| Criterion | Meaning |
| :-- | :-- |
| `peer_reviewed` | Published in a peer-reviewed venue |
| `indexed` | Present in a scholarly index |
| `doi_and_journal_card` | DOI and journal metadata verified |
| `not_preprint` | Not a preprint version |

Peer-review confidence drives the trust tier (`A`, `B`, `source-default`,
`keyword`, or `none`); `eligibility_confidence.overall` summarizes the four
signals. Retraction state is irreversible once observed. The frontend renders
the evidence, confidence, tier, citation count, and twelve client-side
citation formats.

---

## 🧠 PERELMAN Extraction

PERELMAN receives normalized title, abstract, available full text, and visual
article material. PDF sources are rendered into bounded PNG page payloads;
HTML sources receive a full-page screenshot plus supported figure images. The
vision model can inspect those images with a bounded `zoom`/`crop`/`rotate`
tool loop before returning strict JSON containing:

- a Russian TLDR;
- verbatim quotes with location, relevance, and rationale;
- formulas represented as LaTeX from text or images;
- table and figure descriptions converted to markdown from text or images.

The loop is bounded by `CINDEX_LLM_MAX_TOOL_TURNS`. Tool-produced images stay
in the per-extraction registry and are sent back as `image_url` data URIs.
Unreadable regions are reported as uncertainty instead of being fabricated.

Extraction runs only in asynchronous River jobs. Immediate
`POST /api/v1/search` requests read the cache and never invoke the LLM.
Published articles use a single-winner claim, cache the result, and freeze
normalized content under `CINDEX_ARTICLES_DIR`; local Markdown is preferred on
later refreshes. Preprints are not frozen.

The LLM client supports OpenAI-compatible `chat/completions`, bounded input
and output, provider-specific extra request fields, request pacing, context
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

---

## 🔌 API Surface

| Method | Path | Purpose |
| :-- | :-- | :-- |
| `GET` | `/healthz` | Internal PostgreSQL and Redis readiness |
| `GET` | `/api/health/` | Health route exposed through nginx |
| `POST` | `/api/v1/search` | Immediate corpus search |
| `POST` | `/api/v1/search/jobs` | Create or attach to a River job |
| `GET` | `/api/v1/search/jobs/{job_id}` | Poll progress and results |
| `GET` | `/api/v1/source-stats` | Cached source health |
| `POST` | `/api/v1/admin/reindex` | Protected ingestion trigger |

The two search POST routes default to 10 requests per 60 seconds per client
IP. Job polling and source statistics are not throttled. Pagination defaults
to page 1 with 5 results per page and permits at most 50 results per page.
`force_refresh=true` on immediate search performs live ingestion synchronously
before querying the corpus.

The admin reindex request also blocks until ingestion completes. Its response
retains the legacy `status: "queued"` shape, but the returned task ID is not a
pollable background job.

`backend/api/openapi.yaml` is the source for generated search handlers and
types. The compatibility health routes are wired separately in the Go router.

---

## ⚙️ Configuration

Copy `.env.example` to `.env`. `DATABASE_URL` and `REDIS_URL` are required.
The Go loader reads these variable groups:

- **Runtime:** `CINDEX_ENV`, `CINDEX_HTTP_ADDR`, `CINDEX_SHUTDOWN_TIMEOUT`,
  `CINDEX_ARTICLES_DIR`.
- **Storage:** `DATABASE_URL`, `REDIS_URL`.
- **Search:** `CINDEX_SEARCH_DEFAULT_FRESHNESS_DAYS`,
  `CINDEX_SEARCH_FINAL_TOP_K`, `CINDEX_SEARCH_RATE_LIMIT_PER_IP`,
  `CINDEX_SEARCH_RATE_LIMIT_WINDOW`.
- **LLM connection:** `CINDEX_LLM_BASE_URL`, `CINDEX_LLM_API_KEY`,
  `CINDEX_LLM_MODEL`, `CINDEX_LLM_EXTRA_BODY`.
- **LLM tuning:** `CINDEX_LLM_TIMEOUT`, `CINDEX_LLM_TEMPERATURE`,
  `CINDEX_LLM_MAX_QUOTES`, `CINDEX_LLM_CONCURRENCY`,
  `CINDEX_LLM_MIN_REQUEST_INTERVAL`, `CINDEX_LLM_MAX_INPUT_CHARS`,
  `CINDEX_LLM_MAX_TOOL_TURNS`, `CINDEX_LLM_MAX_PDF_PAGES`,
  `CINDEX_LLM_PDF_DPI`, `CINDEX_LLM_MAX_IMAGES`,
  `CINDEX_LLM_IMAGE_DETAIL`, `CINDEX_LLM_MAX_IMAGE_DIM`.
- **Sources:** `CORE_API_KEY`, `EXA_API_KEY`, `CROSSREF_MAILTO`,
  `OPENALEX_API_KEY`, `UNPAYWALL_EMAIL`, `CINDEX_BROWSER_URL`.
- **Administration:** `CINDEX_ADMIN_API_KEY`.

Never commit real credentials. Example values use `YOUR_API_KEY` placeholders.

---

## 🔒 Security

- **Distroless runtime** — Go containers ship no shell or package manager and
  run as `nonroot:nonroot`.
- **Proxy trust** — nginx overwrites client-supplied `X-Forwarded-For` with
  `$remote_addr` on both HTTP and TLS entrypoints.
- **SSRF guards** — connectors and the full-text resolver validate hosts,
  disallow private ranges, and reject untrusted redirects.
- **Rate limiting** — per-client-IP limits on search endpoints; polling is
  intentionally unthrottled.
- **Admin protection** — ingestion triggers require `CINDEX_ADMIN_API_KEY`.
- **No secrets in the repository** — `.env`, `secrets/`, and `certs/` are
  gitignored; GitHub secret scanning and push protection are enabled.
- **Extraction hygiene** — LLM output is cached per article, single-winner
  claims prevent conflicting quotes, and preprints are never frozen.

---

## 🚀 Quick Start

### 1. Environment

```bash
cp .env.example .env
mkdir -p secrets
printf '%s' 'YOUR_DB_PASSWORD' > secrets/postgres_password.txt
```

The password in `DATABASE_URL` must match `secrets/postgres_password.txt`.
Set `CINDEX_HOST_UID` and `CINDEX_HOST_GID` to the host account that should
own the bind-mounted frontend build output (both default to `1000`).

### 2. Build & Run

```bash
./scripts/compose_up.sh
```

`compose_up.sh` pulls runtime images, rebuilds the Go app/migration/worker
image and browser sidecar, force-recreates the stack, removes legacy orphan
containers, waits for readiness, and preserves PostgreSQL and browser-profile
volumes. The one-shot `migrate` service runs `cindex-cli migrate`, including
River migrations; both `app` and `worker` wait for it.

### 3. Health Check

```bash
curl -s http://127.0.0.1/api/health/
docker compose ps -a
```

---

## 💻 Local Development

Go does not load `.env` automatically. Export externally reachable PostgreSQL
and Redis URLs first (Compose service names resolve only inside the Compose
network):

```bash
cd backend
export DATABASE_URL='postgresql://postgres:YOUR_DB_PASSWORD@127.0.0.1:5432/cindex'
export REDIS_URL='redis://127.0.0.1:6379/0'
make migrate-up
```

Run the API and worker in separate terminals with the same environment:

```bash
# terminal 1
cd backend && make run-server

# terminal 2
cd backend && make run-worker
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

---

## ✅ Testing

Backend (the race suite includes Testcontainers integration tests and needs
Docker access for complete coverage):

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

Container configuration (`hadolint` and `shellcheck` are optional local
prerequisites):

```bash
docker compose config --quiet
hadolint --failure-threshold style backend/Dockerfile
shellcheck -x -S style scripts/compose_up.sh nginx/entrypoint.sh
```

`.github/workflows/backend.yml` runs Go lint, unit tests, migration tests, and
repository Testcontainers tests on backend changes.

---

## 🌍 Access Points

| URL | Description |
| :-- | :-- |
| `http://127.0.0.1/` | Frontend SPA (nginx) |
| `http://127.0.0.1/api/health/` | Health check |
| `app:8000` | Go API (internal) |
| `browser:8081` | Browser/PDF/OCR sidecar (internal) |
| `db:5432` | PostgreSQL (internal) |
| `redis:6379` | Redis (internal) |
