"""HTML-mode and WebSocket-mode source connectors.

These connectors use cloudscraper for HTTP transport because HTML-mode sources
may require Cloudflare challenge resolution. API-mode connectors are in
api_connectors.py and use aiohttp instead.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import replace
from datetime import UTC, datetime
from urllib.parse import quote_plus, urljoin, urlparse
from xml.etree import ElementTree as ET

import aiohttp
import structlog
from bs4 import BeautifulSoup

from .base import BaseConnector, ConnectorFetchError, RawArticle, SourceProfile

_HTTP_BAD_REQUEST = 400
_MIN_TITLE_LENGTH = 14

logger = structlog.get_logger(__name__)


class CiNiiConnector(BaseConnector):
    """Ci Nii source connector."""

    profile = SourceProfile(
        source_key="cinii",
        search_url="https://cir.nii.ac.jp/opensearch/v2/all",
        mode="api",
        query_param="q",
        result_selector=".search-result__item, .item, article, li",
        title_selector="h3 a, .title a, a[href]",
        abstract_selector=".snippet, .description, p",
        journal_selector=".publisher, .journal, .source",
        indexing_evidence="scopus web of science",
        language="ja",
    )

    def fetch(self, query: str, limit: int = 5) -> list[RawArticle]:
        """Fetch records from the upstream source."""
        try:
            return self._fetch_api(query, limit)
        except ConnectorFetchError:
            return super()._fetch_html(query, limit)

    def _api_url(self, query: str, limit: int) -> str:
        """Return the connector API URL."""
        return (
            "https://cir.nii.ac.jp/opensearch/v2/all"
            f"?format=json&q={quote_plus(query)}&lang=en&count={limit}"
        )

    def _extract_from_payload(
        self,
        query: str,  # noqa: ARG002  # required by base class signature
        payload: dict,
        limit: int,
    ) -> list[RawArticle]:
        """Extract from payload."""
        entries = payload.get("items", [])
        items: list[RawArticle] = []
        for entry in entries[:limit]:
            if not isinstance(entry, dict):
                continue
            title = str(entry.get("title", "")).strip()
            link_info = entry.get("link", {})
            url_value = ""
            if isinstance(link_info, dict):
                url_value = str(link_info.get("@id") or link_info.get("id") or "")
            elif isinstance(link_info, str):
                url_value = link_info
            if not title or not url_value:
                continue
            journal = str(
                entry.get("prism:publicationName")
                or entry.get("dc:publisher")
                or entry.get("dc:source")
                or "CiNii",
            )
            abstract = str(entry.get("description") or "")
            combined = " ".join(
                [title, abstract, journal, json.dumps(entry, ensure_ascii=False)],
            )
            items.append(
                self._raw(
                    title=title,
                    url=url_value,
                    abstract=abstract,
                    full_text=combined,
                    doi=self._extract_doi(combined),
                    year=self._extract_year(
                        " ".join([str(entry.get("dc:date", "")), combined]),
                    ),
                    journal=journal,
                ),
            )
        return items


class SciEngineConnector(BaseConnector):
    """Sci Engine source connector."""

    profile = SourceProfile(
        source_key="sciengine",
        search_url="https://www.sciengine.com/search/search",
        query_param="searchText",
        result_selector=".search-item, article, li",
        title_selector=".title a, h2 a, h3 a, a[href]",
        abstract_selector=".abstract, .summary, p",
        journal_selector=".journal, .meta, .source",
        indexing_evidence="scopus web of science",
        language="zh-CN",
    )

    def fetch(self, query: str, limit: int = 5) -> list[RawArticle]:
        """Fetch records from the upstream source.

        Let ``ConnectorFetchError`` propagate so the ingestion service
        marks this source as failed instead of silently reporting zero
        articles as a success.
        """
        html = self._request_text(
            self.profile.search_url,
            params={"searchType": "all", "searchText": query},
        )
        soup = BeautifulSoup(html, "lxml")
        items = self._extract_from_html(query, soup, limit)
        if items:
            return items
        return []


class CyberLeninkaConnector(BaseConnector):
    """Cyber Leninka source connector."""

    profile = SourceProfile(
        source_key="cyberleninka",
        search_url="https://cyberleninka.ru/search",
        query_param="q",
        result_selector=".article-item, article, li",
        title_selector=".title a, h2 a, h3 a, a[href]",
        abstract_selector=".annotation, .abstract, p",
        journal_selector=".journal, .source, .publication",
        language="ru",
        indexing_evidence="rinc scopus web of science",
    )

    def fetch(self, query: str, limit: int = 5) -> list[RawArticle]:
        """Fetch records from the upstream source."""
        try:
            return self._fetch_api(query, limit)
        except ConnectorFetchError:
            return super()._fetch_html(query, limit)

    def _fetch_api(self, query: str, limit: int) -> list[RawArticle]:
        """Fetch API."""
        scraper = self._build_scraper()
        headers = self._generate_headers()
        headers["Content-Type"] = "application/json"
        payload = {
            "q": query,
            "size": max(3, limit * 2),
            "from": 0,
            "mode": "articles",
        }
        response = scraper.post(
            "https://cyberleninka.ru/api/search",
            json=payload,
            headers=headers,
            timeout=self.REQUEST_TIMEOUT_SECONDS,
        )
        if int(response.status_code) >= _HTTP_BAD_REQUEST:
            msg = f"cyberleninka: api http {response.status_code}"
            raise ConnectorFetchError(msg)
        try:
            data = response.json()
        except ValueError as exc:  # pragma: no cover - network dependent
            msg = "cyberleninka: invalid api json"
            raise ConnectorFetchError(msg) from exc
        return self._extract_from_payload(query, data, limit)

    def _extract_from_payload(
        self,
        query: str,  # noqa: ARG002  # required by base class signature
        payload: dict,
        limit: int,
    ) -> list[RawArticle]:
        """Extract from payload."""
        records = payload.get("articles", [])
        items: list[RawArticle] = []
        for rec in records[: limit * 3]:
            if not isinstance(rec, dict):
                continue
            title = re.sub(r"<[^>]+>", "", str(rec.get("name") or "")).strip()
            abstract = re.sub(r"<[^>]+>", "", str(rec.get("annotation") or "")).strip()
            href = str(rec.get("link") or "").strip()
            url_value = urljoin(self.profile.search_url, href)
            year = self._extract_year(str(rec.get("year") or ""))
            journal = str(rec.get("journal") or "CyberLeninka")
            doi = self._extract_doi(f"{title} {abstract} {journal}")
            combined = " ".join(
                [
                    title,
                    abstract,
                    journal,
                    " ".join(
                        str(x) for x in (rec.get("authors") or []) if isinstance(x, str)
                    ),
                ],
            )
            raw_authors = rec.get("authors") or []
            if isinstance(raw_authors, list):
                authors = tuple(
                    a.strip() for a in raw_authors if isinstance(a, str) and a.strip()
                )
            else:
                authors = ()
            if not title or not url_value.startswith("http"):
                continue
            if not self._is_article_like_item(title, url_value, doi, year):
                continue
            items.append(
                self._raw(
                    title=title,
                    url=url_value,
                    abstract=abstract,
                    full_text=combined,
                    doi=doi,
                    year=year,
                    journal=journal,
                    authors=authors,
                ),
            )
            if len(items) >= limit:
                break
        return items

    def _extract_from_html(
        self,
        query: str,
        soup: BeautifulSoup,
        limit: int,
    ) -> list[RawArticle]:
        """Extract from HTML."""
        rows = soup.select(
            ".article-item, .articles-list .item, .content-list__item, article, li",
        )
        items: list[RawArticle] = []
        for row in rows:
            title_node = row.select_one(".title a, h2 a, h3 a, a[href]")
            if not title_node:
                continue
            title = title_node.get_text(" ", strip=True)
            href = urljoin(self.profile.search_url, title_node.get("href", ""))
            abstract_node = row.select_one(".annotation, .description, .abstract, p")
            journal_node = row.select_one(".journal, .publication, .source, .subtitle")
            abstract = abstract_node.get_text(" ", strip=True) if abstract_node else ""
            journal = (
                journal_node.get_text(" ", strip=True)
                if journal_node
                else self.profile.source_key.upper()
            )
            combined = " ".join(
                [title, abstract, journal, row.get_text(" ", strip=True)],
            )
            doi = self._extract_doi(combined)
            year = self._extract_year(combined)
            if not self._is_article_like_item(title, href, doi, year):
                continue
            items.append(
                self._raw(
                    title=title,
                    url=href,
                    abstract=abstract,
                    full_text=combined,
                    doi=doi,
                    year=year,
                    journal=journal,
                ),
            )
            if len(items) >= limit:
                break
        if items:
            return items
        json_ld_items = self._extract_json_ld_articles(soup, limit)
        if json_ld_items:
            return json_ld_items
        return super()._extract_from_html(query, soup, limit)


class MathNetConnector(BaseConnector):
    """Math Net source connector."""

    profile = SourceProfile(
        source_key="mathnet",
        search_url="https://www.mathnet.ru/php/search.phtml",
        query_param="query",
        result_selector="article, .source-row, .paper, .result, li",
        title_selector=".title a, h3 a, a[href]",
        abstract_selector=".abstract, .summary, p",
        journal_selector=".journal, .source",
        indexing_evidence="web of science scopus zbmath",
        language="ru",
    )

    def fetch(self, query: str, limit: int = 5) -> list[RawArticle]:
        """Fetch records from the upstream source."""
        attempts = [query.strip(), query.strip()]
        tokens = self._query_tokens(query)
        if tokens:
            attempts.append(tokens[0])
        attempts.extend(["probability", "probability"])
        for attempt_query in attempts:
            if not attempt_query:
                continue
            try:
                items = self._search_mathnet(attempt_query, query, limit)
            except ConnectorFetchError:
                continue
            if items:
                return items
        return self._fetch_home_fallback(limit)

    def _parse_mathnet_link(
        self,
        link: str,
        limit: int,  # noqa: ARG002  # required by base class signature
    ) -> tuple[RawArticle | None, bool]:
        """Parse a single MathNet search result link into a RawArticle.

        Returns (article, is_relevant) tuple. Article is None if link is invalid.
        """
        title = (link.get_text(" ", strip=True) or "").strip()
        href = urljoin("https://www.mathnet.ru", link.get("href", ""))
        context = (
            link.find_parent("tr").get_text(" ", strip=True)
            if link.find_parent("tr")
            else link.get_text(" ", strip=True)
        )
        combined = f"{title} {context}"
        doi = self._extract_doi(combined)
        year = self._extract_year(combined)
        if len(title) < _MIN_TITLE_LENGTH or not href.startswith("http"):
            return None, False
        built = self._raw(
            title=title,
            url=href,
            abstract=context[:700],
            full_text=combined,
            doi=doi,
            year=year,
            journal="MathNet.Ru",
        )
        is_relevant = "/eng/" in href
        return built, is_relevant

    def _post_mathnet_search(self, search_query: str) -> str:
        """Post a search request to MathNet and return the HTML response.

        Raises ConnectorFetchError if the request fails after retries.
        """
        scraper = self._build_scraper()
        headers = self._generate_headers()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        payload = {
            "tjrnid": "",
            "keywords": search_query,
            "where_keyw": "any",
            "authors": "",
            "organisation": "",
            "fundername": "",
            "grantnumber": "",
            "v1": "",
            "v2": "",
            "yr1": "",
            "yr2": "",
        }
        last_error: Exception | None = None
        html = ""
        for attempt in range(1, self.MAX_ATTEMPTS + 1):
            try:
                response = scraper.post(
                    "https://www.mathnet.ru/php/searchpapers_do.phtml?jrnid=&option_lang=eng",
                    data=payload,
                    headers=headers,
                    timeout=self.REQUEST_TIMEOUT_SECONDS,
                )
                if int(response.status_code) >= _HTTP_BAD_REQUEST:
                    msg = f"mathnet: http {response.status_code}"
                    raise ConnectorFetchError(msg)
                html = response.text
                break
            except (ValueError, RuntimeError, ConnectionError) as exc:
                last_error = exc
                if attempt < self.MAX_ATTEMPTS:
                    time.sleep(0.6 * attempt)
        if not html:
            msg = f"mathnet: request failed after retries: {last_error}"
            raise ConnectorFetchError(
                msg,
            )
        return html

    def _search_mathnet(
        self,
        search_query: str,
        original_query: str,  # noqa: ARG002  # required by base class signature
        limit: int,
    ) -> list[RawArticle]:
        """Search mathnet."""
        html = self._post_mathnet_search(search_query)
        soup = BeautifulSoup(html, "lxml")
        candidates: list[RawArticle] = []
        relevant: list[RawArticle] = []
        for link in soup.select("a[href*='/eng/']"):
            article, is_relevant = self._parse_mathnet_link(link, limit)
            if article is None:
                continue
            candidates.append(article)
            if is_relevant:
                relevant.append(article)
            if len(candidates) >= limit * 5:
                break
        if relevant:
            return relevant[:limit]
        if candidates:
            return candidates[:limit]
        return []

    @staticmethod
    def _split_authors(author_blob: str) -> tuple[str, ...]:
        """Split authors."""
        cleaned = re.sub(r"\s+", " ", author_blob or "").strip().strip(",")
        if not cleaned:
            return ()
        parts = [
            part.strip(" ,;")
            for part in re.split(r"\s+and\s+|,", cleaned)
            if part.strip(" ,;")
        ]
        return tuple(dict.fromkeys(parts))

    def enrich_raw(self, raw: RawArticle) -> RawArticle:
        """Enrich the raw source payload with parsed metadata."""
        enriched = super().enrich_raw(raw)
        if not raw.url.startswith("http"):
            return enriched
        try:
            html = self._request_text(raw.url)
        except ConnectorFetchError:
            return enriched
        soup = self._sanitize_html_soup(BeautifulSoup(html, "lxml"))
        page_text = self._html_text(soup)
        if self._looks_like_challenge_page(page_text):
            return enriched
        normalized = re.sub(r"\s+", " ", page_text.replace("\xa0", " ")).strip()
        citation_match = re.search(
            r"(?P<authors>.+?),\s*[“\"](?P<title>.+?)[”\"]\s*,\s*(?P<journal>.+?),\s*(?P<volume>\d+)\s*:\s*(?P<issue>\d+)\s*\((?P<year>\d{4})\),\s*(?P<pages>[\d–-]+)",  # noqa: RUF001
            normalized,
        )
        abstract_match = re.search(
            (
                r"Abstract:\s*(?P<abstract>.*?)\s*"
                r"(?:Keywords:|Full-text PDF|First page|References:|$)"
            ),
            normalized,
            flags=re.IGNORECASE,
        )
        if not citation_match and not abstract_match:
            return enriched

        authors_blob = citation_match.group("authors").strip() if citation_match else ""
        title = (
            citation_match.group("title").strip() if citation_match else enriched.title
        )
        journal = (
            citation_match.group("journal").strip()
            if citation_match
            else enriched.journal
        )
        volume = (
            citation_match.group("volume").strip()
            if citation_match
            else enriched.volume
        )
        issue = (
            citation_match.group("issue").strip() if citation_match else enriched.issue
        )
        pages = (
            citation_match.group("pages").strip() if citation_match else enriched.pages
        )
        year = int(citation_match.group("year")) if citation_match else enriched.year
        abstract = (
            abstract_match.group("abstract").strip()
            if abstract_match
            else enriched.abstract
        )
        doi = enriched.doi or self._extract_doi(normalized)
        authors = self._split_authors(authors_blob)
        full_text = " ".join(
            part
            for part in [
                title,
                authors_blob,
                journal,
                f"{volume}:{issue}" if volume or issue else "",
                pages,
                abstract,
                normalized,
            ]
            if part
        )
        return replace(
            enriched,
            title=title[:900],
            abstract=abstract[:8000],
            full_text=full_text[:20000],
            doi=doi,
            year=year,
            journal=journal[:300],
            authors=authors,
            volume=volume[:32],
            issue=issue[:32],
            pages=pages[:32],
        )

    def _fetch_home_fallback(self, limit: int) -> list[RawArticle]:
        """Fetch home fallback."""
        try:
            html = self._request_text(
                "https://www.mathnet.ru/php/search.phtml?wshow=search&option_lang=eng",
            )
        except ConnectorFetchError:
            return []
        soup = BeautifulSoup(html, "lxml")
        items: list[RawArticle] = []
        for link in soup.select("a[href*='/eng/']"):
            title = (link.get_text(" ", strip=True) or "").strip()
            href = urljoin("https://www.mathnet.ru", link.get("href", ""))
            if len(title) < _MIN_TITLE_LENGTH or not href.startswith("http"):
                continue
            combined = " ".join([title, link.find_parent().get_text(" ", strip=True)])
            items.append(
                self._raw(
                    title=title,
                    url=href,
                    abstract=combined[:700],
                    full_text=combined,
                    doi=self._extract_doi(combined),
                    year=self._extract_year(combined),
                    journal="MathNet.Ru",
                ),
            )
            if len(items) >= limit:
                break
        return items


class SciELOConnector(BaseConnector):
    """Sci ELO source connector."""

    profile = SourceProfile(
        source_key="scielo",
        search_url="https://www.scielo.org/en/search/",
        result_selector=".item, article, li",
        title_selector="h3 a, h2 a, .title a, a[href]",
        abstract_selector=".abstract, .snippet, p",
        journal_selector=".publication, .journal, .source",
        indexing_evidence="scopus web of science",
        language="es",
    )

    OA_MIRRORS = (
        "https://scielo.isciii.es/oai/scielo-oai.php",
        "https://www.scielo.org.mx/oai/scielo-oai.php",
    )

    def fetch(self, query: str, limit: int = 5) -> list[RawArticle]:
        """Fetch records from the upstream source."""
        try:
            return self._fetch_oai(query, limit)
        except ConnectorFetchError:
            return self._fetch_html(query, limit)

    def _parse_oai_record(self, rec: ET.Element) -> RawArticle | None:
        """Parse a single OAI-PMH record into a RawArticle.

        Returns None if the record is deleted, missing a title, or not article-like.
        """
        if rec.find("header", attrs={"status": "deleted"}):
            return None
        titles = [x.get_text(" ", strip=True) for x in rec.find_all("dc:title")]
        if not titles:
            return None
        title = titles[0].strip()
        ids = [x.get_text(" ", strip=True) for x in rec.find_all("dc:identifier")]
        url_value = next((x for x in ids if x.startswith("http")), "")
        descriptions = [
            x.get_text(" ", strip=True) for x in rec.find_all("dc:description")
        ]
        abstract = descriptions[0].strip() if descriptions else ""
        sources = [x.get_text(" ", strip=True) for x in rec.find_all("dc:source")]
        journal = sources[0].strip() if sources else "SciELO"
        dates = [x.get_text(" ", strip=True) for x in rec.find_all("dc:date")]
        subjects = [x.get_text(" ", strip=True) for x in rec.find_all("dc:subject")]
        combined = " ".join(
            [
                title,
                abstract,
                journal,
                " ".join(subjects),
                " ".join(ids),
            ],
        )
        doi = self._extract_doi(combined)
        year = self._extract_year(" ".join([*dates, title]))
        if not url_value:
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

    def _fetch_oai(self, query: str, limit: int) -> list[RawArticle]:  # noqa: ARG002  # OAI fetch is date-based; query unused
        """Fetch OAI."""
        current_year = datetime.now(UTC).year
        from_date = f"{max(2000, current_year - 8)}-01-01"
        for endpoint in self.OA_MIRRORS:
            url = f"{endpoint}?verb=ListRecords&metadataPrefix=oai_dc&from={from_date}"
            items: list[RawArticle] = []
            token: str | None = None
            for _ in range(3):
                response_xml = self._request_xml_text(url)
                soup = BeautifulSoup(response_xml, "xml")
                records = soup.find_all("record")
                for rec in records:
                    article = self._parse_oai_record(rec)
                    if article is not None:
                        items.append(article)
                    if len(items) >= limit:
                        return items
                token_node = soup.find("resumptionToken")
                token = token_node.get_text(strip=True) if token_node else ""
                if not token:
                    break
                url = f"{endpoint}?verb=ListRecords&resumptionToken={quote_plus(token)}"
            if items:
                return items[:limit]
        msg = "scielo: oai mirrors yielded no query-relevant entries"
        raise ConnectorFetchError(
            msg,
        )

    def _request_xml_text(self, url: str) -> str:
        """Send a request for XML text."""
        scraper = self._build_scraper()
        headers = self._generate_headers()
        headers.setdefault("Accept", "application/xml,text/xml,*/*")
        response = scraper.get(
            url,
            headers=headers,
            timeout=self.REQUEST_TIMEOUT_SECONDS,
        )
        status = int(response.status_code)
        if status >= _HTTP_BAD_REQUEST:
            msg = f"{self.profile.source_key}: http {status}"
            raise ConnectorFetchError(msg)
        return response.content.decode("utf-8", errors="replace")

    def _fetch_html(self, query: str, limit: int) -> list[RawArticle]:
        """Fetch HTML."""
        attempts = [
            (
                self.profile.search_url,
                {
                    self.profile.query_param: query,
                },
            ),
            (
                "https://search.scielo.org/",
                {
                    self.profile.query_param: query,
                    "lang": "en",
                    "count": "20",
                    "from": "0",
                    "output": "site",
                    "format": "summary",
                    "page": "1",
                },
            ),
        ]
        for url, params in attempts:
            try:
                html = self._request_text(url, params=params)
                soup = BeautifulSoup(html, "lxml")
                self._assert_page_is_parseable(html, soup)
                items = self._extract_from_html(query, soup, limit)
                if items:
                    return items
            except ConnectorFetchError:
                continue
        msg = "scielo: unable to obtain parseable result page"
        raise ConnectorFetchError(msg)

    def _extract_from_html(
        self,
        query: str,  # noqa: ARG002  # required by base class signature
        soup: BeautifulSoup,
        limit: int,
    ) -> list[RawArticle]:
        """Extract from HTML."""
        rows = soup.select(".item, .search-results .item, .result, article, li")
        candidates: list[RawArticle] = []
        for row in rows:
            title_node = row.select_one(".title a, h2 a, h3 a, a[href]")
            if not title_node:
                continue
            title = title_node.get_text(" ", strip=True)
            href = urljoin(self.profile.search_url, title_node.get("href", ""))
            abstract_node = row.select_one(".abstract, .snippet, .description, p")
            journal_node = row.select_one(".journal, .publication, .source, .meta")
            abstract = abstract_node.get_text(" ", strip=True) if abstract_node else ""
            journal = (
                journal_node.get_text(" ", strip=True)
                if journal_node
                else self.profile.source_key.upper()
            )
            combined = " ".join(
                [title, abstract, journal, row.get_text(" ", strip=True)],
            )
            doi = self._extract_doi(combined)
            year = self._extract_year(combined)
            if not self._is_article_like_item(title, href, doi, year):
                continue
            candidates.append(
                self._raw(
                    title=title,
                    url=href,
                    abstract=abstract,
                    full_text=combined,
                    doi=doi,
                    year=year,
                    journal=journal,
                ),
            )
            if len(candidates) >= limit * 3:
                break
        if candidates:
            return candidates[:limit]
        json_ld_items = self._extract_json_ld_articles(soup, limit)
        if json_ld_items:
            return json_ld_items
        return []


class PerseeConnector(BaseConnector):
    """Persee source connector."""

    profile = SourceProfile(
        source_key="persee",
        search_url="https://www.persee.fr/search",
        result_selector=".result-item, article, li",
        title_selector="h2 a, h3 a, .title a, a[href]",
        abstract_selector=".resume, .abstract, p",
        journal_selector=".revue, .journal, .source",
        indexing_evidence="scopus web of science",
        language="fr",
    )

    def fetch(self, query: str, limit: int = 5) -> list[RawArticle]:
        """Fetch records from the upstream source."""
        try:
            items = self._fetch_oai(query, limit)
            if items:
                return items
        except ConnectorFetchError:
            pass
        return self._fetch_html(query, limit)

    def _fetch_oai(self, query: str, limit: int) -> list[RawArticle]:
        """Fetch OAI."""
        base = "http://oai.persee.fr/oai"
        url = f"{base}?verb=ListRecords&metadataPrefix=oai_dc&set=persee:serie"
        items: list[RawArticle] = []
        for _ in range(3):
            xml_text = self._request_text(url)
            parsed, token = self._parse_oai_records(xml_text, query, limit - len(items))
            items.extend(parsed)
            if len(items) >= limit or not token:
                break
            url = (
                f"{base}?verb=ListRecords&metadataPrefix=oai_dc&resumptionToken="
                f"{quote_plus(token)}"
            )
        return items[:limit]

    def _parse_oai_records(
        self,
        xml_text: str,
        query: str,  # noqa: ARG002  # required by base class signature
        remaining: int,
    ) -> tuple[list[RawArticle], str]:
        """Parse OAI records."""
        try:
            root = ET.fromstring(xml_text)  # noqa: S314  # trusted API XML response
        except ET.ParseError:
            return ([], "")
        ns = {
            "oai": "http://www.openarchives.org/OAI/2.0/",
            "dc": "http://purl.org/dc/elements/1.1/",
        }
        candidates: list[RawArticle] = []
        relevant: list[RawArticle] = []
        for rec in root.findall(".//oai:record", ns):
            header = rec.find("oai:header", ns)
            if header is not None and header.get("status") == "deleted":
                continue
            metadata = rec.find("oai:metadata", ns)
            if metadata is None:
                continue
            title = metadata.findtext(".//dc:title", default="", namespaces=ns).strip()
            description = metadata.findtext(
                ".//dc:description",
                default="",
                namespaces=ns,
            ).strip()
            identifiers = [
                x.text.strip()
                for x in metadata.findall(".//dc:identifier", ns)
                if x.text
            ]
            sources = [
                x.text.strip() for x in metadata.findall(".//dc:source", ns) if x.text
            ]
            journal = sources[0] if sources else "Persee"
            combined = " ".join([title, description, " ".join(identifiers), journal])
            doi = self._extract_doi(combined)
            year = self._extract_year(combined)
            url_value = next((x for x in identifiers if x.startswith("http")), "")
            if not title or not url_value:
                continue
            if not self._is_article_like_item(title, url_value, doi, year):
                continue
            built = self._raw(
                title=title,
                url=url_value,
                abstract=description,
                full_text=combined,
                doi=doi,
                year=year,
                journal=journal,
            )
            candidates.append(built)
            relevant.append(built)
        token_node = root.find(".//oai:resumptionToken", ns)
        token = (
            token_node.text.strip()
            if token_node is not None and token_node.text
            else ""
        )
        if relevant:
            return (relevant[:remaining], token)
        return (candidates[:remaining], token)


class OpenEditionConnector(BaseConnector):
    """Open Edition source connector."""

    profile = SourceProfile(
        source_key="openedition",
        search_url="https://oai.openedition.org/",
        result_selector=".search-result, article, li",
        title_selector="h2 a, .title a, a[href]",
        abstract_selector=".description, .abstract, p",
        journal_selector=".journal, .source",
        indexing_evidence="scopus web of science",
        language="fr",
    )

    def fetch(self, query: str, limit: int = 5) -> list[RawArticle]:
        """Fetch records from the upstream source."""
        url = "https://oai.openedition.org/?verb=ListRecords&metadataPrefix=oai_dc"
        items: list[RawArticle] = []
        for _ in range(3):
            try:
                xml_text = self._request_text(url)
            except ConnectorFetchError:
                break
            parsed, token = self._parse_oai_records(xml_text, query, limit - len(items))
            items.extend(parsed)
            if len(items) >= limit or not token:
                break
            url = f"https://oai.openedition.org/?verb=ListRecords&resumptionToken={quote_plus(token)}"
        return items[:limit]

    def _parse_oai_records(
        self,
        xml_text: str,
        query: str,  # noqa: ARG002  # required by base class signature
        remaining: int,
    ) -> tuple[list[RawArticle], str]:
        """Parse OAI records."""
        try:
            root = ET.fromstring(xml_text)  # noqa: S314  # trusted API XML response
        except ET.ParseError:
            return ([], "")
        ns = {
            "oai": "http://www.openarchives.org/OAI/2.0/",
            "dc": "http://purl.org/dc/elements/1.1/",
        }
        candidates: list[RawArticle] = []
        relevant: list[RawArticle] = []
        for rec in root.findall(".//oai:record", ns):
            metadata = rec.find("oai:metadata", ns)
            if metadata is None:
                continue
            title = metadata.findtext(".//dc:title", default="", namespaces=ns).strip()
            description = metadata.findtext(
                ".//dc:description",
                default="",
                namespaces=ns,
            ).strip()
            identifiers = [
                x.text.strip()
                for x in metadata.findall(".//dc:identifier", ns)
                if x.text
            ]
            url_value = next((x for x in identifiers if x.startswith("http")), "")
            sources = [
                x.text.strip() for x in metadata.findall(".//dc:source", ns) if x.text
            ]
            journal = sources[0] if sources else "OpenEdition"
            combined = " ".join([title, description, " ".join(identifiers), journal])
            doi = self._extract_doi(combined)
            year = self._extract_year(combined)
            if not title or not url_value:
                continue
            if not self._is_true_article_record(
                url_value,
                doi,
                journal,
                title,
                description,
            ):
                continue
            if not self._is_article_like_item(title, url_value, doi, year):
                continue
            built = self._raw(
                title=title,
                url=url_value,
                abstract=description,
                full_text=combined,
                doi=doi,
                year=year,
                journal=journal,
            )
            candidates.append(built)
            relevant.append(built)
            if len(candidates) >= remaining * 3:
                break
        items = relevant[:remaining] if relevant else candidates[:remaining]
        token_node = root.find(".//oai:resumptionToken", ns)
        token = (
            token_node.text.strip()
            if token_node is not None and token_node.text
            else ""
        )
        return (items, token)

    @staticmethod
    def _is_true_article_record(
        url: str,
        doi: str,
        journal: str,
        title: str,
        description: str,
    ) -> bool:
        """Return whether true article record."""
        lowered_url = (url or "").lower()
        lowered_doi = (doi or "").lower()
        lowered_journal = (journal or "").lower()
        lowered_title = (title or "").lower()
        lowered_description = (description or "").lower()

        if lowered_doi.startswith("10.58079/"):
            return False
        if "hypotheses.org" in lowered_url or "hypotheses.org" in lowered_journal:
            return False
        if "blog" in lowered_title or "blog" in lowered_description:
            return False
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            return False
        return (
            "journals.openedition.org" in lowered_url
            or "books.openedition.org" in lowered_url
            or "doi.org/10.4000/" in lowered_url
            or lowered_doi.startswith("10.4000/")
        )


class MedknowConnector(BaseConnector):
    """Medknow source connector."""

    profile = SourceProfile(
        source_key="medknow",
        search_url="https://api.openalex.org/works",
        query_param="query",
        result_selector=".result, article, li",
        title_selector="h2 a, h3 a, .title a, a[href]",
        abstract_selector=".abstract, .summary, p",
        journal_selector=".journal, .source",
        indexing_evidence="medline scopus",
        language="en",
    )

    def fetch(self, query: str, limit: int = 5) -> list[RawArticle]:
        """Fetch records from the upstream source.

        Let ``ConnectorFetchError`` propagate so the ingestion service
        marks this source as failed instead of silently reporting zero
        articles as a success.
        """
        return self._fetch_openalex(query, limit)

    def _fetch_openalex(self, query: str, limit: int) -> list[RawArticle]:
        """Fetch openalex."""
        url = (
            "https://api.openalex.org/works"
            f"?filter=primary_location.source.host_organization:P4310320448"
            f"&search={quote_plus(query)}&per-page={max(3, limit * 2)}"
        )
        try:

            async def _fetch() -> dict:
                async with (
                    aiohttp.ClientSession() as session,
                    session.get(
                        url,
                        timeout=aiohttp.ClientTimeout(
                            total=self.REQUEST_TIMEOUT_SECONDS,
                        ),
                    ) as resp,
                ):
                    resp.raise_for_status()
                    return await resp.json()

            import asyncio as _asyncio  # noqa: PLC0415  # lazy import to avoid circular dependency

            payload = _asyncio.run(_fetch())
        except ConnectorFetchError:
            raise
        except (ValueError, RuntimeError, ConnectionError) as exc:
            msg = f"medknow: openalex request failed: {exc}"
            raise ConnectorFetchError(
                msg,
            ) from exc
        records = payload.get("results", [])
        if not isinstance(records, list):
            return []
        items: list[RawArticle] = []
        for rec in records[: limit * 3]:
            article = self._build_openalex_record(rec, "Medknow")
            if article is not None:
                items.append(article)
            if len(items) >= limit:
                break
        return items


class DergiParkConnector(BaseConnector):
    """Dergi Park source connector."""

    profile = SourceProfile(
        source_key="dergipark",
        search_url="https://dergipark.org.tr/en/search",
        result_selector=".article-card, article, li",
        title_selector="h3 a, .title a, a[href]",
        abstract_selector=".article-abstract, .abstract, p",
        journal_selector=".journal-title, .journal, .source",
        indexing_evidence="tr dizin scopus",
        language="en",
    )

    OAI_BASE = "https://dergipark.org.tr/api/public/oai/"
    _sets_cache: list[tuple[str, str]] | None = None

    def fetch(self, query: str, limit: int = 5) -> list[RawArticle]:
        # Free OAI-PMH path first; HTML search often challenge-gated.
        """Fetch records from the upstream source."""
        try:
            items = self._fetch_via_oai(query, limit)
            if items:
                return items
        except ConnectorFetchError:
            return super()._fetch_html(query, limit)
        return super()._fetch_html(query, limit)

    def _fetch_via_oai(self, query: str, limit: int) -> list[RawArticle]:
        """Fetch via OAI."""
        sets = self._list_sets()
        if not sets:
            msg = "dergipark: no OAI sets available"
            raise ConnectorFetchError(msg)

        max_sets = int(os.getenv("DERGIPARK_OAI_MAX_SETS", "35"))
        recency_year = datetime.now(UTC).year - 2
        results: list[RawArticle] = []
        for set_spec, set_name in sets[:max_sets]:
            if len(results) >= limit:
                break
            url = (
                f"{self.OAI_BASE}?verb=ListRecords&metadataPrefix=oai_dc"
                f"&set={quote_plus(set_spec)}&from={recency_year}-01-01"
            )
            xml_text = self._request_text(url)
            parsed = self._parse_oai_records(
                xml_text,
                query,
                set_name,
                limit - len(results),
            )
            results.extend(parsed)
        return results[:limit]

    def _list_sets(self) -> list[tuple[str, str]]:
        """List sets."""
        if self._sets_cache is not None:
            return self._sets_cache
        xml_text = self._request_text(f"{self.OAI_BASE}?verb=ListSets")
        try:
            root = ET.fromstring(xml_text)  # noqa: S314  # trusted API XML response
        except ET.ParseError as exc:
            msg = f"dergipark: invalid OAI ListSets xml: {exc}"
            raise ConnectorFetchError(
                msg,
            ) from exc
        ns = {"oai": "http://www.openarchives.org/OAI/2.0/"}
        sets: list[tuple[str, str]] = []
        for node in root.findall(".//oai:set", ns):
            set_spec = node.findtext("oai:setSpec", default="", namespaces=ns).strip()
            set_name = node.findtext("oai:setName", default="", namespaces=ns).strip()
            if set_spec:
                sets.append((set_spec, set_name or set_spec))
        self._sets_cache = sets
        return sets

    def _parse_oai_records(
        self,
        xml_text: str,
        query: str,  # noqa: ARG002  # required by base class signature
        set_name: str,
        remaining: int,
    ) -> list[RawArticle]:
        """Parse OAI records."""
        try:
            root = ET.fromstring(xml_text)  # noqa: S314  # trusted API XML response
        except ET.ParseError:
            return []
        ns = {
            "oai": "http://www.openarchives.org/OAI/2.0/",
            "dc": "http://purl.org/dc/elements/1.1/",
        }
        items: list[RawArticle] = []
        for rec in root.findall(".//oai:record", ns):
            metadata = rec.find("oai:metadata", ns)
            if metadata is None:
                continue
            title = metadata.findtext(".//dc:title", default="", namespaces=ns).strip()
            description = metadata.findtext(
                ".//dc:description",
                default="",
                namespaces=ns,
            ).strip()
            identifiers = [
                x.text.strip()
                for x in metadata.findall(".//dc:identifier", ns)
                if x.text
            ]
            text_blob = " ".join([title, description, " ".join(identifiers), set_name])
            doi = self._extract_doi(text_blob)
            year = self._extract_year(
                metadata.findtext(".//dc:date", default="", namespaces=ns) or text_blob,
            )
            url_value = ""
            for ident in identifiers:
                if ident.startswith("http"):
                    url_value = ident
                    break
            if not url_value:
                continue
            if not self._is_article_like_item(title, url_value, doi, year):
                continue
            items.append(
                self._raw(
                    title=title,
                    url=url_value,
                    abstract=description,
                    full_text=text_blob,
                    doi=doi,
                    year=year,
                    journal=set_name,
                ),
            )
            if len(items) >= remaining:
                break
        return items


class HrcakConnector(BaseConnector):
    """Hrcak source connector."""

    profile = SourceProfile(
        source_key="hrcak",
        search_url="https://hrcak.srce.hr/oai/",
        result_selector=".search-result, article, li",
        title_selector="h3 a, .title a, a[href]",
        abstract_selector=".summary, .abstract, p",
        journal_selector=".journal, .source",
        indexing_evidence="scopus web of science",
        language="en",
    )

    def fetch(self, query: str, limit: int = 5) -> list[RawArticle]:
        """Fetch records from the upstream source."""
        url = "https://hrcak.srce.hr/oai/?verb=ListRecords&metadataPrefix=oai_dc"
        items: list[RawArticle] = []
        for _ in range(3):
            try:
                xml_text = self._request_text(url)
            except ConnectorFetchError:
                break
            parsed, token = self._parse_oai_records(xml_text, query, limit - len(items))
            items.extend(parsed)
            if len(items) >= limit or not token:
                break
            url = f"https://hrcak.srce.hr/oai/?verb=ListRecords&resumptionToken={quote_plus(token)}"
        return items[:limit]

    def _parse_oai_records(
        self,
        xml_text: str,
        query: str,  # noqa: ARG002  # required by base class signature
        remaining: int,
    ) -> tuple[list[RawArticle], str]:
        """Parse OAI records."""
        try:
            root = ET.fromstring(xml_text)  # noqa: S314  # trusted API XML response
        except ET.ParseError:
            return ([], "")
        ns = {
            "oai": "http://www.openarchives.org/OAI/2.0/",
            "dc": "http://purl.org/dc/elements/1.1/",
        }
        items: list[RawArticle] = []
        for rec in root.findall(".//oai:record", ns):
            metadata = rec.find("oai:metadata", ns)
            if metadata is None:
                continue
            title = metadata.findtext(".//dc:title", default="", namespaces=ns).strip()
            description = metadata.findtext(
                ".//dc:description",
                default="",
                namespaces=ns,
            ).strip()
            identifiers = [
                x.text.strip()
                for x in metadata.findall(".//dc:identifier", ns)
                if x.text
            ]
            url_value = next((x for x in identifiers if x.startswith("http")), "")
            combined = " ".join([title, description, " ".join(identifiers)])
            doi = self._extract_doi(combined)
            year = self._extract_year(combined)
            if not title or not url_value:
                continue
            if not self._is_article_like_item(title, url_value, doi, year):
                continue
            items.append(
                self._raw(
                    title=title,
                    url=url_value,
                    abstract=description,
                    full_text=combined,
                    doi=doi,
                    year=year,
                    journal="Hrčak",
                ),
            )
            if len(items) >= remaining:
                break
        token_node = root.find(".//oai:resumptionToken", ns)
        token = (
            token_node.text.strip()
            if token_node is not None and token_node.text
            else ""
        )
        return (items, token)


class AJOLConnector(BaseConnector):
    """AJOL source connector."""

    profile = SourceProfile(
        source_key="ajol",
        search_url="https://www.ajol.info/index.php/ajol/search",
        query_param="query",
        result_selector=".obj_article_summary, article, li",
        title_selector=".title a, h3 a, a[href]",
        abstract_selector=".summary, .abstract, p",
        journal_selector=".journal, .source",
        indexing_evidence="scopus doaj",
        language="en",
    )

    OA_POSITIVE_MARKERS = (
        "open access",
        "free access",
        "download full text",
        "creative commons",
        "cc by",
    )
    OA_NEGATIVE_MARKERS = (
        "subscription required",
        "subscription content only",
        "purchase",
        "buy article",
        "paywall",
    )

    def _extract_from_html(
        self,
        query: str,
        soup: BeautifulSoup,
        limit: int,
    ) -> list[RawArticle]:
        """Extract from HTML."""
        rows = soup.select(".article-summary.media, .article-summary")
        candidates: list[RawArticle] = []
        for row in rows:
            title_node = row.select_one("h4.media-heading a, h3 a, h2 a, a[href]")
            if not title_node:
                continue
            title = title_node.get_text(" ", strip=True)
            href = urljoin(self.profile.search_url, title_node.get("href", ""))
            abstract_node = row.select_one(
                ".plugins_generic_lucene_highlighting, .summary, .abstract, p",
            )
            abstract = abstract_node.get_text(" ", strip=True) if abstract_node else ""
            journal_node = row.select_one(".meta .journal, .journal, .source")
            journal = (
                journal_node.get_text(" ", strip=True)
                if journal_node
                else self.profile.source_key
            )
            combined = " ".join(
                [title, abstract, journal, row.get_text(" ", strip=True)],
            )
            doi = self._extract_doi(combined)
            year = self._extract_year(combined)
            if len(title) < _MIN_TITLE_LENGTH or not href.startswith("http"):
                continue
            candidates.append(
                self._raw(
                    title=title,
                    url=href,
                    abstract=abstract,
                    full_text=combined,
                    doi=doi,
                    year=year,
                    journal=journal,
                ),
            )
            if len(candidates) >= limit * 4:
                break

        if not candidates:
            candidates = super()._extract_from_html(query, soup, limit * 4)

        relevant: list[RawArticle] = []
        for item in candidates:
            enriched = item
            if "ajol.info/" in item.url:
                enriched = self.enrich_raw(item)
            text = f"{enriched.title} {enriched.abstract} {enriched.full_text}".lower()
            if self._is_open_access_text(text):
                relevant.append(enriched)
            if len(relevant) >= limit:
                break
        return relevant[:limit]

    def fetch(self, query: str, limit: int = 5) -> list[RawArticle]:
        """Fetch records from the upstream source."""
        try:
            items = self._fetch_oai(query, limit)
            if items:
                return items
        except ConnectorFetchError:
            return self._fetch_html(query, limit)
        return self._fetch_html(query, limit)

    def _fetch_oai(self, query: str, limit: int) -> list[RawArticle]:
        """Fetch OAI."""
        base = "https://www.ajol.info/index.php/ajol/oai"
        url = f"{base}?verb=ListRecords&metadataPrefix=oai_dc"
        items: list[RawArticle] = []
        for _ in range(3):
            xml_text = self._request_text(url)
            parsed, token = self._parse_oai_records(xml_text, query, limit - len(items))
            items.extend(parsed)
            if len(items) >= limit or not token:
                break
            url = f"{base}?verb=ListRecords&resumptionToken={quote_plus(token)}"
        return items[:limit]

    def _parse_oai_records(
        self,
        xml_text: str,
        query: str,  # noqa: ARG002  # required by base class signature
        remaining: int,
    ) -> tuple[list[RawArticle], str]:
        """Parse OAI records."""
        try:
            root = ET.fromstring(xml_text)  # noqa: S314  # trusted API XML response
        except ET.ParseError:
            return ([], "")
        ns = {
            "oai": "http://www.openarchives.org/OAI/2.0/",
            "dc": "http://purl.org/dc/elements/1.1/",
        }
        candidates: list[RawArticle] = []
        relevant: list[RawArticle] = []
        for rec in root.findall(".//oai:record", ns):
            header = rec.find("oai:header", ns)
            if header is not None and header.get("status") == "deleted":
                continue
            metadata = rec.find("oai:metadata", ns)
            if metadata is None:
                continue
            title = metadata.findtext(".//dc:title", default="", namespaces=ns).strip()
            description = metadata.findtext(
                ".//dc:description",
                default="",
                namespaces=ns,
            ).strip()
            identifiers = [
                x.text.strip()
                for x in metadata.findall(".//dc:identifier", ns)
                if x.text
            ]
            rights = [
                x.text.strip().lower()
                for x in metadata.findall(".//dc:rights", ns)
                if x.text
            ]
            # Keep AJOL OA scope in OAI mode as well.
            rights_text = " ".join(rights)
            if rights and not self._is_open_access_text(rights_text):
                continue
            url_value = next((x for x in identifiers if x.startswith("http")), "")
            if not url_value:
                continue
            sources = [
                x.text.strip() for x in metadata.findall(".//dc:source", ns) if x.text
            ]
            journal = sources[0] if sources else "AJOL"
            combined = " ".join(
                [title, description, " ".join(identifiers), journal, rights_text],
            )
            doi = self._extract_doi(combined)
            year = self._extract_year(combined)
            if not title or len(title) < _MIN_TITLE_LENGTH:
                continue
            built = self._raw(
                title=title,
                url=url_value,
                abstract=description,
                full_text=combined,
                doi=doi,
                year=year,
                journal=journal,
            )
            candidates.append(built)
            relevant.append(built)
            if len(candidates) >= remaining * 3:
                break

        items = relevant[:remaining] if relevant else candidates[:remaining]
        token_node = root.find(".//oai:resumptionToken", ns)
        token = (
            token_node.text.strip()
            if token_node is not None and token_node.text
            else ""
        )
        return (items, token)

    def enrich_raw(self, raw: RawArticle) -> RawArticle:
        """Enrich the raw source payload with parsed metadata."""
        enriched = super().enrich_raw(raw)
        if not raw.url.startswith("http"):
            return enriched
        try:
            html = self._request_text(raw.url)
            soup = self._sanitize_html_soup(BeautifulSoup(html, "lxml"))
            page_text = self._html_text(soup).lower()
            doi = enriched.doi or self._extract_doi(page_text)
            year = enriched.year or self._extract_year(page_text)
            journal = enriched.journal
            if journal.upper() == raw.source_key.upper():
                journal = (
                    self._extract_meta_content(
                        soup,
                        [
                            "citation_journal_title",
                            "dc.source",
                            "prism.publicationname",
                        ],
                    )
                    or journal
                )
            return replace(
                enriched,
                doi=doi,
                year=year,
                journal=journal[:300],
                full_text=" ".join([enriched.full_text, page_text[:12000]])[:20000],
            )
        except (ValueError, RuntimeError, ConnectionError):
            return enriched

    def _is_open_access_text(self, text: str) -> bool:
        """Return whether open access text."""
        lowered = (text or "").lower()
        if any(token in lowered for token in self.OA_NEGATIVE_MARKERS):
            return False
        if any(token in lowered for token in self.OA_POSITIVE_MARKERS):
            return True
        # AJOL article pages can omit explicit OA badges in some templates.
        return True
