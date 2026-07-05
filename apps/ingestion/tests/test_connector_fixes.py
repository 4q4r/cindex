"""Tests for connector quick-win fixes: author/volume/journal/abstract extraction."""

from apps.ingestion.connectors import (
    CrossrefConnector,
    CyberLeninkaConnector,
    EuropePMCConnector,
    HALConnector,
    PMCConnector,
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
