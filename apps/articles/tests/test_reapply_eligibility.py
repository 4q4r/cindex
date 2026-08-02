"""Backfill task ``reapply_eligibility`` tests."""

from __future__ import annotations

from apps.articles.models import Article, Journal, Source
from apps.articles.services import TIER_A, ArticleEligibilityService
from apps.articles.tasks import reapply_eligibility

DOI = "10.1000/xyz123"


def _make_article(source_key: str, *, abstract: str = "generic abstract") -> Article:
    """Persist an article for the given source and return it."""
    source, _ = Source.objects.get_or_create(
        key=source_key,
        defaults={"name": source_key, "base_url": "https://example.org"},
    )
    journal, _ = Journal.objects.get_or_create(name="Journal of Tests")
    doi = f"10.1000/{source_key}"
    return Article.objects.create(
        source=source,
        journal=journal,
        title=f"Study {source_key}",
        abstract=abstract,
        full_text=f"DOI {doi} body",
        doi=doi,
        url=f"https://example.org/{source_key}",
    )


def test_reapply_eligibility_backfills_corpus(db) -> None:
    """Re-running apply over a mixed corpus reclassifies by source reputation."""
    pubmed = _make_article("pubmed", abstract="medical study")
    arxiv = _make_article("arxiv", abstract="physics preprint")
    other = _make_article("test", abstract="generic abstract")

    result = reapply_eligibility.apply(kwargs={}).get()

    pubmed.refresh_from_db()
    arxiv.refresh_from_db()
    other.refresh_from_db()

    # PubMed -> peer-reviewed via source reputation default (conf 0.6).
    assert pubmed.is_peer_reviewed_or_refereed is True
    assert pubmed.peer_review_confidence == 0.6
    # arXiv -> preprint via PREPRINT_SOURCES, not peer-reviewed.
    assert arxiv.is_not_preprint_or_author_manuscript is False
    assert arxiv.is_peer_reviewed_or_refereed is False
    # Unknown source, no keywords -> unverified.
    assert other.is_peer_reviewed_or_refereed is False

    assert result["total"] == 3
    assert result["peer_reviewed"] == 1
    assert result["preprint"] == 1


def test_reapply_eligibility_scoped_to_source(db) -> None:
    """The ``source_keys`` filter scopes the backfill to named sources."""
    _make_article("pubmed", abstract="medical study")
    _make_article("arxiv", abstract="physics preprint")

    result = reapply_eligibility.apply(
        kwargs={"source_keys": ["pubmed"]},
    ).get()

    assert result["total"] == 1
    assert result["peer_reviewed"] == 1


def test_reapply_preserves_existing_tier_evidence(db) -> None:
    """Backfill must not overwrite connector-set tier evidence already present."""
    article = _make_article("crossref", abstract="generic abstract")
    article.peer_review_evidence = f"{TIER_A} Crossref: received/accepted"
    article.save(update_fields=["peer_review_evidence"])

    reapply_eligibility.apply(kwargs={}).get()
    article.refresh_from_db()

    assert article.peer_review_evidence == f"{TIER_A} Crossref: received/accepted"
    assert article.is_peer_reviewed_or_refereed is True
    assert article.peer_review_confidence == 1.0


def test_reapply_idempotent(db) -> None:
    """Running the backfill twice yields identical flags and counts."""
    _make_article("pubmed", abstract="medical study")
    first = reapply_eligibility.apply(kwargs={}).get()
    second = reapply_eligibility.apply(kwargs={}).get()
    assert first == second


def test_reapply_fills_reason_for_transparency(db) -> None:
    """The source-default path fills a human-readable reason when evidence is empty."""
    article = _make_article("doaj", abstract="open access study")
    reapply_eligibility.apply(kwargs={}).get()
    article.refresh_from_db()
    assert article.is_peer_reviewed_or_refereed is True
    # Source-default path fills a Russian reason (no tier prefix) so a teacher
    # can verify *why* the article was marked peer-reviewed.
    assert "doaj" in article.peer_review_evidence.lower()


def test_reapply_skips_failing_article_and_continues(db, monkeypatch) -> None:
    """A per-article apply failure is logged + skipped; the batch continues."""
    _make_article("pubmed", abstract="medical study")
    _make_article("arxiv", abstract="physics preprint")
    _make_article("test", abstract="generic abstract")

    original = ArticleEligibilityService.apply
    calls = {"n": 0}

    def flaky_apply(article):  # test double
        calls["n"] += 1
        if calls["n"] == 2:
            raise ValueError("boom")
        return original(article)

    monkeypatch.setattr(ArticleEligibilityService, "apply", flaky_apply)

    result = reapply_eligibility.apply(kwargs={}).get()
    assert result["failed"] == 1
    assert result["total"] == 2
