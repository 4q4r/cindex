"""Tests for DoiEnrichmentService parsers, merge logic, and cascade."""

from unittest.mock import MagicMock

from apps.ingestion.doi_enrichment import DoiEnrichmentService


class TestParseCrossref:
    """Tests for _parse_crossref."""

    def test_extracts_authors(self):
        data = {
            "author": [
                {"given": "Alice", "family": "Smith"},
                {"given": "Bob", "family": "Jones"},
            ]
        }
        result = DoiEnrichmentService._parse_crossref(data)
        assert result["authors"] == ["Alice Smith", "Bob Jones"]

    def test_extracts_year(self):
        data = {"published-print": {"date-parts": [[2024, 3, 15]]}}
        result = DoiEnrichmentService._parse_crossref(data)
        assert result["year"] == 2024

    def test_extracts_volume_issue_pages(self):
        data = {"volume": "42", "issue": "3", "page": "101-115"}
        result = DoiEnrichmentService._parse_crossref(data)
        assert result["volume"] == "42"
        assert result["issue"] == "3"
        assert result["pages"] == "101-115"

    def test_empty_data(self):
        result = DoiEnrichmentService._parse_crossref({})
        assert "authors" not in result
        assert "year" not in result


class TestParseOpenAlex:
    """Tests for _parse_openalex."""

    def test_extracts_authors(self):
        data = {
            "authorships": [
                {"author": {"display_name": "Alice Smith"}},
                {"author": {"display_name": "Bob Jones"}},
            ]
        }
        result = DoiEnrichmentService._parse_openalex(data)
        assert result["authors"] == ["Alice Smith", "Bob Jones"]

    def test_reconstructs_abstract(self):
        inverted = {"The": [0], "quick": [1], "fox": [2]}
        data = {"abstract_inverted_index": inverted}
        result = DoiEnrichmentService._parse_openalex(data)
        assert result["abstract"] == "The quick fox"

    def test_extracts_biblio(self):
        data = {
            "publication_year": 2023,
            "biblio": {
                "volume": "10",
                "issue": "2",
                "first_page": "50",
                "last_page": "60",
            },
        }
        result = DoiEnrichmentService._parse_openalex(data)
        assert result["year"] == 2023
        assert result["volume"] == "10"
        assert result["issue"] == "2"
        assert result["pages"] == "50-60"

    def test_pages_without_last(self):
        data = {"biblio": {"first_page": "42"}}
        result = DoiEnrichmentService._parse_openalex(data)
        assert result["pages"] == "42"


class TestParseSemanticScholar:
    """Tests for _parse_semantic_scholar."""

    def test_extracts_all(self):
        data = {
            "authors": [{"name": "Alice Smith"}, {"name": "Bob Jones"}],
            "abstract": "A study of coral reefs.",
            "year": 2024,
        }
        result = DoiEnrichmentService._parse_semantic_scholar(data)
        assert result["authors"] == ["Alice Smith", "Bob Jones"]
        assert result["abstract"] == "A study of coral reefs."
        assert result["year"] == 2024

    def test_empty(self):
        result = DoiEnrichmentService._parse_semantic_scholar({})
        assert "authors" not in result
        assert "abstract" not in result


class TestReconstructAbstract:
    """Tests for _reconstruct_abstract."""

    def test_simple(self):
        idx = {"Hello": [0], "world": [1]}
        assert DoiEnrichmentService._reconstruct_abstract(idx) == "Hello world"

    def test_empty(self):
        assert DoiEnrichmentService._reconstruct_abstract({}) == ""
        assert DoiEnrichmentService._reconstruct_abstract(None) == ""


class TestNeedsEnrichment:
    """Tests for _needs_enrichment."""

    def test_no_doi_returns_false(self):
        article = MagicMock()
        article.doi = ""
        assert DoiEnrichmentService._needs_enrichment(article) is False

    def test_complete_article_returns_false(self):
        article = MagicMock()
        article.doi = "10.1234/test"
        article.abstract = "An abstract"
        article.publication_year = 2024
        article.volume = "1"
        article.issue = "2"
        article.pages = "3-4"
        article.article_authors.exists.return_value = True
        author = MagicMock()
        author.full_name = "Alice Smith"
        article.article_authors.first.return_value.author = author
        assert DoiEnrichmentService._needs_enrichment(article) is False

    def test_missing_abstract_returns_true(self):
        article = MagicMock()
        article.doi = "10.1234/test"
        article.abstract = ""
        article.publication_year = 2024
        article.volume = "1"
        article.issue = "1"
        article.pages = "1"
        article.article_authors.exists.return_value = True
        author = MagicMock()
        author.full_name = "Alice Smith"
        article.article_authors.first.return_value.author = author
        assert DoiEnrichmentService._needs_enrichment(article) is True

    def test_unknown_author_returns_true(self):
        article = MagicMock()
        article.doi = "10.1234/test"
        article.abstract = "text"
        article.publication_year = 2024
        article.volume = "1"
        article.issue = "1"
        article.pages = "1"
        article.article_authors.exists.return_value = True
        author = MagicMock()
        author.full_name = "Unknown author"
        article.article_authors.first.return_value.author = author
        assert DoiEnrichmentService._needs_enrichment(article) is True


class TestModelField:
    """Tests for _model_field."""

    def test_year_maps_to_publication_year(self):
        assert DoiEnrichmentService._model_field("year") == "publication_year"

    def test_other_fields_pass_through(self):
        assert DoiEnrichmentService._model_field("volume") == "volume"
        assert DoiEnrichmentService._model_field("pages") == "pages"


class TestPubMedEfetchParser:
    """Tests for PubMedConnector._parse_efetch_abstracts."""

    def test_parses_simple_abstract(self):
        from apps.ingestion.connectors.api_connectors import PubMedConnector

        xml = (
            "<PubmedArticleSet>"
            "<PubmedArticle>"
            "<MedlineCitation>"
            "<PMID>12345</PMID>"
            "<Article>"
            "<Abstract><AbstractText>This is an abstract.</AbstractText></Abstract>"
            "</Article>"
            "</MedlineCitation>"
            "</PubmedArticle>"
            "</PubmedArticleSet>"
        )
        result = PubMedConnector._parse_efetch_abstracts(xml)
        assert result.get("12345") == "This is an abstract."

    def test_invalid_xml_returns_empty(self):
        from apps.ingestion.connectors.api_connectors import PubMedConnector

        result = PubMedConnector._parse_efetch_abstracts("not xml")
        assert result == {}
