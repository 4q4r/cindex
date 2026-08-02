"""Tiered peer-review / preprint / indexing classifier precedence tests."""

from __future__ import annotations

import pytest

from apps.articles.models import Article, Journal, Source
from apps.articles.services import (
    TIER_A,
    TIER_B,
    ArticleEligibilityService,
)

DOI = "10.1000/xyz123"


def _make_article(  # test fixture with explicit overrides
    db,
    *,
    source_key: str = "test",
    title: str = "Study",
    abstract: str = "",
    full_text: str = "",
    peer_review_evidence: str = "",
    indexing_evidence: str = "",
    preprint_evidence: str = "",
) -> Article:
    """Persist an article with the given source + evidence and return it."""
    source, _ = Source.objects.get_or_create(
        key=source_key,
        defaults={"name": source_key, "base_url": "https://example.org"},
    )
    journal, _ = Journal.objects.get_or_create(name="Journal of Tests")
    article = Article.objects.create(
        source=source,
        journal=journal,
        title=title,
        abstract=abstract,
        full_text=full_text or f"DOI {DOI} body",
        doi=DOI,
        url=f"https://example.org/{source_key}",
        peer_review_evidence=peer_review_evidence,
        indexing_evidence=indexing_evidence,
        preprint_evidence=preprint_evidence,
    )
    return article


def test_tier_a_preprint_overrides_tier_a_peer_review(db) -> None:
    """A tierA preprint marker must override a tierA peer-review signal."""
    article = _make_article(
        db,
        source_key="pmc",
        peer_review_evidence=f"{TIER_A} PMC Journal Article",
        preprint_evidence=f"{TIER_A} Europe PMC: preprint",
    )
    ArticleEligibilityService.apply(article)
    article.refresh_from_db()
    assert article.is_not_preprint_or_author_manuscript is False
    assert article.is_peer_reviewed_or_refereed is False
    assert article.peer_review_confidence == pytest.approx(1.0)


def test_tier_a_peer_review(db) -> None:
    """A tierA peer-review evidence yields peer-reviewed at confidence 1.0."""
    article = _make_article(
        db,
        source_key="hal",
        peer_review_evidence=f"{TIER_A} HAL: peerReviewing=1",
    )
    ArticleEligibilityService.apply(article)
    article.refresh_from_db()
    assert article.is_peer_reviewed_or_refereed is True
    assert article.peer_review_confidence == pytest.approx(1.0)


def test_tier_b_peer_review(db) -> None:
    """A tierB peer-review evidence yields peer-reviewed at confidence 0.7."""
    article = _make_article(
        db,
        source_key="openalex",
        peer_review_evidence=f"{TIER_B} OpenAlex: journal article",
        indexing_evidence=f"{TIER_B} OpenAlex",
    )
    ArticleEligibilityService.apply(article)
    article.refresh_from_db()
    assert article.is_peer_reviewed_or_refereed is True
    assert article.peer_review_confidence == pytest.approx(0.7)
    assert article.is_indexed_in_reputable_db is True
    assert article.indexing_confidence == pytest.approx(0.7)


def test_source_reputation_default_for_pubmed(db) -> None:
    """A PubMed article with no evidence is peer-reviewed via source reputation."""
    article = _make_article(db, source_key="pubmed")
    ArticleEligibilityService.apply(article)
    article.refresh_from_db()
    assert article.is_peer_reviewed_or_refereed is True
    assert article.peer_review_confidence == pytest.approx(0.6)
    # The source-default path fills a human-readable reason for transparency.
    assert article.peer_review_evidence  # non-empty reason


def test_source_reputation_preprint_for_arxiv(db) -> None:
    """An arXiv article is a preprint via PREPRINT_SOURCES even without evidence."""
    article = _make_article(db, source_key="arxiv")
    ArticleEligibilityService.apply(article)
    article.refresh_from_db()
    assert article.is_not_preprint_or_author_manuscript is False
    assert article.is_peer_reviewed_or_refereed is False
    assert article.preprint_evidence  # reason filled


def test_keyword_scan_peer_review(db) -> None:
    """With no tier and a non-default source, keyword scan classifies at 0.3."""
    article = _make_article(
        db,
        source_key="test",
        abstract="this is a peer reviewed study",
    )
    ArticleEligibilityService.apply(article)
    article.refresh_from_db()
    assert article.is_peer_reviewed_or_refereed is True
    assert article.peer_review_confidence == pytest.approx(0.3)


def test_unverified_stays_false(db) -> None:
    """No evidence, no keywords, non-default source -> unverified (False)."""
    article = _make_article(db, source_key="test", abstract="generic abstract")
    ArticleEligibilityService.apply(article)
    article.refresh_from_db()
    assert article.is_peer_reviewed_or_refereed is False
    assert article.peer_review_confidence == pytest.approx(0.0)


def test_scan_text_excludes_evidence_fields(db) -> None:
    """Evidence fields are NOT keyword-scanned (prevents self-fabricated matches).

    A plain (non-tier) ``indexing_evidence="scopus"`` must NOT make the article
    indexed when ``scopus`` does not appear in title/abstract/full_text.
    """
    article = _make_article(
        db,
        source_key="test",
        abstract="generic abstract with no index keyword",
        indexing_evidence="scopus",  # plain string, no tier prefix
    )
    ArticleEligibilityService.apply(article)
    article.refresh_from_db()
    assert article.is_indexed_in_reputable_db is False
    assert article.indexing_confidence == pytest.approx(0.0)


def test_apply_preserves_connector_tier_evidence(db) -> None:
    """``apply`` must not overwrite connector-set tierA peer-review evidence."""
    explicit = f"{TIER_A} Crossref: received/accepted assertion"
    article = _make_article(
        db,
        source_key="crossref",
        peer_review_evidence=explicit,
    )
    ArticleEligibilityService.apply(article)
    article.refresh_from_db()
    assert article.peer_review_evidence == explicit
    assert article.is_peer_reviewed_or_refereed is True
    assert article.peer_review_confidence == pytest.approx(1.0)


def test_indexed_keyword_scan(db) -> None:
    """``indexed`` falls back to keyword scan over scan_text at confidence 0.3."""
    article = _make_article(
        db,
        source_key="test",
        abstract="indexed in pubmed and scopus",
    )
    ArticleEligibilityService.apply(article)
    article.refresh_from_db()
    assert article.is_indexed_in_reputable_db is True
    assert article.indexing_confidence > 0


def test_indexed_keyword_confidence_capped_at_keyword_tier(db) -> None:
    """Multiple indexing keywords must NOT raise confidence above 0.3.

    A pure keyword scan is the weakest signal; it must never equal tierA (1.0)
    or exceed tierB (0.7), which would invert the tier model.
    """
    article = _make_article(
        db,
        source_key="test",
        abstract="indexed in pubmed scopus ieee doi",
    )
    ArticleEligibilityService.apply(article)
    article.refresh_from_db()
    assert article.is_indexed_in_reputable_db is True
    assert article.indexing_confidence == pytest.approx(0.3)


def test_preprint_keyword_in_text(db) -> None:
    """A preprint keyword in the text marks the article a preprint."""
    article = _make_article(
        db,
        source_key="test",
        abstract="this is a preprint manuscript",
    )
    ArticleEligibilityService.apply(article)
    article.refresh_from_db()
    assert article.is_not_preprint_or_author_manuscript is False
    assert article.is_peer_reviewed_or_refereed is False
