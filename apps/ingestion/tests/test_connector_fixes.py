"""Tests for connector quick-win fixes: author/volume/journal/abstract extraction."""

import pytest

from apps.ingestion.connectors import (
    AJOLConnector,
    BaseConnector,
    CrossrefConnector,
    CyberLeninkaConnector,
    EuropePMCConnector,
    HALConnector,
    MathNetConnector,
    OpenAlexConnector,
    PMCConnector,
    SciELOConnector,
)


class TestEuropePCMAuthors:
    """EuropePMCConnector should extract authors from authorList and authorString."""

    def test_author_list_with_full_name(self) -> None:
        payload = {
            "resultList": {
                "result": [
                    {
                        "title": "Test article",
                        "abstractText": "Abstract",
                        "journalTitle": "Test J",
                        "doi": "10.1234/test",
                        "pubYear": "2024",
                        "fullTextUrlList": {
                            "fullTextUrl": [{"url": "https://example.com/article"}],
                        },
                        "authorList": {
                            "author": [
                                {"fullName": "Alice Smith"},
                                {"firstName": "Bob", "lastName": "Jones"},
                            ],
                        },
                    },
                ],
            },
        }
        conn = EuropePMCConnector()
        items = conn._extract_from_payload("test", payload, 5)
        assert len(items) == 1
        assert items[0].authors == ("Alice Smith", "Bob Jones")

    def test_author_string_fallback(self) -> None:
        payload = {
            "resultList": {
                "result": [
                    {
                        "title": "Test article",
                        "abstractText": "Abstract",
                        "journalTitle": "Test J",
                        "doi": "10.1234/test",
                        "pubYear": "2024",
                        "fullTextUrlList": {
                            "fullTextUrl": [{"url": "https://example.com/article"}],
                        },
                        "authorString": "Smith A, Jones B, Lee C",
                    },
                ],
            },
        }
        conn = EuropePMCConnector()
        items = conn._extract_from_payload("test", payload, 5)
        assert len(items) == 1
        assert items[0].authors == ("Smith A", "Jones B", "Lee C")

    def test_no_authors(self) -> None:
        payload = {
            "resultList": {
                "result": [
                    {
                        "title": "Test article",
                        "abstractText": "Abstract",
                        "journalTitle": "Test J",
                        "doi": "10.1234/test",
                        "pubYear": "2024",
                        "fullTextUrlList": {
                            "fullTextUrl": [{"url": "https://example.com/article"}],
                        },
                    },
                ],
            },
        }
        conn = EuropePMCConnector()
        items = conn._extract_from_payload("test", payload, 5)
        assert len(items) == 1
        assert items[0].authors == ()


class TestPCMAuthors:
    """PMCConnector should extract authors same as EuropePMC."""

    def test_author_list_extraction(self) -> None:
        payload = {
            "resultList": {
                "result": [
                    {
                        "title": "PMC article",
                        "abstractText": "Abstract",
                        "journalTitle": "PMC J",
                        "doi": "10.1234/pmc",
                        "pubYear": "2024",
                        "pmcid": "PMC1234567",
                        "authorList": {
                            "author": [
                                {"fullName": "Carol White"},
                                {"firstName": "Dave", "lastName": "Brown"},
                            ],
                        },
                    },
                ],
            },
        }
        conn = PMCConnector()
        items = conn._extract_from_payload("test", payload, 5)
        assert len(items) == 1
        assert items[0].authors == ("Carol White", "Dave Brown")


class TestEuropePMCJournal:
    """EuropePMCConnector should read journal from journalInfo.journal.title.

    EuropePMC's ``resultType=core`` response has no top-level ``journalTitle``
    field; the journal title lives at ``journalInfo.journal.title``. Reading
    the non-existent top-level field always fell back to ``"Europe PMC"``,
    masking the real venue for every record.
    """

    def test_journal_from_journal_info(self) -> None:
        payload = {
            "resultList": {
                "result": [
                    {
                        "title": "EPMC article",
                        "abstractText": "Abstract",
                        "doi": "10.1234/epmc",
                        "pubYear": "2024",
                        "fullTextUrlList": {
                            "fullTextUrl": [{"url": "https://example.org/a"}],
                        },
                        "journalInfo": {
                            "journal": {
                                "title": "Genetics in medicine",
                            },
                        },
                    },
                ],
            },
        }
        conn = EuropePMCConnector()
        items = conn._extract_from_payload("test", payload, 5)
        assert len(items) == 1
        assert items[0].journal == "Genetics in medicine"

    def test_falls_back_when_journal_info_missing(self) -> None:
        payload = {
            "resultList": {
                "result": [
                    {
                        "title": "EPMC no journal",
                        "abstractText": "Abstract",
                        "doi": "10.1234/epmc2",
                        "pubYear": "2024",
                        "fullTextUrlList": {
                            "fullTextUrl": [{"url": "https://example.org/b"}],
                        },
                    },
                ],
            },
        }
        conn = EuropePMCConnector()
        items = conn._extract_from_payload("test", payload, 5)
        assert len(items) == 1
        assert items[0].journal == "Europe PMC"

    def test_ignores_top_level_journal_title(self) -> None:
        """A stray top-level ``journalTitle`` must not be trusted.

        The real EuropePMC API never emits this field; if a payload carries
        it (e.g. a stale fixture), the connector must still read from
        ``journalInfo.journal.title`` and not regress to the old behaviour.
        """
        payload = {
            "resultList": {
                "result": [
                    {
                        "title": "EPMC stray field",
                        "abstractText": "Abstract",
                        "doi": "10.1234/epmc3",
                        "pubYear": "2024",
                        "fullTextUrlList": {
                            "fullTextUrl": [{"url": "https://example.org/c"}],
                        },
                        "journalTitle": "Stale Top-Level Value",
                        "journalInfo": {
                            "journal": {"title": "The Lancet"},
                        },
                    },
                ],
            },
        }
        conn = EuropePMCConnector()
        items = conn._extract_from_payload("test", payload, 5)
        assert len(items) == 1
        assert items[0].journal == "The Lancet"


class TestPMCJournal:
    """PMCConnector shares the EuropePMC API and the same journal bug."""

    def test_journal_from_journal_info(self) -> None:
        payload = {
            "resultList": {
                "result": [
                    {
                        "title": "PMC article",
                        "abstractText": "Abstract",
                        "doi": "10.1234/pmcj",
                        "pubYear": "2024",
                        "pmcid": "PMC2222222",
                        "journalInfo": {
                            "journal": {
                                "title": (
                                    "Journal of clinical and translational science"
                                ),
                            },
                        },
                    },
                ],
            },
        }
        conn = PMCConnector()
        items = conn._extract_from_payload("test", payload, 5)
        assert len(items) == 1
        assert items[0].journal == "Journal of clinical and translational science"

    def test_falls_back_when_journal_info_missing(self) -> None:
        payload = {
            "resultList": {
                "result": [
                    {
                        "title": "PMC no journal",
                        "abstractText": "Abstract",
                        "doi": "10.1234/pmcj2",
                        "pubYear": "2024",
                        "pmcid": "PMC3333333",
                    },
                ],
            },
        }
        conn = PMCConnector()
        items = conn._extract_from_payload("test", payload, 5)
        assert len(items) == 1
        assert items[0].journal == "PMC"


class TestCrossrefVolumeIssuePages:
    """CrossrefConnector should extract volume/issue/pages."""

    def test_extracts_all_biblio(self) -> None:
        payload = {
            "message": {
                "items": [
                    {
                        "title": ["Crossref article"],
                        "DOI": "10.1234/cr",
                        "author": [{"given": "A", "family": "B"}],
                        "container-title": ["Test Journal"],
                        "volume": "42",
                        "issue": "3",
                        "page": "101-115",
                        "published-print": {"date-parts": [[2024]]},
                    },
                ],
            },
        }
        conn = CrossrefConnector()
        items = conn._extract_from_payload("test", payload, 5)
        assert len(items) == 1
        assert items[0].volume == "42"
        assert items[0].issue == "3"
        assert items[0].pages == "101-115"

    def test_missing_biblio_fields(self) -> None:
        payload = {
            "message": {
                "items": [
                    {
                        "title": ["No biblio"],
                        "DOI": "10.1234/nobiblio",
                    },
                ],
            },
        }
        conn = CrossrefConnector()
        items = conn._extract_from_payload("test", payload, 5)
        assert len(items) == 1
        assert items[0].volume == ""
        assert items[0].issue == ""
        assert items[0].pages == ""


class TestCyberLeninkaAuthors:
    """CyberLeninkaConnector should extract authors from rec['authors']."""

    def test_authors_from_list(self) -> None:
        payload = {
            "articles": [
                {
                    "name": "A long CyberLeninka article title for testing",
                    "annotation": "Abstract",
                    "link": "/article/123",
                    "year": "2023",
                    "journal": "CL Journal",
                    "authors": ["Alice Smith", "Bob Jones"],
                },
            ],
        }
        conn = CyberLeninkaConnector()
        items = conn._extract_from_payload("test", payload, 5)
        assert len(items) == 1
        assert items[0].authors == ("Alice Smith", "Bob Jones")

    def test_no_authors_key(self) -> None:
        payload = {
            "articles": [
                {
                    "name": "Another long CyberLeninka article title for test",
                    "annotation": "Abstract",
                    "link": "/article/456",
                    "year": "2023",
                    "journal": "CL Journal",
                },
            ],
        }
        conn = CyberLeninkaConnector()
        items = conn._extract_from_payload("test", payload, 5)
        assert len(items) == 1
        assert items[0].authors == ()


class TestHALJournal:
    """HALConnector should extract journal from journalTitle_s."""

    def test_journal_from_field(self) -> None:
        payload = {
            "response": {
                "docs": [
                    {
                        "halId_s": "hal-123",
                        "title_s": ["HAL Article"],
                        "abstract_s": ["Abstract"],
                        "doiId_s": "10.1234/hal",
                        "publicationDateY_i": 2024,
                        "uri_s": "https://hal.archives-ouvertes.fr/hal-123",
                        "authFullName_s": ["Alice Smith"],
                        "journalTitle_s": ["Journal du HAL"],
                    },
                ],
            },
        }
        conn = HALConnector()
        items = conn._extract_from_payload("test", payload, 5)
        assert len(items) == 1
        assert items[0].journal == "Journal du HAL"

    def test_journal_from_string_field(self) -> None:
        """HAL Solr returns journalTitle_s as a string, not a list.

        Indexing the string as a list would yield its first character ('J'),
        so the connector must accept either shape.
        """
        payload = {
            "response": {
                "docs": [
                    {
                        "halId_s": "hal-789",
                        "title_s": ["HAL Article"],
                        "abstract_s": ["Abstract"],
                        "doiId_s": "10.1234/hal2",
                        "publicationDateY_i": 2024,
                        "uri_s": "https://hal.archives-ouvertes.fr/hal-789",
                        "authFullName_s": ["Alice Smith"],
                        "journalTitle_s": "Journal of Bioanalysis & Biomedicine",
                    },
                ],
            },
        }
        conn = HALConnector()
        items = conn._extract_from_payload("test", payload, 5)
        assert len(items) == 1
        assert items[0].journal == "Journal of Bioanalysis & Biomedicine"

    def test_fallback_to_hal(self) -> None:
        payload = {
            "response": {
                "docs": [
                    {
                        "halId_s": "hal-456",
                        "title_s": ["No Journal"],
                        "abstract_s": [],
                        "doiId_s": "",
                        "publicationDateY_i": 2024,
                        "uri_s": "https://hal.archives-ouvertes.fr/hal-456",
                        "authFullName_s": ["Bob Jones"],
                    },
                ],
            },
        }
        conn = HALConnector()
        items = conn._extract_from_payload("test", payload, 5)
        assert len(items) == 1
        assert items[0].journal == "HAL"


class TestPubMedEfetch:
    """PubMedConnector._parse_efetch_abstracts should parse XML."""

    def test_labeled_abstract_parts(self) -> None:
        from apps.ingestion.connectors.api_connectors import PubMedConnector

        xml = (
            "<PubmedArticleSet>"
            "<PubmedArticle>"
            "<MedlineCitation>"
            "<PMID>99999</PMID>"
            "<Article>"
            "<Abstract>"
            '<AbstractText Label="BACKGROUND">Some background.</AbstractText>'
            '<AbstractText Label="RESULTS">Key findings.</AbstractText>'
            "</Abstract>"
            "</Article>"
            "</MedlineCitation>"
            "</PubmedArticle>"
            "</PubmedArticleSet>"
        )
        result = PubMedConnector._parse_efetch_abstracts(xml)
        assert "99999" in result
        assert "BACKGROUND: Some background." in result["99999"]
        assert "RESULTS: Key findings." in result["99999"]

    def test_multiple_articles(self) -> None:
        from apps.ingestion.connectors.api_connectors import PubMedConnector

        xml = (
            "<PubmedArticleSet>"
            "<PubmedArticle>"
            "<MedlineCitation><PMID>111</PMID>"
            "<Article><Abstract><AbstractText>First.</AbstractText></Abstract></Article>"
            "</MedlineCitation>"
            "</PubmedArticle>"
            "<PubmedArticle>"
            "<MedlineCitation><PMID>222</PMID>"
            "<Article><Abstract><AbstractText>Second.</AbstractText></Abstract></Article>"
            "</MedlineCitation>"
            "</PubmedArticle>"
            "</PubmedArticleSet>"
        )
        result = PubMedConnector._parse_efetch_abstracts(xml)
        assert result.get("111") == "First."
        assert result.get("222") == "Second."


class TestOpenAlexJournal:
    """OpenAlexConnector should extract journal from primary_location.source."""

    def test_journal_from_primary_location(self) -> None:
        payload = {
            "results": [
                {
                    "id": "https://openalex.org/W123",
                    "title": "OpenAlex article",
                    "abstract_inverted_index": {".": [[0, 0]]},
                    "doi": "https://doi.org/10.1234/oa",
                    "publication_year": 2024,
                    "authorships": [
                        {"author": {"display_name": "Alice Smith"}},
                    ],
                    "primary_location": {
                        "source": {"display_name": "Nature"},
                    },
                },
            ],
        }
        conn = OpenAlexConnector()
        items = conn._extract_from_payload("test", payload, 5)
        assert len(items) == 1
        assert items[0].journal == "Nature"

    def test_journal_from_best_oa_location(self) -> None:
        payload = {
            "results": [
                {
                    "id": "https://openalex.org/W456",
                    "title": "OpenAlex OA article",
                    "doi": "https://doi.org/10.1234/oa2",
                    "publication_year": 2024,
                    "authorships": [
                        {"author": {"display_name": "Bob Jones"}},
                    ],
                    "best_oa_location": {
                        "source": {"display_name": "PLOS ONE"},
                    },
                },
            ],
        }
        conn = OpenAlexConnector()
        items = conn._extract_from_payload("test", payload, 5)
        assert len(items) == 1
        assert items[0].journal == "PLOS ONE"

    def test_no_journal_when_missing(self) -> None:
        payload = {
            "results": [
                {
                    "id": "https://openalex.org/W789",
                    "title": "OpenAlex no journal",
                    "doi": "https://doi.org/10.1234/oa3",
                    "publication_year": 2024,
                    "authorships": [
                        {"author": {"display_name": "Carol Lee"}},
                    ],
                },
            ],
        }
        conn = OpenAlexConnector()
        items = conn._extract_from_payload("test", payload, 5)
        assert len(items) == 1
        assert items[0].journal == ""


class TestBrowserTransport:
    """``BrowserTransport`` proxies fetches to the cloakbrowser sidecar.

    ``Session.post`` is stubbed so no real HTTP is performed. The tests pin
    the contract connectors rely on: transient sidecar failures (network
    errors, 502/504) retry with backoff; upstream HTTP errors
    (``status >= 400`` returned in the 200 body) and non-retryable sidecar
    errors (422/other 4xx) raise :class:`ConnectorFetchError`.
    """

    @staticmethod
    def _make_transport(monkeypatch):
        from apps.ingestion.connectors import base

        monkeypatch.setattr(base.time, "sleep", lambda _seconds: None)
        return base.BrowserTransport(source_key="test", max_attempts=3)

    @staticmethod
    def _response(status_code, payload):
        class _Resp:
            def __init__(self) -> None:
                self.status_code = status_code

            def json(self):
                return payload

        return _Resp()

    def test_fetch_returns_decoded_text_body(self, monkeypatch) -> None:
        transport = self._make_transport(monkeypatch)
        monkeypatch.setattr(
            transport._session,
            "post",
            lambda *a, **k: self._response(
                200,
                {
                    "status": 200,
                    "content_type": "text/html",
                    "encoding": "text",
                    "body": "<html>ok</html>",
                },
            ),
        )
        result = transport.fetch("https://example.org")
        assert result.status == 200
        assert result.body_text == "<html>ok</html>"
        assert result.body_bytes == b"<html>ok</html>"
        assert result.content_type == "text/html"

    def test_fetch_decodes_base64_body(self, monkeypatch) -> None:
        import base64

        transport = self._make_transport(monkeypatch)
        raw = b"%PDF-1.4 binary"
        monkeypatch.setattr(
            transport._session,
            "post",
            lambda *a, **k: self._response(
                200,
                {
                    "status": 200,
                    "content_type": "application/pdf",
                    "encoding": "base64",
                    "body": base64.b64encode(raw).decode(),
                },
            ),
        )
        result = transport.fetch("https://example.org")
        assert result.body_bytes == raw
        assert result.body_text is None

    def test_upstream_http_error_raises(self, monkeypatch) -> None:
        from apps.ingestion.connectors import ConnectorFetchError

        transport = self._make_transport(monkeypatch)
        monkeypatch.setattr(
            transport._session,
            "post",
            lambda *a, **k: self._response(
                200,
                {
                    "status": 403,
                    "content_type": "text/html",
                    "encoding": "text",
                    "body": "<html>403</html>",
                },
            ),
        )
        with pytest.raises(ConnectorFetchError):
            transport.fetch("https://example.org")

    def test_retries_on_network_error_then_succeeds(self, monkeypatch) -> None:
        import requests

        transport = self._make_transport(monkeypatch)
        calls = {"n": 0}

        def _post(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise requests.ConnectionError("boom")
            return self._response(
                200,
                {
                    "status": 200,
                    "content_type": "text/html",
                    "encoding": "text",
                    "body": "ok",
                },
            )

        monkeypatch.setattr(transport._session, "post", _post)
        result = transport.fetch("https://example.org")
        assert result.body_text == "ok"
        assert calls["n"] == 2

    def test_retries_on_502_then_raises_after_max_attempts(self, monkeypatch) -> None:
        from apps.ingestion.connectors import ConnectorFetchError

        transport = self._make_transport(monkeypatch)
        monkeypatch.setattr(
            transport._session,
            "post",
            lambda *a, **k: self._response(502, {}),
        )
        with pytest.raises(ConnectorFetchError):
            transport.fetch("https://example.org")

    def test_sidecar_422_raises_without_retry(self, monkeypatch) -> None:
        from apps.ingestion.connectors import ConnectorFetchError

        transport = self._make_transport(monkeypatch)
        calls = {"n": 0}

        def _post(*a, **k):
            calls["n"] += 1
            return self._response(422, {})

        monkeypatch.setattr(transport._session, "post", _post)
        with pytest.raises(ConnectorFetchError):
            transport.fetch("https://example.org")
        assert calls["n"] == 1

    def test_sidecar_200_non_json_body_raises(self, monkeypatch) -> None:
        from apps.ingestion.connectors import ConnectorFetchError

        transport = self._make_transport(monkeypatch)
        calls = {"n": 0}

        def _post(*a, **k):
            calls["n"] += 1

            class _Resp:
                status_code = 200

                def json(self):
                    msg = "non-JSON body"
                    raise ValueError(msg)

            return _Resp()

        monkeypatch.setattr(transport._session, "post", _post)
        with pytest.raises(ConnectorFetchError):
            transport.fetch("https://example.org")
        # ValueError is a hard failure, not a transient retry trigger.
        assert calls["n"] == 1

    def test_post_json_forwards_json_body(self, monkeypatch) -> None:
        transport = self._make_transport(monkeypatch)
        captured = {}

        def _post(endpoint, json=None, timeout=None):
            captured["payload"] = json
            return self._response(
                200,
                {
                    "status": 200,
                    "content_type": "application/json",
                    "encoding": "text",
                    "body": "{}",
                },
            )

        monkeypatch.setattr(transport._session, "post", _post)
        transport.post_json("https://example.org/api", {"q": "math"})
        assert captured["payload"]["method"] == "POST"
        assert captured["payload"]["json"] == {"q": "math"}
        assert captured["payload"]["url"] == "https://example.org/api"


class TestSciELOOaiJournal:
    """_clean_oai_journal strips the volume/issue/year tail from dc:source.

    SciELO OAI records encode the venue as ``<journal> v.24 n.4 2017``;
    the volume, issue and year must not leak into the ``journal`` field.
    """

    def test_strips_v_n_year_tokens(self) -> None:
        assert (
            SciELOConnector._clean_oai_journal(
                "Revista de la Sociedad Española del Dolor v.24 n.4 2017",
            )
            == "Revista de la Sociedad Española del Dolor"
        )

    def test_strips_vol_no_tokens(self) -> None:
        assert (
            SciELOConnector._clean_oai_journal("Some Journal vol.10 no.2 2020")
            == "Some Journal"
        )

    def test_preserves_plain_journal(self) -> None:
        assert SciELOConnector._clean_oai_journal("Plain Journal") == "Plain Journal"

    def test_empty_returns_empty(self) -> None:
        assert SciELOConnector._clean_oai_journal("") == ""


class TestSciELORssAuthors:
    """_split_rss_authors parses SciELO RSS ``<author>`` semicolon lists.

    SciELO RSS author blobs are ``;``-separated ``Last, First`` pairs; the
    inner comma must not split a name, and the parsed list must be stored on
    the RawArticle (previously parsed then discarded).
    """

    def test_splits_semicolon_list_preserving_inner_commas(self) -> None:
        blob = (
            "Cervantes-Guerrero, Mario Daniel; Galván-Tejada, Carlos E.; Cruz, Miguel"
        )
        assert SciELOConnector._split_rss_authors(blob) == (
            "Cervantes-Guerrero, Mario Daniel",
            "Galván-Tejada, Carlos E.",
            "Cruz, Miguel",
        )

    def test_single_name_without_semicolon(self) -> None:
        assert SciELOConnector._split_rss_authors("Adnan Muhisn, Sinan") == (
            "Adnan Muhisn, Sinan",
        )

    def test_dedupes_preserving_order(self) -> None:
        blob = "Smith, J.; Jones, A.; Smith, J."
        assert SciELOConnector._split_rss_authors(blob) == ("Smith, J.", "Jones, A.")

    def test_empty_returns_empty_tuple(self) -> None:
        assert SciELOConnector._split_rss_authors("") == ()
        assert SciELOConnector._split_rss_authors("   ;  ; ") == ()


class TestSciELOQueryTerms:
    """_query_terms normalizes a query into matchable tokens."""

    def test_lowercases_and_splits(self) -> None:
        assert SciELOConnector._query_terms("Machine Learning 2024") == [
            "machine",
            "learning",
            "2024",
        ]

    def test_drops_short_tokens(self) -> None:
        assert SciELOConnector._query_terms("a of ml") == []

    def test_empty_query_returns_empty(self) -> None:
        assert SciELOConnector._query_terms("") == []


class TestSciELOArticleMatchesTerms:
    """_article_matches_terms checks query terms against article fields."""

    def test_matches_title(self) -> None:
        from apps.ingestion.connectors.base import RawArticle

        art = RawArticle(
            source_key="scielo",
            title="Machine learning approaches to imaging",
            url="https://x",
            abstract="",
            full_text="",
            language="en",
            year=None,
            doi="",
            journal="",
        )
        assert SciELOConnector._article_matches_terms(art, ["machine", "learning"])

    def test_no_match_for_irrelevant(self) -> None:
        from apps.ingestion.connectors.base import RawArticle

        art = RawArticle(
            source_key="scielo",
            title="Acupuntura para el dolor de rodilla",
            url="https://x",
            abstract="",
            full_text="",
            language="es",
            year=None,
            doi="",
            journal="",
        )
        assert not SciELOConnector._article_matches_terms(
            art,
            ["machine", "learning"],
        )

    def test_partial_match_is_rejected(self) -> None:
        """AND semantics: a single shared token must not pass a 2-term query.

        ``learning by doing`` and ``Blended learning`` share ``learning``
        with ``machine learning`` but are not about ML; the matcher must
        require both terms.
        """
        from apps.ingestion.connectors.base import RawArticle

        art = RawArticle(
            source_key="scielo",
            title="A relationship between external public debt and economic growth",
            url="https://x",
            abstract="An endogenous growth model with learning by doing.",
            full_text="",
            language="en",
            year=2015,
            doi="",
            journal="Estudios Económicos",
        )
        assert not SciELOConnector._article_matches_terms(
            art,
            ["machine", "learning"],
        )


class TestSciELOOaiFetch:
    """_fetch_oai post-filters records by query terms and cleans journal.

    OAI-PMH ``ListRecords`` is date-based; before the fix the SciELO
    fallback returned the first N records by date regardless of the
    query, emitting query-irrelevant articles (e.g. acupuncture papers
    for a ``machine learning`` search) with the volume/issue/year tail
    fused into ``journal``. The fallback must keep only records whose
    text fields contain a query term and strip the venue tail.
    """

    _OAI_XML = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/" '
        'xmlns:oai_dc="http://www.openarchives.org/OAI/2.0/oai_dc/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/">'
        "<ListRecords>"
        "<record><metadata><oai_dc:dc>"
        "<dc:title>Acupuntura para el dolor de rodilla</dc:title>"
        "<dc:identifier>https://www.scielo.br/a/acupuntura</dc:identifier>"
        "<dc:source>Revista Dolor v.24 n.4 2017</dc:source>"
        "<dc:date>2017-01-01</dc:date>"
        "</oai_dc:dc></metadata></record>"
        "<record><metadata><oai_dc:dc>"
        "<dc:title>Machine learning approaches to medical imaging</dc:title>"
        "<dc:identifier>https://www.scielo.br/a/ml-imaging</dc:identifier>"
        "<dc:source>Computer Methods v.1 n.2 2024</dc:source>"
        "<dc:date>2024-01-01</dc:date>"
        "</oai_dc:dc></metadata></record>"
        "</ListRecords></OAI-PMH>"
    )

    def test_filters_irrelevant_and_cleans_journal(self, monkeypatch) -> None:
        conn = SciELOConnector()

        monkeypatch.setattr(conn, "_request_xml_text", lambda _url: self._OAI_XML)

        items = conn._fetch_oai("machine learning", 3)
        assert len(items) == 1
        assert "Machine learning" in items[0].title
        assert items[0].journal == "Computer Methods"
        assert items[0].year == 2024

    def test_empty_query_keeps_all(self, monkeypatch) -> None:
        conn = SciELOConnector()

        monkeypatch.setattr(conn, "_request_xml_text", lambda _url: self._OAI_XML)

        items = conn._fetch_oai("", 3)
        assert len(items) == 2

    def test_no_relevant_raises(self, monkeypatch) -> None:

        from apps.ingestion.connectors.base import ConnectorFetchError

        conn = SciELOConnector()

        monkeypatch.setattr(conn, "_request_xml_text", lambda _url: self._OAI_XML)

        with pytest.raises(ConnectorFetchError):
            conn._fetch_oai("quantum computing", 3)

    def test_oai_fails_over_to_next_mirror(self, monkeypatch) -> None:
        """A transport error on the first mirror falls through to the next.

        ``_request_xml_text`` translates network errors into
        ``ConnectorFetchError``. ``_fetch_oai`` must catch that exception per
        endpoint and move on to the next mirror rather than aborting the
        whole fetch — a read timeout on ``scielo.isciii.es`` must surface
        records from ``scielo.org.mx``.
        """
        from apps.ingestion.connectors.base import ConnectorFetchError

        conn = SciELOConnector()
        isciii, mexico = conn.OA_MIRRORS

        def stub(url: str) -> str:
            if url.startswith(isciii):
                msg = "scielo: oai transport error: simulated read timeout"
                raise ConnectorFetchError(msg)
            if url.startswith(mexico):
                return self._OAI_XML
            msg = f"unexpected url: {url}"
            raise AssertionError(msg)

        monkeypatch.setattr(conn, "_request_xml_text", stub)

        items = conn._fetch_oai("machine learning", 3)
        assert len(items) == 1
        assert "Machine learning" in items[0].title
        assert items[0].journal == "Computer Methods"


class TestMathNetEnrichParse:
    """MathNet enrich_raw parses the article page via HTML structure.

    MathNet renders the citation head in ``<title>``, the bibliographic line
    (journal/year/volume/issue/pages/DOI) in the first ``<i>``, and labeled
    fields (Abstract/Keywords/Language) as ``<b>Label:</b>`` + sibling text.
    The previous linearized-text regex mismatched this structure and left
    authors/journal/volume/issue/pages empty; these tests pin the structural
    parse against faithful forthcoming and published fixtures.
    """

    _FORTHCOMING_HTML = (
        "<html><head><title>S. O. Speranski, A. V. Grefenshtein, "
        "“On the complexity of first-order logics of probability”, "
        "Izvestiya Rossiiskoi Akademii Nauk. Seriya Matematicheskaya</title></head>"
        "<body><i>Izvestiya Rossiiskoi Akademii Nauk. Seriya Matematicheskaya, "
        "Forthcoming paper</i>"
        "<b>Abstract:</b> This article is concerned with Halpern logics.<br>"
        " Keywords follow."
        "<b>Keywords:</b> probability logic, quantification"
        "<b>Language:</b> English"
        "</body></html>"
    )

    _PUBLISHED_HTML = (
        "<html><head><title>M. V. Zhitlukhin, "
        "“Asymptotically optimal strategies in multi-agent market "
        "models”, Uspekhi Mat. Nauk, 81:3(489) (2026), 3\u201350"
        "</title></head>"
        "<body><i>Uspekhi Matematicheskikh Nauk, 2026, Volume 81 , Issue 3(489) , "
        "Pages 3\u201350 DOI: https://doi.org/10.4213/rm10325</i>"
        "<b>Abstract:</b> A survey of results on asymptotically optimal strategies."
        "<b>Keywords:</b> financial mathematics"
        "<b>Language:</b> Russian"
        "</body></html>"
    )

    def test_citation_head_splits_comma_separated_authors(self) -> None:
        authors, title = MathNetConnector._mathnet_citation_head(
            "S. O. Speranski, A. V. Grefenshtein, “On probability”, J",
        )
        assert authors == "S. O. Speranski, A. V. Grefenshtein"
        assert title == "On probability"

    def test_citation_head_returns_empty_when_no_quote(self) -> None:
        assert MathNetConnector._mathnet_citation_head("no quoted title here") == (
            "",
            "",
        )

    def test_italics_meta_published(self) -> None:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(self._PUBLISHED_HTML, "lxml")
        journal, volume, issue, pages, year, doi = (
            MathNetConnector._mathnet_italics_meta(soup)
        )
        assert journal == "Uspekhi Matematicheskikh Nauk"
        assert volume == "81"
        assert issue == "3(489)"
        assert pages == "3\u201350"
        assert year == "2026"
        assert doi == "10.4213/rm10325"

    def test_italics_meta_forthcoming_has_journal_only(self) -> None:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(self._FORTHCOMING_HTML, "lxml")
        journal, volume, issue, pages, year, doi = (
            MathNetConnector._mathnet_italics_meta(soup)
        )
        assert journal == "Izvestiya Rossiiskoi Akademii Nauk. Seriya Matematicheskaya"
        assert volume == issue == pages == year == doi == ""

    def test_italics_meta_journal_name_with_comma_is_not_truncated(self) -> None:
        from bs4 import BeautifulSoup

        # Journal full name contains a comma; the year marker anchors the
        # split so "Series A" is preserved.
        html = (
            "<html><body><i>Transactions of the Moscow Math. Society, "
            "Series A, 2026, Volume 71, Issue 3, Pages 174\u2013185 "
            "DOI: https://doi.org/10.4213/tmm71</i></body></html>"
        )
        soup = BeautifulSoup(html, "lxml")
        journal, volume, issue, pages, year, doi = (
            MathNetConnector._mathnet_italics_meta(soup)
        )
        assert journal == "Transactions of the Moscow Math. Society, Series A"
        assert volume == "71"
        assert issue == "3"
        assert pages == "174\u2013185"
        assert year == "2026"
        assert doi == "10.4213/tmm71"

    def test_italics_meta_single_page_is_captured(self) -> None:
        from bs4 import BeautifulSoup

        # Errata / short notes carry a lone page, not a range.
        html = (
            "<html><body><i>Short Notes Journal, 2026, Volume 5, Issue 1, "
            "Pages 42 DOI: https://doi.org/10.4213/sn5</i></body></html>"
        )
        soup = BeautifulSoup(html, "lxml")
        _journal, _volume, _issue, pages, _year, _doi = (
            MathNetConnector._mathnet_italics_meta(soup)
        )
        assert pages == "42"

    def test_labeled_value_captures_multiline_abstract(self) -> None:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(self._FORTHCOMING_HTML, "lxml")
        abstract = MathNetConnector._mathnet_labeled_value(soup, "Abstract:")
        assert "Halpern logics" in abstract
        assert "Keywords follow" in abstract

    def test_language_code_maps_known_labels(self) -> None:
        assert MathNetConnector._mathnet_language_code("English") == "en"
        assert MathNetConnector._mathnet_language_code("Russian") == "ru"
        assert MathNetConnector._mathnet_language_code("Klingon") == ""

    def test_enrich_raw_forthcoming_fills_authors_journal_abstract(
        self,
        monkeypatch,
    ) -> None:
        from apps.ingestion.connectors.base import RawArticle

        conn = MathNetConnector()
        monkeypatch.setattr(
            conn,
            "_request_text",
            lambda _url, **_kwargs: self._FORTHCOMING_HTML,
        )
        raw = RawArticle(
            source_key="mathnet",
            title="On the complexity of first-order logics of probability",
            url="https://www.mathnet.ru/eng/im9718",
            abstract="",
            full_text="",
            language="en",
            year=None,
            doi="",
            journal="MathNet.Ru",
        )
        enriched = conn.enrich_raw(raw)
        assert enriched.authors == ("S. O. Speranski", "A. V. Grefenshtein")
        assert (
            enriched.journal
            == "Izvestiya Rossiiskoi Akademii Nauk. Seriya Matematicheskaya"
        )
        assert "Halpern logics" in enriched.abstract
        assert enriched.language == "en"
        # Forthcoming paper has no volume/issue/pages/year in the <i> line.
        assert enriched.volume == enriched.issue == enriched.pages == ""

    def test_enrich_raw_published_fills_biblio_and_doi(self, monkeypatch) -> None:
        from apps.ingestion.connectors.base import RawArticle

        conn = MathNetConnector()
        monkeypatch.setattr(
            conn,
            "_request_text",
            lambda _url, **_kwargs: self._PUBLISHED_HTML,
        )
        raw = RawArticle(
            source_key="mathnet",
            title="Asymptotically optimal strategies in multi-agent market models",
            url="https://www.mathnet.ru/eng/rm10325",
            abstract="",
            full_text="",
            language="en",
            year=None,
            doi="",
            journal="MathNet.Ru",
        )
        enriched = conn.enrich_raw(raw)
        assert enriched.authors == ("M. V. Zhitlukhin",)
        assert (
            enriched.title
            == "Asymptotically optimal strategies in multi-agent market models"
        )
        assert enriched.journal == "Uspekhi Matematicheskikh Nauk"
        assert enriched.volume == "81"
        assert enriched.issue == "3(489)"
        assert enriched.pages == "3\u201350"
        assert enriched.year == 2026
        assert enriched.doi == "10.4213/rm10325"
        assert enriched.language == "ru"
        assert "asymptotically optimal strategies" in enriched.abstract

    def test_enrich_raw_keeps_existing_authors_when_title_has_no_quote(
        self,
        monkeypatch,
    ) -> None:
        from apps.ingestion.connectors.base import RawArticle

        conn = MathNetConnector()
        monkeypatch.setattr(
            conn,
            "_request_text",
            lambda _url, **_kwargs: "<html></html>",
        )
        raw = RawArticle(
            source_key="mathnet",
            title="t",
            url="https://www.mathnet.ru/eng/x1",
            abstract="",
            full_text="",
            language="en",
            year=None,
            doi="",
            journal="MathNet.Ru",
            authors=("Existing Author",),
        )
        enriched = conn.enrich_raw(raw)
        assert enriched.authors == ("Existing Author",)


class TestAJOLAbstractFromArticlePage:
    """AJOL ``enrich_raw`` replaces page-range OAI abstracts with the real one.

    AJOL OAI ``dc:description`` is occasionally just the article page span
    (e.g. ``"8-16"``). The article page exposes the true abstract in
    ``div.article-abstract``; ``enrich_raw`` must prefer it over the
    page-range pseudo-abstract, while keeping a legitimate OAI abstract
    when no article-page abstract is present.
    """

    _ARTICLE_HTML = (
        "<html><head>"
        '<meta name="citation_journal_title" content="West African Journal"/>'
        '<meta name="citation_date" content="2017/09/20"/>'
        "</head><body>"
        "<h2>Abstract</h2>"
        '<div class="article-abstract"><p>'
        "The two major motivations in medical science are to prevent and "
        "diagnose diseases with care."
        "</p></div>"
        "</body></html>"
    )

    _ARTICLE_HTML_NO_ABSTRACT = (
        "<html><head>"
        '<meta name="citation_journal_title" content="West African Journal"/>'
        "</head><body><p>no abstract here</p></body></html>"
    )

    # Heading INSIDE the abstract container — the extractor must prefer the
    # ``<p>`` over the bare ``div`` so the literal word "Abstract" does not
    # leak into the extracted abstract (``select_one`` returns matches in
    # document order, so a bare ``div.article-abstract`` listed alongside the
    # ``<p>`` selector would always win and include the heading text).
    _ARTICLE_HTML_HEADING_INSIDE = (
        "<html><body>"
        '<div class="article-abstract">'
        "<h3>Abstract</h3>"
        "<p>The two major motivations in medical science are to prevent and "
        "diagnose diseases with care.</p>"
        "</div>"
        "</body></html>"
    )

    def test_page_range_abstract_replaced_by_article_page_abstract(
        self,
        monkeypatch,
    ) -> None:
        from apps.ingestion.connectors.base import RawArticle

        conn = AJOLConnector()
        monkeypatch.setattr(
            conn,
            "_request_text",
            lambda _url, **_kwargs: self._ARTICLE_HTML,
        )
        raw = RawArticle(
            source_key="ajol",
            title="A framework for diagnosing confusable diseases",
            url="https://www.ajol.info/index.php/wajiar/article/view/161476",
            abstract="8-16",
            full_text="",
            language="en",
            year=None,
            doi="",
            journal="AJOL",
        )
        enriched = conn.enrich_raw(raw)
        assert "diagnose diseases with care" in enriched.abstract
        assert enriched.abstract != "8-16"

    def test_legitimate_oai_abstract_kept_when_no_article_page_abstract(
        self,
        monkeypatch,
    ) -> None:
        from apps.ingestion.connectors.base import RawArticle

        conn = AJOLConnector()
        monkeypatch.setattr(
            conn,
            "_request_text",
            lambda _url, **_kwargs: self._ARTICLE_HTML_NO_ABSTRACT,
        )
        raw = RawArticle(
            source_key="ajol",
            title="Some real article",
            url="https://www.ajol.info/index.php/wajiar/article/view/1",
            abstract="A genuine long abstract describing the study in detail.",
            full_text="",
            language="en",
            year=None,
            doi="",
            journal="AJOL",
        )
        enriched = conn.enrich_raw(raw)
        assert enriched.abstract == (
            "A genuine long abstract describing the study in detail."
        )

    def test_heading_inside_abstract_div_does_not_leak(
        self,
        monkeypatch,
    ) -> None:
        from apps.ingestion.connectors.base import RawArticle

        conn = AJOLConnector()
        monkeypatch.setattr(
            conn,
            "_request_text",
            lambda _url, **_kwargs: self._ARTICLE_HTML_HEADING_INSIDE,
        )
        raw = RawArticle(
            source_key="ajol",
            title="Heading inside abstract div",
            url="https://www.ajol.info/index.php/wajiar/article/view/2",
            abstract="8-16",
            full_text="",
            language="en",
            year=None,
            doi="",
            journal="AJOL",
        )
        enriched = conn.enrich_raw(raw)
        assert "diagnose diseases with care" in enriched.abstract
        # The literal heading word must not be prepended to the abstract.
        assert not enriched.abstract.lower().startswith("abstract")

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("8-16", True),
            ("37-53", True),
            (" 8 – 16 ", True),  # noqa: RUF001 - en dash is the test payload
            ("8–16", True),  # noqa: RUF001 - en dash is the test payload
            ("A real abstract sentence.", False),
            ("", False),
            ("pages 8-16 and more", False),
            ("1234567890123", False),
        ],
    )
    def test_looks_like_page_range(self, text: str, expected: bool) -> None:
        assert AJOLConnector._looks_like_page_range(text) is expected


class TestChallengePageDetector:
    """Regression guard for the residual challenge-page detector.

    The detector must catch real Cloudflare/BunnyCDN interstitials while
    leaving legitimate pages that merely reference Cloudflare (a
    ``cdnjs.cloudflare.com`` CDN script URL — the CiNii false positive — or a
    ``Ray ID`` footer) or discuss challenges in prose (a ``proof-of-work``
    abstract) untouched.
    """

    @pytest.mark.parametrize(
        ("html", "reason"),
        [
            (
                "<html><head><title>Just a moment...</title></head>"
                '<body><div id="challenge-running"></div>'
                '<div class="cf-browser-verification"></div></body></html>',
                "cf challenge interstitial",
            ),
            (
                '<html><body><script src="/cdn-cgi/challenge-platform/h/'
                'g/orchestrate/managed/v1"></script></body></html>',
                "cdn-cgi challenge-platform path",
            ),
            (
                "<html><body><script>window._cf_chl_opt={}</script></body></html>",
                "_cf_chl_opt js var",
            ),
            (
                '<html><body><div class="cf-turnstile"'
                ' data-sitekey="x"></div></body></html>',
                "turnstile widget",
            ),
            (
                "<html><head><title>Attention Required! | Cloudflare</title>"
                "</head><body>Sorry, you have been blocked.</body></html>",
                "cf block page",
            ),
        ],
    )
    def test_detects_real_challenge_pages(self, html: str, reason: str) -> None:
        assert BaseConnector._looks_like_challenge_page(html) is True, reason

    @pytest.mark.parametrize(
        ("html", "reason"),
        [
            # The CiNii false positive: a legitimate page loading jsrender from
            # the cdnjs.cloudflare.com CDN. The bare word "cloudflare" must not
            # trip the detector.
            (
                "<html><body>Back to top</body>"
                '<script src="https://cdnjs.cloudflare.com/ajax/libs/'
                'jsrender/1.0.7/jsrender.min.js"></script></html>',
                "cdnjs CDN script url",
            ),
            # A normal Cloudflare-proxied page footer with a Ray ID — not a
            # challenge page.
            (
                "<html><body>Article body</body>"
                "<footer>Ray ID: 8a4f-abc123</footer></html>",
                "ray id footer",
            ),
            # A scholarly abstract that discusses proof-of-work consensus.
            (
                "<html><body><p>This paper analyses the proof-of-work scheme"
                " used in early cryptocurrencies.</p></body></html>",
                "proof-of-work in abstract prose",
            ),
            # Generic phrases that previously caused false positives.
            (
                "<html><body><noscript>Please enable JavaScript to use this"
                " site.</noscript></body></html>",
                "enable javascript noscript",
            ),
            (
                "<html><body>Please wait while the dataset loads.</body></html>",
                "please wait loader",
            ),
            (
                "<html><body>Security check completed for this session.</body></html>",
                "security check prose",
            ),
            ("", "empty body"),
        ],
    )
    def test_does_not_flag_legitimate_pages(
        self,
        html: str,
        reason: str,
    ) -> None:
        assert BaseConnector._looks_like_challenge_page(html) is False, reason
