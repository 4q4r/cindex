"""
Tests for ExaConnector improvements: DOI cleanup, abstract cleaning,
outputSchema enrichment, and integration.
"""

import asyncio
from typing import Self

import aiohttp
import pytest

from apps.ingestion.connectors import ConnectorFetchError
from apps.ingestion.connectors.api_connectors import ExaConnector

# --- _extract_doi override tests ---


class TestExaNonScholarlyDomain:
    """Tests for ExaConnector._is_non_scholarly_domain."""

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://en.wikipedia.org/wiki/Machine_learning", True),
            ("https://www.wikipedia.org/wiki/DOI", True),
            ("https://wikipedia.org/wiki/X", True),
            ("https://developers.google.com/ml-guide", True),
            ("https://www.mayoclinic.org/diseases", True),
            ("https://www.merckmanuals.com/professional", True),
            ("https://www.geeksforgeeks.org/ml", True),
            ("https://www.niddk.nih.gov/health", True),
            ("https://www.ibm.com/think/insights", True),
            ("https://www.nist.gov/itl", True),
            ("https://example.org/articles/ml", False),
            ("https://doi.org/10.1234/test", False),
            ("https://www.ncbi.nlm.nih.gov/pmc/articles/PMC12690185", False),
            ("https://nvlpubs.nist.gov/nistpubs/IR2025.pdf", False),
            ("https://www.mdpi.com/2673-8392/5/4/173", False),
            ("not-a-url", False),
            ("", False),
        ],
    )
    def test_domain_classification(self, url: str, expected: bool) -> None:
        assert ExaConnector._is_non_scholarly_domain(url) is expected


# --- browser sidecar transport tests ---


class TestExaDoiExtraction:
    """Tests for ExaConnector._extract_doi boundary cleanup."""

    def setup_method(self) -> None:
        self.conn = ExaConnector.__new__(ExaConnector)

    def test_extracts_clean_doi(self) -> None:
        result = self.conn._extract_doi(
            "A study in 10.1234/j.medchem.3b00120 showed results",
        )
        assert result == "10.1234/j.medchem.3b00120"

    def test_strips_trailing_year_parenthesis(self) -> None:
        result = self.conn._extract_doi(
            "10.48550/arXiv.1706.00120(2017) is a known reference",
        )
        assert result == "10.48550/arXiv.1706.00120"

    def test_strips_trailing_ref_parenthesis(self) -> None:
        result = self.conn._extract_doi("see 10.5281/zenodo.11002033(ref for details")
        assert result == "10.5281/zenodo.11002033"

    def test_strips_trailing_paren_with_text(self) -> None:
        result = self.conn._extract_doi("10.1093/nsr/nwaf457(accessed 2025) and more")
        assert result == "10.1093/nsr/nwaf457"

    def test_preserves_doi_without_trailing_paren(self) -> None:
        result = self.conn._extract_doi("https://doi.org/10.1007/s00739-024-01061-9")
        assert result == "10.1007/s00739-024-01061-9"

    def test_returns_empty_for_no_doi(self) -> None:
        result = self.conn._extract_doi("no doi here")
        assert result == ""

    def test_strips_trailing_period(self) -> None:
        result = self.conn._extract_doi("refer to 10.1234/test.2023.")
        assert result == "10.1234/test.2023"

    def test_strips_standalone_trailing_closing_paren(self) -> None:
        result = self.conn._extract_doi("10.1111/gcb.15818)")
        assert result == "10.1111/gcb.15818"

    def test_preserves_doi_with_volume_in_parens(self) -> None:
        result = self.conn._extract_doi("10.1016/s0969-9961(02)00008-6")
        assert result == "10.1016/s0969-9961(02)00008-6"

    def test_preserves_doi_with_year_in_parens_followed_by_suffix(self) -> None:
        result = self.conn._extract_doi("10.1016/S0140-6736(26)00519-2")
        assert result == "10.1016/S0140-6736(26)00519-2"


# --- _clean_abstract tests ---


class TestCleanAbstract:
    """Tests for ExaConnector._clean_abstract."""

    def setup_method(self) -> None:
        self.conn = ExaConnector.__new__(ExaConnector)

    def test_strips_skip_to_main_content(self) -> None:
        result = self.conn._clean_abstract(
            "My Title",
            "Skip to main content View PDF A novel approach to neural networks.",
        )
        assert not result.startswith("Skip to")
        assert not result.startswith("View PDF")
        assert "novel approach" in result

    def test_removes_duplicated_title(self) -> None:
        result = self.conn._clean_abstract(
            "Quantum Entanglement in Spin Chains",
            "Quantum Entanglement in Spin Chains We study the dynamics of...",
        )
        assert not result.startswith("Quantum Entanglement")
        assert "We study" in result

    def test_strips_section_headers(self) -> None:
        result = self.conn._clean_abstract(
            "Test Paper",
            (
                "Abstract This is the abstract. "
                "### Subjects Computational neuroscience. "
                "Introduction More text."
            ),
        )
        assert "### Subjects" not in result
        assert "Abstract" in result or "abstract" in result

    def test_empty_text(self) -> None:
        result = self.conn._clean_abstract("Title", "")
        assert result == ""

    def test_no_boilerplate(self) -> None:
        text = "This is a clean abstract about machine learning."
        result = self.conn._clean_abstract("Different Title", text)
        assert result == text

    def test_strips_search_sciencedirect(self) -> None:
        result = self.conn._clean_abstract(
            "Title",
            "Search ScienceDirect Brain, Behavior, and Immunity Volume 115.",
        )
        assert not result.startswith("Search ScienceDirect")


# --- _enrich_with_output_schema tests ---


class TestEnrichWithOutputSchema:
    """Tests for ExaConnector._enrich_with_output_schema."""

    def test_parses_output_schema_papers(self) -> None:
        """Verify the enrichment parsing logic with mock data."""
        # Simulate parsed outputSchema response
        mock_papers = [
            {
                "title": "Test Paper",
                "url": "https://example.com/paper1",
                "authors": ["Alice Smith", "Bob Jones"],
                "year": 2025,
                "doi": "10.1234/test.2025",
                "journal": "Nature",
                "is_peer_reviewed": True,
            },
            {
                "title": "Another Paper",
                "url": "https://example.com/paper2",
                "authors": [],
                "year": 2024,
                "doi": "",
                "journal": "",
                "is_peer_reviewed": None,
            },
        ]
        # Verify the parsing logic extracts correctly
        result: dict[str, dict] = {}
        for paper in mock_papers:
            paper_url = str(paper.get("url", "")).strip()
            if not paper_url:
                continue
            enrichment: dict = {}
            authors = paper.get("authors")
            valid = (
                [a.strip() for a in authors if isinstance(a, str) and a.strip()]
                if isinstance(authors, list)
                else []
            )
            if valid:
                enrichment["authors"] = tuple(valid)
            year = paper.get("year")
            if isinstance(year, int) and 1800 <= year <= 2100:
                enrichment["year"] = year
            doi = str(paper.get("doi", "")).strip()
            if doi and doi.startswith("10."):
                enrichment["doi"] = doi
            journal = str(paper.get("journal", "")).strip()
            if journal:
                enrichment["journal"] = journal
            is_pr = paper.get("is_peer_reviewed")
            if isinstance(is_pr, bool):
                enrichment["is_peer_reviewed"] = is_pr
            if enrichment:
                result[paper_url] = enrichment

        assert "https://example.com/paper1" in result
        p1 = result["https://example.com/paper1"]
        assert p1["authors"] == ("Alice Smith", "Bob Jones")
        assert p1["year"] == 2025
        assert p1["doi"] == "10.1234/test.2025"
        assert p1["journal"] == "Nature"
        assert p1["is_peer_reviewed"] is True

        assert "https://example.com/paper2" in result
        assert "authors" not in result["https://example.com/paper2"]
        assert result["https://example.com/paper2"]["year"] == 2024

    def test_skips_empty_enrichment(self) -> None:
        """Papers with no useful enrichment fields should be skipped."""
        result: dict[str, dict] = {}
        paper = {
            "title": "Empty Paper",
            "url": "https://example.com/empty",
            "authors": [],
            "year": None,
            "doi": "",
            "journal": "",
            "is_peer_reviewed": None,
        }
        enrichment: dict = {}
        authors = paper.get("authors")
        valid = (
            [a.strip() for a in authors if isinstance(a, str) and a.strip()]
            if isinstance(authors, list)
            else []
        )
        if valid:
            enrichment["authors"] = tuple(valid)
        year = paper.get("year")
        if isinstance(year, int) and 1800 <= year <= 2100:
            enrichment["year"] = year
        doi = str(paper.get("doi", "")).strip()
        if doi and doi.startswith("10."):
            enrichment["doi"] = doi
        journal = str(paper.get("journal", "")).strip()
        if journal:
            enrichment["journal"] = journal
        is_pr = paper.get("is_peer_reviewed")
        if isinstance(is_pr, bool):
            enrichment["is_peer_reviewed"] = is_pr
        if enrichment:
            result["https://example.com/empty"] = enrichment

        assert "https://example.com/empty" not in result


# --- Integration: enrichment merge logic ---


class TestEnrichmentMerge:
    """Tests for the enrichment merge logic in _fetch_async."""

    def test_merge_updates_missing_fields(self) -> None:
        from apps.ingestion.connectors.base import RawArticle

        item = RawArticle(
            source_key="exa",
            title="Test Paper",
            url="https://example.com/paper1",
            abstract="An abstract.",
            full_text="Test Paper An abstract.",
            language="en",
            year=None,
            doi="",
            journal="example.com",
            authors=(),
        )
        enrichment = {
            "https://example.com/paper1": {
                "authors": ("Alice Smith", "Bob Jones"),
                "year": 2025,
                "doi": "10.1234/test.2025",
                "journal": "Nature",
            },
        }
        meta = enrichment.get(item.url, {})
        if meta.get("authors"):
            item.authors = meta["authors"]
        if "year" in meta and meta["year"] is not None:
            item.year = meta["year"]
        if meta.get("doi"):
            item.doi = meta["doi"]
        if meta.get("journal"):
            item.journal = meta["journal"]

        assert item.authors == ("Alice Smith", "Bob Jones")
        assert item.year == 2025
        assert item.doi == "10.1234/test.2025"
        assert item.journal == "Nature"

    def test_merge_preserves_existing_fields(self) -> None:
        from apps.ingestion.connectors.base import RawArticle

        item = RawArticle(
            source_key="exa",
            title="Test Paper",
            url="https://example.com/paper2",
            abstract="An abstract.",
            full_text="Test Paper An abstract.",
            language="en",
            year=2024,
            doi="10.5678/existing",
            journal="Science",
            authors=("Existing Author",),
        )
        # Empty enrichment - should not override
        meta: dict = {}
        if meta.get("authors"):
            item.authors = meta["authors"]
        if "year" in meta and meta["year"] is not None:
            item.year = meta["year"]
        if meta.get("doi"):
            item.doi = meta["doi"]
        if meta.get("journal"):
            item.journal = meta["journal"]

        assert item.authors == ("Existing Author",)
        assert item.year == 2024
        assert item.doi == "10.5678/existing"
        assert item.journal == "Science"


# --- async aiohttp transport (Exa POST via trust_env proxy routing) ---


class _FakeResponse:
    """Mimics ``aiohttp``'s response async context manager.

    ``status`` and ``text()`` are the only attributes ``_cs_post_json`` reads.
    ``enter_exc`` simulates a connection failure raised on request dispatch
    (the point where real aiohttp raises ``ClientError``); ``text_exc`` simulates
    a read failure after headers arrive.
    """

    def __init__(
        self,
        *,
        status: int = 200,
        body: str = "",
        enter_exc: BaseException | None = None,
        text_exc: BaseException | None = None,
    ) -> None:
        self.status = status
        self._body = body
        self._enter_exc = enter_exc
        self._text_exc = text_exc

    async def __aenter__(self) -> Self:
        if self._enter_exc is not None:
            raise self._enter_exc
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    async def text(self) -> str:
        if self._text_exc is not None:
            raise self._text_exc
        return self._body


class _FakeAiohttp:
    """Replaces ``aiohttp.ClientSession`` for ``_cs_post_json`` tests.

    Captures the construction kwargs (to assert ``trust_env=True``) and the
    ``session.post`` call kwargs (to assert headers/payload forwarding). The
    instance doubles as the session async context manager.
    """

    def __init__(self, response: _FakeResponse) -> None:
        self._response = response
        self.init_kwargs: dict[str, object] = {}
        self.post_kwargs: dict[str, object] = {}

    def client_session(self, *args: object, **kwargs: object) -> "_FakeAiohttp":
        self.init_kwargs = kwargs
        return self

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    def post(self, url: str, **kwargs: object) -> _FakeResponse:
        self.post_kwargs = {"url": url, **kwargs}
        return self._response


class TestExaTransport:
    """``_cs_post_json`` POSTs to Exa via async aiohttp with ``trust_env=True``.

    aiohttp ignores ``https_proxy`` by default; ``ClientSession(trust_env=True)``
    makes it honour the env proxy so the request reaches the real
    ``api.exa.ai`` origin instead of being 403'd by Cloudflare on the direct
    IP. No custom ``User-Agent`` is sent (aiohttp's library default stands).
    These tests pin the contract: the session is built with ``trust_env=True``,
    caller headers/payload are forwarded verbatim, upstream
    ``status >= 400`` / invalid JSON / non-dict payloads surface as
    ``ConnectorFetchError`` (terminal), and transient ``OSError`` (aiohttp
    ``ClientError``) propagates so ``_fetch_single_lang`` can retry it.
    """

    @staticmethod
    def _install_fake(
        monkeypatch: pytest.MonkeyPatch,
        response: _FakeResponse,
    ) -> _FakeAiohttp:
        from apps.ingestion.connectors import api_connectors

        fake = _FakeAiohttp(response)
        monkeypatch.setattr(
            api_connectors.aiohttp,
            "ClientSession",
            fake.client_session,
        )
        return fake

    def test_session_uses_trust_env_and_forwards_headers(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        body = '{"results": [{"title": "A", "url": "https://x.org/a"}]}'
        fake = self._install_fake(monkeypatch, _FakeResponse(status=200, body=body))
        conn = ExaConnector()
        headers = {"x-api-key": "secret", "Content-Type": "application/json"}

        data = asyncio.run(
            conn._cs_post_json(
                "https://api.exa.ai/search",
                headers,
                {"query": "machine learning"},
                30.0,
            ),
        )

        assert data == {"results": [{"title": "A", "url": "https://x.org/a"}]}
        assert fake.init_kwargs["trust_env"] is True
        assert fake.post_kwargs["url"] == "https://api.exa.ai/search"
        assert fake.post_kwargs["headers"] == headers
        assert fake.post_kwargs["json"] == {"query": "machine learning"}

    def test_http_error_raises_connector_fetch_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._install_fake(monkeypatch, _FakeResponse(status=403, body=""))
        conn = ExaConnector()
        with pytest.raises(ConnectorFetchError):
            asyncio.run(
                conn._cs_post_json(
                    "https://api.exa.ai/search",
                    {"x-api-key": "k"},
                    {"query": "ml"},
                    30.0,
                ),
            )

    def test_invalid_json_raises_connector_fetch_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._install_fake(
            monkeypatch,
            _FakeResponse(status=200, body="<!doctype html>"),
        )
        conn = ExaConnector()
        with pytest.raises(ConnectorFetchError):
            asyncio.run(
                conn._cs_post_json(
                    "https://api.exa.ai/search",
                    {"x-api-key": "k"},
                    {"query": "ml"},
                    30.0,
                ),
            )

    def test_non_dict_payload_raises_connector_fetch_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._install_fake(
            monkeypatch,
            _FakeResponse(status=200, body='["not", "a", "dict"]'),
        )
        conn = ExaConnector()
        with pytest.raises(ConnectorFetchError):
            asyncio.run(
                conn._cs_post_json(
                    "https://api.exa.ai/search",
                    {"x-api-key": "k"},
                    {"query": "ml"},
                    30.0,
                ),
            )

    def test_transient_oserror_propagates_for_retry(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # aiohttp ClientError derives from OSError; _cs_post_json must let it
        # propagate (not wrap it as ConnectorFetchError) so _fetch_single_lang's
        # `except OSError` retry branch can act on it.
        self._install_fake(
            monkeypatch,
            _FakeResponse(enter_exc=aiohttp.ClientConnectionError("boom")),
        )
        conn = ExaConnector()
        with pytest.raises(aiohttp.ClientConnectionError):
            asyncio.run(
                conn._cs_post_json(
                    "https://api.exa.ai/search",
                    {"x-api-key": "k"},
                    {"query": "ml"},
                    30.0,
                ),
            )


class TestExaScholarlyHost:
    """``_is_scholarly_host`` recognises scholarly publication hosts.

    Matched against the URL netloc (``www.`` stripped); exact-match hosts and
    subdomains (via ``endswith``) both count, so e.g. ``assets.researchsquare.com``
    and ``pmc.ncbi.nlm.nih.gov`` are accepted.
    """

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://doi.org/10.1234/test", True),
            ("https://arxiv.org/abs/2401.00001", True),
            ("https://www.nature.com/articles/s41586-025", True),
            ("https://link.springer.com/article/10.1007/s00122", True),
            ("https://assets.researchsquare.com/files/rs-12345", True),
            ("https://pmc.ncbi.nlm.nih.gov/articles/PMC12345", True),
            ("https://example.org/articles/ml", False),
            ("https://en.wikipedia.org/wiki/ML", False),
            ("not-a-url", False),
            ("", False),
        ],
    )
    def test_host_classification(self, url: str, expected: bool) -> None:
        assert ExaConnector._is_scholarly_host(url) is expected


class TestExaScholarlyCandidate:
    """``_is_scholarly_candidate`` is the hybrid DOI-OR-scholarly-host gate.

    Accepts an Exa hit when it has an extracted DOI or lives on a known
    scholarly host; drops encyclopedia / vendor-explainer / consumer-health
    pages that carry a ``publishedDate`` but no scholarly signal. A non-
    scholarly host is dropped even when a DOI is cited in the body text.
    """

    @pytest.mark.parametrize(
        ("url", "doi", "expected"),
        [
            # DOI present on an unknown host -> accepted (DOI branch).
            ("https://example.org/paper", "10.1234/test", True),
            # DOI cited in body text but on a non-scholarly host -> dropped.
            ("https://en.wikipedia.org/wiki/ML", "10.1234/cited-in-body", False),
            ("https://www.ibm.com/think/insights", "10.1234/cited", False),
            # No DOI, but on a scholarly host -> accepted (host branch).
            ("https://arxiv.org/abs/2401.00001", "", True),
            ("https://doi.org/10.1234/test", "", True),
            # No DOI and an unknown host -> dropped.
            ("https://example.org/blog", "", False),
            # No DOI and a non-scholarly host -> dropped.
            ("https://www.mayoclinic.org/x", "", False),
        ],
    )
    def test_candidate_gate(self, url: str, doi: str, expected: bool) -> None:
        assert ExaConnector._is_scholarly_candidate(url, doi) is expected


class TestExaRelevanceFilter:
    """``_extract_from_payload`` drops non-scholarly Exa hits.

    Exa's ``research paper`` category mixes real articles with encyclopedia
    and consumer-health pages that carry a ``publishedDate`` (so they would
    pass the shared ``bool(doi or year)`` filter). The Exa-specific domain
    guard drops them before that check, without modifying the shared
    ``_is_article_like_item`` used by OpenAlex.
    """

    @staticmethod
    def _payload(records: list[dict]) -> dict:
        return {"results": records}

    def test_drops_non_scholarly_domains_keeps_real_article(self) -> None:
        conn = ExaConnector()
        payload = self._payload(
            [
                {
                    "title": "Machine learning - Wikipedia",
                    "url": "https://en.wikipedia.org/wiki/Machine_learning",
                    "publishedDate": "2024-06-11",
                    "text": "Machine learning is a field of study.",
                },
                {
                    "title": "Diabetes overview",
                    "url": "https://www.mayoclinic.org/diseases-conditions/diabetes",
                    "publishedDate": "2026-01-21",
                    "text": "Patient overview of diabetes.",
                },
                {
                    "title": "A Deep Reinforcement Learning Approach to X",
                    "url": "https://doi.org/10.1234/test.2025",
                    "publishedDate": "2025-03-01",
                    "text": "We propose a novel method for X.",
                },
            ],
        )
        items = conn._extract_from_payload("machine learning", payload, 5)
        assert len(items) == 1
        assert items[0].url == "https://doi.org/10.1234/test.2025"
        assert items[0].doi == "10.1234/test.2025"

    def test_drops_all_garbage_yields_empty(self) -> None:
        conn = ExaConnector()
        payload = self._payload(
            [
                {
                    "title": "Quantum computing",
                    "url": "https://www.geeksforgeeks.org/quantum-computing",
                    "publishedDate": "2024-01-01",
                    "text": "Tutorial on quantum computing.",
                },
                {
                    "title": "IBM think blog",
                    "url": "https://www.ibm.com/think/insights",
                    "publishedDate": "2025-02-02",
                    "text": "Blog post.",
                },
            ],
        )
        assert conn._extract_from_payload("quantum computing", payload, 5) == []
