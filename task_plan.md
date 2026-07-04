# Task Card

- Goal: Довести ingestion/search до production-уровня для всех требуемых источников (24 core источника, плюс optional Exa при наличии EXA_API_KEY), с live-устойчивым парсингом, 4-критериальной проверкой качества статьи, векторизацией (Qdrant multivector + dense), индексацией/поиском (Elasticsearch) и выдачей цитат/слайсов.
- Constraints: Базовый контур — только бесплатные публичные источники/API, без платных ключей; Exa подключается как optional source только при наличии `EXA_API_KEY`; без ослабления `live_quality` (все активные источники должны проходить); использовать cloudscraper + BrowserForge для anti-bot; не удалять/не откатывать пользовательские изменения.
- User Preference (Pinned): Для frontend использовать только `bun`; `npm` не использовать.
- Done Definition:
  - Все 24 core источника реализованы и включены в `CONNECTORS`; optional Exa включается отдельно при наличии `EXA_API_KEY`.
  - `pytest -m live_quality` проходит строго (0 fail, без "minimum pass").
  - `pytest -m live_smoke` проходит.
  - Пост-обогащение карточек (doi/peer-review/indexing/preprint-evidence) выполняется после fetch.
  - CI содержит отдельные live-smoke/live-quality сценарии.
  - Exa admin visibility uses the official team-management API (`/api-keys` + `/api-keys/{id}/usage`) and is not inferred from search response headers.
- Current Step: The active running jobs were stopped, derived search state was wiped (`Snippet`, `Passage`, Elasticsearch `articles/article_passages`, Qdrant `articles/passages`), and a fresh search job was restarted on the normalized full query `Интеллект-карты как методическая находка: формирование навыка обширных устных высказываний в проектах по иностранному языку`; the current live job is `769d05d0-1345-40ad-a1aa-b383d79d0e77`, and the next step is to let it rebuild from clean data and watch for any parser noise or recovery regressions.
- Open Questions:
  - После privileged refresh/redeploy прочитать текущий live job и посмотреть `source_timings`, чтобы выделить медленные источники, если это снова понадобится для performance work.
  - Проверить на живом frontend, что long-running `SearchJob` остаётся в loading до terminal status, а empty-result jobs показывают `Ничего не найдено`.
  - Проверить, что persisted search wait averages остаются корректными после нескольких completed jobs и после restart/redeploy.
  - Какие ещё lawful public article APIs стоит добавить после OpenAlex/Crossref/Semantic Scholar/PubMed/arXiv/EPMC/Unpaywall, если нужен более широкий мировой охват.

## Decisions

- Django monolith + Celery for ingestion orchestration: simpler operations with strong async support.
- API-first without auth, HTML parsing fallback: respects legal/public-access constraints.
- Local embedding model default `BAAI/bge-m3`: multilingual and CPU-capable baseline.
- Hybrid retrieval orientation: passage-level Qdrant multivector semantic retrieval + Elasticsearch lexical/metadata recall + cross-encoder reranking + article-level dedupe.
- Дополнительно принято:
  - `enrich_raw()` запускается после fetch для всех источников в ingestion pipeline.
  - Добавлены confidence-поля eligibility в `Article` и API-ответ поиска.
  - Для `Medknow` выбран бесплатный OpenAlex publisher-filter fallback (publisher Medknow), т.к. `journalonweb` де-факто только notice.
  - Для `SciOpen` реализован реальный API-hook через `/search/search` с корректным JSON payload из live JS.
  - Для `MathNet` реализован POST-hook через `searchpapers_do.phtml` (не generic HTML search).
  - Для `COAJ` реализован бесплатный публичный API-хук (`pub-journal/all`, `journal-top/show`).
  - Для `SciBot` реализован websocket-hook через `wss://sci-bot.ru/` с BrowserForge headers, ALTCHA proof-of-work verification, и разбором `tool_end.read_article` карточек в реальные `RawArticle` записи.
  - По живому DevTools trace выяснено, что SciBot использует двухфазный websocket flow: `queue_redirect` на корневом socket, затем `/?queue=1` с `queue_join`/`queue_resume`, `queue_verify`, `queue_position`, `queue_ready`, и только потом возврат на корневой socket с `queueToken`/`enqueuedAt`.
  - Для UX-прозрачности backend теперь отдает по job реальный stage/progress (`checking_index`, `live_scan`, `searching_index`, `completed/failed`) вместо фронтовой имитации.
  - Политика доскана:
    - если `index_hits_before == 0` -> обязательный live-доскан,
    - если последний успешный scan по запросу старше `APP.search_query_freshness_days` -> live-доскан,
    - если `force_refresh_requested` -> live-доскан независимо от индекса.
  - `celery-worker` держится в CPU/RAM budget: `0.50` CPU и `7GiB` RAM, а `scripts/compose_up.sh` использует `docker compose --compatibility`, чтобы лимиты сохранялись после пересоздания.

## Error Log (active)

- 2026-04-22: `live_quality` падал из-за `koreascience` (TLS EOF/read timeout) -> была выполнена стабилизация источника без кросс-сорс fallback.
- 2026-04-22: `live_quality` падал из-за `mathnet: non-empty=1/2`, `ajol: non-empty=0/2`, затем `openedition: non-empty=1/2` -> устранено через parser/fallback/query-matrix стабилизацию.
- 2026-04-22 (current): блокирующих ошибок для live gate нет (последние прогоны: PASS).
- 2026-04-23: live job `dd2ca1c0-b820-47db-aa2d-f994bd817f95` завершился `failed` с ошибкой `BadRequestError(400, 'None')`, этап `failed`, `source_done=0/23`, `rescan_reason=empty_index_hits`.
- 2026-04-27: job `455bd31f-4de9-40fd-9200-adc433a3fd98` exposed raw `<em>` highlights, PDF blob text, and duplicate DOI entries; backend normalization, serializer cleanup, and DOI/title dedupe were added to remove those artifacts from live output.
- 2026-05-04: Both running jobs (`e7257571-ff97-469d-9f3d-d79ab3396639`, `32307d36-5a34-41bd-936f-544924569932`) were stopped, derived search state was reset, and a fresh job `769d05d0-1345-40ad-a1aa-b383d79d0e77` was created for the normalized full Russian query so the next run starts from clean data.
