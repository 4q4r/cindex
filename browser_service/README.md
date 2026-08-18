# cindex-browser-sidecar

A small FastAPI service that wraps [cloakbrowser](https://pypi.org/project/cloakbrowser/)
(source-patched Chromium) so the distroless cindex worker can fetch
HTML/XML/JSON/RSS from sources protected by JS challenges (BunnyCDN Shield,
Cloudflare Turnstile, FingerprintJS) without importing a browser stack.

The worker calls `POST /fetch`, `POST /screenshot`, `POST /pdf-text`, and
`POST /pdf-pages` over the internal docker network. This service owns a single
persistent Chromium context whose profile keeps challenge cookies warm across
requests.

## Architecture

```text
distroless cindex worker
  BaseConnector._request_text/_request_xml_text/_request_json
      └─> BrowserTransport  ──HTTP POST──>  browser sidecar (this service)
                                              POST /fetch {url, method, ...}
                                                  └─> cloakbrowser persistent context
                                                       (humanize=True, human_preset="careful")
```

The worker **never imports** `cloakbrowser` — it only performs HTTP calls to
this sidecar. That keeps the worker image lean and isolates the browser
dependency tree (Chromium + libs ≈ 500 MB) in its own container.

### Why re-fetch via `page.evaluate(fetch)`

When a CDN serves a JS challenge page that solves itself and then reloads,
`page.goto` resolves with the **first** response (the challenge HTML), not the
post-reload resource. This service therefore:

1. `page.goto(url, wait_until="networkidle")` — solves the challenge, sets the
   cookie.
2. Re-fetches the URL from inside the page via `fetch(url, {credentials:'include'})`
   — returns the raw server body for every content type (HTML, XML, RSS, JSON)
   without DOM serialization.

## Endpoints

### `POST /fetch`

Forward a fetch request through the stealth browser context.

**Request body** (JSON):

<!-- markdownlint-disable MD013 -->

| field     | type                | default | notes                                                |
| --------- | ------------------- | ------- | ---------------------------------------------------- |
| `url`     | string (HttpUrl)    | —       | required, validated by pydantic                      |
| `method`  | `"GET"` \| `"POST"` | `GET`   |                                                      |
| `params`  | `dict[str,str]`     | `null`  | appended to the URL query string                     |
| `headers` | `dict[str,str]`     | `null`  | caller headers, merged with `accept`                 |
| `data`    | `dict[str,str]`     | `null`  | POST form body (`application/x-www-form-urlencoded`) |
| `json`    | any                 | `null`  | POST JSON body (`application/json`)                  |
| `accept`  | string              | `null`  | injected as `Accept` if not already set              |
| `timeout` | float (seconds)     | `25.0`  | `0 < t ≤ 120`                                        |

<!-- markdownlint-enable MD013 -->

**Response** (`200`, `FetchResponse`):

```json
{
  "status": 200,
  "body": "<!doctype html>...",
  "content_type": "text/html; charset=utf-8",
  "encoding": "text"
}
```

`status` is the **upstream** server's HTTP status (not the sidecar's). The
worker maps `status >= 400` to a connector error. `encoding` is `"text"`
(browser-decoded text for HTML/XML/JSON/RSS) or `"base64"` (raw bytes for
binary content types such as `application/pdf`, so the worker can feed them
to a PDF parser without corruption).

**Error responses:**

| sidecar status | meaning                               |
| -------------- | ------------------------------------- |
| `422`          | invalid payload (pydantic validation) |
| `502`          | browser pool / navigation failure     |
| `504`          | fetch timed out                       |

### `POST /pdf-text`

Extract native text from base64-encoded PDF bytes, falling back to Tesseract
OCR for empty or garbled pages. The OCR language is restricted to the language
packs installed in the sidecar image.

```json
{
  "body": "JVBERi0xLjQK...",
  "ocr_language": "eng"
}
```

Response:

```json
{"text": "Normalized article text..."}
```

Malformed base64 and unsupported OCR language values return `422`. Invalid or
unreadable PDF documents return an empty `text` value, matching connector
degradation behavior.

### `POST /pdf-pages`

Render a bounded number of PDF pages as inline PNG payloads for the PERELMAN
vision loop. Rendering uses the sidecar's existing pymupdf runtime and never
writes to a shared filesystem.

```json
{
  "body": "JVBERi0xLjQK...",
  "ocr_language": "eng",
  "max_pages": 8,
  "dpi": 144
}
```

The response contains `pages[]` entries with `id`, base64 `body`, pixel
dimensions, and page text, plus a combined `text` field. `max_pages` is capped
at 32, `dpi` at 300, and PDF bytes at the same 32 MiB limit as `/pdf-text`.

### `GET /healthz`

Returns `{"status": "ok"}`. Used by the container `HEALTHCHECK` and docker
compose health check.

## Configuration (environment)

<!-- markdownlint-disable MD013 -->

| variable                     | default               | notes                                           |
| ---------------------------- | --------------------- | ----------------------------------------------- |
| `CINDEX_BROWSER_PORT`        | `8081`                | uvicorn listen port                             |
| `CINDEX_PROFILE_DIR`         | `/data/cloak-profile` | persistent browser profile (challenge cookies)  |
| `CINDEX_BROWSER_HEADLESS`    | `1`                   | `0`/`false` runs Chromium headed (debug only)   |
| `CINDEX_BROWSER_CONCURRENCY` | `4`                   | max concurrent in-flight fetches (semaphore)    |
| `CLOAKBROWSER_CACHE_DIR`     | `/opt/cloak-cache`    | where the pre-downloaded Chromium binary lives  |
| `CLOAKBROWSER_AUTO_UPDATE`   | `false`               | do not phone home for binary updates at runtime |

<!-- markdownlint-enable MD013 -->

## Build & run (local)

This project is **uv-only** (PEP 621 `pyproject.toml`, no `requirements.txt`).

```bash
cd browser_service
uv sync                # install dev deps
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
uv run uvicorn cindex_browser_sidecar.main:app --port 8081
```

```bash
curl -X POST http://localhost:8081/fetch \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com","method":"GET","timeout":25}'
```

## Build & run (docker)

The `Dockerfile` is multi-stage: a `uv` builder installs deps and pre-downloads
the cloakbrowser Chromium binary into `/opt/cloak-cache`; the `python:3.13-slim`
runtime copies the venv + binary + Chromium shared libraries and runs as a
non-root user (`65532`) behind `tini`.

```bash
docker compose build browser
docker compose up -d browser
curl http://localhost:8081/healthz
```

The persistent profile is a named volume (`cloak_profile`) so challenge cookies
survive container restarts.
