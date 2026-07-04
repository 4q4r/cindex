from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from .models import Article, Identifier

INDEXING_KEYWORDS = [
    "scopus",
    "web of science",
    "medline",
    "pmc",
    "pubmed",
    "pubmed central",
    "kci",
    "tr dizin",
    "esci",
    "doaj",
    "openalex",
    "crossref",
    "semantic scholar",
]
PREPRINT_KEYWORDS = [
    "preprint",
    "author manuscript",
    "accepted manuscript",
    "working paper",
]
PEER_REVIEW_KEYWORDS = [
    "peer reviewed",
    "peer-review",
    "refereed",
    "double blind review",
]
DOI_PATTERN = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)


@dataclass
class EligibilityDecision:
    """Boolean and confidence outcome for article eligibility filtering."""

    peer_reviewed: bool
    indexed: bool
    doi_and_card: bool
    not_preprint: bool
    peer_review_confidence: float
    indexing_confidence: float
    doi_and_card_confidence: float
    not_preprint_confidence: float

    @property
    def eligible(self) -> bool:
        """Return whether the article is eligible for indexing."""
        return (
            self.peer_reviewed
            and self.indexed
            and self.doi_and_card
            and self.not_preprint
        )

    @property
    def overall_confidence(self) -> float:
        """Compute the overall confidence score."""
        return round(
            (
                self.peer_review_confidence
                + self.indexing_confidence
                + self.doi_and_card_confidence
                + self.not_preprint_confidence
            )
            / 4.0,
            4,
        )


class ArticleEligibilityService:
    """Compute and persist article eligibility decisions."""

    @staticmethod
    def _token_confidence(text: str, tokens: list[str], cap: int) -> float:
        """Compute token-level confidence for the article."""
        if cap <= 0:
            return 0.0
        matched = sum(1 for token in tokens if token in text)
        return round(min(1.0, matched / cap), 4)

    @classmethod
    def evaluate(cls, article: Article) -> EligibilityDecision:
        """Evaluate."""
        text = " ".join(
            [
                article.title or "",
                article.abstract or "",
                article.full_text[:5000] if article.full_text else "",
                article.peer_review_evidence or "",
                article.indexing_evidence or "",
                article.preprint_evidence or "",
            ],
        ).lower()

        peer_reviewed = any(token in text for token in PEER_REVIEW_KEYWORDS)
        indexed = any(token in text for token in INDEXING_KEYWORDS)
        peer_review_confidence = cls._token_confidence(
            text, PEER_REVIEW_KEYWORDS, cap=1,
        )
        indexing_confidence = cls._token_confidence(text, INDEXING_KEYWORDS, cap=2)
        has_doi = bool(article.doi and DOI_PATTERN.search(article.doi)) or bool(
            DOI_PATTERN.search(text),
        )
        journal_card = bool(
            article.journal_id and article.journal and article.journal.name,
        )
        doi_and_card = has_doi and journal_card
        doi_confidence = 1.0 if has_doi else 0.0
        journal_confidence = 1.0 if journal_card else 0.0
        doi_and_card_confidence = round((doi_confidence + journal_confidence) / 2.0, 4)
        not_preprint = not any(token in text for token in PREPRINT_KEYWORDS)
        not_preprint_confidence = 1.0 if not_preprint else 0.0
        return EligibilityDecision(
            peer_reviewed=peer_reviewed,
            indexed=indexed,
            doi_and_card=doi_and_card,
            not_preprint=not_preprint,
            peer_review_confidence=peer_review_confidence,
            indexing_confidence=indexing_confidence,
            doi_and_card_confidence=doi_and_card_confidence,
            not_preprint_confidence=not_preprint_confidence,
        )

    @classmethod
    def apply(cls, article: Article) -> Article:
        """Apply the computed result to the domain object."""
        decision = cls.evaluate(article)
        article.is_peer_reviewed_or_refereed = decision.peer_reviewed
        article.is_indexed_in_reputable_db = decision.indexed
        article.has_doi_and_journal_card = decision.doi_and_card
        article.is_not_preprint_or_author_manuscript = decision.not_preprint
        article.is_eligible = decision.eligible
        article.peer_review_confidence = decision.peer_review_confidence
        article.indexing_confidence = decision.indexing_confidence
        article.doi_and_card_confidence = decision.doi_and_card_confidence
        article.not_preprint_confidence = decision.not_preprint_confidence
        article.eligibility_confidence = decision.overall_confidence
        article.save(
            update_fields=[
                "is_peer_reviewed_or_refereed",
                "is_indexed_in_reputable_db",
                "has_doi_and_journal_card",
                "is_not_preprint_or_author_manuscript",
                "is_eligible",
                "peer_review_confidence",
                "indexing_confidence",
                "doi_and_card_confidence",
                "not_preprint_confidence",
                "eligibility_confidence",
                "updated_at",
            ],
        )
        return article


class CitationService:
    """Render bibliographic citations from structured article metadata."""

    @staticmethod
    def _authors(article: Article) -> str:
        """Return the normalized author list."""
        names = [
            x.author.full_name
            for x in article.article_authors.select_related("author").all()
        ]
        if not names:
            return "Unknown author"
        if len(names) > 3:
            return ", ".join(names[:3]) + " [et al.]"
        return ", ".join(names)

    @classmethod
    def render(cls, article: Article, style: str = "gost_2018") -> str:
        """Render the response payload."""
        authors = cls._authors(article)
        year = article.publication_year or "n.d."
        journal = article.journal.name if article.journal else "Unknown journal"
        doi = f" DOI: {article.doi}" if article.doi else ""

        if style == "gost_2003":
            return (
                f"{authors} {article.title} // {journal}. {year}. "
                f"Т. {article.volume or '-'} № {article.issue or '-'}."  # noqa: RUF001
                f" С. {article.pages or '-'}{doi}"  # noqa: RUF001
            ).strip()
        return (
            f"{authors}. {article.title}. {journal}. {year};"
            f"{article.volume or '-'}({article.issue or '-'}):"
            f"{article.pages or '-'}{doi}"
        ).strip()


class IdentifierService:
    """Maintain normalized article identifiers."""

    @staticmethod
    def upsert(article: Article, identifiers: Iterable[tuple[str, str]]) -> None:
        """Upsert the record in persistent storage."""
        for kind, value in identifiers:
            if not value:
                continue
            Identifier.objects.get_or_create(article=article, kind=kind, value=value)
