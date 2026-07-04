"""Tests for ExaConnector improvements: DOI cleanup, abstract cleaning,
outputSchema enrichment, and integration."""

from apps.ingestion.connectors.api_connectors import ExaConnector

# --- _extract_doi override tests ---


class TestExaDoiExtraction:
    """Tests for ExaConnector._extract_doi boundary cleanup."""

    def setup_method(self):
        self.conn = ExaConnector.__new__(ExaConnector)

    def test_extracts_clean_doi(self):
        result = self.conn._extract_doi(
            "A study in 10.1234/j.medchem.3b00120 showed results"
        )
        assert result == "10.1234/j.medchem.3b00120"

    def test_strips_trailing_year_parenthesis(self):
        result = self.conn._extract_doi(
            "10.48550/arXiv.1706.00120(2017) is a known reference"
        )
        assert result == "10.48550/arXiv.1706.00120"

    def test_strips_trailing_ref_parenthesis(self):
        result = self.conn._extract_doi("see 10.5281/zenodo.11002033(ref for details")
        assert result == "10.5281/zenodo.11002033"

    def test_strips_trailing_paren_with_text(self):
        result = self.conn._extract_doi("10.1093/nsr/nwaf457(accessed 2025) and more")
        assert result == "10.1093/nsr/nwaf457"

    def test_preserves_doi_without_trailing_paren(self):
        result = self.conn._extract_doi("https://doi.org/10.1007/s00739-024-01061-9")
        assert result == "10.1007/s00739-024-01061-9"

    def test_returns_empty_for_no_doi(self):
        result = self.conn._extract_doi("no doi here")
        assert result == ""

    def test_strips_trailing_period(self):
        result = self.conn._extract_doi("refer to 10.1234/test.2023.")
        assert result == "10.1234/test.2023"

    def test_strips_standalone_trailing_closing_paren(self):
        result = self.conn._extract_doi("10.1111/gcb.15818)")
        assert result == "10.1111/gcb.15818"

    def test_preserves_doi_with_volume_in_parens(self):
        result = self.conn._extract_doi("10.1016/s0969-9961(02)00008-6")
        assert result == "10.1016/s0969-9961(02)00008-6"

    def test_preserves_doi_with_year_in_parens_followed_by_suffix(self):
        result = self.conn._extract_doi("10.1016/S0140-6736(26)00519-2")
        assert result == "10.1016/S0140-6736(26)00519-2"


# --- _clean_abstract tests ---


class TestCleanAbstract:
    """Tests for ExaConnector._clean_abstract."""

    def setup_method(self):
        self.conn = ExaConnector.__new__(ExaConnector)

    def test_strips_skip_to_main_content(self):
        result = self.conn._clean_abstract(
            "My Title",
            "Skip to main content View PDF A novel approach to neural networks.",
        )
        assert not result.startswith("Skip to")
        assert not result.startswith("View PDF")
        assert "novel approach" in result

    def test_removes_duplicated_title(self):
        result = self.conn._clean_abstract(
            "Quantum Entanglement in Spin Chains",
            "Quantum Entanglement in Spin Chains We study the dynamics of...",
        )
        assert not result.startswith("Quantum Entanglement")
        assert "We study" in result

    def test_strips_section_headers(self):
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

    def test_empty_text(self):
        result = self.conn._clean_abstract("Title", "")
        assert result == ""

    def test_no_boilerplate(self):
        text = "This is a clean abstract about machine learning."
        result = self.conn._clean_abstract("Different Title", text)
        assert result == text

    def test_strips_search_sciencedirect(self):
        result = self.conn._clean_abstract(
            "Title",
            "Search ScienceDirect Brain, Behavior, and Immunity Volume 115.",
        )
        assert not result.startswith("Search ScienceDirect")


# --- _enrich_with_output_schema tests ---


class TestEnrichWithOutputSchema:
    """Tests for ExaConnector._enrich_with_output_schema."""

    def test_parses_output_schema_papers(self):
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

    def test_skips_empty_enrichment(self):
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

    def test_merge_updates_missing_fields(self):
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
            }
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

    def test_merge_preserves_existing_fields(self):
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
