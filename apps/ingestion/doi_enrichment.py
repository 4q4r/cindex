"""DOI-based metadata enrichment via Crossref, OpenAlex, and Semantic Scholar."""

from __future__ import annotations

import asyncio
import contextlib

import aiohttp
import structlog
from django.conf import settings

from apps.articles.models import Article, ArticleAuthor, Author

logger = structlog.get_logger(__name__)

# Rate limit intervals (seconds)
_CROSSREF_INTERVAL = 0.1  # 10 req/s polite pool
_OPENALEX_INTERVAL = 0.01  # 100 req/s
_S2_INTERVAL = 1.0  # 1 RPS
_HTTP_NOT_FOUND = 404


class DoiEnrichmentService:
    """Backfill missing article metadata from Crossref, OpenAlex, Semantic Scholar."""

    @classmethod
    def enrich_sync(cls, articles: list[Article]) -> int:
        """Enrich articles synchronously — all DB work is sync, only HTTP is async."""
        candidates = [
            (a, cls._missing_fields(a))
            for a in articles
            if a.doi and a.doi.startswith("10.")
        ]
        candidates = [(a, m) for a, m in candidates if m]
        if not candidates:
            return 0

        # Phase 1: async HTTP fetches only — no DB access
        enrichment_map = cls._fetch_enrichments(candidates)

        # Phase 2: sync DB writes
        updated = 0
        for article, initial_missing in candidates:
            enrichment = enrichment_map.get(article.pk)
            if not enrichment:
                continue
            changed, pending_authors = cls._apply_cascade(
                article,
                initial_missing,
                enrichment,
            )
            if changed:
                cls._save_enriched(article, pending_authors)
                updated += 1
        return updated

    @classmethod
    def _fetch_enrichments(
        cls,
        candidates: list[tuple[Article, set[str]]],
    ) -> dict[int, list[dict]]:
        """Fetch metadata from all three APIs for candidate articles.

        Returns ``{article.pk: [enrichment_dict, ...]}``.

        Uses ``asyncio.run()`` unconditionally — Celery workers run in
        dedicated processes with no running event loop, so nesting is safe.
        """
        mailto = getattr(settings.APP, "crossref_mailto", "") or "cindex@app.local"
        openalex_key = getattr(settings.APP, "openalex_api_key", "") or ""

        return asyncio.run(cls._fetch_all(candidates, mailto, openalex_key))

    @classmethod
    async def _fetch_all(
        cls,
        candidates: list[tuple[Article, set[str]]],
        mailto: str,
        openalex_key: str,
    ) -> dict[int, list[dict]]:
        """Async: fetch enrichment data from APIs. No DB access."""
        sem: dict[str, asyncio.Semaphore] = {
            "crossref": asyncio.Semaphore(10),
            "openalex": asyncio.Semaphore(50),
            "s2": asyncio.Semaphore(1),
        }
        last_call: dict[str, float] = {
            "crossref": 0.0,
            "openalex": 0.0,
            "s2": 0.0,
        }

        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30),
        ) as session:
            tasks = [
                cls._fetch_enrichment_for_article(
                    article,
                    missing,
                    session,
                    sem,
                    last_call,
                    mailto,
                    openalex_key,
                )
                for article, missing in candidates
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        enrichment_map: dict[int, list[dict]] = {}
        for (article, _), result in zip(candidates, results, strict=True):
            if isinstance(result, Exception):
                logger.warning("doi_enrichment: failed for %s: %s", article.doi, result)
            elif result:
                enrichment_map[article.pk] = result
        return enrichment_map

    @classmethod
    async def _fetch_enrichment_for_article(  # noqa: PLR0913
        cls,
        article: Article,
        missing: set[str],
        session: aiohttp.ClientSession,
        sem: dict[str, asyncio.Semaphore],
        last_call: dict[str, float],
        mailto: str,
        openalex_key: str,
    ) -> list[dict] | None:
        """Fetch enrichment data from APIs for one article. No DB access."""
        enrichments: list[dict] = []

        # Crossref: authors, year, volume, issue, pages (no abstract)
        crossref_fields = {"authors", "year", "volume", "issue", "pages"}
        if missing & crossref_fields:
            data = await cls._fetch_crossref(
                article.doi,
                session,
                sem,
                last_call,
                mailto,
            )
            if data:
                enrichments.append(cls._parse_crossref(data))

        # OpenAlex: abstract, authors, year, volume, issue, pages
        openalex_fields = {"authors", "abstract", "year", "volume", "issue", "pages"}
        if missing & openalex_fields:
            data = await cls._fetch_openalex(
                article.doi,
                session,
                sem,
                last_call,
                openalex_key,
            )
            if data:
                enrichments.append(cls._parse_openalex(data))

        # Semantic Scholar: abstract, authors, year
        s2_fields = {"authors", "abstract", "year"}
        if missing & s2_fields:
            data = await cls._fetch_semantic_scholar(
                article.doi,
                session,
                sem,
                last_call,
            )
            if data:
                enrichments.append(cls._parse_semantic_scholar(data))

        return enrichments or None

    @classmethod
    def _apply_cascade(
        cls,
        article: Article,
        initial_missing: set[str],
        enrichments: list[dict],
    ) -> tuple[bool, list[str]]:
        """Apply enrichment dicts to article in cascade order. Sync only."""
        missing = set(initial_missing)
        changed = False
        pending_authors: list[str] = []

        for enrichment in enrichments:
            changed, pending_authors = cls._apply_step(
                article,
                enrichment,
                missing,
                changed,
                pending_authors,
                abstract_field="abstract" if "abstract" in enrichment else None,
            )
            # Update missing set based on what was actually set
            missing -= {
                f
                for f in ("year", "volume", "issue", "pages", "abstract")
                if cls._field_filled(article, f)
            }
            if pending_authors:
                missing.discard("authors")

        return changed, pending_authors

    @classmethod
    def _needs_enrichment(cls, article: Article) -> bool:
        """Return True if article has a DOI and is missing at least one field."""
        if not article.doi or not article.doi.startswith("10."):
            return False
        return bool(cls._missing_fields(article))

    @classmethod
    def _missing_fields(cls, article: Article) -> set[str]:
        """Return set of field names that are still empty on the article."""
        missing: set[str] = set()
        has_unknown = (
            not article.article_authors.exists()
            or article.article_authors.first().author.full_name == "Unknown author"
        )
        if has_unknown:
            missing.add("authors")
        if not article.abstract:
            missing.add("abstract")
        if article.publication_year is None:
            missing.add("year")
        if not article.volume:
            missing.add("volume")
        if not article.issue:
            missing.add("issue")
        if not article.pages:
            missing.add("pages")
        return missing

    @classmethod
    def _save_enriched(cls, article: Article, pending_authors: list[str]) -> None:
        """Persist enriched article fields and update authors."""
        save_fields = ["updated_at"]
        for f in ("publication_year", "abstract", "volume", "issue", "pages"):
            val = getattr(article, f, None)
            if val:
                save_fields.append(f)
        article.save(update_fields=save_fields)
        if pending_authors:
            cls._update_authors(article, pending_authors)

    @classmethod
    def _apply_step(  # noqa: PLR0913
        cls,
        article: Article,
        enrichment: dict,
        missing: set[str],
        changed: bool,  # noqa: FBT001
        pending_authors: list[str],
        abstract_field: str | None = None,
    ) -> tuple[bool, list[str]]:
        """Apply one API step's enrichment to an article."""
        if enrichment.get("authors") and "authors" in missing and not pending_authors:
            pending_authors = enrichment.pop("authors")
            changed = True
        if abstract_field and enrichment.get(abstract_field) and "abstract" in missing:
            article.abstract = enrichment[abstract_field][:8000]
            changed = True
        for field in ("year", "volume", "issue", "pages"):
            if enrichment.get(field) and field in missing:
                setattr(article, cls._model_field(field), enrichment[field])
                changed = True
        return changed, pending_authors

    @staticmethod
    def _model_field(enrichment_field: str) -> str:
        """Map enrichment field name to Article model field name."""
        return {"year": "publication_year"}.get(enrichment_field, enrichment_field)

    @staticmethod
    def _field_filled(article: Article, field: str) -> bool:
        """Check if an enrichment field is filled on the article (no ORM calls)."""
        if field == "authors":
            return False
        model_field = DoiEnrichmentService._model_field(field)
        val = getattr(article, model_field, None)
        if val is None:
            return False
        return bool(str(val).strip())

    @classmethod
    def _update_authors(cls, article: Article, author_names: list[str]) -> None:
        """Replace article authors with the given names."""
        names = [n.strip() for n in author_names if n.strip()]
        if not names:
            return
        article.article_authors.all().delete()
        for order, full_name in enumerate(names, start=1):
            author, _ = Author.objects.get_or_create(full_name=full_name)
            ArticleAuthor.objects.get_or_create(
                article=article,
                author=author,
                order=order,
            )

    # --- API fetchers ---

    @classmethod
    async def _fetch_crossref(
        cls,
        doi: str,
        session: aiohttp.ClientSession,
        sem: dict[str, asyncio.Semaphore],
        last_call: dict[str, float],
        mailto: str,
    ) -> dict | None:
        url = f"https://api.crossref.org/works/{doi}?mailto={mailto}"
        try:
            async with sem["crossref"]:
                await cls._rate_limit(last_call, "crossref", _CROSSREF_INTERVAL)
                async with session.get(url) as resp:
                    if resp.status == _HTTP_NOT_FOUND:
                        return None
                    resp.raise_for_status()
                    data = await resp.json()
            return data.get("message", data)
        except (aiohttp.ClientError, ValueError, TimeoutError) as exc:
            logger.debug("crossref: DOI %s lookup failed: %s", doi, exc)
            return None

    @classmethod
    async def _fetch_openalex(
        cls,
        doi: str,
        session: aiohttp.ClientSession,
        sem: dict[str, asyncio.Semaphore],
        last_call: dict[str, float],
        api_key: str,
    ) -> dict | None:
        url = f"https://api.openalex.org/works/doi:{doi}"
        if api_key:
            url += f"?api_key={api_key}"
        try:
            async with sem["openalex"]:
                await cls._rate_limit(last_call, "openalex", _OPENALEX_INTERVAL)
                async with session.get(url) as resp:
                    if resp.status == _HTTP_NOT_FOUND:
                        return None
                    resp.raise_for_status()
                    return await resp.json()
        except (aiohttp.ClientError, ValueError, TimeoutError) as exc:
            logger.debug("openalex: DOI %s lookup failed: %s", doi, exc)
            return None

    @classmethod
    async def _fetch_semantic_scholar(
        cls,
        doi: str,
        session: aiohttp.ClientSession,
        sem: dict[str, asyncio.Semaphore],
        last_call: dict[str, float],
    ) -> dict | None:
        fields = "title,authors,year,abstract,venue,journal"
        url = (
            f"https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}?fields={fields}"
        )
        try:
            async with sem["s2"]:
                await cls._rate_limit(last_call, "s2", _S2_INTERVAL)
                async with session.get(url) as resp:
                    if resp.status == _HTTP_NOT_FOUND:
                        return None
                    resp.raise_for_status()
                    return await resp.json()
        except (aiohttp.ClientError, ValueError, TimeoutError) as exc:
            logger.debug("s2: DOI %s lookup failed: %s", doi, exc)
            return None

    @staticmethod
    async def _rate_limit(
        last_call: dict[str, float],
        api_name: str,
        min_interval: float,
    ) -> None:
        """Sleep if needed to respect the minimum interval between calls."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is None:
            return
        now = loop.time()
        elapsed = now - last_call.get(api_name, 0.0)
        if elapsed < min_interval:
            await asyncio.sleep(min_interval - elapsed)
        last_call[api_name] = loop.time()

    # --- API response parsers ---

    @classmethod
    def _parse_crossref(cls, data: dict) -> dict:
        """Extract metadata from Crossref /works/{DOI} response."""
        result: dict = {}
        authors_list = data.get("author", [])
        if authors_list:
            result["authors"] = [
                f"{a.get('given', '')} {a.get('family', '')}".strip()
                for a in authors_list
                if isinstance(a, dict)
            ]
        published = data.get("published-print") or data.get("published-online") or {}
        date_parts = published.get("date-parts", [[]])
        if date_parts and date_parts[0]:
            with contextlib.suppress(ValueError, IndexError):
                result["year"] = int(date_parts[0][0])
        volume = str(data.get("volume", "") or "").strip()
        if volume:
            result["volume"] = volume
        issue = str(data.get("issue", "") or "").strip()
        if issue:
            result["issue"] = issue
        page = str(data.get("page", "") or "").strip()
        if page:
            result["pages"] = page
        return result

    @classmethod
    def _parse_openalex(cls, data: dict) -> dict:
        """Extract metadata from OpenAlex /works/doi:{DOI} response."""
        result: dict = {}
        authorships = data.get("authorships", [])
        if authorships:
            result["authors"] = [
                a.get("author", {}).get("display_name", "")
                for a in authorships
                if isinstance(a, dict) and a.get("author", {}).get("display_name")
            ]
        abstract_index = data.get("abstract_inverted_index")
        if abstract_index:
            result["abstract"] = cls._reconstruct_abstract(abstract_index)
        year = data.get("publication_year")
        if isinstance(year, int):
            result["year"] = year
        biblio = data.get("biblio", {})
        volume = str(biblio.get("volume", "") or "").strip()
        if volume:
            result["volume"] = volume
        issue = str(biblio.get("issue", "") or "").strip()
        if issue:
            result["issue"] = issue
        first_page = str(biblio.get("first_page", "") or "").strip()
        last_page = str(biblio.get("last_page", "") or "").strip()
        if first_page:
            result["pages"] = f"{first_page}-{last_page}" if last_page else first_page
        return result

    @classmethod
    def _parse_semantic_scholar(cls, data: dict) -> dict:
        """Extract metadata from Semantic Scholar /paper/DOI:{DOI} response."""
        result: dict = {}
        authors = data.get("authors", [])
        if authors:
            result["authors"] = [
                a.get("name", "")
                for a in authors
                if isinstance(a, dict) and a.get("name")
            ]
        abstract = data.get("abstract")
        if abstract and isinstance(abstract, str):
            result["abstract"] = abstract.strip()
        year = data.get("year")
        if isinstance(year, int):
            result["year"] = year
        return result

    @staticmethod
    def _reconstruct_abstract(inverted_index: dict) -> str:
        """Reconstruct plain-text abstract from OpenAlex inverted index."""
        if not inverted_index:
            return ""
        word_positions: list[tuple[int, str]] = []
        for word, positions in inverted_index.items():
            word_positions.extend((pos, word) for pos in positions)
        word_positions.sort()
        return " ".join(word for _, word in word_positions)

    @classmethod
    def _select_doi_candidates(cls) -> list[Article]:
        """Query Article rows that have DOIs but are missing metadata."""
        return list(
            Article.objects.filter(doi__startswith="10.")
            .select_related("journal", "source")
            .prefetch_related("article_authors__author"),
        )
