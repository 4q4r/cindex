"""Connector peer-review / preprint / indexing tier evidence emission tests."""

from __future__ import annotations

import json
from pathlib import Path

from apps.articles.services import TIER_A, TIER_B
from apps.ingestion.connectors import (
    ArXivConnector,
    COREConnector,
    CrossrefConnector,
    DBLPConnector,
    DOAJConnector,
    EuropePMCConnector,
    HALConnector,
    IACRConnector,
    OpenAlexConnector,
    PMCConnector,
    PubMedConnector,
    ZenodoConnector,
)

FIXTURES = Path(__file__).parent / "fixtures" / "sources"


def _fixture(name: str) -> dict:
    """Load a source fixture JSON as a dict."""
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


# --- OpenAlex ---------------------------------------------------------------


def test_openalex_preprint_emits_tier_a_preprint() -> None:
    """An OpenAlex ``preprint`` work type emits tierA preprint evidence."""
    payload = {
        "results": [
            {
                "title": "Preprint Work",
                "abstract_inverted_index": {"hello": [0], "world": [1]},
                "doi": "10.1/oa.preprint",
                "type": "preprint",
                "primary_location": {
                    "landing_page_url": "https://example.org/oa/1",
                    "source": {"type": "repository", "display_name": "arXiv"},
                },
                "publication_date": "2024-01-01",
                "authorships": [],
            },
        ],
    }
    items = OpenAlexConnector()._extract_from_payload("q", payload, 1)
    assert items[0].preprint_evidence.startswith(TIER_A)
    assert items[0].peer_review_evidence == ""
    assert items[0].indexing_evidence.startswith(TIER_B)


def test_openalex_journal_article_emits_tier_b_peer() -> None:
    """An OpenAlex ``article`` in a ``journal`` venue emits tierB peer-review."""
    payload = {
        "results": [
            {
                "title": "Journal Article",
                "abstract_inverted_index": {"hello": [0], "world": [1]},
                "doi": "10.1/oa.article",
                "type": "article",
                "primary_location": {
                    "landing_page_url": "https://example.org/oa/2",
                    "source": {"type": "journal", "display_name": "Nature"},
                },
                "publication_date": "2024-01-01",
                "authorships": [],
            },
        ],
    }
    items = OpenAlexConnector()._extract_from_payload("q", payload, 1)
    assert items[0].peer_review_evidence.startswith(TIER_B)
    assert items[0].preprint_evidence == ""


def test_openalex_fixture_venue_without_type_stays_unverified() -> None:
    """The shipped fixture has a venue without ``type`` -> peer stays unverified."""
    items = OpenAlexConnector()._extract_from_payload("q", _fixture("openalex"), 1)
    assert items[0].peer_review_evidence == ""
    assert items[0].preprint_evidence == ""
    # OpenAlex is a reputable index regardless of peer-review verdict.
    assert items[0].indexing_evidence.startswith(TIER_B)


# --- Crossref ----------------------------------------------------------------


def test_crossref_posted_content_is_preprint() -> None:
    """Crossref ``posted-content`` is a preprint (tierA)."""
    payload = {
        "message": {
            "items": [
                {
                    "type": "posted-content",
                    "DOI": "10.1/cr.preprint",
                    "title": ["Posted Content"],
                    "abstract": "abstract text",
                    "container-title": ["Preprint Server"],
                    "author": [],
                    "issued": {"date-parts": [[2024]]},
                },
            ],
        },
    }
    items = CrossrefConnector()._extract_from_payload("q", payload, 1)
    assert items[0].preprint_evidence.startswith(TIER_A)
    assert items[0].peer_review_evidence == ""
    assert items[0].indexing_evidence.startswith(TIER_B)


def test_crossref_journal_article_with_assertion_is_tier_a() -> None:
    """A Crossref journal-article with a Received/Accepted assertion is tierA."""
    payload = {
        "message": {
            "items": [
                {
                    "type": "journal-article",
                    "DOI": "10.1/cr.asserted",
                    "title": ["Asserted Article"],
                    "abstract": "abstract text",
                    "container-title": ["Journal"],
                    "author": [],
                    "issued": {"date-parts": [[2024]]},
                    "assertion": [
                        {"name": "received", "value": "2024-01-01"},
                        {"name": "accepted", "value": "2024-03-01"},
                    ],
                },
            ],
        },
    }
    items = CrossrefConnector()._extract_from_payload("q", payload, 1)
    assert items[0].peer_review_evidence.startswith(TIER_A)
    assert items[0].preprint_evidence == ""


def test_crossref_journal_article_without_assertion_is_tier_b() -> None:
    """A Crossref journal-article without assertions is tierB peer-reviewed."""
    items = CrossrefConnector()._extract_from_payload("q", _fixture("crossref"), 1)
    assert items[0].peer_review_evidence.startswith(TIER_B)
    assert items[0].indexing_evidence.startswith(TIER_B)


# --- Europe PMC / PMC --------------------------------------------------------


def test_epmc_preprint_pubtype_is_preprint() -> None:
    """Europe PMC ``preprint`` pubType emits tierA preprint."""
    rec = {
        "title": "EPMC Preprint",
        "abstractText": "abstract",
        "pubYear": "2024",
        "doi": "10.1/epmc.preprint",
        "fullTextUrlList": {"fullTextUrl": [{"url": "https://x"}]},
        "pubTypeList": {"pubType": ["preprint"]},
        "journalInfo": {"journal": {"title": "EPMC"}},
    }
    payload = {"resultList": {"result": [rec]}}
    items = EuropePMCConnector()._extract_from_payload("q", payload, 1)
    assert items[0].preprint_evidence.startswith(TIER_A)
    assert items[0].peer_review_evidence == ""
    assert items[0].indexing_evidence.startswith(TIER_B)


def test_epmc_journal_article_pubtype_is_tier_a_peer() -> None:
    """Europe PMC ``Journal Article`` pubType emits tierA peer-review."""
    rec = {
        "title": "EPMC Journal Article",
        "abstractText": "abstract",
        "pubYear": "2024",
        "doi": "10.1/epmc.article",
        "fullTextUrlList": {"fullTextUrl": [{"url": "https://x"}]},
        "pubTypeList": {"pubType": ["Journal Article"]},
        "journalInfo": {"journal": {"title": "EPMC"}},
    }
    payload = {"resultList": {"result": [rec]}}
    items = EuropePMCConnector()._extract_from_payload("q", payload, 1)
    assert items[0].peer_review_evidence.startswith(TIER_A)
    assert items[0].preprint_evidence == ""


def test_epmc_fixture_without_pubtype_is_tier_b_indexed() -> None:
    """Shipped Europe PMC fixture has no pubType -> peer unverified, indexed tierB."""
    items = EuropePMCConnector()._extract_from_payload("q", _fixture("europe_pmc"), 1)
    assert items[0].peer_review_evidence == ""
    assert items[0].preprint_evidence == ""
    assert items[0].indexing_evidence.startswith(TIER_B)


def test_pmc_fixture_emits_tier_b_peer_and_index() -> None:
    """PMC fixture (no explicit preprint) -> tierB peer-review + tierB indexing."""
    items = PMCConnector()._extract_from_payload("q", _fixture("pmc"), 1)
    assert items[0].peer_review_evidence.startswith(TIER_B)
    assert items[0].indexing_evidence.startswith(TIER_B)


# --- PubMed ------------------------------------------------------------------


def test_pubmed_fixture_emits_tier_b_peer_and_index() -> None:
    """PubMed records fixture -> tierB peer-review + tierB indexing (MEDLINE)."""
    items = PubMedConnector()._extract_from_payload("q", _fixture("pubmed"), 1)
    assert items[0].peer_review_evidence.startswith(TIER_B)
    assert items[0].indexing_evidence.startswith(TIER_B)
    assert items[0].preprint_evidence == ""


# --- DOAJ --------------------------------------------------------------------


def test_doaj_fixture_emits_tier_b_peer_and_index() -> None:
    """DOAJ fixture -> tierB peer-review (by policy) + tierB indexing."""
    items = DOAJConnector()._extract_from_payload("q", _fixture("doaj"), 1)
    assert items[0].peer_review_evidence.startswith(TIER_B)
    assert items[0].indexing_evidence.startswith(TIER_B)


# --- ArXiv -------------------------------------------------------------------


def test_arxiv_records_fixture_emits_tier_a_preprint() -> None:
    """ArXiv records fixture -> tierA preprint."""
    items = ArXivConnector()._extract_from_payload("q", _fixture("arxiv"), 1)
    assert items[0].preprint_evidence.startswith(TIER_A)
    assert items[0].peer_review_evidence == ""


def test_arxiv_xml_emits_tier_a_preprint() -> None:
    """ArXiv Atom XML -> tierA preprint."""
    xml = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<feed xmlns="http://www.w3.org/2005/Atom">'
        "<entry>"
        "<title>ArXiv XML Preprint</title>"
        "<summary>Abstract text for arxiv.</summary>"
        "<id>http://arxiv.org/abs/2501.00002</id>"
        "<published>2024-01-01T00:00:00Z</published>"
        "<author><name>Jane Doe</name></author>"
        "</entry>"
        "</feed>"
    )
    items = ArXivConnector()._parse_arxiv_xml(xml, 1)
    assert items[0].preprint_evidence.startswith(TIER_A)


# --- HAL ---------------------------------------------------------------------


def test_hal_peer_reviewing_flag_emits_tier_a() -> None:
    """HAL ``peerReviewing_s=1`` emits tierA peer-review."""
    payload = {
        "response": {
            "numFound": 1,
            "start": 0,
            "docs": [
                {
                    "halId_s": "hal-1",
                    "title_s": ["HAL Reviewed Article"],
                    "authFullName_s": ["Test Author"],
                    "abstract_s": ["A study."],
                    "doiId_s": "10.1/hal.reviewed",
                    "publicationDateY_i": 2024,
                    "uri_s": "https://hal.archives-ouvertes.fr/hal-1",
                    "peerReviewing_s": "1",
                },
            ],
        },
    }
    items = HALConnector()._extract_from_payload("q", payload, 1)
    assert items[0].peer_review_evidence.startswith(TIER_A)


def test_hal_fixture_without_flag_stays_unverified() -> None:
    """The shipped HAL fixture has no ``peerReviewing_s`` -> peer unverified."""
    items = HALConnector()._extract_from_payload("q", _fixture("hal"), 1)
    assert items[0].peer_review_evidence == ""


# --- IACR --------------------------------------------------------------------


def test_iacr_fixture_emits_tier_a_preprint() -> None:
    """IACR RSS-shaped fixture -> tierA preprint."""
    items = IACRConnector()._extract_from_payload("q", _fixture("iacr"), 1)
    assert items[0].preprint_evidence.startswith(TIER_A)


# --- Conservative (unverified) sources ---------------------------------------


def test_dblp_fixture_emits_no_evidence() -> None:
    """DBLP is a mixed aggregator -> no tier evidence emitted (conservative)."""
    items = DBLPConnector()._extract_from_payload("q", _fixture("dblp"), 1)
    assert items[0].peer_review_evidence == ""
    assert items[0].preprint_evidence == ""
    assert items[0].indexing_evidence == ""


def test_zenodo_fixture_emits_no_evidence() -> None:
    """Zenodo is a mixed repository -> no tier evidence emitted (conservative)."""
    items = ZenodoConnector()._extract_from_payload("q", _fixture("zenodo"), 1)
    assert items[0].peer_review_evidence == ""
    assert items[0].preprint_evidence == ""


def test_core_fixture_emits_no_evidence() -> None:
    """CORE is a mixed aggregator -> no tier evidence emitted (conservative)."""
    items = COREConnector()._extract_from_payload("q", _fixture("core"), 1)
    assert items[0].peer_review_evidence == ""
    assert items[0].preprint_evidence == ""


# --- Retraction + citation metadata ------------------------------------------


def test_openalex_retracted_emits_flag_and_note() -> None:
    """An OpenAlex ``is_retracted`` work emits the flag; notice stays empty.

    OpenAlex exposes no retraction-notice field, so the note is left empty
    and ``apply`` fills the default text; other sources may contribute a
    notice on re-ingest (monotonic in ``_save_article``).
    """
    payload = {
        "results": [
            {
                "title": "Retracted Work",
                "doi": "https://doi.org/10.1000/retracted",
                "publication_year": 2020,
                "primary_location": {"source": {"display_name": "Test Journal"}},
                "abstract_inverted_index": {"Test": [0], "abstract": [1]},
                "is_retracted": True,
                "cited_by_count": 7,
            },
        ],
    }
    items = OpenAlexConnector()._extract_from_payload("q", payload, 1)
    assert items[0].is_retracted is True
    assert items[0].retraction_note == ""
    assert items[0].cited_by_count == 7


def test_openalex_not_retracted_emits_defaults() -> None:
    """An OpenAlex work without retraction flags emits conservative defaults."""
    payload = {
        "results": [
            {
                "title": "Clean Work",
                "doi": "https://doi.org/10.1000/clean",
                "publication_year": 2021,
                "primary_location": {"source": {"display_name": "Test Journal"}},
                "abstract_inverted_index": {"Test": [0], "abstract": [1]},
            },
        ],
    }
    items = OpenAlexConnector()._extract_from_payload("q", payload, 1)
    assert items[0].is_retracted is False
    assert items[0].retraction_note == ""
    assert items[0].cited_by_count == 0


def test_crossref_retraction_assertion_emits_flag() -> None:
    """A Crossref ``retraction`` assertion flags the work as retracted."""
    payload = {
        "message": {
            "items": [
                {
                    "title": ["Retracted Paper"],
                    "DOI": "10.1000/retracted",
                    "type": "journal-article",
                    "abstract": "A test abstract.",
                    "assertion": [
                        {"name": "retraction", "value": "withdrawn by authors"},
                    ],
                },
            ],
        },
    }
    items = CrossrefConnector()._extract_from_payload("q", payload, 1)
    assert items[0].is_retracted is True
    assert items[0].retraction_note == "withdrawn by authors"


def test_crossref_relation_retraction_emits_flag() -> None:
    """A Crossref canonical ``relation.retraction`` flags the work."""
    payload = {
        "message": {
            "items": [
                {
                    "title": ["Retracted via relation"],
                    "DOI": "10.1000/retracted2",
                    "type": "journal-article",
                    "abstract": "A test abstract.",
                    "relation": {
                        "retraction": [
                            {"id-type": "doi", "id": "https://doi.org/notice"},
                        ],
                    },
                },
            ],
        },
    }
    items = CrossrefConnector()._extract_from_payload("q", payload, 1)
    assert items[0].is_retracted is True
    assert items[0].retraction_note == "https://doi.org/notice"


def test_crossref_citation_count_emits_referenced_by() -> None:
    """Crossref ``is-referenced-by-count`` maps to ``cited_by_count``."""
    payload = {
        "message": {
            "items": [
                {
                    "title": ["Cited Paper"],
                    "DOI": "10.1000/cited",
                    "type": "journal-article",
                    "abstract": "A test abstract.",
                    "is-referenced-by-count": 42,
                },
            ],
        },
    }
    items = CrossrefConnector()._extract_from_payload("q", payload, 1)
    assert items[0].cited_by_count == 42


def test_epmc_retraction_pubtype_emits_flag() -> None:
    """Europe PMC ``Retraction of Publication`` pubType flags the work."""
    payload = {
        "resultList": {
            "result": [
                {
                    "title": "Retracted EPMC Work",
                    "doi": "10.1000/epmc-retracted",
                    "fullTextUrlList": {
                        "fullTextUrl": [{"url": "https://example.org/paper"}]
                    },
                    "id": "PMC1001",
                    "abstractText": "A test abstract.",
                    "pubTypeList": {
                        "pubType": ["Retracted Publication", "Journal Article"]
                    },
                    "citedByCount": 3,
                },
            ],
        },
    }
    items = EuropePMCConnector()._extract_from_payload("q", payload, 1)
    assert items[0].is_retracted is True
    assert items[0].cited_by_count == 3


def test_epmc_cited_by_count_emits_citations() -> None:
    """Europe PMC ``citedByCount`` maps to ``cited_by_count``."""
    payload = {
        "resultList": {
            "result": [
                {
                    "title": "EPMC Cited Work",
                    "doi": "10.1000/epmc-cited",
                    "fullTextUrlList": {
                        "fullTextUrl": [{"url": "https://example.org/paper"}]
                    },
                    "id": "PMC1002",
                    "abstractText": "A test abstract.",
                    "pubTypeList": {"pubType": ["Journal Article"]},
                    "citedByCount": 9,
                },
            ],
        },
    }
    items = EuropePMCConnector()._extract_from_payload("q", payload, 1)
    assert items[0].is_retracted is False
    assert items[0].cited_by_count == 9


def test_pmc_retraction_pubtype_emits_flag() -> None:
    """PMC records carrying a retraction pubType are flagged."""
    payload = {
        "resultList": {
            "result": [
                {
                    "title": "Retracted PMC Work",
                    "doi": "10.1000/pmc-retracted",
                    "fullTextUrlList": {
                        "fullTextUrl": [{"url": "https://example.org/paper"}]
                    },
                    "id": "PMC1003",
                    "abstractText": "A test abstract.",
                    "pubTypeList": {
                        "pubType": ["Retracted Publication", "Journal Article"]
                    },
                    "citedByCount": 1,
                },
            ],
        },
    }
    items = PMCConnector()._extract_from_payload("q", payload, 1)
    assert items[0].is_retracted is True
    assert items[0].cited_by_count == 1
