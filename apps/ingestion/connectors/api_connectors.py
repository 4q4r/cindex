"""API-mode connectors adapted from paper-search-mcp.

These connectors talk to explicit JSON REST endpoints over aiohttp; they do
not need the browser sidecar because API endpoints serve real JSON rather
than JS-challenge pages.
"""

# ruff: noqa: RUF001  # en-dashes in citation regex patterns are intentional

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
from datetime import datetime
from typing import TYPE_CHECKING
from urllib.parse import quote_plus, urlparse
from xml.etree import ElementTree as ET

import aiohttp
import structlog

from apps.core.text import normalize_scholarly_text

from .base import (
    DOI_PATTERN,
    HTTP_ERROR_THRESHOLD,
    INDEXING_TOKENS,
    MIN_PUBLICATION_YEAR,
    PEER_REVIEW_TOKENS,
    PREPRINT_TOKENS,
    AsyncApiConnector,
    ConnectorFetchError,
    RawArticle,
    SourceProfile,
    current_max_publication_year,
)

if TYPE_CHECKING:
    from bs4 import BeautifulSoup

logger = structlog.get_logger(__name__)


def _extract_epmc_journal(rec: dict) -> str:
    """Return the journal title from a EuropePMC core record.

    EuropePMC's ``resultType=core`` response carries no top-level
    ``journalTitle`` field; the journal title lives at
    ``journalInfo.journal.title``. Records without journal metadata
    return an empty string so the caller can apply a venue fallback.
    """
    journal_info = rec.get("journalInfo")
    if not isinstance(journal_info, dict):
        return ""
    journal = journal_info.get("journal")
    if not isinstance(journal, dict):
        return ""
    return (journal.get("title") or "").strip()


class EuropePMCConnector(AsyncApiConnector):
    """Europe PMC source connector via public REST API."""

    profile = SourceProfile(
        source_key="europe_pmc",
        search_url="https://www.ebi.ac.uk/europepmc/webservices/rest/search",
        mode="api",
        query_param="query",
        indexing_evidence="medline pmc",
        language="en",
    )

    def _api_url(self, query: str, limit: int) -> str:
        return (
            "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
            f"?query={quote_plus(query)}&format=json&pageSize={limit}&resultType=core"
        )

    def _extract_from_payload(
        self,
        query: str,  # noqa: ARG002  # required by base class signature
        payload: dict,
        limit: int,
    ) -> list[RawArticle]:
        records = payload.get("resultList", {}).get("result", [])
        items: list[RawArticle] = []
        for rec in records[:limit]:
            title = rec.get("title", "").strip()
            abstract = rec.get("abstractText", "")
            journal = _extract_epmc_journal(rec) or "Europe PMC"
            doi = rec.get("doi", "") or self._extract_doi(f"{title} {abstract}")
            year = rec.get("pubYear")
            url = rec.get("fullTextUrlList", {}).get("fullTextUrl", [{}])
            url_value = url[0].get("url") if url else rec.get("id", "")
            author_list = rec.get("authorList", {}).get("author", [])
            if author_list:
                authors = tuple(
                    a.get("fullName", "")
                    or f"{a.get('firstName', '')} {a.get('lastName', '')}".strip()
                    for a in author_list
                    if isinstance(a, dict)
                )
            else:
                author_string = rec.get("authorString", "")
                authors = (
                    tuple(a.strip() for a in author_string.split(",") if a.strip())
                    if author_string
                    else ()
                )
            if not title or not url_value:
                continue
            items.append(
                self._raw(
                    title=title,
                    url=url_value,
                    abstract=abstract,
                    full_text=f"{title} {abstract}",
                    doi=doi,
                    year=int(year) if str(year).isdigit() else None,
                    journal=journal,
                    authors=authors,
                ),
            )
        return items


class OpenAlexConnector(AsyncApiConnector):
    """OpenAlex source connector via public REST API."""

    profile = SourceProfile(
        source_key="openalex",
        search_url="https://api.openalex.org/works",
        mode="api",
        query_param="search",
        indexing_evidence="openalex crossref",
        language="en",
    )

    def _api_url(self, query: str, limit: int) -> str:
        return f"{self.profile.search_url}?search={quote_plus(query)}&per_page={limit}"

    @staticmethod
    def _reconstruct_abstract(inverted_index: dict) -> str:
        if not inverted_index:
            return ""
        try:
            word_positions = []
            for word, positions in inverted_index.items():
                word_positions.extend((pos, word) for pos in positions)
            word_positions.sort(key=lambda x: x[0])
            return " ".join([word for _, word in word_positions])
        except (ValueError, KeyError, TypeError):
            return ""

    @staticmethod
    def _extract_journal(item: dict) -> str:
        """Extract the journal name from primary_location or best_oa_location."""
        primary_location = item.get("primary_location")
        source = (
            primary_location.get("source")
            if isinstance(primary_location, dict)
            else None
        )
        if isinstance(source, dict):
            journal = (source.get("display_name") or "").strip()
            if journal:
                return journal
        best_oa = item.get("best_oa_location")
        oa_source = best_oa.get("source") if isinstance(best_oa, dict) else None
        if isinstance(oa_source, dict):
            return (oa_source.get("display_name") or "").strip()
        return ""

    def _extract_from_payload(
        self,
        query: str,  # noqa: ARG002  # required by base class signature
        payload: dict,
        limit: int,
    ) -> list[RawArticle]:
        results = payload.get("results", [])
        items: list[RawArticle] = []
        for item in results[:limit]:
            title = item.get("title") or item.get("display_name")
            if not title:
                continue
            abstract = self._reconstruct_abstract(item.get("abstract_inverted_index"))
            doi = item.get("doi", "")
            if doi:
                doi = doi.replace("https://doi.org/", "")
            if not doi:
                doi = self._extract_doi(abstract or "")
            url = ""
            primary_location = item.get("primary_location")
            if primary_location:
                url = primary_location.get("landing_page_url", "")
            if not url:
                url = item.get("id", "")
            journal = self._extract_journal(item)
            authors = [
                a.get("author", {}).get("display_name", "")
                for a in item.get("authorships", [])
                if a.get("author", {}).get("display_name")
            ]
            pub_date_str = item.get("publication_date")
            year = None
            if pub_date_str:
                with contextlib.suppress(ValueError):
                    year = int(datetime.strptime(pub_date_str, "%Y-%m-%d").year)  # noqa: DTZ007  # only .year is used; timezone irrelevant
            items.append(
                self._raw(
                    title=title,
                    url=url,
                    abstract=abstract,
                    full_text=" ".join([title, abstract or ""]),
                    doi=doi,
                    year=year,
                    journal=journal,
                    authors=tuple(authors),
                ),
            )
        return items


class CrossrefConnector(AsyncApiConnector):
    """Crossref source connector via public REST API."""

    profile = SourceProfile(
        source_key="crossref",
        search_url="https://api.crossref.org/works",
        mode="api",
        query_param="query",
        indexing_evidence="crossref doi",
        language="en",
    )

    def _api_url(self, query: str, limit: int) -> str:
        return f"{self.profile.search_url}?query={quote_plus(query)}&rows={limit}"

    def _extract_from_payload(
        self,
        query: str,  # noqa: ARG002  # required by base class signature
        payload: dict,
        limit: int,
    ) -> list[RawArticle]:
        items_list = payload.get("message", {}).get("items", [])
        items: list[RawArticle] = []
        for item in items_list[:limit]:
            titles = item.get("title", [])
            title = titles[0] if titles else ""
            abstracts = item.get("abstract", "")
            if isinstance(abstracts, list):
                abstracts = " ".join(abstracts)
            doi = item.get("DOI", "") or self._extract_doi(f"{title} {abstracts}")
            year = None
            published = (
                item.get("published-print") or item.get("published-online") or {}
            )
            date_parts = published.get("date-parts", [[]])
            if date_parts and date_parts[0]:
                with contextlib.suppress(ValueError, IndexError):
                    year = int(date_parts[0][0])
            url = f"https://doi.org/{doi}" if doi else ""
            if not url:
                continue
            authors_list = item.get("author", [])
            authors = [
                f"{a.get('given', '')} {a.get('family', '')}".strip()
                for a in authors_list
            ]
            container = item.get("container-title", [])
            journal = container[0] if container else ""
            volume = str(item.get("volume", "") or "").strip()
            issue = str(item.get("issue", "") or "").strip()
            pages = str(item.get("page", "") or "").strip()
            items.append(
                self._raw(
                    title=title,
                    url=url,
                    abstract=abstracts,
                    full_text=f"{title} {abstracts}",
                    doi=doi,
                    year=year,
                    journal=journal,
                    authors=tuple(authors),
                    volume=volume,
                    issue=issue,
                    pages=pages,
                ),
            )
        return items


class PubMedConnector(AsyncApiConnector):
    """PubMed source connector via NCBI E-utilities API."""

    profile = SourceProfile(
        source_key="pubmed",
        search_url="https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
        mode="api",
        query_param="term",
        indexing_evidence="medline pubmed",
        language="en",
    )

    def _api_url(self, query: str, limit: int) -> str:
        return (
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
            f"?db=pubmed&term={quote_plus(query)}&retmax={limit}&retmode=json"
        )

    async def _fetch_async(self, query: str, limit: int) -> list[RawArticle]:
        try:
            import aiohttp  # noqa: PLC0415  # lazy import to avoid circular dependency
        except ImportError:
            return self._fetch_sync(query, limit)
        search_url = self._api_url(query, limit)
        try:
            async with (
                aiohttp.ClientSession() as session,
                session.get(search_url) as resp,
            ):
                resp.raise_for_status()
                search_data = await resp.json()
        except Exception as exc:
            msg = f"{self.profile.source_key}: async search failed: {exc}"
            raise ConnectorFetchError(
                msg,
            ) from exc
        pmids = search_data.get("esearchresult", {}).get("idlist", [])
        if not pmids:
            return []
        summary_url = (
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
            f"?db=pubmed&id={','.join(pmids[:limit])}&retmode=json"
        )
        try:
            async with (
                aiohttp.ClientSession() as session,
                session.get(summary_url) as resp,
            ):
                resp.raise_for_status()
                summary_data = await resp.json()
        except Exception as exc:
            msg = f"{self.profile.source_key}: async summary failed: {exc}"
            raise ConnectorFetchError(
                msg,
            ) from exc
        # Fetch abstracts via efetch (esummary omits them)
        abstract_map: dict[str, str] = {}
        try:
            efetch_url = (
                "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
                f"?db=pubmed&id={','.join(pmids[:limit])}&retmode=xml"
            )
            async with (
                aiohttp.ClientSession() as session,
                session.get(efetch_url) as resp,
            ):
                resp.raise_for_status()
                xml_text = await resp.text()
            abstract_map = self._parse_efetch_abstracts(xml_text)
        except (aiohttp.ClientError, ValueError, TimeoutError) as exc:
            logger.warning("pubmed: efetch for abstracts failed: %s", exc)
        return self._parse_pubmed_summary(summary_data, limit, abstract_map)

    def _extract_from_payload(
        self,
        query: str,  # noqa: ARG002  # required by base class signature
        payload: dict,
        limit: int,
    ) -> list[RawArticle]:
        """Extract from PubMed JSON fixture or API payload."""
        # Support both esummary format and simple records format
        result = payload.get("result", payload)
        uids = result.get("uids", [])
        if uids:
            return self._parse_pubmed_summary(payload, limit)
        # Simple records format (fixture compatibility)
        records = payload.get("records", payload.get("results", []))
        items: list[RawArticle] = []
        for rec in records[:limit]:
            title = rec.get("title", "").strip()
            if not title:
                continue
            doi = rec.get("doi", "") or self._extract_doi(title)
            url = rec.get("url", "")
            if not url and doi:
                url = f"https://doi.org/{doi}"
            if not url:
                continue
            year = rec.get("year")
            if year:
                try:
                    year = int(year)
                except (ValueError, TypeError):
                    year = None
            authors = rec.get("authors", [])
            if isinstance(authors, list):
                authors = tuple(str(a) for a in authors if a)
            else:
                authors = ()
            journal = rec.get("journal", "") or ""
            items.append(
                self._raw(
                    title=title,
                    url=url,
                    abstract=rec.get("abstract", ""),
                    full_text=" ".join([title, rec.get("abstract", "")]),
                    doi=doi,
                    year=year,
                    journal=journal,
                    authors=authors,
                ),
            )
        return items

    def _parse_pubmed_summary(
        self,
        summary_data: dict,
        limit: int,
        abstract_map: dict[str, str] | None = None,
    ) -> list[RawArticle]:
        result = summary_data.get("result", {})
        uids = result.get("uids", [])
        items: list[RawArticle] = []
        for uid in uids[:limit]:
            rec = result.get(uid, {})
            title = rec.get("title", "").strip()
            if not title:
                continue
            authors = [
                a.get("name", "") for a in rec.get("authors", []) if a.get("name")
            ]
            journal = rec.get("fulljournalname", "") or rec.get("source", "")
            doi = ""
            for aid in rec.get("articleids", []):
                if aid.get("idtype") == "doi":
                    doi = aid.get("value", "")
                    break
            if not doi:
                doi = self._extract_doi(title)
            url = f"https://pubmed.ncbi.nlm.nih.gov/{uid}/"
            year = None
            pub_date = rec.get("pubdate", "")
            if pub_date:
                with contextlib.suppress(ValueError, IndexError):
                    year = int(pub_date.split()[0])
            abstract = (abstract_map or {}).get(uid, "")
            items.append(
                self._raw(
                    title=title,
                    url=url,
                    abstract=abstract,
                    full_text=f"{title} {abstract}" if abstract else title,
                    doi=doi,
                    year=year,
                    journal=journal,
                    authors=tuple(authors),
                ),
            )
        return items

    @staticmethod
    def _parse_efetch_abstracts(xml_text: str) -> dict[str, str]:
        """Parse PubMed efetch XML, returning pmid -> abstract mapping."""
        try:
            root = ET.fromstring(xml_text)  # noqa: S314  # trusted API XML response
        except ET.ParseError:
            return {}
        abstracts: dict[str, str] = {}
        for article in root.iter("PubmedArticle"):
            pmid_el = article.find(".//PMID")
            pmid = pmid_el.text.strip() if pmid_el is not None and pmid_el.text else ""
            if not pmid:
                continue
            parts: list[str] = []
            for el in article.iter("AbstractText"):
                label = el.get("Label", "")
                text = (el.text or "").strip()
                if label and text:
                    parts.append(f"{label}: {text}")
                elif text:
                    parts.append(text)
            if parts:
                abstracts[pmid] = " ".join(parts)
        return abstracts


class DOAJConnector(AsyncApiConnector):
    """DOAJ source connector via public REST API."""

    profile = SourceProfile(
        source_key="doaj",
        search_url="https://doaj.org/api/search/articles",
        mode="api",
        query_param="q",
        indexing_evidence="doaj open access",
        language="en",
    )

    def _api_url(self, query: str, limit: int) -> str:
        return f"{self.profile.search_url}/{quote_plus(query)}?pageSize={limit}"

    # Match a leading ``Abstract`` label followed by either a colon (ASCII or
    # full-width CJK) with optional trailing whitespace, or by whitespace
    # (space/newline). ``"Abstract: Background"``, the full-width colon form
    # with no trailing space, and ``"Abstract\nBackground"`` all resolve as
    # labels; a bare ``"Abstract"`` with no separator is left untouched.
    _ABSTRACT_LABEL_RE = re.compile(r"^\s*abstract\s*(?:[:：]\s*|\s+)", re.IGNORECASE)

    @staticmethod
    def _strip_abstract_label(abstract: str) -> str:
        """Strip a leading ``Abstract`` label some publishers prepend.

        BMC / BioData Central and similar publishers emit the abstract field
        with a literal ``"Abstract"`` heading, e.g. ``"Abstract Background
        Constructing a predictive model..."``. A bare leading ``Abstract`` is
        removed only when it behaves as a label — followed by a colon (ASCII or
        full-width) or by a capitalised token (a structured-abstract section
        header or a sentence start) — so legitimate sentences such as
        ``"Abstract algebra is..."`` (next token lower-case, no colon) are
        preserved verbatim.
        """
        if not abstract:
            return abstract
        match = DOAJConnector._ABSTRACT_LABEL_RE.match(abstract)
        if match is None:
            return abstract
        remainder = abstract[match.end() :]
        matched = match.group()
        had_colon = ":" in matched or "：" in matched
        if had_colon or (remainder and remainder[0].isupper()):
            return remainder
        return abstract

    def _extract_doaj_doi(self, bibjson: dict, title: str, abstract: str) -> str:
        """Extract DOI from DOAJ bibjson identifiers or text.

        Args:
            bibjson: The DOAJ bibjson record.
            title: Article title for fallback DOI extraction.
            abstract: Article abstract for fallback DOI extraction.

        Returns:
            The DOI string, or empty string if not found.

        """
        for ident in bibjson.get("identifier", []):
            if ident.get("type") == "doi":
                return ident.get("id", "")
        return self._extract_doi(title + " " + abstract)

    def _extract_doaj_url(self, bibjson: dict, doi: str) -> str:
        """Extract fulltext URL from DOAJ bibjson links.

        Args:
            bibjson: The DOAJ bibjson record.
            doi: Article DOI for fallback URL construction.

        Returns:
            The URL string, or empty string if not found.

        """
        links = bibjson.get("link", [])
        for link in links:
            if link.get("type") == "fulltext":
                return link.get("url", "")
        if links:
            return links[0].get("url", "")
        if doi:
            return f"https://doi.org/{doi}"
        return ""

    def _extract_doaj_item(self, bibjson: dict) -> RawArticle | None:
        """Extract a single RawArticle from a DOAJ bibjson record.

        Returns None if the record is missing required fields.
        """
        title = bibjson.get("title", "").strip()
        if not title:
            return None
        abstract = self._strip_abstract_label(bibjson.get("abstract", "") or "")
        doi = self._extract_doaj_doi(bibjson, title, abstract)
        url = self._extract_doaj_url(bibjson, doi)
        year = bibjson.get("year")
        if year:
            try:
                year = int(year)
            except (ValueError, TypeError):
                year = None
        authors = [
            a.get("name", "") for a in bibjson.get("author", []) if a.get("name")
        ]
        journal_info = bibjson.get("journal", {})
        journal = (
            journal_info.get("title", "") if isinstance(journal_info, dict) else ""
        )
        return self._raw(
            title=title,
            url=url,
            abstract=abstract,
            full_text=f"{title} {abstract}",
            doi=doi,
            year=year,
            journal=journal,
            authors=tuple(authors),
        )

    def _extract_from_payload(
        self,
        query: str,  # noqa: ARG002  # required by base class signature
        payload: dict,
        limit: int,
    ) -> list[RawArticle]:
        results = payload.get("results", [])
        items: list[RawArticle] = []
        for item in results[:limit]:
            article = self._extract_doaj_item(item.get("bibjson", {}))
            if article:
                items.append(article)
        return items


class COREConnector(AsyncApiConnector):
    """CORE source connector via public REST API."""

    profile = SourceProfile(
        source_key="core",
        search_url="https://api.core.ac.uk/v3/search/works",
        mode="api",
        query_param="q",
        indexing_evidence="core open access",
        language="en",
    )

    def _api_url(self, query: str, limit: int) -> str:
        return f"{self.profile.search_url}?q={quote_plus(query)}&limit={limit}"

    async def _fetch_async(self, query: str, limit: int) -> list[RawArticle]:
        try:
            import aiohttp  # noqa: PLC0415  # lazy import to avoid circular dependency
        except ImportError:
            return self._fetch_sync(query, limit)
        url = self._api_url(query, limit)
        api_key = os.getenv("CORE_API_KEY", "").strip()
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        try:
            async with (
                aiohttp.ClientSession() as session,
                session.get(url, headers=headers) as resp,
            ):
                resp.raise_for_status()
                payload = await resp.json()
        except Exception as exc:
            msg = f"{self.profile.source_key}: async request failed: {exc}"
            raise ConnectorFetchError(
                msg,
            ) from exc
        if not isinstance(payload, dict):
            msg = f"{self.profile.source_key}: invalid payload type"
            raise ConnectorFetchError(
                msg,
            )
        return self._extract_from_payload(query, payload, limit)

    def _extract_from_html(
        self,
        query: str,  # noqa: ARG002  # required by base class signature
        soup: BeautifulSoup,
        limit: int,
    ) -> list[RawArticle]:
        """Parse CORE __NEXT_DATA__ payload from HTML pages."""
        import json as _json  # noqa: PLC0415  # lazy import to avoid circular dependency

        for script in soup.select('script[id="__NEXT_DATA__"]'):
            try:
                data = _json.loads(script.string or "")
                search_results = (
                    data.get("props", {})
                    .get("pageProps", {})
                    .get("searchResults", {})
                    .get("data", [])
                )
                items: list[RawArticle] = []
                for item in search_results[:limit]:
                    title = item.get("title", "").strip()
                    if not title:
                        continue
                    abstract = item.get("abstract", "") or ""
                    doi = item.get("doi", "") or self._extract_doi(
                        title + " " + abstract,
                    )
                    url = item.get("downloadUrl") or ""
                    if not url and doi:
                        url = f"https://doi.org/{doi}"
                    if not url:
                        continue
                    year = self._extract_year(title + " " + abstract)
                    journal = item.get("publisher", "") or ""
                    items.append(
                        self._raw(
                            title=title,
                            url=url,
                            abstract=abstract,
                            full_text=f"{title} {abstract}",
                            doi=doi,
                            year=year,
                            journal=journal,
                        ),
                    )
            except (ValueError, KeyError, TypeError):
                logger.warning("core: __NEXT_DATA__ parse failed", exc_info=True)
            else:
                return items
        return []

    def _extract_from_payload(
        self,
        query: str,  # noqa: ARG002  # required by base class signature
        payload: dict,
        limit: int,
    ) -> list[RawArticle]:
        results = payload.get("results", [])
        items: list[RawArticle] = []
        for item in results[:limit]:
            title = item.get("title", "").strip()
            if not title:
                continue
            abstract = item.get("abstract", "") or ""
            doi = item.get("doi", "") or self._extract_doi(title + " " + abstract)
            url = item.get("downloadUrl") or item.get("sourceFulltextUrl") or ""
            if not url and doi:
                url = f"https://doi.org/{doi}"
            if not url:
                continue
            year = item.get("yearPublished")
            if year:
                try:
                    year = int(year)
                except (ValueError, TypeError):
                    year = None
            authors_data = item.get("authors", [])
            authors = [
                a.get("name", "")
                for a in authors_data
                if isinstance(a, dict) and a.get("name")
            ]
            journal = item.get("publisher", "") or ""
            items.append(
                self._raw(
                    title=title,
                    url=url,
                    abstract=abstract,
                    full_text=f"{title} {abstract}",
                    doi=doi,
                    year=year,
                    journal=journal,
                    authors=tuple(authors),
                ),
            )
        return items


class PMCConnector(AsyncApiConnector):
    """PubMed Central source connector via Europe PMC API."""

    profile = SourceProfile(
        source_key="pmc",
        search_url="https://www.ebi.ac.uk/europepmc/webservices/rest/search",
        mode="api",
        query_param="query",
        indexing_evidence="pmc pubmed central",
        language="en",
    )

    def _api_url(self, query: str, limit: int) -> str:
        return (
            "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
            f"?query={quote_plus(query)}%20AND%20OPEN_ACCESS:y"
            f"&format=json&pageSize={limit}&resultType=core"
        )

    def _extract_from_payload(
        self,
        query: str,  # noqa: ARG002  # required by base class signature
        payload: dict,
        limit: int,
    ) -> list[RawArticle]:
        records = payload.get("resultList", {}).get("result", [])
        items: list[RawArticle] = []
        for rec in records[:limit]:
            title = self._clean_pmc_title(rec)
            abstract = rec.get("abstractText", "")
            journal = _extract_epmc_journal(rec) or "PMC"
            doi = rec.get("doi", "") or self._extract_doi(f"{title} {abstract}")
            year = self._extract_pmc_year(rec) or self._extract_year(
                f"{title} {abstract} {journal}",
            )
            pmcid = rec.get("pmcid", "")
            url = ""
            if pmcid:
                url = f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmcid}/"
            if not url:
                url_list = rec.get("fullTextUrlList", {}).get("fullTextUrl", [])
                if url_list:
                    url = url_list[0].get("url", "")
            if not title or not url:
                continue
            author_list = rec.get("authorList", {}).get("author", [])
            if author_list:
                authors = tuple(
                    a.get("fullName", "")
                    or f"{a.get('firstName', '')} {a.get('lastName', '')}".strip()
                    for a in author_list
                    if isinstance(a, dict)
                )
            else:
                author_string = rec.get("authorString", "")
                authors = (
                    tuple(a.strip() for a in author_string.split(",") if a.strip())
                    if author_string
                    else ()
                )
            items.append(
                self._raw(
                    title=title,
                    url=url,
                    abstract=abstract,
                    full_text=f"{title} {abstract}",
                    doi=doi,
                    year=year,
                    journal=journal,
                    authors=authors,
                ),
            )
        return items

    @staticmethod
    def _clean_pmc_title(rec: dict) -> str:
        """Return the article title with conference-abstract numbers stripped.

        Europe PMC concatenates the poster/abstract number into the ``title``
        field for conference-supplement records (``pubType`` containing
        ``Abstract``), producing garbage like ``"122 Statistically valid
        machine learning fairness evaluation"``. The leading number is not
        part of the real title and is stripped only for conference-abstract
        records so legitimate titles that start with a number (``"5G ..."``)
        are untouched.
        """
        title = str(rec.get("title", "")).strip()
        pub_types = (rec.get("pubTypeList") or {}).get("pubType") or []
        is_conference_abstract = any(
            "abstract" in str(t).lower() or "congress" in str(t).lower()
            for t in pub_types
        )
        if is_conference_abstract:
            title = re.sub(r"^\d{1,4}\s+(?=[A-Z])", "", title).strip()
        return title

    @staticmethod
    def _extract_pmc_year(rec: dict) -> int | None:
        """Return the publication year from ``pubYear`` or ``firstPublicationDate``.

        Conference-supplement and accepted-manuscript records often omit
        ``pubYear`` while carrying ``firstPublicationDate`` (``YYYY-MM-DD``),
        so fall back to that date before giving up.
        """
        year = rec.get("pubYear")
        if str(year).isdigit():
            return int(year)
        pub_date = str(rec.get("firstPublicationDate") or "")
        match = re.match(r"(19|20)\d{2}", pub_date)
        return int(match.group(0)) if match else None


class ArXivConnector(AsyncApiConnector):
    """arXiv source connector via public API."""

    profile = SourceProfile(
        source_key="arxiv",
        search_url="http://export.arxiv.org/api/query",
        mode="api",
        query_param="search_query",
        preprint_evidence="preprint",
        language="en",
    )

    def _api_url(self, query: str, limit: int) -> str:
        return (
            f"{self.profile.search_url}"
            f"?search_query=all:{quote_plus(query)}&start=0&max_results={limit}"
        )

    async def _fetch_async(self, query: str, limit: int) -> list[RawArticle]:
        try:
            import aiohttp  # noqa: PLC0415  # lazy import to avoid circular dependency
        except ImportError:
            return self._fetch_sync(query, limit)
        url = self._api_url(query, limit)
        try:
            async with aiohttp.ClientSession() as session, session.get(url) as resp:
                resp.raise_for_status()
                xml_text = await resp.text()
        except Exception as exc:
            msg = f"{self.profile.source_key}: async request failed: {exc}"
            raise ConnectorFetchError(
                msg,
            ) from exc
        return self._parse_arxiv_xml(xml_text, limit)

    def _extract_from_payload(
        self,
        query: str,  # noqa: ARG002  # required by base class signature
        payload: dict,
        limit: int,
    ) -> list[RawArticle]:
        """Extract from arXiv-style JSON fixture or API payload."""
        records = payload.get("records", payload.get("results", []))
        items: list[RawArticle] = []
        for rec in records[:limit]:
            title = rec.get("title", "").strip()
            if not title:
                continue
            abstract = rec.get("abstract", "") or ""
            url = rec.get("url", "")
            doi = rec.get("doi", "") or self._extract_doi(title + " " + abstract)
            authors = rec.get("authors", [])
            if isinstance(authors, list):
                authors = tuple(str(a) for a in authors if a)
            else:
                authors = ()
            year = rec.get("year")
            if year:
                try:
                    year = int(year)
                except (ValueError, TypeError):
                    year = None
            items.append(
                self._raw(
                    title=title,
                    url=url,
                    abstract=abstract,
                    full_text=f"{title} {abstract}",
                    doi=doi,
                    year=year,
                    journal="arXiv",
                    authors=authors,
                ),
            )
        return items

    def _parse_arxiv_xml(self, xml_text: str, limit: int) -> list[RawArticle]:
        try:
            root = ET.fromstring(xml_text)  # noqa: S314  # trusted API XML response
        except Exception as exc:
            msg = f"{self.profile.source_key}: XML parse failed: {exc}"
            raise ConnectorFetchError(
                msg,
            ) from exc
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        items: list[RawArticle] = []
        for entry in root.findall("atom:entry", ns):
            title = entry.findtext("atom:title", "", namespaces=ns).strip()
            if not title:
                continue
            abstract = entry.findtext("atom:summary", "", namespaces=ns).strip()
            doi = ""
            for eid in entry.findall("atom:id", ns):
                arxiv_id = eid.text.strip()
                url = arxiv_id
                if "arxiv.org/abs/" in arxiv_id:
                    arxiv_id = arxiv_id.split("/abs/")[-1]
                    doi = f"10.48550/arXiv.{arxiv_id}"
            authors = [
                a.findtext("atom:name", "", namespaces=ns).strip()
                for a in entry.findall("atom:author", ns)
            ]
            published = entry.findtext("atom:published", "", namespaces=ns)
            year = None
            if published:
                with contextlib.suppress(ValueError):
                    year = int(published[:4])
            items.append(
                self._raw(
                    title=title,
                    url=url,
                    abstract=abstract,
                    full_text=f"{title} {abstract}",
                    doi=doi,
                    year=year,
                    journal="arXiv",
                    authors=tuple(authors),
                ),
            )
            if len(items) >= limit:
                break
        return items


class DBLPConnector(AsyncApiConnector):
    """DBLP source connector via public XML API."""

    profile = SourceProfile(
        source_key="dblp",
        search_url="https://dblp.org/search/publ/api",
        mode="api",
        query_param="q",
        language="en",
    )

    def _api_url(self, query: str, limit: int) -> str:
        return f"{self.profile.search_url}?q={quote_plus(query)}&format=json&h={limit}"

    def _extract_from_payload(
        self,
        query: str,  # noqa: ARG002  # required by base class signature
        payload: dict,
        limit: int,
    ) -> list[RawArticle]:
        hits = payload.get("result", {}).get("hits", {}).get("hit", [])
        if isinstance(hits, dict):
            hits = [hits]
        items: list[RawArticle] = []
        for hit in hits[:limit]:
            info = hit.get("info", {})
            title = info.get("title", "").strip()
            if not title:
                continue
            url = info.get("url", "") or info.get("ee", "")
            doi = info.get("doi", "") or self._extract_doi(title)
            if not doi:
                doi = self._extract_doi(url)
            if not url and doi:
                url = f"https://doi.org/{doi}"
            if not url:
                continue
            authors = info.get("authors", {}).get("author", [])
            if isinstance(authors, dict):
                authors = [authors]
            author_names = [
                a.get("text", "") if isinstance(a, dict) else str(a) for a in authors
            ]
            year = info.get("year")
            if year:
                try:
                    year = int(year)
                except (ValueError, TypeError):
                    year = None
            journal = info.get("venue", "") or ""
            items.append(
                self._raw(
                    title=title,
                    url=url,
                    abstract="",
                    full_text=title,
                    doi=doi,
                    year=year,
                    journal=journal,
                    authors=tuple(author_names),
                ),
            )
        return items


class HALConnector(AsyncApiConnector):
    """HAL (France) source connector via public Solr API."""

    profile = SourceProfile(
        source_key="hal",
        search_url="https://api.archives-ouvertes.fr/search/",
        mode="api",
        query_param="q",
        language="fr",
    )

    def _api_url(self, query: str, limit: int) -> str:
        fields = (
            "halId_s,title_s,authFullName_s,abstract_s,"
            "doiId_s,publicationDateY_i,uri_s,journalTitle_s"
        )
        return (
            f"{self.profile.search_url}"
            f"?q={quote_plus(query)}&fl={fields}&rows={limit}&wt=json&sort=score desc"
        )

    _MIN_JOURNAL_LEN = 3

    def _extract_from_payload(
        self,
        query: str,  # noqa: ARG002  # required by base class signature
        payload: dict,
        limit: int,
    ) -> list[RawArticle]:
        docs = payload.get("response", {}).get("docs", [])
        items: list[RawArticle] = []
        for doc in docs[:limit]:
            titles = doc.get("title_s", [])
            title = titles[0] if titles else ""
            if not title:
                continue
            abstracts = doc.get("abstract_s", [])
            abstract = abstracts[0] if abstracts else ""
            doi = doc.get("doiId_s", "") or self._extract_doi(title + " " + abstract)
            url = doc.get("uri_s", "")
            if not url and doi:
                url = f"https://doi.org/{doi}"
            year = doc.get("publicationDateY_i")
            if year:
                try:
                    year = int(year)
                except (ValueError, TypeError):
                    year = None
            authors_data = doc.get("authFullName_s", [])
            authors = tuple(authors_data) if isinstance(authors_data, list) else ()
            journal_titles = doc.get("journalTitle_s")
            if isinstance(journal_titles, list):
                journal = journal_titles[0].strip() if journal_titles else ""
            elif isinstance(journal_titles, str):
                journal = journal_titles.strip()
            else:
                journal = ""
            # HAL returns the full journal title as a string; single-char or
            # empty values are SOLR index noise, so fall back to the platform
            # marker (consistent with arXiv/PMC preprint connectors).
            if len(journal) < HALConnector._MIN_JOURNAL_LEN:
                journal = "HAL"
            items.append(
                self._raw(
                    title=title,
                    url=url,
                    abstract=abstract,
                    full_text=f"{title} {abstract}",
                    doi=doi,
                    year=year,
                    journal=journal,
                    authors=authors,
                ),
            )
        return items


class ZenodoConnector(AsyncApiConnector):
    """Zenodo source connector via public REST API."""

    profile = SourceProfile(
        source_key="zenodo",
        search_url="https://zenodo.org/api/records",
        mode="api",
        query_param="q",
        language="en",
    )

    def _api_url(self, query: str, limit: int) -> str:
        return (
            f"{self.profile.search_url}"
            f"?q={quote_plus(query)}&size={limit}&sort=mostrecent"
            "&type=publication&subtype=article"
        )

    def _extract_from_payload(
        self,
        query: str,  # noqa: ARG002  # required by base class signature
        payload: dict,
        limit: int,
    ) -> list[RawArticle]:
        hits = payload.get("hits", {}).get("hits", [])
        items: list[RawArticle] = []
        for hit in hits[:limit]:
            metadata = hit.get("metadata", {})
            title = metadata.get("title", "").strip()
            if not title:
                continue
            abstract = metadata.get("description", "") or ""
            doi = metadata.get("doi", "") or self._extract_doi(title + " " + abstract)
            url = (
                f"https://doi.org/{doi}"
                if doi
                else f"https://zenodo.org/record/{hit.get('id', '')}"
            )
            year = None
            pub_date = metadata.get("publication_date", "")
            if pub_date:
                with contextlib.suppress(ValueError):
                    year = int(pub_date[:4])
            creators = metadata.get("creators", [])
            authors = [c.get("name", "") for c in creators if c.get("name")]
            journal = (
                metadata.get("journal", {}).get("title", "")
                if isinstance(metadata.get("journal"), dict)
                else ""
            )
            items.append(
                self._raw(
                    title=title,
                    url=url,
                    abstract=abstract,
                    full_text=f"{title} {abstract}",
                    doi=doi,
                    year=year,
                    journal=journal,
                    authors=tuple(authors),
                ),
            )
        return items


class IACRConnector(AsyncApiConnector):
    """IACR ePrint Archive connector via the public RSS feed.

    The ePrint search page (``/search``) is guarded by an aggressive
    anti-bot "tin foil hat" wall that returns a block page even to real
    browsers, and IACR exposes no public search API. The RSS feed
    (``/rss/rss.xml``) is the only unblocked, machine-readable channel: it
    lists the ~100 most recent eprints with title, link, authors
    (``dc:creator``), publication date and an abstract snippet. The feed is
    fetched with aiohttp (it is served without the anti-bot wall) and
    filtered client-side to items whose title or abstract contains every
    query token -- the same approach every known IACR integration takes.
    IACR ePrint does not assign DOIs, so ``doi`` is left empty rather than
    fabricated.
    """

    _DC_NS = "http://purl.org/dc/elements/1.1/"
    _RSS_URL = "https://eprint.iacr.org/rss/rss.xml"

    profile = SourceProfile(
        source_key="iacr",
        search_url="https://eprint.iacr.org/rss/rss.xml",
        mode="api",
        query_param="q",
        indexing_evidence="iacr cryptology eprint",
        language="en",
    )

    def _api_url(self, query: str, limit: int) -> str:  # noqa: ARG002  # RSS is one feed
        """IACR RSS is a single feed; the query is filtered client-side."""
        return self._RSS_URL

    async def _fetch_async(self, query: str, limit: int) -> list[RawArticle]:
        headers = {
            "Accept": "application/rss+xml,application/xml,text/xml,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "User-Agent": "cindex/1.0 (academic-article-search)",
        }
        try:
            async with (
                aiohttp.ClientSession() as session,
                session.get(
                    self._RSS_URL,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(
                        total=self.REQUEST_TIMEOUT_SECONDS,
                    ),
                ) as response,
            ):
                response.raise_for_status()
                xml_text = await response.text()
        except aiohttp.ClientResponseError as exc:
            status = getattr(exc, "status", None) or getattr(exc, "code", None)
            msg = f"{self.profile.source_key}: HTTP {status} for {self._RSS_URL}: {exc}"
            raise ConnectorFetchError(msg) from exc
        except aiohttp.ClientError as exc:
            msg = f"{self.profile.source_key}: request failed: {exc}"
            raise ConnectorFetchError(msg) from exc
        return self._build_articles(query, self._parse_rss_xml(xml_text), limit)

    def _extract_from_payload(
        self,
        query: str,
        payload: dict,
        limit: int,
    ) -> list[RawArticle]:
        """Extract from an IACR RSS-shaped JSON fixture."""
        records = payload.get("items", payload.get("papers", []))
        return self._build_articles(query, records, limit)

    @classmethod
    def _parse_rss_xml(cls, xml_text: str) -> list[dict]:
        """Parse the IACR RSS feed into a list of record dicts."""
        root = ET.fromstring(xml_text)  # noqa: S314  # trusted API XML response
        return [
            {
                "title": (item.findtext("title") or "").strip(),
                "link": (item.findtext("link") or "").strip(),
                "description": item.findtext("description") or "",
                "pubDate": item.findtext("pubDate") or "",
                "creators": [
                    (c.text or "").strip()
                    for c in item.findall(f"{{{cls._DC_NS}}}creator")
                ],
            }
            for item in root.findall(".//item")
        ]

    def _build_articles(
        self,
        query: str,
        records: list[dict],
        limit: int,
    ) -> list[RawArticle]:
        """Filter RSS records by query tokens and build RawArticle list."""
        items: list[RawArticle] = []
        for rec in records:
            title = (rec.get("title") or "").strip()
            if not title:
                continue
            link = (rec.get("link") or "").strip()
            if not link.startswith("http"):
                continue
            abstract = rec.get("description") or ""
            if not self._matches_query(f"{title} {abstract}", query):
                continue
            year = self._extract_year(
                f"{rec.get('pubDate', '')} {link}",
            )
            creators = rec.get("creators") or []
            authors = [c for c in creators if isinstance(c, str) and c]
            items.append(
                self._raw(
                    title=title,
                    url=link,
                    abstract=abstract,
                    full_text=f"{title} {abstract}",
                    doi="",
                    year=year,
                    journal="IACR ePrint",
                    authors=tuple(authors),
                    preprint_evidence="iacr cryptology eprint",
                ),
            )
            if len(items) >= limit:
                break
        return items


class ExaConnector(AsyncApiConnector):
    """Exa source connector via REST API (aiohttp transport).

    ``api.exa.ai`` is a documented REST endpoint; per the project transport
    policy explicit APIs use aiohttp directly. The POST is a JSON request
    authenticated with ``x-api-key``.
    """

    profile = SourceProfile(
        source_key="exa",
        search_url="https://api.exa.ai/search",
        mode="api",
        indexing_evidence="exa research paper web search",
        language="en",
    )

    @staticmethod
    def _api_key() -> str:
        """Return the Exa API key."""
        return os.getenv("EXA_API_KEY", "").strip()

    def _api_url(self, query: str, limit: int) -> str:  # noqa: ARG002  # required by base class signature
        """Exa uses POST with JSON body, not a simple GET URL."""
        return self.profile.search_url

    async def _cs_post_json(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict,
        timeout: float,  # noqa: ASYNC109  # total aiohttp request timeout
    ) -> dict:
        """POST JSON to Exa over aiohttp.

        ``api.exa.ai`` is a documented REST endpoint; per the project transport
        policy explicit APIs use aiohttp directly. Transient transport errors
        (network, timeout) propagate as ``aiohttp.ClientError`` /
        ``TimeoutError`` so ``_fetch_single_lang`` can retry them; non-2xx
        responses and invalid JSON raise ``ConnectorFetchError`` (terminal —
        retrying a 4xx or a corrupt body will not help).
        """
        async with (
            aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as session,
            session.post(url, json=payload, headers=headers) as resp,
        ):
            text = await resp.text()
            status = resp.status
        if status >= HTTP_ERROR_THRESHOLD:
            msg = f"{self.profile.source_key}: HTTP {status} for {url}"
            raise ConnectorFetchError(msg)
        try:
            data = json.loads(text)
        except ValueError as exc:
            msg = f"{self.profile.source_key}: invalid JSON response: {exc}"
            raise ConnectorFetchError(msg) from exc
        if not isinstance(data, dict):
            msg = f"{self.profile.source_key}: invalid JSON payload type"
            raise ConnectorFetchError(msg)
        return data

    async def _fetch_single_lang(
        self,
        lang_query: str,
        url: str,
        headers: dict,
        payload: dict,
        per_lang: int,
    ) -> tuple[list[RawArticle], Exception | None]:
        """Fetch results for a single language query with retry logic.

        Returns:
            A tuple of (items, last_error) where last_error is None on success.

        """
        for attempt in range(1, self.MAX_ATTEMPTS + 1):
            try:
                data = await self._cs_post_json(
                    url,
                    headers,
                    payload,
                    self.REQUEST_TIMEOUT_SECONDS,
                )
                return self._extract_from_payload(lang_query, data, per_lang), None
            except ConnectorFetchError:
                break
            except (aiohttp.ClientError, TimeoutError, OSError) as exc:
                if attempt < self.MAX_ATTEMPTS:
                    await asyncio.sleep(0.6 * attempt)
                else:
                    return [], exc
        return [], None

    async def _fetch_async(self, query: str, limit: int) -> list[RawArticle]:
        """Fetch records from Exa across multiple languages.

        Exa auto-detects query language and only returns results in that language.
        To get 20-30 multilingual results, we translate the query and make one
        API call per language, then merge and deduplicate.
        """
        from apps.core.translate import expand_query_for_exa  # noqa: PLC0415, I001  # lazy import to avoid circular dependency

        api_key = self._api_key()
        if not api_key:
            msg = "exa: EXA_API_KEY is required"
            raise ConnectorFetchError(msg)

        lang_queries = expand_query_for_exa(query)
        per_lang = max(3, limit // len(lang_queries) + 2)

        all_items: list[RawArticle] = []
        seen_urls: set[str] = set()
        last_error: Exception | None = None

        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "cindex/1.0",
            "x-api-key": api_key,
        }
        system_prompt = (
            "Find scholarly articles that clearly indicate their publication "
            "status. For each result, signal whether it is: "
            "(1) peer-reviewed or refereed — look for explicit statements like "
            "'peer-reviewed', 'refereed journal', or journal names known for "
            "peer review; "
            "(2) indexed in a reputable database — Scopus, Web of Science, "
            "MEDLINE, PubMed Central, DOAJ, RINC/eLibrary, KCI; "
            "(3) has a DOI and journal citation — DOI must be present, journal "
            "name must be identifiable; "
            "(4) is a preprint or author manuscript — preprints.org, arXiv, "
            "bioRxiv, medRxiv, SSRN, or labelled 'preprint', 'working paper', "
            "'author manuscript'. "
            "Prefer published peer-reviewed journal articles over preprints. "
            "Avoid duplicates and non-scholarly content."
        )
        url = self.profile.search_url

        for lang_query in lang_queries.values():
            payload = {
                "query": lang_query,
                "type": "auto",
                "category": "research paper",
                "num_results": per_lang,
                "contents": {
                    "text": {"maxCharacters": 10000},
                    "highlights": {
                        "query": (
                            "peer-reviewed refereed status, journal indexing in "
                            "Scopus or Web of Science, DOI, preprint vs published "
                            "article"
                        ),
                        "maxCharacters": 3000,
                    },
                },
                "systemPrompt": system_prompt,
            }
            items, error = await self._fetch_single_lang(
                lang_query,
                url,
                headers,
                payload,
                per_lang,
            )
            if error:
                last_error = error

            for item in items:
                if item.url not in seen_urls:
                    seen_urls.add(item.url)
                    all_items.append(item)

            if len(all_items) >= limit:
                break

            await asyncio.sleep(0.3)

        if not all_items and last_error:
            msg = (
                f"{self.profile.source_key}: request failed after retries: {last_error}"
            )
            raise ConnectorFetchError(
                msg,
            )

        # Enrich articles missing key metadata via outputSchema
        await self._apply_enrichment(all_items, query)

        return all_items[:limit]

    async def _apply_enrichment(self, items: list[RawArticle], query: str) -> None:
        """Enrich articles missing key metadata via outputSchema deep-lite."""
        enrichment_needed = [
            item
            for item in items
            if not item.authors or item.year is None or not item.doi
        ]
        if not enrichment_needed:
            return
        enrichment_urls = [item.url for item in enrichment_needed]
        try:
            enrichment = await self._enrich_with_output_schema(enrichment_urls, query)
        except (TimeoutError, OSError, ValueError, KeyError) as e:
            logger.warning("exa: outputSchema enrichment failed: %s", e)
            return
        if not enrichment:
            return
        for item in items:
            meta = enrichment.get(item.url, {})
            if not meta:
                continue
            if meta.get("authors"):
                item.authors = meta["authors"]
            if "year" in meta and meta["year"] is not None:
                item.year = meta["year"]
            if meta.get("doi"):
                item.doi = meta["doi"]
            if meta.get("journal"):
                item.journal = meta["journal"]

    @staticmethod
    def _normalize_author_value(author: object) -> tuple[str, ...]:
        """Normalize author payload into a tuple."""
        if isinstance(author, str):
            cleaned = normalize_scholarly_text(author)
            return (cleaned,) if cleaned else ()
        if isinstance(author, dict):
            cleaned = normalize_scholarly_text(
                str(author.get("name") or author.get("full_name") or "").strip(),
            )
            return (cleaned,) if cleaned else ()
        if isinstance(author, list):
            names: list[str] = []
            for item in author:
                names.extend(ExaConnector._normalize_author_value(item))
            return tuple(names)
        return ()

    # --- DOI extraction with boundary cleanup ---

    @staticmethod
    def _extract_doi(text: str) -> str:
        """Extract DOI from text, stripping trailing noise specific to Exa payloads.

        The base DOI_PATTERN can over-match trailing parentheses like (2017)
        and incomplete parentheticals like (ref.  Override to clean these up.
        Also strips trailing unmatched closing parens like gcb.15818)
        """
        found = DOI_PATTERN.search(text or "")
        if not found:
            return ""
        doi = found.group(0).rstrip(".")
        # Strip trailing complete parenthesized noise: (YYYY), (accessed 2025)
        doi = re.sub(r"\([^)]*\)$", "", doi)
        # Strip trailing incomplete parenthesized noise: (ref — only if no closing paren
        doi = re.sub(r"\([^)]*$", "", doi)
        # Strip trailing unmatched closing paren: e.g. gcb.15818)
        doi = re.sub(r"\)$", "", doi)
        return doi.strip()

    # --- enrichment via outputSchema ---

    async def _enrich_with_output_schema(
        self,
        urls: list[str],
        query: str,
    ) -> dict[str, dict]:
        """Enrich articles with structured metadata via Exa deep-lite outputSchema.

        Makes a single deep-lite call requesting authors, year, DOI, journal,
        and peer-reviewed status for each URL. Returns a dict mapping URL to
        the extracted metadata dict.
        """
        if not urls:
            return {}
        api_key = self._api_key()
        if not api_key:
            return {}
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "cindex/1.0",
            "x-api-key": api_key,
        }
        payload = self._build_enrichment_payload(urls, query)
        try:
            data = await self._cs_post_json(
                self.profile.search_url,
                headers,
                payload,
                60.0,
            )
        except (ConnectorFetchError, OSError, ValueError, KeyError) as e:
            logger.warning("exa: outputSchema enrichment failed: %s", e)
            return {}
        return self._parse_enrichment_response(data)

    @staticmethod
    def _build_enrichment_payload(urls: list[str], query: str) -> dict:
        """Build the outputSchema enrichment request payload."""
        site_filter = " OR ".join(urlparse(u).netloc for u in urls[:5])
        return {
            "query": f"{query} site:({site_filter})",
            "type": "deep-lite",
            "category": "research paper",
            "num_results": min(len(urls), 10),
            "contents": {"text": {"maxCharacters": 5000}},
            "outputSchema": {
                "type": "object",
                "required": ["papers"],
                "properties": {
                    "papers": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["title", "url"],
                            "properties": {
                                "title": {"type": "string"},
                                "url": {"type": "string"},
                                "authors": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                                "year": {"type": "integer"},
                                "doi": {"type": "string"},
                                "journal": {"type": "string"},
                                "is_peer_reviewed": {"type": "boolean"},
                            },
                        },
                    },
                },
            },
        }

    @staticmethod
    def _parse_enrichment_response(data: dict) -> dict[str, dict]:
        """Parse the outputSchema enrichment response into URL -> metadata map."""
        output = data.get("output", {})
        if not isinstance(output, dict) or "content" not in output:
            return {}
        content = output["content"]
        if not isinstance(content, dict):
            return {}
        papers = content.get("papers", [])
        if not isinstance(papers, list):
            return {}
        result: dict[str, dict] = {}
        for paper in papers:
            if not isinstance(paper, dict):
                continue
            paper_url = str(paper.get("url", "")).strip()
            if not paper_url:
                continue
            enrichment = ExaConnector._extract_paper_enrichment(paper)
            if enrichment:
                result[paper_url] = enrichment
        return result

    @staticmethod
    def _extract_paper_enrichment(paper: dict) -> dict:
        """Extract enrichment fields from a single outputSchema paper record."""
        enrichment: dict = {}
        authors = paper.get("authors")
        valid_authors = (
            [a.strip() for a in authors if isinstance(a, str) and a.strip()]
            if isinstance(authors, list)
            else []
        )
        if valid_authors:
            enrichment["authors"] = tuple(valid_authors)
        year = paper.get("year")
        if isinstance(year, int) and (
            MIN_PUBLICATION_YEAR <= year <= current_max_publication_year()
        ):
            enrichment["year"] = year
        doi = str(paper.get("doi", "")).strip()
        if doi and doi.startswith("10."):
            enrichment["doi"] = doi
        journal = str(paper.get("journal", "")).strip().rstrip(".,;:")
        if journal and ExaConnector._validate_journal(journal):
            enrichment["journal"] = journal
        is_pr = paper.get("is_peer_reviewed")
        if isinstance(is_pr, bool):
            enrichment["is_peer_reviewed"] = is_pr
        return enrichment

    # --- abstract cleanup ---

    _BOILERPLATE_RE = re.compile(
        r"^(?:(?:skip\s+to\s+main\s+content|view\s+pdf"
        r"|download\s+(?:full\s+)?(?:issue|article|pdf)"
        r"|search\s+sciencedirect|open\s+access"
        r"|close|hide\s+(?:modal|popup|notification)"
        r"|cookies?\s+(?:preferences|settings)|sign\s+in|log\s+in"
        r"|register|access\s+options|buy\s+(?:this\s+)?article"
        r"|share\s+(?:this\s+)?article|article\s+metadata"
        r"|check\s+access|pdf\s+download"
        r")\s*[,.]?\s*)+",
        re.IGNORECASE,
    )
    _SECTION_HEADER_KEYWORDS = (
        r"Subjects?|Keywords?|References|Bibliography"
        r"|Cited\s+by|Cite\s+(?:this\s+)?article|Related\s+articles"
        r"|Download\s+PDF|Share|Figures?|Tables?|Acknowledgments?"
        r"|Appendix|Supplementary|Copyright|License|Publisher\s+note"
        r"|Funding|Data\s+availability|Code\s+availability"
        r"|Ethics\s+declarations?|Author\s+information"
        r"|Author\s+contributions?|Competing\s+interests?"
        r"|Additional\s+information|About\s+this\s+article|Comments"
    )
    _SECTION_HEADER_RE = re.compile(
        rf"^(?:###\s*)?(?:{_SECTION_HEADER_KEYWORDS})\s*:?\s*$"
        rf"|(?:###\s*)(?:{_SECTION_HEADER_KEYWORDS})\b\s*:?\s*",
        re.IGNORECASE | re.MULTILINE,
    )

    @staticmethod
    def _clean_abstract(title: str, raw_text: str) -> str:
        """Remove navigation boilerplate and duplicated title from Exa text."""
        if not raw_text:
            return ""
        text = raw_text
        # Strip boilerplate prefixes (up to 200 chars of prefix)
        prefix = text[:200]
        cleaned_prefix = ExaConnector._BOILERPLATE_RE.sub("", prefix)
        if cleaned_prefix != prefix:
            text = cleaned_prefix + text[200:]
        # Deduplicate title prefix
        norm_title = normalize_scholarly_text(title).lower()
        norm_text_start = normalize_scholarly_text(text[: len(title) + 50]).lower()
        if norm_title and norm_text_start.startswith(norm_title):
            text = text[len(title) :].lstrip(" .,-—–")
        # Strip section header lines
        text = ExaConnector._SECTION_HEADER_RE.sub("", text)
        # Collapse whitespace
        return re.sub(r"\s+", " ", text).strip()

    # --- citation metadata extraction from page text ---

    _JOURNAL_PATTERN = re.compile(
        r"(?:"
        r"published\s+in[:\s]+([A-Z][^\n,;]{3,120}?)\s*(?:,|;|\n|\.\s)"
        r"|journal\s*[:=]\s*([^\n,;]{3,120}?)\s*(?:,|;|\n|\.\s)"
        r"|([A-Z][a-zA-Z\s&]+(?:Journal|Review|Letters|Annals|Archive|"
        r"Proceedings|Transactions|Bulletin|Reports?|Communications|"
        r"Research|Studies|Science|Medicine|Physics|Chemistry|Biology|"
        r"Engineering|Computing|Informatics))\b"
        r")",
    )

    _JOURNAL_DENY = re.compile(
        r"\b(?:further\s+reading|related\s+articles?|see\s+also|references?|"
        r"pubmed|medline|abstract|introduction|background|methods?|results?|"
        r"discussion|conclusions?|acknowledg|funding|author\s+contrib|"
        r"competing\s+interest|data\s+availab|ethics\s+declar|supplement|"
        r"appendix|keywords?|subjects?|cite\s+this|cited\s+by|full\s+text|"
        r"open\s+access|download|sign\s+in|register|this\s+article|"
        r"the\s+author|articles?\s+for\s+further|scientific\s+journal\s+articles|"
        r"licensing\s+statements?|publication\s+(?:history|identifier|syntax)|"
        r"editorial\s+review|technical\s+series|nist\s+technical|approved\s+by|"
        r"issn|isbn|doi\s+is|available\s+at|accessed)\b",
        re.IGNORECASE,
    )

    _JOURNAL_MAX_LEN = 60

    _VOLUME_PATTERN = re.compile(
        r"\b(?:vol(?:ume)?\.?\s*(\d+)|v\.(\d+))\b",
        re.IGNORECASE,
    )

    _ISSUE_PATTERN = re.compile(
        r"\b(?:no\.?\s*(\d+)|issue\s+(\d+)|n\.(\d+))\b",
        re.IGNORECASE,
    )

    _PAGES_PATTERN = re.compile(
        r"\b(?:pp?\.?\s*(\d+\s*[-–]\s*\d+)|pages?\s+(\d+\s*[-–]\s*\d+))\b",
        re.IGNORECASE,
    )

    _CITATION_PATTERN = re.compile(
        r"(?:"
        r"(?P<journal>[A-Z][a-zA-Z\s.&]{2,80}?)\s*"
        r"(?:,\s*)?"
        r"(?P<year>(?:19|20)\d{2})\s*"
        r"[;:]\s*"
        r"(?P<volume>\d+)\s*"
        r"(?:\((?P<issue>\d+)\)\s*)?"
        r"[;:]?\s*"
        r"(?P<pages>\d+\s*[-–]\s*\d+)"
        r")",
    )

    @staticmethod
    def _validate_journal(journal: str) -> bool:
        """Return True if a candidate journal name looks credible.

        Free-text page dumps from Exa routinely contain section headers,
        citation fragments and publisher boilerplate that regexes mistake
        for a journal. Reject empty, bracket-bearing, oversized or
        boilerplate-marked candidates so the field is left empty and
        structured ``outputSchema`` enrichment can fill the real journal.
        """
        if not journal:
            return False
        if "[" in journal or "]" in journal:
            return False
        if len(journal) > ExaConnector._JOURNAL_MAX_LEN:
            return False
        return not ExaConnector._JOURNAL_DENY.search(journal)

    @staticmethod
    def _extract_journal_from_text(text: str) -> str:
        """Extract journal name from page text, rejecting boilerplate."""
        match = ExaConnector._JOURNAL_PATTERN.search(text)
        if not match:
            return ""
        journal = (match.group(1) or match.group(2) or match.group(3) or "").strip()
        if not ExaConnector._validate_journal(journal):
            return ""
        return journal.rstrip(".,;:")

    @staticmethod
    def _extract_volume_from_text(text: str) -> str:
        """Extract volume number from page text."""
        match = ExaConnector._VOLUME_PATTERN.search(text)
        if not match:
            return ""
        return match.group(1) or match.group(2) or ""

    @staticmethod
    def _extract_issue_from_text(text: str) -> str:
        """Extract issue number from page text."""
        match = ExaConnector._ISSUE_PATTERN.search(text)
        if not match:
            return ""
        return match.group(1) or match.group(2) or match.group(3) or ""

    @staticmethod
    def _extract_pages_from_text(text: str) -> str:
        """Extract page range from page text."""
        match = ExaConnector._PAGES_PATTERN.search(text)
        if not match:
            return ""
        return (match.group(1) or match.group(2) or "").replace("–", "-")

    @staticmethod
    def _apply_citation_match(
        cite_match: re.Match[str] | None,
    ) -> dict[str, str]:
        """Extract fields from a combined citation regex match."""
        if not cite_match:
            return {}
        result: dict[str, str] = {}
        for field in ("journal", "volume", "issue", "pages"):
            value = cite_match.group(field)
            if value:
                result[field] = value.strip().replace("–", "-")
        journal = result.get("journal", "")
        if journal:
            journal = journal.rstrip(".,;:")
        if journal and not ExaConnector._validate_journal(journal):
            result.pop("journal", None)
        else:
            result["journal"] = journal
        return result

    @staticmethod
    def _fill_citation_fallbacks(result: dict[str, str], text: str) -> dict[str, str]:
        """Fill missing citation fields from individual regex patterns."""
        fallbacks: dict[str, str] = {
            "journal": ExaConnector._extract_journal_from_text(text),
            "volume": ExaConnector._extract_volume_from_text(text),
            "issue": ExaConnector._extract_issue_from_text(text),
            "pages": ExaConnector._extract_pages_from_text(text),
        }
        for key, value in fallbacks.items():
            if key not in result and value:
                result[key] = value
        return result

    @staticmethod
    def _extract_citation_from_text(text: str) -> dict[str, str]:
        """Extract structured citation metadata from text.

        Tries a combined citation pattern first
        (e.g. "J Med Chem 2023;15(3):123-145"),
        then falls back to individual field patterns.
        """
        result = ExaConnector._apply_citation_match(
            ExaConnector._CITATION_PATTERN.search(text),
        )
        return ExaConnector._fill_citation_fallbacks(result, text)

    def _extract_from_payload(
        self,
        query: str,  # noqa: ARG002  # required by base class signature
        payload: dict,
        limit: int,
    ) -> list[RawArticle]:
        """Extract records from Exa search payload."""
        records = payload.get("results", [])
        if not isinstance(records, list):
            return []
        items: list[RawArticle] = []
        for rec in records:
            if not isinstance(rec, dict):
                continue
            title = str(rec.get("title") or rec.get("name") or "").strip()
            url_value = str(rec.get("url") or rec.get("id") or "").strip()
            raw_text = str(
                rec.get("text") or rec.get("content") or rec.get("snippet") or "",
            ).strip()
            text = self._clean_abstract(title, raw_text)
            highlights = rec.get("highlights") or []
            if isinstance(highlights, list):
                highlights_text = " ".join(
                    str(h) for h in highlights if isinstance(h, str)
                )
            else:
                highlights_text = ""
            authors = self._normalize_author_value(rec.get("author"))
            if not authors and isinstance(rec.get("authors"), (list, dict, str)):
                authors = self._normalize_author_value(rec.get("authors"))
            published_date = str(
                rec.get("publishedDate")
                or rec.get("published_date")
                or rec.get("publishedAt")
                or "",
            ).strip()
            year = self._extract_year(published_date)
            doi = self._extract_doi(f"{title} {text} {url_value}")
            citation = self._extract_citation_from_text(
                f"{title} {text} {highlights_text}",
            )
            journal = citation.get("journal", "")
            volume = citation.get("volume", "")
            issue = citation.get("issue", "")
            pages = citation.get("pages", "")
            combined = " ".join([title, text, journal, " ".join(authors)])
            if not title or not url_value:
                continue
            if not self._is_article_like_item(title, url_value, doi, year):
                continue
            evidence_source = f"{highlights_text} {combined}"
            peer_review_evidence = self._merge_evidence(
                "",
                evidence_source.lower(),
                PEER_REVIEW_TOKENS,
            )
            indexing_evidence = self._merge_evidence(
                "",
                evidence_source.lower(),
                INDEXING_TOKENS,
            )
            preprint_evidence = self._merge_evidence(
                "",
                evidence_source.lower(),
                PREPRINT_TOKENS,
            )
            items.append(
                self._raw(
                    title=title,
                    url=url_value,
                    abstract=text[:8000],
                    full_text=combined,
                    doi=doi,
                    year=year,
                    journal=journal,
                    authors=authors,
                    volume=volume,
                    issue=issue,
                    pages=pages,
                    peer_review_evidence=peer_review_evidence,
                    indexing_evidence=indexing_evidence,
                    preprint_evidence=preprint_evidence,
                ),
            )
            if len(items) >= limit:
                break
        return items
