# Source Endpoints Report

Updated: 2026-04-30 (Europe/Moscow)

This document describes the current source-specific hooks used by ingestion in [`apps/ingestion/connectors.py`](/home/whoami/projects/cindex/apps/ingestion/connectors.py), including API/OAI/HTML paths, free-access assumptions, and fallback behavior.

## Japan

### J-STAGE (`jstage`)
- Primary mode: API (free, no key in current use)
- Primary endpoint: `https://api.jstage.jst.go.jp/searchapi/do?service=3&keyword=...&count=...`
- Fallback: HTML search pages
  - `https://www.jstage.jst.go.jp/search/global`
  - `https://www.jstage.jst.go.jp/search/global/_search/-char/en`
- Parser: XML entry extraction (title/link/doi/pubyear/journal), then HTML selectors, then JSON-LD fallback.

### CiNii Research (`cinii`)
- Primary mode: API (free OpenSearch)
- Primary endpoint: `https://cir.nii.ac.jp/opensearch/v2/all?format=json&q=...&lang=en&count=...`
- Fallback: generic HTML parsing (`super()._fetch_html`).
- Parser: item mapping from OpenSearch payload.

### SciOpen (`sciopen`)
- Primary mode: API hook (reverse-engineered from live JS payload shape)
- Warm-up endpoint: `https://www.sciopen.com/search/to_search_page?keywords=...`
- Main endpoint: `POST https://www.sciopen.com/search/search`
- Fallback: homepage article links
  - `https://www.sciopen.com/home`
- Notes:
  - Uses cloudscraper + BrowserForge headers to reduce 403/UA ACL blocks.
  - Returns DOI-centric article URLs (`/article/{doi}`).

## Russia

### CyberLeninka (`cyberleninka`)
- Primary mode: API
- Endpoint: `POST https://cyberleninka.ru/api/search`
- Fallback: HTML search
  - `https://cyberleninka.ru/search?q=...`

### MathNet.Ru (`mathnet`)
- Primary mode: HTML form POST hook
- Endpoint: `POST https://www.mathnet.ru/php/searchpapers_do.phtml?jrnid=&option_lang=eng`
- Fallbacks:
  - repeated query attempts inside connector
  - homepage extraction fallback: `https://www.mathnet.ru/php/search.phtml?wshow=search&option_lang=eng`
- Note: source-specific parser extracts `/eng/...` article links.

## Biomedical / Life Sciences

### Europe PMC (`europe_pmc`)
- Primary mode: API
- Endpoint:
  - `https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=...&format=json&pageSize=...&resultType=core`

## Global OA Search

### DOAJ (`doaj`)
- Primary mode: API
- Endpoint:
  - `https://doaj.org/api/search/articles/{query}?pageSize=...`

### Unpaywall (`unpaywall`)
- Primary mode: API title search
- Endpoint:
  - `https://api.unpaywall.org/v2/search/?query=...&is_oa=true&email=...`
- Notes:
  - Free public API; no key required.
  - Search is title-based and returns DOI records plus OA metadata.
  - The API expects a contact email query parameter.

### Exa (`exa`)
- Primary mode: search API
- Endpoint:
  - `https://api.exa.ai/search`
- Request shape:
  - `POST` JSON with `query`, `type`, `category: "research paper"`, `num_results`, and `contents`
- Auth:
  - `x-api-key: $EXA_API_KEY`
- Notes:
  - This source is active only when `EXA_API_KEY` is set.
  - The parser consumes `results[].title`, `results[].url`, `results[].publishedDate`, `results[].author`, and `results[].text`.

### SciBot (`scibot`)
- Primary mode: WebSocket chat endpoint with proof-of-work verification
- Endpoint:
  - `wss://sci-bot.ru/`
- Protocol:
  - `session` -> `verify` -> `verify_response` -> `queue_position` -> `question_handle` -> `content`/`tool_start`/`tool_end`
- Parser strategy:
  - solve the ALTCHA challenge from the frontend JS (`sha256(salt + number)`), then collect structured article cards from `tool_end.read_article`
  - assistant prose is used only as fallback text when no structured cards are returned
- Notes:
  - BrowserForge headers are used on the websocket handshake.
  - Optional cookie fallback is supported through `SCIBOT_COOKIE` / `SCIBOT_COOKIES`.

## Latin America

### SciELO (`scielo`)
- Primary mode: OAI mirrors (free)
- Endpoints:
  - `https://scielo.isciii.es/oai/scielo-oai.php`
  - `https://www.scielo.org.mx/oai/scielo-oai.php`
- Fallback: HTML search
  - `https://www.scielo.org/en/search/`
  - `https://search.scielo.org/`

### Redalyc (`redalyc`)
- Primary mode: API-style service endpoint
- Endpoint pattern:
  - `https://www.redalyc.org/service/r2020/getArticles/{query}/1/{size}/0/default`

## European Humanities

### Persée (`persee`)
- Primary mode: HTML search
- Endpoint: `https://www.persee.fr/search?q=...`

### AJOL (`ajol`)
- Primary mode: OAI-PMH first, HTML fallback (OA-filtered)
- OAI endpoint: `https://www.ajol.info/index.php/ajol/oai?verb=ListRecords&metadataPrefix=oai_dc`
- Endpoint: `https://www.ajol.info/index.php/ajol/search?query=...`
- OAI note: repository can return deleted/no-match windows; connector falls back to HTML parsing when OAI yields no usable items.
- OA filtering:
  - positive markers: `open access`, `free access`, `download full text`, `creative commons`, `cc by`
  - negative markers: `subscription required`, `subscription content only`, `purchase`, `buy article`, `paywall`
- Fallback behavior:
  - query-relevant OA items first, then OA fallback candidates.

## Transport Layer (Common)

- Request engine: `cloudscraper` (with `js2py` interpreter)
- Header strategy: BrowserForge (`HeaderGenerator`) per request
- Challenge handling:
  - token flow: `cloudscraper.get_tokens`
  - cookie flow: `cloudscraper.get_cookie_string`
- Generic retries: `MAX_ATTEMPTS=3`, incremental sleep on failure.
