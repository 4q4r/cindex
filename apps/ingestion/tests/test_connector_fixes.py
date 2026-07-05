"""Tests for connector quick-win fixes: author/volume/journal/abstract extraction."""

from apps.ingestion.connectors import (
    CrossrefConnector,
    CyberLeninkaConnector,
    EuropePMCConnector,
    HALConnector,
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


class TestChallengeRetryHTTPError:
    """_challenge_retry must swallow cloudscraper's HTTPError and return None.

    ``cloudscraper.get_tokens`` raises ``requests.HTTPError`` when the
    upstream answers 403 before tokens can be collected (e.g. the SciELO
    search endpoint). The retry helper must absorb that and return ``None``
    so ``_resolve_cloudflare_challenge`` returns the original 403 status,
    ``_request_response`` raises ``ConnectorFetchError``, and the SciELO
    connector falls back to OAI/HTML. Before the fix the raw ``HTTPError``
    propagated past every ``except ConnectorFetchError`` and aborted the
    whole fetch.
    """

    def test_challenge_retry_returns_none_on_http_error(self, monkeypatch) -> None:
        import requests

        def _raise(*args: object, **kwargs: object) -> None:
            raise requests.HTTPError("403 Client Error: Forbidden for url: x")

        monkeypatch.setattr(
            "apps.ingestion.connectors.base.cloudscraper.get_tokens",
            _raise,
        )
        conn = SciELOConnector()
        scraper = conn._build_scraper()
        assert conn._challenge_retry(scraper, "https://example.org", None) is None

    def test_challenge_retry_cookie_string_returns_none_on_http_error(
        self,
        monkeypatch,
    ) -> None:
        import requests

        def _raise(*args: object, **kwargs: object) -> None:
            raise requests.HTTPError("403 Client Error: Forbidden for url: x")

        monkeypatch.setattr(
            "apps.ingestion.connectors.base.cloudscraper.get_cookie_string",
            _raise,
        )
        conn = SciELOConnector()
        scraper = conn._build_scraper()
        assert (
            conn._challenge_retry_cookie_string(scraper, "https://example.org", None)
            is None
        )
