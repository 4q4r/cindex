"""Article eligibility, citation rendering, and identifier services."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .models import Article, Identifier

if TYPE_CHECKING:
    from collections.abc import Iterable

_MAX_SHORT_AUTHORS = 3

# --- Peer-review / indexing tier protocol -------------------------------------
# Connectors encode the strength of the per-record signal as a leading tier
# prefix on the ``*_evidence`` text fields. The classifier evaluates tiers in
# order of authority (explicit per-record > source reputation > keyword scan)
# and never lets a weaker signal override a stronger one. Prefixes are plain
# ASCII so they survive ``normalize_scholarly_text`` and are stable in the DB.
#
#   tierA: explicit per-record confirmation from the source API
#          (HAL ``peerReviewing=1``, Crossref Received/Accepted assertion,
#           Europe PMC ``Journal Article`` pubType, OpenAlex ``preprint`` work
#           type). confidence 1.0.
#   tierB: venue / source-reputation inference (OpenAlex venue=journal,
#          Crossref journal-article+ISSN, MEDLINE/PubMed/PMC, DOAJ policy).
#          confidence 0.7.
#
# The classifier also keeps a conservative source-reputation fallback (see
# ``PEER_REVIEWED_BY_DEFAULT``) so the one-shot backfill can reclassify the
# pre-existing corpus (whose ``peer_review_evidence`` is empty because it was
# ingested before the connectors emitted tier signals). confidence 0.6.
TIER_A = "tierA:"
TIER_B = "tierB:"

# Sources whose entire corpus is peer-reviewed by reputation. Used only as a
# Tier B fallback when no per-record evidence is present (mainly the backfill
# of pre-existing articles). Conservative: only sources that are *by policy*
# peer-reviewed belong here -- mixed repositories (Zenodo, CORE, HAL, DBLP)
# are deliberately omitted so their records stay "unverified" rather than
# silently marked peer-reviewed.
PEER_REVIEWED_BY_DEFAULT: frozenset[str] = frozenset(
    {"pubmed", "pmc", "europe_pmc", "doaj", "scielo", "mathnet"},
)

# Sources whose entire corpus is preprints (never peer-reviewed).
PREPRINT_SOURCES: frozenset[str] = frozenset({"arxiv", "iacr"})

# Confidence assigned per decision path. Lower than the explicit tiers, the
# source-default and keyword-scan paths still surface a result so the
# ``peer_reviewed_only`` / ``indexed_only`` filters return real matches
# instead of the near-empty results seen before this fix.
_CONFIDENCE_TIER_A = 1.0
_CONFIDENCE_TIER_B = 0.7
_CONFIDENCE_SOURCE_DEFAULT = 0.6
_CONFIDENCE_KEYWORD = 0.3
_CONFIDENCE_NONE = 0.0

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


def _has_tier(evidence: str | None, tier: str) -> bool:
    """Return whether ``evidence`` carries the given tier prefix."""
    return bool(evidence) and evidence.startswith(tier)


def tier_label(article: Article) -> str:
    """
    Return the peer-review trust tier label for a search-result card.

    Labels mirror the classifier's confidence tiers: ``A`` for an explicit
    per-record signal (conf 1.0), ``B`` for venue/reputation inference (conf
    0.7), ``source-default`` for the source-reputation fallback (conf 0.6),
    ``keyword`` for the text-scan inference (conf 0.3), and ``none`` for
    unverified or non-peer-reviewed articles. The label is derived from the
    persisted confidence so the frontend never re-implements the tier model.
    """
    if not article.is_peer_reviewed_or_refereed:
        return "none"
    confidence = article.peer_review_confidence
    if confidence >= _CONFIDENCE_TIER_A:
        return "A"
    if confidence >= _CONFIDENCE_TIER_B:
        return "B"
    if confidence >= _CONFIDENCE_SOURCE_DEFAULT:
        return "source-default"
    if confidence >= _CONFIDENCE_KEYWORD:
        return "keyword"
    return "none"


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
    # Human-readable Russian reason for the peer-review / preprint call. The
    # classifier fills these only on the source-default and keyword paths (the
    # connector-set tier evidence is preserved verbatim) so a teacher reading
    # the evidence field can see *why* an article was marked peer-reviewed.
    peer_review_reason: str = ""
    preprint_reason: str = ""
    # Set when the article carries a retraction flag (connector-provided).
    # A retracted article is never eligible, regardless of its peer-review
    # verdict, and can be excluded from results via the ``exclude_retracted``
    # search filter (default search keeps it, badge-marked).
    retracted: bool = False

    @property
    def eligible(self) -> bool:
        """Return whether the article is eligible for indexing."""
        return (
            not self.retracted
            and self.peer_reviewed
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
    def _source_key(article: Article) -> str:
        """Return the connector source key for ``article`` (``""`` if unknown)."""
        source = getattr(article, "source", None)
        return getattr(source, "key", "") or ""

    @classmethod
    def _decide_preprint(
        cls,
        preprint_ev: str,
        scan_text: str,
        source_key: str,
    ) -> tuple[bool, str]:
        """
        Return ``(is_preprint, reason)``.

        A preprint verdict is reached on any of: explicit tierA preprint
        evidence, a preprint keyword in the article text, or membership in a
        preprint-only source (arXiv/IACR). The reason is filled only on the
        non-explicit paths so connector evidence is preserved verbatim.
        """
        if _has_tier(preprint_ev, TIER_A):
            return True, ""
        if source_key in PREPRINT_SOURCES:
            return True, f"препринт-источник: {source_key}"
        if any(tok in scan_text for tok in PREPRINT_KEYWORDS):
            return True, "упоминание препринта в тексте"
        return False, ""

    @classmethod
    def _decide_peer_review(
        cls,
        peer_ev: str,
        scan_text: str,
        source_key: str,
        is_preprint: bool,  # noqa: FBT001  # boolean flag is the semantic input
    ) -> tuple[bool, float, str]:
        """
        Return ``(peer_reviewed, confidence, reason)`` by tier precedence.

        Precedence: preprint override > tierA > tierB > source default >
        keyword scan > unverified. A preprint is never peer-reviewed.
        """
        if is_preprint:
            return False, _CONFIDENCE_TIER_A, ""
        if _has_tier(peer_ev, TIER_A):
            return True, _CONFIDENCE_TIER_A, ""
        if _has_tier(peer_ev, TIER_B):
            return True, _CONFIDENCE_TIER_B, ""
        if source_key in PEER_REVIEWED_BY_DEFAULT:
            return (
                True,
                _CONFIDENCE_SOURCE_DEFAULT,
                f"рецензируемый источник по репутации: {source_key}",
            )
        if any(tok in scan_text for tok in PEER_REVIEW_KEYWORDS):
            return True, _CONFIDENCE_KEYWORD, "упоминание рецензирования в тексте"
        return False, _CONFIDENCE_NONE, ""

    @classmethod
    def evaluate(cls, article: Article) -> EligibilityDecision:
        """
        Evaluate peer-review / preprint / indexed eligibility by tiers.

        Precedence (strongest signal wins; a weaker signal never overrides a
        stronger one):

        1. Explicit preprint tierA evidence  -> preprint (overrides peer-review).
        2. Explicit peer-review tierA evidence -> peer-reviewed, conf 1.0.
        3. Explicit peer-review tierB evidence -> peer-reviewed, conf 0.7.
        4. Source-reputation default           -> peer-reviewed, conf 0.6.
        5. Keyword scan of title/abstract/text  -> peer-reviewed, conf 0.3.
        6. Otherwise                           -> unverified (False), conf 0.0.

        A retracted article is additionally never eligible: the retraction
        flag is applied last (strongest signal) and flips ``is_eligible`` off
        without touching the peer-review flags, which the search UI still
        shows for transparency. Search keeps retracted articles by default
        (badge-marked); the ``exclude_retracted`` filter drops them.
        """
        peer_ev = article.peer_review_evidence or ""
        index_ev = article.indexing_evidence or ""
        preprint_ev = article.preprint_evidence or ""
        source_key = cls._source_key(article)

        # Keyword scan runs over title/abstract/fulltext only -- NOT over the
        # evidence fields -- so a Russian reason we wrote on a prior pass (or a
        # connector tier string) cannot fabricate a keyword match.
        scan_text = " ".join(
            [
                article.title or "",
                article.abstract or "",
                article.full_text[:5000] if article.full_text else "",
            ],
        ).lower()

        # --- preprint (overrides peer-review) --------------------------------
        is_preprint, preprint_reason = cls._decide_preprint(
            preprint_ev,
            scan_text,
            source_key,
        )
        not_preprint = not is_preprint

        # --- peer-reviewed ---------------------------------------------------
        peer_reviewed, peer_review_confidence, peer_review_reason = (
            cls._decide_peer_review(peer_ev, scan_text, source_key, is_preprint)
        )

        # --- indexed in a reputable DB --------------------------------------
        if _has_tier(index_ev, TIER_A) or _has_tier(index_ev, TIER_B):
            indexed = True
            indexing_confidence = (
                _CONFIDENCE_TIER_A
                if _has_tier(index_ev, TIER_A)
                else _CONFIDENCE_TIER_B
            )
        else:
            # Keyword-tier fallback: a single keyword hit is the weakest signal
            # (conf 0.3), never 1.0 -- otherwise a pure keyword scan could equal
            # an explicit tierA connector signal, inverting the tier model.
            indexed = any(tok in scan_text for tok in INDEXING_KEYWORDS)
            indexing_confidence = _CONFIDENCE_KEYWORD if indexed else _CONFIDENCE_NONE

        has_doi = bool(article.doi and DOI_PATTERN.search(article.doi)) or bool(
            DOI_PATTERN.search(scan_text),
        )
        journal_card = bool(
            article.journal_id and article.journal and article.journal.name,
        )
        doi_and_card = has_doi and journal_card
        doi_confidence = 1.0 if has_doi else 0.0
        journal_confidence = 1.0 if journal_card else 0.0
        doi_and_card_confidence = round((doi_confidence + journal_confidence) / 2.0, 4)
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
            peer_review_reason=peer_review_reason,
            preprint_reason=preprint_reason,
            retracted=bool(article.is_retracted),
        )

    @classmethod
    def apply(cls, article: Article) -> Article:
        """
        Apply the computed result to the domain object.

        Connector-set tier evidence is preserved verbatim. When the classifier
        reached its verdict via the source-default or keyword path, it fills a
        human-readable Russian reason into the (empty) evidence field so a
        teacher can verify *why* an article was marked peer-reviewed -- the
        transparency the high-trust audience requires.
        """
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

        update_fields = [
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
        ]
        if decision.peer_review_reason and not article.peer_review_evidence:
            article.peer_review_evidence = decision.peer_review_reason
            update_fields.append("peer_review_evidence")
        if decision.preprint_reason and not article.preprint_evidence:
            article.preprint_evidence = decision.preprint_reason
            update_fields.append("preprint_evidence")
        if decision.retracted and not article.retraction_note:
            article.retraction_note = "статья отозвана (retraction)"
            update_fields.append("retraction_note")

        update_fields.append("updated_at")
        article.save(update_fields=update_fields)
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
        if len(names) > _MAX_SHORT_AUTHORS:
            return ", ".join(names[:_MAX_SHORT_AUTHORS]) + " [et al.]"
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
