"""HTML-mode and WebSocket-mode source connectors.

These connectors fetch HTML/XML/RSS payloads through the browser sidecar
(``BrowserTransport`` → cloakbrowser Chromium), which solves JS challenges
(BunnyCDN Shield, Cloudflare Turnstile) and presents a real-browser TLS
fingerprint. API-mode connectors are in api_connectors.py and talk to explicit
JSON endpoints over aiohttp.
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
from bs4 import BeautifulSoup, NavigableString

from apps.core.text import normalize_scholarly_text

from .base import (
    INDEXING_TOKENS,
    PEER_REVIEW_TOKENS,
    PREPRINT_TOKENS,
    BaseConnector,
    ConnectorFetchError,
    RawArticle,
    SourceProfile,
)

_HTTP_BAD_REQUEST = 400
_MIN_TITLE_LENGTH = 14
# CyberLeninka article-body container: holds the article text with a
# recommended/related article preview prepended and the bibliography
# interleaved, so the abstract must be located relative to the article's
# own title paragraph, not the first paragraph.
_CYBERLENINKA_ABSTRACT_MAX = 1500
# A journal-citation / volume marker requires a following number, so ordinary
# Russian prose (e.g. "in such cases", "etc.", "i.e.", "since") does not end
# the abstract run prematurely.
_CYBERLENINKA_CITATION_RE = re.compile(
    r"\b(?:Том\s+\d{1,4}|Т\.\s*\d{1,4}|вып\.?\s*\d{1,4}|"  # noqa: RUF001
    r"issn[\s:]*\d|udk[\s:]*\d|удк[\s:]*\d|"
    r"doi[\s:]*10\.\d{4,}|10\.\d{4,})",
    re.IGNORECASE,
)
# A bare issue sign ``№ N`` also marks a citation line, but it appears inside
# prose too ("в таблице № 3", "на рис. № 1"), so it only ends the abstract run
# when the whole paragraph is short enough to be a citation marker rather than
# a sentence (see ``_classify_cyberleninka_paragraph``).
_CYBERLENINKA_ISSUE_RE = re.compile(r"№\s*\d+", re.IGNORECASE)
_CYBERLENINKA_CODE_RE = re.compile(
    r"^\s*(?:from\s+\w|import\s+\w|>>>|#|def\s+\w|class\s+\w|\.{3})",
    re.IGNORECASE,
)
# A numbered reference line starts either with ``1.``/``1)`` (followed by a
# space, so a decimal sentence such as ``1.5 method`` is not matched) or with
# a square-bracketed index ``[1]`` (with optional trailing dot/space).
_CYBERLENINKA_REF_RE = re.compile(r"^\s*(?:\d+[\.\)]\s|\[\d+\][\s\.]+)")
_CYBERLENINKA_AFFILIATION_RE = re.compile(
    r"(?:университет|институт|росси[яй]|научный\s+руководитель|кафедра|"
    r"студент|аспирант|доцент|профессор|лаборатор)",
    re.IGNORECASE,
)
# A bare author line such as ``Иванов И.И., Петров П.П.`` (Russian surname with
# two initials) is skipped before the abstract run starts; the two-initial
# pattern is specific enough to avoid ordinary prose abbreviations.
_CYBERLENINKA_AUTHOR_RE = re.compile(r"\b[А-ЯЁA-Z]\.\s*[А-ЯЁA-Z]\.")  # noqa: RUF001
# Russian past-participle / finite-verb stems that mark a sentence (an
# abstract opener mentioning a university and continuing into prose)
# rather than a standalone affiliation/author noun phrase. Matched as a
# word prefix so gender/number endings of each stem all hit; a noun sharing
# a stem also matches. The guard biases toward keeping prose: a real
# affiliation line that happens to contain one of these participles (e.g.
# an institution "основан в 1755") is then kept too, but such phrasings are
# rare while openers carrying these verbs are common, so the trade-off
# favours keeping. Both ``yo`` and ``e`` forms are listed so OCR flattening
# of the dotted letter does not defeat the guard.
_CYBERLENINKA_VERB_STEMS_RE = re.compile(
    r"\b(?:предложен|разработан|рассмотрен|изучен|исследован|показан|"
    r"применён|применен|описан|представлен|обоснован|проанализирован|"
    r"получен|найден|определён|определен|установлен|доказан|вычислен|"
    r"построен|основан|направлен|реализован|апробирован|посвящён|посвящен|"
    r"проведён|проведен|сделан|выполнен|выявлен|обнаружен|сформулирован|"
    r"оценён|оценен|выбран|синтезирован|измерён|измерен|"
    r"рассматривается|исследуется|применяется|описывается|строится)\w*",
    re.IGNORECASE,
)
# Unambiguous bibliography/list headings: a paragraph that starts with these
# prefixes always ends the abstract run.
_CYBERLENINKA_STOP_HEADER_PREFIXES = frozenset(
    {"список ", "библиограф", "references"},
)
# Single-word heading stems (``Литература``, ``Источники``) that also appear in
# prose, so they are only treated as a stop header when the paragraph is short
# enough to be a heading rather than an abstract sentence.
_CYBERLENINKA_STOP_HEADER_STEMS = frozenset({"литература", "источники"})
_CYBERLENINKA_HEADER_MAX = 40
_CYBERLENINKA_SKIP_HEADERS = frozenset({"ключевые слова", "keywords"})
# Related-article preview prefixes that the body block prepends before the
# article's own title; normalized (alphanumeric-only, lowercased) so the
# matching against ``_cyberleninka_normalize`` output is direct.
_CYBERLENINKA_PREVIEW_PREFIXES = frozenset(
    {
        "смотрите также",
        "читайте также",
        "см также",
        "также см",
        "также читайте",
        "также смотрите",
        "также в этом номере",
        "читайте в номере",
        "смотрите в номере",
        "см в номере",
        "также в этом выпуске",
        "читайте в выпуске",
        "смотрите в выпуске",
        "см в выпуске",
        "читайте также в выпуске",
        "в этом номере",
        "в этом выпуске",
    },
)
_CYBERLENINKA_AFFILIATION_MAX = 200
_CYBERLENINKA_TITLE_FUZZ = 80
_CYBERLENINKA_TITLE_TOKEN_MIN = 0.6
_CYBERLENINKA_TITLE_OVERLAP_SLACK = 20


def _cyberleninka_normalize(text: str) -> str:
    """Lowercase, fold OCR variants, and collapse non-alphanumeric runs.

    The landing page sometimes renders the title with OCR variants (``й``
    flattened to ``и``, ``ё`` to ``e``), so both the raw title and the body
    paragraph are folded to the flattened form before matching. Used to
    fuzzy-match the article's title paragraph inside the body block
    regardless of punctuation or case differences.
    """
    folded = text.lower().replace("й", "и").replace("ё", "е")  # noqa: RUF001
    collapsed = re.sub(r"[^a-zа-я0-9]+", " ", folded)  # noqa: RUF001
    return re.sub(r"\s+", " ", collapsed).strip()


def _find_cyberleninka_title(paragraphs: list[str], norm_title: str) -> int:
    """Return the index of the paragraph matching the article title, or -1.

    The landing page sometimes renders the title with OCR variants (e.g. ``й``
    flattened to ``и``), so beyond an exact prefix match we also accept a short
    paragraph whose token overlap with the title clears
    ``_CYBERLENINKA_TITLE_TOKEN_MIN``. A related-article preview prepended to
    the body block can share topic words with the title, so paragraphs that
    begin with a preview prefix (``_CYBERLENINKA_PREVIEW_PREFIXES``) are never
    matched as the title; the overlap match is additionally capped at
    ``_CYBERLENINKA_TITLE_OVERLAP_SLACK`` beyond the title length, because a
    preview prepends a lead that makes it noticeably longer than the title.
    """
    title_tokens = frozenset(norm_title.split())
    for idx, para in enumerate(paragraphs):
        norm_para = _cyberleninka_normalize(para)
        if not norm_para:
            continue
        if any(
            norm_para.startswith(prefix) for prefix in _CYBERLENINKA_PREVIEW_PREFIXES
        ):
            continue
        if norm_para.startswith(norm_title):
            return idx
        if len(norm_para) > len(norm_title) + _CYBERLENINKA_TITLE_FUZZ:
            continue
        if (
            title_tokens
            and len(norm_para) <= len(norm_title) + _CYBERLENINKA_TITLE_OVERLAP_SLACK
            and _title_token_overlap(norm_para, title_tokens)
        ):
            return idx
    return -1


def _title_token_overlap(norm_para: str, title_tokens: frozenset[str]) -> bool:
    """Return True if the paragraph shares enough title tokens."""
    overlap = len(title_tokens & frozenset(norm_para.split()))
    return overlap / len(title_tokens) >= _CYBERLENINKA_TITLE_TOKEN_MIN


_CYBERLENINKA_BIBLIO_TAIL_CHARS = frozenset("0123456789-/")
# A bare issue sign may open the line or follow a citation separator -- a
# sentence period, a comma/semicolon (``Series 5, № 4``), or an em/en dash --
# but not a Cyrillic word glued with no separator (``в таблице № 3``), which
# is prose.
_CYBERLENINKA_ISSUE_SEP_CHARS = frozenset(".,;") | frozenset("\u2014\u2013")
# Short Russian prose abbreviations that may end in a period right before a
# bare issue sign (table, figure, see, page) are references, not citation
# separators, so a paragraph ending in one is kept rather than truncated.
_CYBERLENINKA_PROSE_ABBREV_PREFIXES = frozenset({"табл", "рис", "см", "стр"})


def _cyberleninka_issue_is_terminal(text: str) -> bool:
    """Return True when an issue-number token sits at the end of a citation.

    A journal citation that numbers by issue only -- without a volume,
    issue-word, ISSN, UDC, or DOI marker -- is not caught by
    ``_CYBERLENINKA_CITATION_RE``, so the bare issue sign is the only stop
    signal. Three rules keep prose out while still catching issue-only
    citations:

    1. The LAST issue sign is considered. An inline reference (``в таблице
       № 3``) followed later in the same paragraph by a trailing citation
       (``Вестник. № 12. 2023.``) would otherwise latch onto the inline sign
       and let the citation leak.
    2. The sign must open the line or follow a citation separator (period,
       comma, semicolon, or dash) -- not a glued word. A short prose
       abbreviation (``табл. № 3``) is a reference, so it is treated as prose.
    3. After the sign, only a bibliographic tail may follow: digits with
       ``-``/``/`` separators, an optional Russian page-number marker, an
       optional Russian year marker, and optional parentheses or a dash
       around the year. A sentence continuing past the sign is prose.
    """
    matches = list(_CYBERLENINKA_ISSUE_RE.finditer(text))
    if not matches:
        return False
    match = matches[-1]
    before = text[: match.start()].rstrip()
    if before:
        # Strip trailing citation separators *before* splitting so a prose
        # abbreviation stays the last token even when a dash/comma/period
        # separates it from the issue sign (``табл. \u2014 № 3``). Otherwise
        # the separator itself is the last token and the denylist misses the
        # reference, falsely marking prose as a terminal citation.
        stripped = before.rstrip(".,;:-\u2014\u2013")
        if stripped:
            last_token = (
                stripped.rsplit(maxsplit=1)[-1].rstrip(".,;:-\u2014\u2013").lower()
            )
            if last_token in _CYBERLENINKA_PROSE_ABBREV_PREFIXES:
                return False
        # The char immediately before the issue sign must be a citation
        # separator. ``stripped`` is empty only when ``before`` is all
        # separators (e.g. an OCR-degenerate ``— № 3``), so ``before[-1]`` is
        # itself a separator and this passes — matching pre-fix behavior. The
        # rsplit above is guarded because ``"".rsplit(maxsplit=1)`` is ``[]``.
        if before[-1] not in _CYBERLENINKA_ISSUE_SEP_CHARS:
            return False
    tail = re.sub(
        r"^[.,;:()\s\u2014\u2013]+|[.,;:()\s\u2014\u2013]+$",
        "",
        text[match.end() :],
    )
    if not tail:
        return True
    tail = re.sub(r"(?i)^(?:[Сс]\.|стр\.?)\s*", "", tail)  # noqa: RUF001
    tail = re.sub(r"(?i)\s*г\.?\s*$", "", tail)  # noqa: RUF001
    tail = re.sub(r"[\s.,;:()]+", "", tail)
    tail = tail.replace("\u2014", "-").replace("\u2013", "-")
    return bool(tail) and set(tail) <= _CYBERLENINKA_BIBLIO_TAIL_CHARS


def _classify_cyberleninka_paragraph(text: str, *, started: bool) -> str:
    """Classify a body paragraph relative to the abstract run.

    Returns ``"stop"`` when the paragraph ends the abstract (a journal
    citation, code snippet, numbered reference, or bibliography header),
    ``"skip"`` when it should be ignored without ending the run (blank line,
    keyword list, or a leading author/affiliation line), and ``"keep"`` when
    it is abstract prose to collect. A bare ``№ N`` token only ends the run
    when it is terminal (``_cyberleninka_issue_is_terminal``), so a prose
    sentence that happens to mention ``в таблице № 3`` is kept while an
    issue-only citation line still stops. Before any prose is collected, a
    terminal ``№ N`` is a pre-abstract metadata line (a numbered faculty or
    a UDC-less journal stamp) and is skipped rather than ending the run with
    an empty abstract; once prose has started it is the citation that stops
    the run. The affiliation skip runs BEFORE the terminal-issue stop so a
    pre-abstract affiliation line that happens to end in a terminal ``№ N``
    is skipped rather than stopping. The affiliation skip is suppressed when
    the line contains a finite-verb stem, so an abstract opener mentioning a
    university is kept rather than dropped as an affiliation.
    """
    if not text:
        return "skip"
    lowered = text.lower()
    if any(
        lowered.startswith(prefix) for prefix in _CYBERLENINKA_STOP_HEADER_PREFIXES
    ) or (
        len(text) <= _CYBERLENINKA_HEADER_MAX
        and any(lowered.startswith(stem) for stem in _CYBERLENINKA_STOP_HEADER_STEMS)
    ):
        return "stop"
    if any(lowered.startswith(header) for header in _CYBERLENINKA_SKIP_HEADERS):
        return "skip"
    if (
        _CYBERLENINKA_CODE_RE.match(text)
        or _CYBERLENINKA_REF_RE.match(text)
        or _CYBERLENINKA_CITATION_RE.search(text)
    ):
        return "stop"
    if (
        not started
        and len(text) < _CYBERLENINKA_AFFILIATION_MAX
        and not _CYBERLENINKA_VERB_STEMS_RE.search(text)
        and (
            _CYBERLENINKA_AFFILIATION_RE.search(text)
            or _CYBERLENINKA_AUTHOR_RE.search(text)
        )
    ):
        return "skip"
    # A terminal issue sign before any prose is a pre-abstract metadata line
    # (a numbered faculty, a UDC-less journal stamp) rather than the citation
    # that ends the abstract, so the run continues instead of ending with an
    # empty abstract; once prose has started it stops.
    terminal = _cyberleninka_issue_is_terminal(text)
    return ("stop" if started else "skip") if terminal else "keep"


logger = structlog.get_logger(__name__)


class CiNiiConnector(BaseConnector):
    """Ci Nii source connector.

    Queries the OpenSearch ``/articles`` endpoint, not ``/all``: ``/all``
    mixes journal articles with conference proceedings, books and
    dissertations, which surfaced as non-article garbage (e.g. ``"31st
    International Conference on Machine Learning (ICML 2014)"`` with no
    DOI/language and a 43-character abstract). ``/articles`` returns only
    journal articles carrying ``prism:publicationName``, ``dc:creator``,
    ``prism:publicationDate`` and a ``cir:DOI`` identifier.
    """

    profile = SourceProfile(
        source_key="cinii",
        search_url="https://cir.nii.ac.jp/opensearch/v2/articles",
        mode="api",
        query_param="q",
        result_selector=".search-result__item, .item, article, li",
        title_selector="h3 a, .title a, a[href]",
        abstract_selector=".snippet, .description, p",
        journal_selector=".publisher, .journal, .source",
        indexing_evidence="scopus web of science",
        language="",
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
            "https://cir.nii.ac.jp/opensearch/v2/articles"
            f"?format=json&q={quote_plus(query)}&lang=en&count={limit}"
        )

    def _extract_from_payload(
        self,
        query: str,  # noqa: ARG002  # required by base class signature
        payload: dict,
        limit: int,
    ) -> list[RawArticle]:
        """Extract from payload.

        CiNii OpenSearch items carry the publication date as
        ``prism:publicationDate`` (``dc:date`` is usually absent), the DOI as
        a ``dc:identifier`` entry typed ``cir:DOI``, and authors as the
        ``dc:creator`` list. The ``@id``/``link`` CRID is a long numeric
        identifier whose first four digits (often ``1970``) are not a year,
        so the year is read from the explicit date field first and the CRID
        is never fed to the year scanner.
        """
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
            journal = self._extract_cinii_journal(entry)
            abstract = str(entry.get("description") or "")
            authors_list = self._extract_cinii_authors(entry)
            doi = self._extract_cinii_doi(entry) or self._extract_doi(
                f"{title} {abstract}",
            )
            year = self._extract_year(
                str(entry.get("prism:publicationDate") or entry.get("dc:date") or ""),
            ) or self._extract_year(f"{title} {abstract} {journal}")
            authors = " ".join(authors_list).strip()
            combined = f"{title} {abstract} {authors} {journal}"
            items.append(
                self._raw(
                    title=title,
                    url=url_value,
                    abstract=abstract,
                    full_text=combined,
                    doi=doi,
                    year=year,
                    journal=journal,
                    authors=authors_list or None,
                    language=self._infer_cinii_language(f"{title} {abstract}"),
                ),
            )
        return items

    # Unicode block bounds for Japanese-script detection. Hiragana and
    # Katakana are Japanese-specific; CJK Unified Ideographs (Han) are shared
    # with Chinese but treated as Japanese here because CiNii is a Japanese
    # database whose CJK records are overwhelmingly Japanese.
    _HIRAGANA_START = 0x3040
    _KATAKANA_END = 0x30FF
    _CJK_IDEOGRAPH_START = 0x4E00
    _CJK_IDEOGRAPH_END = 0x9FFF
    _HALFWIDTH_KATAKANA_START = 0xFF66
    _HALFWIDTH_KATAKANA_END = 0xFF9F

    @classmethod
    def _infer_cinii_language(cls, text: str) -> str:
        """Infer the record language from title/abstract script.

        CiNii OpenSearch items omit ``dc:language``, and the source indexes
        both Japanese and English records, so a hardcoded ``ja`` profile
        default mislabels English proceedings. Hiragana and Katakana are
        Japanese-specific; Han ideographs without kana are ambiguous between
        Japanese and Chinese, but CiNii is a Japanese database where the
        overwhelming CJK majority is Japanese, so any CJK character resolves
        to ``ja``. Latin-only text resolves to empty (language unknown) rather
        than a wrong guess.
        """
        for char in text or "":
            code = ord(char)
            if (
                cls._HIRAGANA_START <= code <= cls._KATAKANA_END
                or cls._CJK_IDEOGRAPH_START <= code <= cls._CJK_IDEOGRAPH_END
                or cls._HALFWIDTH_KATAKANA_START <= code <= cls._HALFWIDTH_KATAKANA_END
            ):
                return "ja"
        return ""

    @staticmethod
    def _extract_cinii_journal(entry: dict) -> str:
        """Return the cleanest journal name from a CiNii item.

        Prefers ``prism:publicationName``, then ``dc:publisher``, then a
        string-valued ``dc:source``. ``dc:source`` may be a dict (``@id`` URI)
        or a list of dicts, which would render as garbage, so non-string
        shapes are skipped.
        """
        for key in ("prism:publicationName", "dc:publisher"):
            value = entry.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        source = entry.get("dc:source")
        if isinstance(source, str) and source.strip():
            return source.strip()
        if isinstance(source, list):
            for item in source:
                if isinstance(item, str) and item.strip():
                    return item.strip()
        return "CiNii"

    @staticmethod
    def _extract_cinii_authors(entry: dict) -> list[str]:
        """Return the ``dc:creator`` authors as a list of names."""
        creator = entry.get("dc:creator")
        if isinstance(creator, str):
            return [creator] if creator.strip() else []
        if isinstance(creator, list):
            return [str(c).strip() for c in creator if str(c).strip()]
        return []

    @staticmethod
    def _extract_cinii_doi(entry: dict) -> str:
        """Return the DOI from a ``cir:DOI`` ``dc:identifier`` entry."""
        identifiers = entry.get("dc:identifier")
        if not isinstance(identifiers, list):
            return ""
        for ident in identifiers:
            if not isinstance(ident, dict):
                continue
            if str(ident.get("@type", "")).lower() == "cir:doi":
                value = str(ident.get("@value", "")).strip()
                if value:
                    return value
        return ""


class SciEngineConnector(BaseConnector):
    """Sci Engine source connector.

    The public ``/search/search`` page is a JavaScript SPA shell that returns
    only navigation chrome (``Journals`` / ``Books`` / ``Collections`` ...),
    so results come from the POST ``/SciSearch/searchNew`` endpoint, which
    returns a ``relateList`` JSON array capped at 10 items per page regardless
    of the requested page size.
    """

    profile = SourceProfile(
        source_key="sciengine",
        search_url="https://www.sciengine.com/SciSearch/searchNew",
        mode="api",
        query_param="queryField_a",
        indexing_evidence="scopus web of science",
        language="zh-CN",
    )

    _PAGE_SIZE = 10  # server caps results at 10 per page
    _MAX_PAGES = 20  # safety bound for pagination

    def fetch(self, query: str, limit: int = 5) -> list[RawArticle]:
        """Fetch records from the upstream SciEngine search API.

        Pages are concatenated until ``limit`` is satisfied or an empty/short
        page is returned. ``ConnectorFetchError`` propagates so the ingestion
        service marks this source as failed instead of silently reporting zero
        articles as a success.
        """
        records: list[dict] = []
        page = 1
        while len(records) < limit and page <= self._MAX_PAGES:
            payload = self._request_search_page(query, page)
            relate = payload.get("relateList") or []
            if not isinstance(relate, list) or not relate:
                break
            records.extend(relate)
            if len(relate) < self._PAGE_SIZE:
                break
            page += 1
        return self._extract_from_payload(query, {"relateList": records}, limit)

    def _request_search_page(self, query: str, page: int) -> dict:
        """Request one SciEngine search page and return the parsed JSON."""
        form = {
            self.profile.query_param: query,
            "searchType": "all",
            "curpage": str(page),
            "dept": str(self._PAGE_SIZE),
        }
        result = self._transport.post_form(
            self.profile.search_url,
            form,
            accept="application/json, text/plain, */*",
        )
        body_text = result.body_text
        if body_text is None:
            body_text = result.body_bytes.decode("utf-8", errors="replace")
        try:
            return json.loads(body_text)
        except json.JSONDecodeError as exc:
            msg = "sciengine: invalid api json"
            raise ConnectorFetchError(msg) from exc

    def _extract_from_payload(
        self,
        query: str,  # noqa: ARG002  # required by base class signature
        payload: dict,
        limit: int,
    ) -> list[RawArticle]:
        """Extract from payload.

        Each ``relateList`` item carries ``doi`` (always present), ``pubYear``,
        ``title_en``/``title_cn``, ``fullname_en``/``fullname_cn`` (author
        lists) and ``intro_en``/``intro_cn`` (HTML abstract). There is no
        journal or article URL field, so the URL is rebuilt from the DOI and
        the journal is reported as ``SciEngine``.
        """
        records = payload.get("relateList") or []
        items: list[RawArticle] = []
        for rec in records[: limit * 3]:
            if not isinstance(rec, dict):
                continue
            title = str(rec.get("title_en") or rec.get("title_cn") or "").strip()
            doi = str(rec.get("doi") or "").strip()
            if not title or not doi:
                continue
            url_value = f"https://doi.org/{doi}"
            abstract = self._strip_html(
                str(rec.get("intro_en") or rec.get("intro_cn") or ""),
            )
            year = self._extract_year(
                str(rec.get("pubYear") or rec.get("pubDateStr") or ""),
            )
            authors_list = self._extract_sciengine_authors(rec)
            authors = " ".join(authors_list).strip()
            combined = f"{title} {abstract} {authors} SciEngine"
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
                    journal="SciEngine",
                    authors=authors_list or None,
                ),
            )
            if len(items) >= limit:
                break
        return items

    @staticmethod
    def _extract_sciengine_authors(rec: dict) -> list[str]:
        """Return the ``fullname_en``/``fullname_cn`` authors as a list."""
        for key in ("fullname_en", "fullname_cn"):
            value = rec.get(key)
            if isinstance(value, str) and value.strip():
                return [value.strip()]
            if isinstance(value, list):
                names = [str(a).strip() for a in value if str(a).strip()]
                if names:
                    return names
        return []

    @staticmethod
    def _strip_html(fragment: str) -> str:
        """Strip HTML tags and collapse whitespace from an abstract fragment."""
        if not fragment:
            return ""
        text = BeautifulSoup(fragment, "lxml").get_text(" ")
        return re.sub(r"\s+", " ", text).strip()


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
        payload = {
            "q": query,
            "size": max(3, limit * 2),
            "from": 0,
            "mode": "articles",
        }
        result = self._transport.post_json(
            "https://cyberleninka.ru/api/search",
            payload,
            accept="application/json, text/plain, */*",
        )
        body_text = result.body_text
        if body_text is None:
            body_text = result.body_bytes.decode("utf-8", errors="replace")
        try:
            data = json.loads(body_text)
        except json.JSONDecodeError as exc:
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

    def enrich_raw(self, raw: RawArticle) -> RawArticle:
        """Enrich a CyberLeninka raw article, backfilling an empty abstract.

        The search API returns an ``annotation`` for most articles, but a
        minority come back with an empty one, and the article landing page
        carries no ``description``/``citation_abstract`` meta tag, so the
        inherited ``enrich_raw`` leaves ``abstract`` empty. The page does
        render the article body inside ``div.ocr[itemprop="articleBody"]``,
        but that block is prepended with a recommended/related article preview
        and interleaved with the bibliography, so the abstract is the run of
        paragraphs that follows the article's *own* title paragraph (matched
        against ``raw.title``) and precedes the journal citation / code /
        reference block. Only when the API + meta abstracts are both empty
        is this fallback extraction run; it never overwrites a real abstract.
        """
        enriched = super().enrich_raw(raw)
        if enriched.abstract.strip():
            return enriched
        if not raw.url.startswith("http") or not raw.title.strip():
            return enriched
        try:
            html = self._request_text(
                raw.url,
                ocr_language=self._ocr_language(raw.language),
            )
            soup = self._sanitize_html_soup(BeautifulSoup(html, "lxml"))
        except (
            ValueError,
            RuntimeError,
            ConnectionError,
            TimeoutError,
            ConnectorFetchError,
        ):
            logger.warning(
                "cyberleninka: abstract-fallback request failed for %s",
                raw.url,
                exc_info=True,
            )
            return enriched
        abstract = self._extract_cyberleninka_abstract(soup, raw.title)
        if not abstract:
            return enriched
        return replace(enriched, abstract=abstract[:8000])

    @staticmethod
    def _extract_cyberleninka_abstract(soup: BeautifulSoup, title: str) -> str:
        """Locate the abstract paragraphs inside the article-body block.

        The ``div.ocr`` block leads with a related-article preview, so the
        article's own paragraphs are found by matching the title against each
        paragraph, then collecting the prose that follows (after the
        author/affiliation line) until a journal citation, code snippet,
        numbered reference, or section header ends the abstract run.
        """
        ocr = soup.select_one('div.ocr[itemprop="articleBody"]')
        if ocr is None:
            return ""
        paragraphs = [p.get_text(" ", strip=True) for p in ocr.find_all("p")]
        norm_title = _cyberleninka_normalize(title)
        if not norm_title:
            return ""
        title_idx = _find_cyberleninka_title(paragraphs, norm_title)
        if title_idx < 0:
            return ""
        collected: list[str] = []
        total = 0
        for para in paragraphs[title_idx + 1 :]:
            text = para.strip()
            action = _classify_cyberleninka_paragraph(text, started=bool(collected))
            if action == "stop":
                break
            if action == "skip":
                continue
            collected.append(text)
            total += len(text)
            if total >= _CYBERLENINKA_ABSTRACT_MAX:
                break
        return normalize_scholarly_text(" ".join(collected), max_length=2000)


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

        Raises ``ConnectorFetchError`` if the sidecar transport fails; the
        ``BrowserTransport`` already retries transient sidecar/network errors.
        """
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
        result = self._transport.post_form(
            "https://www.mathnet.ru/php/searchpapers_do.phtml?jrnid=&option_lang=eng",
            payload,
        )
        body_text = result.body_text
        if body_text is None:
            body_text = result.body_bytes.decode("utf-8", errors="replace")
        return body_text

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
        """Enrich a MathNet article with metadata parsed from its page.

        MathNet renders the citation head (``Authors, "Title", Journal``) in the
        ``<title>`` tag and the bibliographic line (journal, year, volume,
        issue, pages, DOI) in the first ``<i>`` element; labeled fields
        (Abstract, Keywords, Language) are ``<b>Label:</b>`` followed by sibling
        text. The previous linearized-text regex mismatched this structure
        (authors parsed empty, journal hardcoded to "MathNet.Ru", volume/issue/
        pages never matched because they live in ``<i>``, not in labeled ``<b>``
        fields). Parse the HTML structure directly instead.
        """
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

        title_text = soup.title.get_text(" ", strip=True) if soup.title else ""
        authors_blob, parsed_title = self._mathnet_citation_head(title_text)
        journal, volume, issue, pages, year_str, doi = self._mathnet_italics_meta(soup)
        abstract = self._mathnet_labeled_value(soup, "Abstract:")
        language = self._mathnet_language_code(
            self._mathnet_labeled_value(soup, "Language:"),
        )

        final_title = parsed_title or enriched.title
        final_journal = journal or enriched.journal
        authors = (
            self._split_authors(authors_blob) if authors_blob else enriched.authors
        )
        final_year = int(year_str) if year_str else enriched.year
        final_doi = doi or enriched.doi or self._extract_doi(page_text)

        full_text = " ".join(
            part
            for part in (
                final_title,
                authors_blob,
                final_journal,
                f"{volume}:{issue}" if volume or issue else "",
                pages,
                abstract,
                page_text,
            )
            if part
        )
        return replace(
            enriched,
            title=final_title[:900],
            abstract=abstract[:8000] if abstract else enriched.abstract,
            full_text=full_text[:20000],
            doi=final_doi,
            year=final_year,
            journal=final_journal[:300],
            authors=authors,
            volume=volume[:32],
            issue=issue[:32],
            pages=pages[:32],
            language=language or enriched.language,
        )

    @staticmethod
    def _mathnet_citation_head(title_text: str) -> tuple[str, str]:
        """Parse ``Authors, "Title", …`` from the MathNet ``<title>`` tag.

        Returns ``(authors_blob, title)`` — authors are the comma-separated
        names before the first quoted title; empty strings when the ``<title>``
        is absent or does not match the citation shape.
        """
        match = re.match(
            r"\s*(?P<authors>[^“”\"]+?)\s*,\s*[“”\"](?P<title>[^“”\"]+)[””\"]",
            title_text,
        )
        if not match:
            return "", ""
        return match.group("authors").strip(), match.group("title").strip()

    @staticmethod
    def _mathnet_italics_meta(
        soup: BeautifulSoup,
    ) -> tuple[str, str, str, str, str, str]:
        """Parse the bibliographic ``<i>`` line on a MathNet article page.

        The first ``<i>`` carrying the bibliographic line holds either
        ``Journal Full, Forthcoming paper`` (no volume/year/pages) or
        ``Journal Full, <year>, Volume V, Issue I, Pages P-Q DOI: https://doi.org/...``.
        Returns ``(journal, volume, issue, pages, year, doi)`` with empty
        strings where a field is absent (e.g. forthcoming papers).
        """
        line = ""
        for italics in soup.find_all("i"):
            text = re.sub(r"\s+", " ", italics.get_text(" ", strip=True)).strip()
            if "DOI:" in text or "Forthcoming" in text or "Volume" in text:
                line = text
                break
        if not line:
            return "", "", "", "", "", ""
        # Journal is the text before the year marker (published) or the
        # "Forthcoming" marker; anchoring avoids truncating journal names
        # that themselves contain a comma (e.g. "Transactions of the Moscow
        # Math. Society, Series A").
        journal_match = re.match(r"\s*(.+?)\s*,\s*(?:\d{4}|Forthcoming)", line)
        journal = (
            journal_match.group(1).strip()
            if journal_match
            else line.split(",", 1)[0].strip()
        )
        year_match = re.search(r",\s*(\d{4})\s*,", line)
        volume_match = re.search(r"Volume\s+(\d+)", line)
        issue_match = re.search(r"Issue\s+(\d+(?:\(\d+\))?)", line)
        # Page range uses an en-dash or hyphen; a lone page (errata, short
        # notes) is captured too.
        pages_match = re.search(r"Pages\s+(\d+(?:[–-]\d+)?)", line)  # noqa: RUF001
        doi_match = re.search(r"doi\.org/(10\.\S+?)(?:\s|$)", line, re.IGNORECASE)
        return (
            journal[:300],
            volume_match.group(1) if volume_match else "",
            issue_match.group(1) if issue_match else "",
            pages_match.group(1) if pages_match else "",
            year_match.group(1) if year_match else "",
            doi_match.group(1).rstrip(".,;)") if doi_match else "",
        )

    @staticmethod
    def _mathnet_labeled_value(soup: BeautifulSoup, label: str) -> str:
        """Return the text following a ``<b>Label:</b>`` field on a MathNet page.

        Labeled metadata (Abstract, Keywords, Language, …) is rendered as a
        ``<b>Label:</b>`` element followed by the value in sibling text/inline
        nodes, ending at the next ``<b>``. Walk those siblings and join their
        text so multi-line abstracts (with ``<br>`` separators) are captured
        whole rather than truncated at the first line break.
        """
        for bold in soup.find_all("b"):
            if bold.get_text(" ", strip=True) != label:
                continue
            parts: list[str] = []
            sibling = bold.next_sibling
            while sibling is not None:
                if isinstance(sibling, NavigableString):
                    parts.append(str(sibling))
                elif sibling.name == "b":
                    break
                elif sibling.name == "br":
                    parts.append(" ")
                else:
                    parts.append(sibling.get_text(" ", strip=True))
                sibling = sibling.next_sibling
            value = re.sub(r"\s+", " ", " ".join(parts)).strip()
            if value:
                return value
        return ""

    @staticmethod
    def _mathnet_language_code(label: str) -> str:
        """Map a MathNet ``Language:`` label to an ISO 639-1 code."""
        return {
            "english": "en",
            "russian": "ru",
            "french": "fr",
            "german": "de",
        }.get(label.strip().lower(), "")

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

    _RSS_URL = "https://search.scielo.org/"

    def fetch(self, query: str, limit: int = 5) -> list[RawArticle]:
        """Fetch records from the upstream source.

        Prefer the SciELO RSS keyword search (real relevance) over the
        date-only OAI harvest, which returns arbitrary recent articles
        unrelated to the query. The RSS endpoint throttles repeated requests
        with HTTP 403, so a short backoff is applied before falling back to
        OAI and then HTML.
        """
        try:
            items = self._fetch_rss(query, limit)
            if items:
                return items
        except ConnectorFetchError:
            pass
        try:
            items = self._fetch_oai(query, limit)
            if items:
                return items
        except ConnectorFetchError:
            pass
        return self._fetch_html(query, limit)

    def _fetch_rss(self, query: str, limit: int) -> list[RawArticle]:
        """Fetch via the SciELO RSS search feed."""
        params = {"q": query, "count": str(max(limit, 5)), "output": "rss"}
        last_exc: ConnectorFetchError | None = None
        for attempt in range(3):
            try:
                text = self._request_text(self._RSS_URL, params=params)
            except ConnectorFetchError as exc:
                last_exc = exc
                time.sleep(8 * (attempt + 1))
                continue
            return self._parse_rss(text, query, limit)
        msg = f"scielo: rss search throttled: {last_exc}"
        raise ConnectorFetchError(msg)

    def _parse_rss(
        self,
        xml_text: str,
        query: str,  # noqa: ARG002  # RSS is a real keyword search; relevance is inherent
        limit: int,
    ) -> list[RawArticle]:
        """Parse SciELO RSS items into RawArticles.

        Titles are multilingual ``primary / secondary / tertiary`` strings;
        the first segment is the article's primary title. The publication
        year is encoded in the SciELO PID inside the resource link
        (``S{issn}{YYYY}{volume}{issue}{article}-{collection}``).
        """
        soup = BeautifulSoup(xml_text, "xml")
        items: list[RawArticle] = []
        for node in soup.find_all("item"):
            title_node = node.find("title")
            title_raw = title_node.get_text(" ", strip=True) if title_node else ""
            title = title_raw.split(" / ")[0].strip() if title_raw else ""
            if not title:
                continue
            link_node = node.find("link")
            url_value = link_node.get_text(strip=True) if link_node else ""
            if not url_value:
                continue
            desc_node = node.find("description")
            desc = desc_node.get_text(" ", strip=True) if desc_node else ""
            author_node = node.find("author")
            author = author_node.get_text(" ", strip=True) if author_node else ""
            authors = self._split_rss_authors(author)
            abstract = self._clean_rss_abstract(desc)
            year = self._extract_year(url_value) or self._extract_year(desc)
            doi = self._extract_doi(desc) or self._extract_doi(url_value)
            journal = "SciELO"
            combined = f"{title} {abstract} {author} {journal} {url_value} {desc}"
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

    @staticmethod
    def _split_rss_authors(author_blob: str) -> tuple[str, ...]:
        """Split a SciELO RSS ``<author>`` blob into individual authors.

        SciELO RSS author lists are semicolon-separated, each entry a
        ``Last, First`` pair (e.g. ``Cervantes-Guerrero, Mario Daniel;
        Galván-Tejada, Carlos E.``). Splitting on ``;`` preserves the
        ``Last, First`` comma inside each name; a single name with no
        semicolon yields a one-element tuple. Empty entries and exact
        duplicates are dropped, order preserved.
        """
        cleaned = re.sub(r"\s+", " ", author_blob or "").strip().strip(";")
        if not cleaned:
            return ()
        parts = [part.strip(" ,;") for part in cleaned.split(";") if part.strip(" ,;")]
        return tuple(dict.fromkeys(parts))

    @staticmethod
    def _clean_rss_abstract(desc: str) -> str:
        """Strip the leading author list and language label from an RSS description.

        SciELO RSS descriptions are structured as::

            Autor(es): <author list> Resumo em <lang> <Resumen|Resumo|Abstract> <text>

        The ``Resumo em <lang>`` token is a label meaning "Abstract in
        <language>", not the abstract itself; the real abstract begins after
        the language-specific header word that follows it. Return that block,
        or the cleaned tail when no marker is present.
        """
        if not desc:
            return ""
        header = (
            r"Resumo em \w+\s+(?:Resum[oa]s?|Resumen|Abstract|"
            r"RESUM[OA]S?|RESUMEN|ABSTRACT)\b"
        )
        match = re.search(header, desc, re.IGNORECASE)
        if match:
            return desc[match.end() :].strip()[:1500]
        marker = re.search(
            r"(Resum[oa]s?|Resumen|Abstract|RESUM[OA]S?|RESUMEN|ABSTRACT)\b",
            desc,
            re.IGNORECASE,
        )
        if marker:
            return desc[marker.end() :].strip()[:1500]
        # No abstract block: the description is only the author list.
        return ""

    _OAI_MAX_RESUMPTION_PAGES = 8
    _MIN_QUERY_TERM_LENGTH = 3

    @staticmethod
    def _query_terms(query: str) -> list[str]:
        """Return normalized query terms for OAI post-filtering.

        OAI-PMH ``ListRecords`` has no text-search parameter, so relevance
        is enforced client-side by matching query terms against each
        record's text fields. Terms are lowercased and stripped of
        punctuation; tokens shorter than three characters are dropped as
        noise. An empty result means the query carried no usable terms,
        in which case the caller skips filtering.
        """
        return [
            raw
            for raw in re.split(r"[^\w]+", (query or "").lower())
            if len(raw) >= SciELOConnector._MIN_QUERY_TERM_LENGTH
        ]

    @staticmethod
    def _clean_oai_journal(raw: str) -> str:
        """Strip the volume/issue/year tail from a SciELO OAI ``dc:source``.

        SciELO OAI records encode the venue as ``<journal title> v.24
        n.4 2017`` — the volume, issue and year belong on the article,
        not the journal field. Truncate at the first volume/issue
        marker so ``journal`` carries only the publication name.
        """
        if not raw:
            return ""
        return re.sub(
            r"\s+(?:v|vol|n|no|nº|num)\.?\s*\d.*$",
            "",
            raw,
            flags=re.IGNORECASE,
        ).strip()

    @staticmethod
    def _article_matches_terms(article: RawArticle, terms: list[str]) -> bool:
        """Return True if every query term appears in the article's text fields.

        ``all`` (AND) semantics, not ``any``: a ``machine learning`` query
        must surface records containing both ``machine`` and ``learning``.
        ``any`` matched single tokens and produced false positives such as
        economic ``learning by doing`` or pedagogical ``Blended learning``
        records that share the word ``learning`` with the query but are not
        about machine learning.
        """
        haystack = " ".join(
            [
                article.title or "",
                article.abstract or "",
                article.full_text or "",
                article.journal or "",
            ],
        ).lower()
        return all(term in haystack for term in terms)

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
        journal = self._clean_oai_journal(sources[0]) if sources else ""
        journal = journal or "SciELO"
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

    def _fetch_oai(self, query: str, limit: int) -> list[RawArticle]:
        """Fetch OAI.

        OAI-PMH ``ListRecords`` is date-based and carries no text-search
        parameter, so records are post-filtered against the query terms:
        only articles whose title/abstract/subjects/journal contain
        every query term (AND semantics) are kept. The harvest iterates
        resumption tokens until ``limit`` relevant articles are collected
        or ``_OAI_MAX_RESUMPTION_PAGES`` pages are exhausted, after which
        the fallback raises so the caller moves on rather than returning
        query-irrelevant records. When the query yields no usable terms
        (e.g. empty or stop-word-only), filtering is skipped.
        """
        terms = self._query_terms(query)
        current_year = datetime.now(UTC).year
        from_date = f"{max(2000, current_year - 8)}-01-01"
        for endpoint in self.OA_MIRRORS:
            url = f"{endpoint}?verb=ListRecords&metadataPrefix=oai_dc&from={from_date}"
            items: list[RawArticle] = []
            token: str | None = None
            try:
                for _ in range(self._OAI_MAX_RESUMPTION_PAGES):
                    response_xml = self._request_xml_text(url)
                    soup = BeautifulSoup(response_xml, "xml")
                    records = soup.find_all("record")
                    for rec in records:
                        article = self._parse_oai_record(rec)
                        if article is None:
                            continue
                        if terms and not self._article_matches_terms(article, terms):
                            continue
                        items.append(article)
                        if len(items) >= limit:
                            return items
                    token_node = soup.find("resumptionToken")
                    token = token_node.get_text(strip=True) if token_node else ""
                    if not token:
                        break
                    url = (
                        f"{endpoint}?verb=ListRecords"
                        f"&resumptionToken={quote_plus(token)}"
                    )
            except ConnectorFetchError:
                # A transport error on one mirror (read timeout, HTTP 503,
                # connection reset) must not abort the whole SciELO fetch.
                # Discard the partial harvest from the failing mirror and
                # fall through to the next endpoint in ``OA_MIRRORS``.
                continue
            if items:
                return items[:limit]
        msg = "scielo: oai mirrors yielded no query-relevant entries"
        raise ConnectorFetchError(
            msg,
        )

    def _request_xml_text(self, url: str) -> str:
        """Send a request for XML text.

        Transport failures (read timeout, connection reset, DNS failure,
        sidecar 502/504) are raised as ``ConnectorFetchError`` by
        ``BrowserTransport``, so ``_fetch_oai`` can move on to the next OAI
        mirror instead of aborting the whole SciELO fetch. A transport error
        on one mirror must not propagate past the ``except
        ConnectorFetchError`` guard in ``fetch``.
        """
        result = self._transport.fetch(
            url,
            accept="application/xml,text/xml,*/*",
        )
        return result.body_bytes.decode("utf-8", errors="replace")

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

    # --- ArticleMeta enrichment -------------------------------------------

    _ARTICLEMETA_API = "https://articlemeta.scielo.org/api/v1/article/"
    _ARTICLEMETA_TIMEOUT_SECONDS = 20.0
    _ABSTRACT_LABEL_RE = re.compile(
        r"^(?:RESUM[EO]S?|RESUMEN(?:ES)?|ABSTRACTS?|SUMMAR(?:Y|IES)|"
        r"ZUSAMMENFASSUNG(?:EN)?|RIASSUNT[OI]|SAMENVATTING(?:EN)?|"
        r"RÉSUMÉS?)\s+",
        re.IGNORECASE,
    )
    _RESOURCE_PID_RE = re.compile(
        r"/resource/[a-z]+/(S[A-Za-z0-9-]+)(?:[/?#]|$)",
    )
    _QUERY_PID_RE = re.compile(r"[?&]pid=(S[A-Za-z0-9-]+)")
    _COLLECTION_RE = re.compile(r"^(S.*)-([a-z]{2,5})$")

    def enrich_raw(self, raw: RawArticle) -> RawArticle:
        """Enrich a SciELO raw article via the ArticleMeta REST API.

        SciELO RSS carries the title, authors and a Spanish abstract but
        hardcodes ``journal="SciELO"`` (RSS has no journal field) and lacks
        the DOI. The article landing page
        (``search.scielo.org/resource/<lang>/<PID>``) sits behind a BunnyCDN
        interstitial the browser sidecar cannot reliably clear within its
        navigation budget, so the inherited ``enrich_raw`` page fetch raises
        ``ConnectorFetchError`` and breaks ingestion. ArticleMeta
        (``articlemeta.scielo.org/api/v1/article/``) returns the same metadata
        as structured JSON with no challenge, so it is the clean enrichment
        source. On any ArticleMeta failure the RSS payload is returned
        unchanged — the article is still indexable, just without the enriched
        journal/DOI. This is graceful degradation, not a fabricated fallback:
        no journal or DOI is invented when the API is unreachable.
        """
        if not raw.url.startswith("http"):
            return raw
        pid = self._scielo_pid_from_url(raw.url)
        if pid is None:
            return raw
        code, collection = pid
        data = self._fetch_articlemeta(code, collection)
        if not isinstance(data, dict):
            return raw
        journal = self._articlemeta_journal(data) or raw.journal
        doi = self._articlemeta_doi(data) or raw.doi
        year = self._articlemeta_year(data) or raw.year
        abstract = self._articlemeta_abstract(data) or raw.abstract
        authors = self._articlemeta_authors(data) or raw.authors
        combined = " ".join(
            [raw.title or "", abstract or "", journal or "", " ".join(authors)],
        )
        peer_review_evidence = self._merge_evidence(
            raw.peer_review_evidence,
            combined,
            PEER_REVIEW_TOKENS,
        )
        indexing_evidence = self._merge_evidence(
            raw.indexing_evidence,
            combined,
            INDEXING_TOKENS,
        )
        preprint_evidence = self._merge_evidence(
            raw.preprint_evidence,
            combined,
            PREPRINT_TOKENS,
        )
        full_text = normalize_scholarly_text(f"{raw.title} {abstract} {combined}")
        return replace(
            raw,
            doi=doi,
            year=year,
            journal=normalize_scholarly_text(journal, max_length=300),
            abstract=(abstract or "")[:8000],
            authors=authors,
            full_text=full_text,
            peer_review_evidence=peer_review_evidence[:3000],
            indexing_evidence=indexing_evidence[:3000],
            preprint_evidence=preprint_evidence[:3000],
        )

    @classmethod
    def _scielo_pid_from_url(cls, url: str) -> tuple[str, str | None] | None:
        """Extract the SciELO PID code and optional collection from a URL.

        Two URL shapes carry a PID:

        - ``search.scielo.org/resource/<lang>/<code>[-<collection>]`` (RSS);
        - ``...scielo...php?...&pid=<code>&...`` (OAI/article), with no
          collection suffix (ArticleMeta resolves by code alone).

        Returns ``(code, collection_or_none)`` or ``None`` when no PID is
        present. The collection is split off the resource PID by matching a
        trailing ``-<2-5 lowercase letters>`` suffix, so the hyphen inside an
        ISSN (``S2960-2467…``) is never mistaken for the separator.
        """
        if not url:
            return None
        match = cls._RESOURCE_PID_RE.search(url)
        if match:
            pid = match.group(1)
            coll = cls._COLLECTION_RE.match(pid)
            if coll:
                return coll.group(1), coll.group(2)
            return pid, None
        match = cls._QUERY_PID_RE.search(url)
        if match:
            return match.group(1), None
        return None

    def _fetch_articlemeta(
        self,
        code: str,
        collection: str | None,
    ) -> dict | None:
        """Fetch the ArticleMeta record for a SciELO PID via aiohttp.

        Returns the parsed JSON document, or ``None`` on any network, HTTP or
        parse failure so the caller can fall back to the RSS payload. Uses an
        ``asyncio.run`` bridge (the connector runs sync inside a prefork
        celery worker with no event loop), mirroring ``MedknowConnector``.
        """
        params: dict[str, str] = {"code": code}
        if collection:
            params["collection"] = collection

        async def _fetch() -> dict:
            async with (
                aiohttp.ClientSession(trust_env=True) as session,
                session.get(
                    self._ARTICLEMETA_API,
                    params=params,
                    timeout=aiohttp.ClientTimeout(
                        total=self._ARTICLEMETA_TIMEOUT_SECONDS,
                    ),
                ) as resp,
            ):
                resp.raise_for_status()
                return await resp.json()

        import asyncio as _asyncio  # noqa: PLC0415  # lazy import; celery prefork has no loop

        try:
            return _asyncio.run(_fetch())
        except (
            ValueError,
            RuntimeError,
            ConnectionError,
            TimeoutError,
            aiohttp.ClientError,
        ):
            logger.warning(
                "scielo: articlemeta fetch failed for %s/%s",
                code,
                collection,
                exc_info=True,
            )
            return None

    @staticmethod
    def _first_field(block: list[dict] | None) -> str:
        """Return the ``_`` value of the first entry in an ISIS field list."""
        if not block or not isinstance(block, list):
            return ""
        first = block[0]
        if not isinstance(first, dict):
            return ""
        value = first.get("_")
        return str(value).strip() if value is not None else ""

    def _articlemeta_journal(self, data: dict) -> str:
        """Return the full journal name from ArticleMeta ``title.v100``."""
        title_block = data.get("title") or {}
        if not isinstance(title_block, dict):
            return ""
        return self._first_field(title_block.get("v100"))

    def _articlemeta_doi(self, data: dict) -> str:
        """Return the DOI from ArticleMeta (top-level ``doi`` or ``v237``)."""
        doi = str(data.get("doi") or "").strip()
        if doi:
            return doi
        article = data.get("article") or {}
        if not isinstance(article, dict):
            return ""
        v237 = article.get("v237")
        if isinstance(v237, list) and v237 and isinstance(v237[0], dict):
            return str(v237[0].get("_") or "").strip()
        return ""

    @staticmethod
    def _articlemeta_year(data: dict) -> int | None:
        """Return the publication year from ArticleMeta ``publication_year``."""
        year = str(data.get("publication_year") or "").strip()
        if year.isdigit():
            return int(year)
        return None

    def _articlemeta_abstract(self, data: dict) -> str:
        """Return the cleanest abstract from ArticleMeta ``article.v83``.

        ``v83`` entries carry a language-prefixed label (``Abstract `` /
        ``Resumen `` / ``Resumo ``) in the ``a`` field. Prefer the English
        abstract, then the article's original language (``v40``), then the
        first available. The leading label is stripped.
        """
        article = data.get("article") or {}
        if not isinstance(article, dict):
            return ""
        entries = article.get("v83")
        if not isinstance(entries, list) or not entries:
            return ""
        original = self._first_field(article.get("v40")).lower()
        chosen: dict | None = None
        for entry in entries:
            if str(entry.get("l") or "").lower() == "en":
                chosen = entry
                break
        if chosen is None and original:
            for entry in entries:
                if str(entry.get("l") or "").lower() == original:
                    chosen = entry
                    break
        if chosen is None:
            chosen = entries[0]
        if not isinstance(chosen, dict):
            return ""
        text = str(chosen.get("a") or "").strip()
        text = self._ABSTRACT_LABEL_RE.sub("", text, count=1).strip()
        return text[:8000]

    def _articlemeta_authors(self, data: dict) -> tuple[str, ...]:
        """Return authors from ArticleMeta ``article.v10`` as ``surname, given``.

        ``v10`` entries use ``s`` for the surname and ``n`` for the given
        name; both are optional. Entries missing both are dropped. Order is
        preserved and exact duplicates are removed.
        """
        article = data.get("article") or {}
        if not isinstance(article, dict):
            return ()
        entries = article.get("v10")
        if not isinstance(entries, list):
            return ()
        authors: list[str] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            surname = str(entry.get("s") or "").strip()
            given = str(entry.get("n") or "").strip()
            if surname and given:
                name = f"{surname}, {given}"
            elif surname or given:
                name = surname or given
            else:
                continue
            authors.append(name)
        return tuple(dict.fromkeys(authors))


class PerseeConnector(BaseConnector):
    """Persee source connector."""

    profile = SourceProfile(
        source_key="persee",
        search_url="https://www.persee.fr/search",
        result_selector=".doc-result",
        title_selector="a.title",
        abstract_selector=".searchContext",
        journal_selector=".documentBibRef .collection a",
        indexing_evidence="scopus web of science",
        language="fr",
    )
    _PERSEE_CITATION_RE = re.compile(
        r"\bPour citer cet article\b",
        re.IGNORECASE,
    )

    def fetch(self, query: str, limit: int = 5) -> list[RawArticle]:
        """Fetch records from the upstream source.

        Persee exposes a real keyword HTML search at ``/search?q=`` which
        returns article-level results with relevance ranking. The legacy OAI
        endpoint only offers ``set=persee:serie`` (journal-series records, not
        articles) and has no keyword search, so it is intentionally not used.
        """
        return self._fetch_html(query, limit)

    def _extract_from_html(
        self,
        query: str,  # noqa: ARG002  # required by base class signature
        soup: BeautifulSoup,
        limit: int,
    ) -> list[RawArticle]:
        """Parse Persee ``.doc-result`` containers into article records.

        Each result card exposes the article title as ``a.title`` (whose
        ``href`` carries a ``?q=`` search suffix that must be stripped),
        per-author spans under ``.contributors .name``, the journal as
        ``.documentBibRef .collection a``, the publication year inside
        ``.documentYear``, and a relevance-highlighted abstract in
        ``.searchContext``.
        """
        items: list[RawArticle] = []
        for node in soup.select(".doc-result"):
            title_node = node.select_one("a.title")
            if not title_node:
                continue
            title = title_node.get_text(" ", strip=True)
            url_value = title_node.get("href", "").split("?")[0]
            if not title or not url_value:
                continue
            authors_list = [
                n.get_text(" ", strip=True) for n in node.select(".contributors .name")
            ]
            authors = " ".join(authors_list).strip()
            journal_node = node.select_one(".documentBibRef .collection a")
            journal = (
                journal_node.get_text(" ", strip=True) if journal_node else "Persee"
            )
            node_text = node.get_text(" ", strip=True)
            year = self._extract_year(node_text) or self._extract_year(url_value)
            abstract_node = node.select_one(".searchContext")
            abstract = abstract_node.get_text(" ", strip=True) if abstract_node else ""
            # ``.searchContext`` is a relevance-highlighted snippet that Persee
            # concatenates from several document fragments; it regularly trails
            # into the "Pour citer cet article" citation block (authors,
            # journal, pages, DOI, affiliations). Strip from that marker so
            # the fallback abstract (used when enrichment cannot reach the
            # landing page) is the abstract fragment, not the bibliography.
            abstract = self._PERSEE_CITATION_RE.split(abstract, maxsplit=1)[0].strip()
            doi = self._extract_doi(node_text)
            combined = f"{title} {abstract} {authors} {journal} {url_value}"
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
                    authors=authors_list or None,
                ),
            )
            if len(items) >= limit:
                break
        return items


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
                    aiohttp.ClientSession(trust_env=True) as session,
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
        # Free OAI-PMH path first; HTML search is Cloudflare-Turnstile-gated
        # (requires a paid CapSolver key we do not use). OAI harvests by date
        # and filters for query relevance here. When OAI is reachable but finds
        # no relevant records we return an empty list rather than falling back
        # to the challenge-gated HTML search, which would only raise.
        """Fetch records from the upstream source."""
        try:
            return self._fetch_via_oai(query, limit)
        except ConnectorFetchError:
            return super()._fetch_html(query, limit)

    def _fetch_via_oai(self, query: str, limit: int) -> list[RawArticle]:
        """Fetch via OAI.

        Per-set ListRecords requests intermittently hit Cloudflare challenges;
        one failing set must not abort the whole harvest. Failed sets are
        skipped and the best-effort result is returned. If every set fails the
        harvest is treated as unreachable so the caller can fall back to HTML.
        """
        sets = self._list_sets()
        if not sets:
            msg = "dergipark: no OAI sets available"
            raise ConnectorFetchError(msg)

        max_sets = int(os.getenv("DERGIPARK_OAI_MAX_SETS", "60"))
        recency_year = datetime.now(UTC).year - 2
        results: list[RawArticle] = []
        failures = 0
        for set_spec, set_name in sets[:max_sets]:
            if len(results) >= limit:
                break
            url = (
                f"{self.OAI_BASE}?verb=ListRecords&metadataPrefix=oai_dc"
                f"&set={quote_plus(set_spec)}&from={recency_year}-01-01"
            )
            try:
                xml_text = self._request_text(url)
            except ConnectorFetchError:
                failures += 1
                continue
            parsed = self._parse_oai_records(
                xml_text,
                query,
                set_name,
                limit - len(results),
            )
            results.extend(parsed)
        if results:
            return results[:limit]
        if failures == min(max_sets, len(sets)):
            msg = "dergipark: all OAI sets cloudflare-gated"
            raise ConnectorFetchError(msg)
        return []

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
        query: str,
        set_name: str,
        remaining: int,
    ) -> list[RawArticle]:
        """Parse OAI records, keeping only query-relevant ones.

        OAI-PMH has no keyword search, so DergiPark's sets are harvested by
        date and filtered here against the query tokens to avoid returning
        off-topic articles from arbitrary journals.
        """
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
            subjects = [
                x.text.strip() for x in metadata.findall(".//dc:subject", ns) if x.text
            ]
            identifiers = [
                x.text.strip()
                for x in metadata.findall(".//dc:identifier", ns)
                if x.text
            ]
            text_blob = " ".join(
                [
                    title,
                    description,
                    " ".join(subjects),
                    " ".join(identifiers),
                    set_name,
                ],
            )
            if not self._matches_query(text_blob, query):
                continue
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
        """Fetch records from the upstream source.

        Hrčak exposes no keyword search API and its HTML search is
        Cloudflare-gated, so we harvest recent records via OAI-PMH (using a
        ``from`` datestamp window to skip the oldest archive pages) and filter
        them against the query tokens for relevance.
        """
        recency_year = datetime.now(UTC).year - 3
        url = (
            "https://hrcak.srce.hr/oai/?verb=ListRecords"
            f"&metadataPrefix=oai_dc&from={recency_year}-01-01"
        )
        items: list[RawArticle] = []
        max_pages = int(os.getenv("HRCAK_OAI_MAX_PAGES", "8"))
        for _ in range(max_pages):
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
        query: str,
        remaining: int,
    ) -> tuple[list[RawArticle], str]:
        """Parse OAI records, keeping only query-relevant ones."""
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
            subjects = [
                x.text.strip() for x in metadata.findall(".//dc:subject", ns) if x.text
            ]
            identifiers = [
                x.text.strip()
                for x in metadata.findall(".//dc:identifier", ns)
                if x.text
            ]
            url_value = next((x for x in identifiers if x.startswith("http")), "")
            date_value = metadata.findtext(
                ".//dc:date",
                default="",
                namespaces=ns,
            ).strip()
            combined = " ".join(
                [
                    title,
                    description,
                    " ".join(subjects),
                    " ".join(identifiers),
                    date_value,
                ],
            )
            if not self._matches_query(combined, query):
                continue
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
            # AJOL OAI ``dc:description`` sometimes carries only a page range
            # (e.g. ``"8-16"``) rather than a real abstract. The article page
            # exposes the true abstract in ``div.article-abstract`` — prefer it
            # whenever the OAI abstract is missing or looks like a page range.
            abstract = enriched.abstract
            page_abstract = self._extract_article_abstract(soup)
            if page_abstract and (
                not abstract or self._looks_like_page_range(abstract)
            ):
                abstract = page_abstract
            return replace(
                enriched,
                doi=doi,
                year=year,
                journal=journal[:300],
                abstract=abstract,
                full_text=" ".join([enriched.full_text, page_text[:12000]])[:20000],
            )
        except (ValueError, RuntimeError, ConnectionError):
            return enriched

    def _extract_article_abstract(self, soup: BeautifulSoup) -> str:
        """Return the article-page abstract text, or ``""`` if absent.

        AJOL article pages render the abstract inside
        ``div.article-abstract`` (and sometimes ``div.abstract`` / a
        ``section.abstract``). A ``citation_abstract`` meta tag is the
        fallback for templates that inline the abstract differently.
        """
        node = soup.select_one(
            "div.article-abstract p, div.abstract p, section.abstract p",
        )
        if node is None:
            # Fall back to the bare container only when no ``<p>`` is present.
            # ``select_one`` returns matches in document order, so listing the
            # bare ``div.article-abstract`` alongside the ``<p>`` selector would
            # always pick the container (it precedes its child) and leak any
            # heading text (e.g. a literal ``"Abstract"``) into the result.
            node = soup.select_one("div.article-abstract")
        if node:
            text = node.get_text(" ", strip=True)
            if text:
                return text[:4000]
        return self._extract_meta_content(soup, ["citation_abstract"])

    _PAGE_RANGE_MAX_LEN = 12

    @staticmethod
    def _looks_like_page_range(text: str) -> bool:
        """Return ``True`` when ``text`` is just a page range like ``"8-16"``.

        AJOL OAI ``dc:description`` is occasionally populated with the article
        page span instead of an abstract; such a value is not a usable
        abstract and should be replaced by the article-page abstract.
        """
        t = (text or "").strip()
        if not t or len(t) > AJOLConnector._PAGE_RANGE_MAX_LEN:
            return False
        return bool(re.fullmatch(r"\d+\s*[-–]\s*\d+", t))  # noqa: RUF001

    def _is_open_access_text(self, text: str) -> bool:
        """Return whether open access text."""
        lowered = (text or "").lower()
        if any(token in lowered for token in self.OA_NEGATIVE_MARKERS):
            return False
        if any(token in lowered for token in self.OA_POSITIVE_MARKERS):
            return True
        # AJOL article pages can omit explicit OA badges in some templates.
        return True
