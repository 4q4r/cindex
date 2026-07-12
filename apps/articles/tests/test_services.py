from apps.articles.models import Article, Journal, Source
from apps.articles.services import ArticleEligibilityService


def test_article_eligibility_true(db) -> None:
    """Test article eligibility true helper."""
    source = Source.objects.create(
        key="test",
        name="Test",
        base_url="https://example.org",
    )
    journal = Journal.objects.create(name="Journal of Tests")
    article = Article.objects.create(
        source=source,
        journal=journal,
        title="Peer Reviewed Study",
        abstract="This peer reviewed study is indexed in Scopus and Web of Science.",
        full_text="DOI 10.1000/xyz123 journal article",
        doi="10.1000/xyz123",
        url="https://example.org/a1",
    )
    ArticleEligibilityService.apply(article)
    article.refresh_from_db()
    assert article.is_eligible is True
    assert article.peer_review_confidence > 0
    assert article.indexing_confidence > 0
    assert article.doi_and_card_confidence > 0
    assert article.not_preprint_confidence == 1.0
    assert article.eligibility_confidence > 0


def test_article_eligibility_preprint_fails(db) -> None:
    """Test article eligibility preprint fails helper."""
    source = Source.objects.create(
        key="test2",
        name="Test2",
        base_url="https://example.org",
    )
    journal = Journal.objects.create(name="Journal 2")
    article = Article.objects.create(
        source=source,
        journal=journal,
        title="Preprint Item",
        abstract="peer reviewed scopus web of science",
        full_text="this is preprint doi 10.1000/xyz123",
        doi="10.1000/xyz123",
        url="https://example.org/a2",
    )
    ArticleEligibilityService.apply(article)
    article.refresh_from_db()
    assert article.is_eligible is False
