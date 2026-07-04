"""Base connector primitives for source ingestion.

Provides BaseConnector (HTML/cloudscraper transport) and AsyncApiConnector
(aiohttp transport), shared by all source connectors.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from urllib.parse import quote_plus, urljoin

import aiohttp
import cloudscraper
import pymupdf
import structlog
from browserforge.headers import Browser, HeaderGenerator
from bs4 import BeautifulSoup, Tag

from apps.core.text import normalize_scholarly_text

logger = structlog.get_logger(__name__)

DOI_PATTERN = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)
YEAR_PATTERN = re.compile(r"(?:19|20)\d{2}")
PDF_URL_PATTERN = re.compile(
    r"https?://[^\s\"'<>]+?\.pdf(?:\?[^\s\"'<>]+)?",
    re.IGNORECASE,
)
OCR_LANGUAGE_MAP: dict[str, str] = {
    "ar": "ara",
    "de": "deu",
    "en": "eng",
    "eng": "eng",
    "es": "spa",
    "fr": "fra",
    "it": "ita",
    "ja": "jpn",
    "jpn": "jpn",
    "ko": "kor",
    "kor": "kor",
    "pt": "por",
    "ru": "rus",
    "rus": "rus",
    "zh": "chi_sim+chi_tra",
    "zho": "chi_sim+chi_tra",
}
HTML_BOILERPLATE_TAGS = (
    "script",
    "style",
    "noscript",
    "template",
    "svg",
    "canvas",
    "iframe",
)

PEER_REVIEW_TOKENS = (
    "peer reviewed",
    "peer-review",
    "refereed",
    "double blind review",
)
INDEXING_TOKENS = (
    "scopus",
    "web of science",
    "medline",
    "pmc",
    "pubmed central",
    "kci",
    "tr dizin",
    "doaj",
)
PREPRINT_TOKENS = (
    "preprint",
    "author manuscript",
    "accepted manuscript",
    "working paper",
)

HTTP_ERROR_THRESHOLD = 400
HTTP_FORBIDDEN_STATUS = 403
MIN_PUBLICATION_YEAR = 1800
MIN_ARTICLE_TITLE_LENGTH = 18


def current_max_publication_year() -> int:
    """Plausible upper bound for a publication year: current year + 1.

    Articles cannot be published far in the future, so a 4-digit year that
    exceeds next year (e.g. ``2048`` pulled from an article identifier) is
    treated as non-year noise rather than a publication date.
    """
    return datetime.now(UTC).year + 1


class ConnectorFetchError(Exception):
    """Raised when source content is blocked, invalid, or unavailable."""


@dataclass(frozen=True)
class SourceProfile:
    """Source Profile class."""

    source_key: str
    search_url: str
    mode: str = "html"
    query_param: str = "q"
    result_selector: str = "article, .result, .search-result, li, .item"
    link_selector: str = "a[href]"
    title_selector: str = "h1, h2, h3, .title, .article-title, a[href]"
    abstract_selector: str = ".abstract, .summary, p"
    journal_selector: str = ".journal, .source, .pub, .citation"
    peer_review_evidence: str = "peer reviewed"
    indexing_evidence: str = "scopus web of science"
    preprint_evidence: str = "journal article"
    language: str = "en"


@dataclass
class RawArticle:
    """Raw Article class."""

    source_key: str
    title: str
    url: str
    abstract: str
    full_text: str
    language: str
    year: int | None
    doi: str
    journal: str
    authors: tuple[str, ...] = ()
    volume: str = ""
    issue: str = ""
    pages: str = ""
    peer_review_evidence: str = ""
    indexing_evidence: str = ""
    preprint_evidence: str = ""


class BaseConnector:
    """Base source connector for HTML-mode sources (uses cloudscraper)."""

    profile: SourceProfile
    REQUEST_TIMEOUT_SECONDS = 25
    MAX_ATTEMPTS = 3
    _header_generator: HeaderGenerator | None = None

    def fetch(self, query: str, limit: int = 5) -> list[RawArticle]:
        """Fetch records from the upstream source."""
        if self.profile.mode == "api":
            return self._fetch_api(query, limit)
        if self.profile.mode == "ws":
            return self._fetch_ws(query, limit)
        return self._fetch_html(query, limit)

    def enrich_raw(self, raw: RawArticle) -> RawArticle:
        """Enrich the raw source payload with parsed metadata."""
        if not raw.url.startswith("http"):
            return raw
        try:
            html = self._request_text(
                raw.url,
                ocr_language=self._ocr_language(raw.language),
            )
            soup = self._sanitize_html_soup(BeautifulSoup(html, "lxml"))
        except (ValueError, RuntimeError, ConnectionError, TimeoutError):
            logger.warning(
                "%s: enrich_raw request failed for %s",
                self.profile.source_key,
                raw.url,
                exc_info=True,
            )
            return raw

        meta_text = normalize_scholarly_text(
            self._extract_meta_text(soup),
            max_length=12000,
        )
        body_text = self._html_text(soup)
        page_sample = body_text[:20000]
        combined_page_text = f"{meta_text} {page_sample}"
        if self._looks_like_challenge_page(combined_page_text):
            msg = (
                f"{self.profile.source_key}: challenge page returned for"
                " article landing page"
            )
            raise ConnectorFetchError(
                msg,
            )

        pdf_url = self._extract_pdf_url(
            soup,
            raw.url,
            raw.full_text,
            combined_page_text,
        )
        pdf_text = ""
        if pdf_url and pdf_url != raw.url:
            try:
                pdf_text = self._request_pdf_text(
                    pdf_url,
                    ocr_language=self._ocr_language(raw.language),
                )
            except (ValueError, RuntimeError, ConnectionError, TimeoutError):
                logger.warning(
                    "%s: PDF extraction failed for %s",
                    self.profile.source_key,
                    pdf_url,
                    exc_info=True,
                )
                pdf_text = ""

        doi = raw.doi or self._extract_doi(f"{combined_page_text} {pdf_text}")
        year = raw.year or self._extract_year(f"{combined_page_text} {pdf_text}")
        journal = raw.journal
        if journal.upper() == raw.source_key.upper():
            journal = (
                self._extract_meta_content(
                    soup,
                    [
                        "citation_journal_title",
                        "dc.source",
                        "dc.Source",
                        "prism.publicationname",
                        "og:site_name",
                    ],
                )
                or journal
            )
        abstract = normalize_scholarly_text(
            raw.abstract
            or self._extract_meta_content(
                soup,
                [
                    "citation_abstract",
                    "description",
                    "dc.description",
                    "og:description",
                ],
            ),
            max_length=8000,
        )

        peer_review_evidence = self._merge_evidence(
            raw.peer_review_evidence,
            combined_page_text,
            PEER_REVIEW_TOKENS,
        )
        indexing_evidence = self._merge_evidence(
            raw.indexing_evidence,
            combined_page_text,
            INDEXING_TOKENS,
        )
        preprint_evidence = self._merge_evidence(
            raw.preprint_evidence,
            f"{combined_page_text} {pdf_text}",
            PREPRINT_TOKENS,
        )
        source_text = pdf_text or body_text
        if source_text or raw.doi:
            try:
                from apps.ingestion.fulltext_resolver import (  # noqa: PLC0415  # lazy import to avoid circular dependency
                    LawfulFullTextResolver,
                )

                source_text = LawfulFullTextResolver(self).resolve(
                    raw,
                    existing_text=source_text,
                )
            except (ImportError, ValueError, RuntimeError, AttributeError):
                logger.warning(
                    "%s: fulltext_resolver failed for %s",
                    self.profile.source_key,
                    raw.url,
                    exc_info=True,
                )
        if source_text:
            full_text = normalize_scholarly_text(
                f"{raw.title} {source_text}",
            )
        else:
            full_text = normalize_scholarly_text(
                f"{raw.full_text} {page_sample}",
            )
        return replace(
            raw,
            doi=doi,
            year=year,
            journal=normalize_scholarly_text(journal, max_length=300),
            abstract=(abstract or "")[:8000],
            full_text=full_text,
            peer_review_evidence=peer_review_evidence[:3000],
            indexing_evidence=indexing_evidence[:3000],
            preprint_evidence=preprint_evidence[:3000],
        )

    @staticmethod
    def _build_scraper() -> cloudscraper.CloudScraper:
        """Build scraper."""
        kwargs: dict = {
            "interpreter": "native",
            "browser": {"browser": "chrome", "platform": "linux", "mobile": False},
            "delay": 8,
        }
        captcha_provider = os.getenv("CLOUDSCRAPER_CAPTCHA_PROVIDER", "").strip()
        captcha_key = os.getenv("CLOUDSCRAPER_CAPTCHA_API_KEY", "").strip()
        if captcha_provider and captcha_key:
            kwargs["captcha"] = {
                "provider": captcha_provider,
                "api_key": captcha_key,
            }
        return cloudscraper.create_scraper(**kwargs)

    @classmethod
    def _get_header_generator(cls) -> HeaderGenerator:
        """Return header generator."""
        if cls._header_generator is None:
            cls._header_generator = HeaderGenerator(
                browser=[
                    Browser(name="chrome", min_version=120),
                    Browser(name="firefox", min_version=120),
                    Browser(name="edge", min_version=120),
                ],
            )
        return cls._header_generator

    @classmethod
    def _generate_headers(cls) -> dict[str, str]:
        """Generate browser-like request headers."""
        headers = cls._get_header_generator().generate()
        headers.setdefault("Accept", "text/html,application/xhtml+xml,*/*")
        return dict(headers)

    @classmethod
    def _looks_like_challenge_page(cls, text: str) -> bool:
        """Detect Cloudflare or similar challenge pages."""
        lowered = (text or "").lower()
        markers = (
            "checking your browser",
            "please wait",
            "cf-browser-verification",
            "cf_chl_opt",
            "challenge-platform",
            "enable javascript",
            "just a moment",
            "attention required",
            "cloudflare",
            "ray id",
            "are you a robot",
            "are you human",
            "verify you are human",
            "please verify",
            "security check",
            "checking your connection",
            "proof-of-work",
            "proof-of-work scheme",
        )
        return any(marker in lowered for marker in markers)

    def _challenge_retry(
        self,
        scraper: cloudscraper.CloudScraper,
        url: str,
        params: dict[str, str] | None,
    ) -> str | None:
        """Retry a Cloudflare challenge by fetching tokens."""
        try:
            tokens, user_agent = cloudscraper.get_tokens(
                url,
                browser={"browser": "chrome", "platform": "linux", "mobile": False},
            )
            headers = self._generate_headers()
            headers["User-Agent"] = user_agent
            headers = {
                **headers,
                "Referer": url,
            }
            response = scraper.get(
                url,
                params=params,
                headers=headers,
                cookies=tokens,
                timeout=self.REQUEST_TIMEOUT_SECONDS,
            )
            if int(response.status_code) >= HTTP_ERROR_THRESHOLD:
                return None
        except (ValueError, RuntimeError, ConnectionError, TimeoutError):
            # pragma: no cover - network dependent
            return None
        else:
            return response.text

    def _challenge_retry_cookie_string(
        self,
        scraper: cloudscraper.CloudScraper,
        url: str,
        params: dict[str, str] | None,
    ) -> str | None:
        """Retry a Cloudflare challenge by fetching a cookie string."""
        try:
            generated_headers = self._generate_headers()
            cookie_header, user_agent = cloudscraper.get_cookie_string(
                url,
                user_agent=generated_headers.get("User-Agent"),
            )
            headers = {
                **generated_headers,
                "User-Agent": user_agent,
                "Referer": url,
                "Cookie": cookie_header,
            }
            response = scraper.get(
                url,
                params=params,
                headers=headers,
                timeout=self.REQUEST_TIMEOUT_SECONDS,
            )
            if int(response.status_code) >= HTTP_ERROR_THRESHOLD:
                return None
        except (ValueError, RuntimeError, ConnectionError, TimeoutError):
            # pragma: no cover - network dependent
            return None
        else:
            return response.text

    def _resolve_cloudflare_challenge(
        self,
        scraper: cloudscraper.CloudScraper,
        url: str,
        params: dict[str, str] | None,
        body: str,
        status: int,
    ) -> tuple[str, int]:
        """Attempt to resolve a Cloudflare challenge page.

        Returns the resolved body and status code, or the original
        body and status if no challenge was detected.
        """
        if status == HTTP_FORBIDDEN_STATUS or self._looks_like_challenge_page(body):
            solved = self._challenge_retry(scraper, url, params)
            if not solved:
                solved = self._challenge_retry_cookie_string(scraper, url, params)
            if solved:
                return solved, 200
        return body, status

    def _request_response(
        self,
        url: str,
        params: dict[str, str] | None = None,
        accept: str | None = None,
    ) -> tuple[cloudscraper.CloudScraper, object, str]:
        """Send a request for response."""
        scraper = self._build_scraper()
        last_error: Exception | None = None
        for attempt in range(1, self.MAX_ATTEMPTS + 1):
            try:
                headers = self._generate_headers()
                if accept:
                    headers.setdefault("Accept", accept)
                response = scraper.get(
                    url,
                    params=params,
                    headers=headers,
                    timeout=self.REQUEST_TIMEOUT_SECONDS,
                )
                status = int(response.status_code)
                body = response.text
                body, status = self._resolve_cloudflare_challenge(
                    scraper,
                    url,
                    params,
                    body,
                    status,
                )
                if status >= HTTP_ERROR_THRESHOLD:
                    msg = f"{self.profile.source_key}: http {status}"
                    raise ConnectorFetchError(msg)  # noqa: TRY301  # contextual validation raise
                if self._looks_like_challenge_page(body):
                    msg = f"{self.profile.source_key}: cloudflare challenge unresolved"
                    raise ConnectorFetchError(msg)  # noqa: TRY301  # contextual validation raise
            except ConnectorFetchError:
                raise
            except (ValueError, RuntimeError, ConnectionError, TimeoutError) as exc:
                # pragma: no cover - network dependent
                last_error = exc
                if attempt < self.MAX_ATTEMPTS:
                    time.sleep(0.6 * attempt)
            else:
                return scraper, response, body
        msg = f"{self.profile.source_key}: request failed after retries: {last_error}"
        raise ConnectorFetchError(
            msg,
        )

    @staticmethod
    def _is_pdf_response(url: str, content_type: str, body: bytes) -> bool:
        """Return whether PDF response."""
        content_type_lower = content_type.lower()
        url_lower = url.lower()
        return (
            url_lower.endswith(".pdf")
            or "application/pdf" in content_type_lower
            or body.startswith(b"%PDF")
        )

    @staticmethod
    def _extract_pdf_text(pdf_bytes: bytes) -> str:
        """Extract PDF text."""
        return BaseConnector._extract_pdf_text_with_language(
            pdf_bytes,
            ocr_language="eng",
        )

    @staticmethod
    def _extract_pdf_text_with_language(
        pdf_bytes: bytes,
        *,
        ocr_language: str,
    ) -> str:
        """Extract PDF text with an OCR language hint."""
        try:
            document = pymupdf.open(stream=pdf_bytes, filetype="pdf")
        except (ValueError, RuntimeError, OSError):
            return ""
        parts: list[str] = []
        for page in document:
            try:
                page_text = page.get_text("text", sort=True).strip()
            except (ValueError, RuntimeError):
                page_text = ""
            if not page_text or "\ufffd" in page_text:
                try:
                    page_text = page.get_textpage_ocr(language=ocr_language)
                    page_text = page.get_text(textpage=page_text).strip()
                except (ValueError, RuntimeError, OSError):
                    page_text = ""
            if page_text:
                parts.append(page_text)
        return normalize_scholarly_text(" ".join(parts))

    @staticmethod
    def _ocr_language(language: str) -> str:
        """Map a source language to a Tesseract OCR language code."""
        normalized = normalize_scholarly_text(language).lower().strip()
        if not normalized:
            return "eng"
        return OCR_LANGUAGE_MAP.get(
            normalized,
            OCR_LANGUAGE_MAP.get(normalized[:2], "eng"),
        )

    def _request_text(
        self,
        url: str,
        params: dict[str, str] | None = None,
        *,
        ocr_language: str = "eng",
    ) -> str:
        """Send a request for text."""
        _, response, body_text = self._request_response(url, params=params)
        content_type = str(response.headers.get("Content-Type", ""))
        body_bytes = bytes(response.content or b"")
        if self._is_pdf_response(url, content_type, body_bytes):
            return self._extract_pdf_text_with_language(
                body_bytes,
                ocr_language=ocr_language,
            )
        return body_text

    def _request_pdf_text(
        self,
        url: str,
        *,
        ocr_language: str = "eng",
    ) -> str:
        """Fetch a PDF resource and extract its textual content."""
        _, response, body_text = self._request_response(
            url,
            params=None,
            accept="application/pdf,*/*",
        )
        content_type = str(response.headers.get("Content-Type", ""))
        body_bytes = bytes(response.content or b"")
        if self._is_pdf_response(url, content_type, body_bytes):
            return self._extract_pdf_text_with_language(
                body_bytes,
                ocr_language=ocr_language,
            )
        if body_text:
            return normalize_scholarly_text(body_text)
        return ""

    def _fetch_html(self, query: str, limit: int) -> list[RawArticle]:
        """Fetch HTML."""
        html = self._request_text(
            self.profile.search_url,
            params={self.profile.query_param: query},
        )
        soup = self._sanitize_html_soup(BeautifulSoup(html, "lxml"))
        self._assert_page_is_parseable(html, soup)
        return self._extract_from_html(query, soup, limit)

    def _request_json(self, url: str) -> dict:
        """Send a request for JSON."""
        last_error: Exception | None = None
        for attempt in range(1, self.MAX_ATTEMPTS + 1):
            try:
                _, _, body = self._request_response(
                    url,
                    params=None,
                    accept="application/json,text/plain,*/*",
                )
                payload = json.loads(body)
                if not isinstance(payload, dict):
                    msg = f"{self.profile.source_key}: invalid JSON payload type"
                    raise ConnectorFetchError(msg)  # noqa: TRY301  # contextual validation raise
            except ConnectorFetchError:
                raise
            except (ValueError, RuntimeError, ConnectionError, TimeoutError) as exc:
                last_error = exc
                if attempt < self.MAX_ATTEMPTS:
                    time.sleep(0.6 * attempt)
            else:
                return payload
        msg = (
            f"{self.profile.source_key}: json request failed after retries:"
            f" {last_error}"
        )
        raise ConnectorFetchError(
            msg,
        )

    def _fetch_api(self, query: str, limit: int) -> list[RawArticle]:
        """Fetch API."""
        payload = self._request_json(self._api_url(query, limit))
        return self._extract_from_payload(query, payload, limit)

    def _fetch_ws(self, query: str, limit: int) -> list[RawArticle]:  # noqa: ARG002  # required by base class signature
        """Fetch websocket source payload."""
        msg = f"{self.profile.source_key}: websocket transport is not implemented"
        raise ConnectorFetchError(
            msg,
        )

    def _api_url(self, query: str, limit: int) -> str:
        """Build the API URL for a search query."""
        return (
            f"{self.profile.search_url}"
            f"?{self.profile.query_param}={quote_plus(query)}"
            f"&pageSize={limit}"
        )

    @staticmethod
    def _flatten_json_ld_payload(payload: dict | list) -> list[dict]:
        """Flatten a JSON-LD payload into a list of record dicts."""
        records: list[dict] = []
        if isinstance(payload, dict):
            graph = payload.get("@graph")
            if isinstance(graph, list):
                records.extend([x for x in graph if isinstance(x, dict)])
            records.append(payload)
        elif isinstance(payload, list):
            records.extend([x for x in payload if isinstance(x, dict)])
        return records

    def _build_raw_from_json_ld_record(self, record: dict) -> RawArticle | None:
        """Build a RawArticle from a single JSON-LD record dict.

        Returns None if the record is not article-like or lacks required fields.
        """
        schema_type = str(record.get("@type", "")).lower()
        if not any(
            token in schema_type
            for token in ("article", "scholarlyarticle", "creativework")
        ):
            return None
        title = str(record.get("headline") or record.get("name") or "").strip()
        url_value = str(
            record.get("url") or record.get("mainEntityOfPage") or "",
        ).strip()
        abstract = str(record.get("description") or "").strip()
        journal_info = record.get("isPartOf")
        if isinstance(journal_info, dict):
            journal = str(
                journal_info.get("name") or journal_info.get("headline") or "",
            ).strip()
        else:
            journal = str(journal_info or "").strip()
        date_published = str(record.get("datePublished") or "")
        doi = self._extract_doi(
            " ".join(
                [
                    title,
                    abstract,
                    str(record.get("identifier", "")),
                    str(record.get("sameAs", "")),
                ],
            ),
        )
        year = self._extract_year(date_published) or self._extract_year(
            f"{title} {abstract}",
        )
        if not title or not url_value:
            return None
        return self._raw(
            title=title,
            url=url_value,
            abstract=abstract,
            full_text=f"{title} {abstract} {journal}",
            doi=doi,
            year=year,
            journal=journal or self.profile.source_key.upper(),
        )

    def _extract_json_ld_articles(
        self,
        soup: BeautifulSoup,
        limit: int,
    ) -> list[RawArticle]:
        """Extract JSON-LD structured data articles from HTML."""
        items: list[RawArticle] = []
        for script in soup.select("script[type='application/ld+json']"):
            body = script.string or script.get_text(strip=True)
            if not body:
                continue
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                continue
            records = self._flatten_json_ld_payload(payload)
            for record in records:
                article = self._build_raw_from_json_ld_record(record)
                if article is not None:
                    items.append(article)
                    if len(items) >= limit:
                        return items
        return items

    def _assert_page_is_parseable(self, raw_html: str, soup: BeautifulSoup) -> None:
        """Assert page is parseable."""
        text = self._html_text(soup).lower()
        if any(marker in text for marker in ("verify you are human", "captcha")):
            msg = f"{self.profile.source_key}: blocked by verification challenge"
            raise ConnectorFetchError(
                msg,
            )
        page_title = soup.title.get_text(strip=True).lower() if soup.title else ""
        if "404" in page_title and "not found" in page_title:
            msg = f"{self.profile.source_key}: search page not found"
            raise ConnectorFetchError(
                msg,
            )
        if not raw_html.strip():
            msg = f"{self.profile.source_key}: empty response body"
            raise ConnectorFetchError(msg)

    def _extract_from_html(
        self,
        query: str,
        soup: BeautifulSoup,
        limit: int,
    ) -> list[RawArticle]:
        """Extract from HTML."""
        rows = soup.select(self.profile.result_selector)
        items: list[RawArticle] = []
        for row in rows:
            parsed = self._build_from_row(query, row)
            if not parsed:
                continue
            items.append(parsed)
            if len(items) >= limit:
                break
        if items:
            return items
        return []

    @staticmethod
    def _sanitize_html_soup(soup: BeautifulSoup) -> BeautifulSoup:
        """Remove boilerplate tags."""
        for tag in soup.find_all(HTML_BOILERPLATE_TAGS):
            tag.decompose()
        return soup

    @staticmethod
    def _html_text(soup: BeautifulSoup) -> str:
        """Render sanitized HTML text."""
        return normalize_scholarly_text(" ".join(soup.stripped_strings))

    def _build_from_row(self, query: str, row: Tag) -> RawArticle | None:  # noqa: ARG002  # required by base class signature
        """Build a RawArticle from an HTML row."""
        title_node = row.select_one(self.profile.title_selector)
        link_node = row.select_one(self.profile.link_selector)
        abstract_node = row.select_one(self.profile.abstract_selector)
        journal_node = row.select_one(self.profile.journal_selector)
        if not title_node or not link_node:
            return None
        title = title_node.get_text(" ", strip=True)
        href = urljoin(self.profile.search_url, link_node.get("href", ""))
        abstract = abstract_node.get_text(" ", strip=True) if abstract_node else ""
        journal = (
            journal_node.get_text(" ", strip=True)
            if journal_node
            else self.profile.source_key.upper()
        )
        combined = f"{title} {abstract} {journal}"
        doi = self._extract_doi(combined)
        year = self._extract_year(combined)
        return self._raw(
            title=title,
            url=href,
            abstract=abstract,
            full_text=combined,
            doi=doi,
            year=year,
            journal=journal,
        )

    def _raw(  # noqa: PLR0913  # connector interface requires these params
        self,
        *,
        title: str,
        url: str,
        abstract: str,
        full_text: str,
        doi: str,
        year: int | None,
        journal: str,
        authors: tuple[str, ...] | list[str] | None = None,
        volume: str = "",
        issue: str = "",
        pages: str = "",
        peer_review_evidence: str = "",
        indexing_evidence: str = "",
        preprint_evidence: str = "",
    ) -> RawArticle:
        """Build a RawArticle instance."""
        return RawArticle(
            source_key=self.profile.source_key,
            title=normalize_scholarly_text(title, max_length=900),
            url=url,
            abstract=normalize_scholarly_text(abstract, max_length=8000),
            full_text=normalize_scholarly_text(full_text),
            language=self.profile.language,
            year=year,
            doi=normalize_scholarly_text(doi, max_length=128),
            journal=normalize_scholarly_text(journal, max_length=300),
            authors=tuple(
                item.strip()
                for item in (authors or ())
                if isinstance(item, str) and item.strip()
            ),
            volume=normalize_scholarly_text(volume, max_length=32),
            issue=normalize_scholarly_text(issue, max_length=32),
            pages=normalize_scholarly_text(pages, max_length=32),
            peer_review_evidence=peer_review_evidence,
            indexing_evidence=indexing_evidence,
            preprint_evidence=preprint_evidence,
        )

    @staticmethod
    def _extract_doi(text: str) -> str:
        """Extract DOI from text."""
        found = DOI_PATTERN.search(text or "")
        return found.group(0).rstrip(".") if found else ""

    @staticmethod
    def _extract_year(text: str) -> int | None:
        """Extract the most plausible publication year from text.

        Scans every ``(19|20)xx`` match and returns the most recent one within
        ``[MIN_PUBLICATION_YEAR, current_max_publication_year()]``. Returning
        the maximum avoids mistaking an incidental 4-digit number (article
        identifiers, citation years) for the publication date while still
        tolerating older reference years present in the same text.
        """
        max_year = current_max_publication_year()
        plausible = [
            int(m.group(0))
            for m in YEAR_PATTERN.finditer(text or "")
            if MIN_PUBLICATION_YEAR <= int(m.group(0)) <= max_year
        ]
        if not plausible:
            return None
        return max(plausible)

    def _extract_pdf_url(
        self,
        soup: BeautifulSoup,
        *blobs: str,
    ) -> str:
        """Extract a likely PDF URL from metadata, anchors, or raw text blobs."""
        meta_keys = (
            "citation_pdf_url",
            "citation_pdfurl",
            "dc.identifier",
            "dc.source",
            "prism.url",
            "og:url",
        )
        for key in meta_keys:
            value = self._extract_meta_content(soup, [key]).strip()
            if value.lower().endswith(".pdf") and value.startswith("http"):
                return value
        for link in soup.select("a[href]"):
            href = urljoin(self.profile.search_url, link.get("href", ""))
            label = link.get_text(" ", strip=True).lower()
            href_lower = href.lower()
            if (
                href_lower.endswith(".pdf") or "pdf" in href_lower or "pdf" in label
            ) and href.startswith("http"):
                return href
        for blob in blobs:
            found = PDF_URL_PATTERN.search(blob or "")
            if found:
                return found.group(0)
        return ""

    @staticmethod
    def _query_tokens(query: str) -> list[str]:
        """Query tokens."""
        return [
            x.lower()
            for x in re.findall(r"[a-zA-Zа-яА-Я0-9]+", query)  # noqa: RUF001
            if len(x) > 2  # noqa: PLR2004  # min token length
        ]

    @classmethod
    def _matches_query(cls, text: str, query: str) -> bool:
        """Return True when every query token appears in ``text``.

        Used to filter OAI-PMH harvests (which cannot keyword-search) down to
        records plausibly relevant to the query, so that sources without a
        search API do not return off-topic garbage. All tokens must match to
        keep precision high; an empty token list (e.g. very short query) is
        treated as a match so the source still returns results.
        """
        tokens = cls._query_tokens(query)
        if not tokens:
            return True
        lowered = (text or "").lower()
        return all(token in lowered for token in tokens)

    @staticmethod
    def _extract_meta_content(soup: BeautifulSoup, keys: list[str]) -> str:
        """Extract meta content."""
        lowered_keys = {k.lower() for k in keys}
        for meta in soup.select("meta[name], meta[property]"):
            name = (meta.get("name") or meta.get("property") or "").strip().lower()
            if name in lowered_keys:
                content = (meta.get("content") or "").strip()
                if content:
                    return content
        return ""

    @classmethod
    def _extract_meta_text(cls, soup: BeautifulSoup) -> str:
        """Extract meta text."""
        values: list[str] = []
        for meta in soup.select("meta[name], meta[property]"):
            content = (meta.get("content") or "").strip()
            if content:
                values.append(content)
        return " ".join(values)

    @staticmethod
    def _merge_evidence(base: str, page_text: str, tokens: tuple[str, ...]) -> str:
        """Merge evidence."""
        existing = (base or "").strip()
        lower_page = (page_text or "").lower()
        found = [token for token in tokens if token in lower_page]
        if not found:
            return existing
        merged = existing
        for token in found:
            if token.lower() not in merged.lower():
                merged = f"{merged} {token}".strip()
        return merged

    @staticmethod
    def _is_article_like_item(title: str, url: str, doi: str, year: int | None) -> bool:
        """Return whether article like item."""
        if not title or len(title.strip()) < MIN_ARTICLE_TITLE_LENGTH:
            return False
        bad_title_tokens = {
            "browse",
            "advanced search",
            "journal collections",
            "home",
            "login",
            "about",
            "help",
        }
        normalized = title.strip().lower()
        if normalized in bad_title_tokens:
            return False
        if not url.startswith("http"):
            return False
        return bool(doi or year)

    def _build_openalex_record(
        self,
        rec: dict,
        default_journal: str,
    ) -> RawArticle | None:
        """Build a RawArticle from a single OpenAlex API record.

        Returns None if the record lacks required fields.
        """
        title = str(rec.get("display_name") or rec.get("title") or "").strip()
        doi_url = str(rec.get("doi") or "").strip()
        doi = doi_url.replace("https://doi.org/", "").replace("http://doi.org/", "")
        primary_location = rec.get("primary_location") or {}
        if not isinstance(primary_location, dict):
            primary_location = {}
        source = primary_location.get("source") or {}
        if not isinstance(source, dict):
            source = {}
        url_value = str(
            primary_location.get("landing_page_url")
            or primary_location.get("pdf_url")
            or doi_url
            or "",
        ).strip()
        journal = str(source.get("display_name") or default_journal).strip()
        abstract = ""
        abstract_index = rec.get("abstract_inverted_index")
        if isinstance(abstract_index, dict):
            abstract = BaseConnector._build_abstract_from_index(abstract_index)
        year_raw = rec.get("publication_year")
        year = int(year_raw) if str(year_raw).isdigit() else None
        combined = " ".join([title, abstract, journal, str(rec.get("language") or "")])
        if not title or not url_value.startswith("http"):
            return None
        if not self._is_article_like_item(title, url_value, doi, year):
            return None
        return self._raw(
            title=title,
            url=url_value,
            abstract=abstract,
            full_text=combined,
            doi=doi,
            year=year,
            journal=journal,
        )

    @staticmethod
    def _build_abstract_from_index(index: dict) -> str:
        """Build abstract from OpenAlex inverted index."""
        tokens: list[tuple[int, str]] = []
        for word, positions in index.items():
            if not isinstance(word, str) or not isinstance(positions, list):
                continue
            tokens.extend((pos, word) for pos in positions if isinstance(pos, int))
        if not tokens:
            return ""
        tokens.sort(key=lambda x: x[0])
        return " ".join(word for _, word in tokens)[:8000]


class AsyncApiConnector(BaseConnector):
    """Base connector for API-mode sources using aiohttp instead of cloudscraper.

    API-mode connectors do not need Cloudflare challenge handling.
    They use aiohttp for all HTTP requests (both async and sync contexts).
    """

    profile: SourceProfile

    def fetch(self, query: str, limit: int = 5) -> list[RawArticle]:
        """Fetch records from the upstream API source."""
        return asyncio.run(self._fetch_async(query, limit))

    async def _fetch_async(self, query: str, limit: int) -> list[RawArticle]:
        """Async fetch using aiohttp."""
        url = self._api_url(query, limit)
        try:
            async with (
                aiohttp.ClientSession() as session,
                session.get(
                    url,
                    timeout=aiohttp.ClientTimeout(
                        total=self.REQUEST_TIMEOUT_SECONDS,
                    ),
                ) as response,
            ):
                response.raise_for_status()
                payload = await response.json()
        except aiohttp.ClientResponseError as exc:
            status = getattr(exc, "status", None) or getattr(exc, "code", None)
            msg = f"{self.profile.source_key}: HTTP {status} for {url}: {exc}"
            raise ConnectorFetchError(
                msg,
            ) from exc
        except aiohttp.ClientError as exc:
            msg = f"{self.profile.source_key}: request failed: {exc}"
            raise ConnectorFetchError(
                msg,
            ) from exc
        if not isinstance(payload, dict):
            msg = f"{self.profile.source_key}: invalid JSON payload type"
            raise ConnectorFetchError(
                msg,
            )
        return self._extract_from_payload(query, payload, limit)

    def _api_url(self, query: str, limit: int) -> str:
        """Build the API URL for a search query."""
        return (
            f"{self.profile.search_url}"
            f"?{self.profile.query_param}={quote_plus(query)}"
            f"&pageSize={limit}"
        )

    def _extract_from_payload(
        self,
        query: str,  # noqa: ARG002  # required by base class signature
        payload: dict,  # noqa: ARG002  # required by base class signature
        limit: int,  # noqa: ARG002  # required by base class signature
    ) -> list[RawArticle]:
        """Parse API response payload into RawArticle list."""
        return []

    def enrich_raw(self, raw: RawArticle) -> RawArticle:
        """Return raw unchanged; API connectors provide complete metadata."""
        return raw
