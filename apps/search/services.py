"""Article-level search orchestration backed by PostgreSQL rows."""

from __future__ import annotations

from typing import ClassVar

from django.conf import settings
from django.contrib.postgres.search import SearchQuery, SearchVectorField
from django.db import connections
from django.db.models import Case, FloatField, Q, QuerySet, Value, When
from django.db.models.expressions import RawSQL

from apps.articles.models import Article
from apps.core.text import canonical_text_key, normalize_doi, normalize_scholarly_text
from apps.core.translate import expand_search_terms
from apps.ingestion.services import IngestionService


class SearchService:
    """Article-level search orchestration backed by PostgreSQL rows."""

    TITLE_WEIGHT = 6.0
    ABSTRACT_WEIGHT = 4.0
    FULL_TEXT_WEIGHT = 2.0
    JOURNAL_WEIGHT = 1.0
    DOI_WEIGHT = 10.0

    SOURCE_WEIGHT_PENALTY: ClassVar[dict[str, float]] = {
        "zenodo": 0.3,
    }

    @staticmethod
    def _preview_from_article(article: Article) -> str:
        """Build a display preview from the article text fields."""
        candidates = (
            article.abstract,
            article.full_text,
            article.title,
        )
        for value in candidates:
            preview = normalize_scholarly_text(value or "", max_length=500)
            if preview:
                return preview
        return ""

    @staticmethod
    def _search_text(query: str, expression: str) -> str:
        """Combine the public query and expression into a single search string."""
        combined = " ".join(
            part.strip() for part in (query, expression) if part.strip()
        )
        return normalize_scholarly_text(combined, max_length=512)

    @staticmethod
    def _search_terms(query: str, expression: str) -> list[str]:
        """Return normalized search terms for article-level matching."""
        text = SearchService._search_text(query, expression).casefold()
        if not text:
            return []
        terms = [term for term in text.split() if term]
        return terms or [text]

    @staticmethod
    def _payload(
        article: Article,
        preview: str,
        *,
        rerank_score: float = 0.0,
    ) -> dict:
        """Build the serialized payload."""
        authors = [
            item.author.full_name
            for item in article.article_authors.select_related("author").all()
        ]
        return {
            "id": article.id,
            "title": normalize_scholarly_text(article.title, max_length=900),
            "preview": normalize_scholarly_text(preview, max_length=500),
            "year": article.publication_year,
            "publication_date": article.publication_date,
            "source": article.source.name,
            "journal": normalize_scholarly_text(
                article.journal.name if article.journal else "",
                max_length=300,
            ),
            "authors": authors,
            "volume": article.volume or "",
            "issue": article.issue or "",
            "pages": article.pages or "",
            "doi": article.doi or "",
            "identifiers": {x.kind: x.value for x in article.identifiers.all()},
            "eligibility_evidence": {
                "peer_reviewed": article.is_peer_reviewed_or_refereed,
                "indexed": article.is_indexed_in_reputable_db,
                "doi_and_journal_card": article.has_doi_and_journal_card,
                "not_preprint": article.is_not_preprint_or_author_manuscript,
            },
            "eligibility_confidence": {
                "peer_reviewed": article.peer_review_confidence,
                "indexed": article.indexing_confidence,
                "doi_and_journal_card": article.doi_and_card_confidence,
                "not_preprint": article.not_preprint_confidence,
                "overall": article.eligibility_confidence,
            },
            "url": article.url,
            "rerank_score": rerank_score,
        }

    @staticmethod
    def _articles_queryset() -> QuerySet[Article]:
        """Return the searchable article queryset with related rows loaded."""
        return (
            Article.objects.filter(doi__startswith="10.")
            .select_related("source", "journal")
            .prefetch_related("article_authors__author", "identifiers")
        )

    @classmethod
    def _dedupe_key(cls, article: Article) -> str:
        """Compute the stable deduplication key."""
        doi = normalize_doi(article.doi or "")
        if doi:
            return f"doi:{doi}"
        title_key = canonical_text_key(article.title)
        journal_key = canonical_text_key(
            article.journal.name if article.journal else "",
        )
        year = str(article.publication_year or "")
        return "|".join(
            [
                f"title:{title_key}",
                f"year:{year}",
                f"journal:{journal_key}",
            ],
        )

    @classmethod
    def _term_score(cls, term: str) -> Case:
        """Build a score contribution for a single search term."""
        return Case(
            When(doi__iexact=term, then=Value(cls.DOI_WEIGHT)),
            When(title__icontains=term, then=Value(cls.TITLE_WEIGHT)),
            When(abstract__icontains=term, then=Value(cls.ABSTRACT_WEIGHT)),
            When(full_text__icontains=term, then=Value(cls.FULL_TEXT_WEIGHT)),
            When(journal__name__icontains=term, then=Value(cls.JOURNAL_WEIGHT)),
            default=Value(0.0),
            output_field=FloatField(),
        )

    @classmethod
    def _exact_query_score(cls, search_text: str) -> Case:
        """Build a score bonus for exact article-level matches."""
        return Case(
            When(doi__iexact=normalize_doi(search_text), then=Value(cls.DOI_WEIGHT)),
            When(title__iexact=search_text, then=Value(4.0)),
            When(title__icontains=search_text, then=Value(2.0)),
            default=Value(0.0),
            output_field=FloatField(),
        )

    CROSSLANG_WEIGHT = 0.5

    @classmethod
    def _crosslang_term_score(cls, term: str) -> Case:
        """Build a lower-weight score for cross-lingual search terms."""
        tw = cls.TITLE_WEIGHT * cls.CROSSLANG_WEIGHT
        aw = cls.ABSTRACT_WEIGHT * cls.CROSSLANG_WEIGHT
        fw = cls.FULL_TEXT_WEIGHT * cls.CROSSLANG_WEIGHT
        return Case(
            When(title__icontains=term, then=Value(tw)),
            When(abstract__icontains=term, then=Value(aw)),
            When(full_text__icontains=term, then=Value(fw)),
            default=Value(0.0),
            output_field=FloatField(),
        )

    @classmethod
    def _source_penalty(cls) -> Case:
        """Build a multiplicative penalty for low-relevance sources."""
        whens = [
            When(
                source__key=key,
                then=Value(penalty, output_field=FloatField()),
            )
            for key, penalty in cls.SOURCE_WEIGHT_PENALTY.items()
        ]
        if not whens:
            return Case(default=Value(1.0), output_field=FloatField())
        return Case(*whens, default=Value(1.0), output_field=FloatField())

    @classmethod
    def _score_expression(
        cls,
        search_text: str,
        terms: list[str],
        cross_lingual: list[str] | None = None,
    ) -> Value | Case:
        """Build a database-side score expression for ranked search.

        Args:
            search_text: The original combined query string.
            terms: Primary language search terms.
            cross_lingual: Optional cross-lingual equivalents scored at lower weight.

        Returns:
            A Django expression producing the search score per row.

        """
        score: Value | Case = Value(0.0, output_field=FloatField())
        if search_text:
            score = score + cls._exact_query_score(search_text)
        for term in terms:
            score = score + cls._term_score(term)
        for term in cross_lingual or []:
            score = score + cls._crosslang_term_score(term)
        return score * cls._source_penalty()

    @staticmethod
    def _is_postgres() -> bool:
        """Return True when the default database connection is PostgreSQL."""
        return connections["default"].vendor == "postgresql"

    @staticmethod
    def _pg_fts_vector() -> RawSQL:
        """Return a raw tsvector expression matching the GIN index in 0009.

        The expression is intentionally written as raw SQL so it stays
        byte-identical to the index definition in migration
        ``articles/0009_pg_fulltext_gin_index`` -- PostgreSQL only uses a GIN
        expression index for ``vector @@ query`` when the indexed expression
        matches the query expression exactly.
        """
        return RawSQL(
            "to_tsvector('simple', coalesce(title, '') || ' ' || "
            "coalesce(abstract, '') || ' ' || coalesce(full_text, ''))",
            (),
            output_field=SearchVectorField(),
        )

    @classmethod
    def _pg_search_query(
        cls,
        search_text: str,
        cross_lingual: list[str],
    ) -> SearchQuery:
        """Build a tsquery combining the main text and cross-lingual terms.

        Cross-lingual translations are OR-ed into the query so an article
        written in a different language than the query still matches.
        """
        query = SearchQuery(search_text, search_type="websearch", config="simple")
        for translated in cross_lingual:
            cleaned = translated.strip()
            if not cleaned:
                continue
            query = query | SearchQuery(
                cleaned,
                search_type="websearch",
                config="simple",
            )
        return query

    @classmethod
    def _cross_lingual_terms(cls, query: str) -> tuple[list[str], list[str]]:
        """Return cross-lingual phrases and their individual tokens."""
        cross_lingual = expand_search_terms(query)
        tokens: list[str] = []
        for translated in cross_lingual:
            tokens.extend(t for t in translated.split() if len(t) > 1)
        return list(cross_lingual), tokens

    @classmethod
    def _build_sqlite_queryset(
        cls,
        search_text: str,
        terms: list[str],
        cross_lingual: list[str],
        cross_lingual_tokens: list[str],
    ) -> QuerySet[Article]:
        """Build the icontains OR-tree queryset for non-PostgreSQL backends."""
        filters = Q(title__icontains=search_text)
        filters |= Q(abstract__icontains=search_text)
        filters |= Q(full_text__icontains=search_text)
        filters |= Q(journal__name__icontains=search_text)
        filters |= Q(doi__icontains=search_text)
        for term in terms:
            filters |= Q(title__icontains=term)
            filters |= Q(abstract__icontains=term)
            filters |= Q(full_text__icontains=term)
            filters |= Q(journal__name__icontains=term)
            filters |= Q(doi__icontains=term)
        for translated in cross_lingual:
            tokens = [t for t in translated.split() if len(t) > 1]
            filters |= Q(title__icontains=translated)
            filters |= Q(abstract__icontains=translated)
            filters |= Q(full_text__icontains=translated)
            for token in tokens:
                filters |= Q(title__icontains=token)
                filters |= Q(abstract__icontains=token)
                filters |= Q(full_text__icontains=token)
        return (
            cls._articles_queryset()
            .filter(filters)
            .annotate(
                search_score=cls._score_expression(
                    search_text,
                    terms,
                    cross_lingual=cross_lingual_tokens,
                ),
            )
            .order_by("-search_score", "-publication_year", "-updated_at", "id")
        )

    @classmethod
    def _build_pg_queryset(
        cls,
        search_text: str,
        terms: list[str],
        cross_lingual: list[str],
        cross_lingual_tokens: list[str],
    ) -> QuerySet[Article]:
        """Build the PostgreSQL full-text queryset using a GIN-indexed tsvector.

        The primary filter is ``vector @@ tsquery`` (index-accelerated); the
        existing Case/When scoring expression is applied on top so DOI exact
        boosts, source penalties, and cross-lingual token weights are preserved.
        """
        return (
            cls._articles_queryset()
            .annotate(_fts_vector=cls._pg_fts_vector())
            .filter(_fts_vector=cls._pg_search_query(search_text, cross_lingual))
            .annotate(
                search_score=cls._score_expression(
                    search_text,
                    terms,
                    cross_lingual=cross_lingual_tokens,
                ),
            )
            .order_by("-search_score", "-publication_year", "-updated_at", "id")
        )

    @classmethod
    def _search_queryset(
        cls,
        query: str,
        expression: str,
    ) -> tuple[QuerySet[Article], str, list[str]]:
        """Build the article queryset for the current search.

        On PostgreSQL the primary filter is a GIN-indexed full-text search over
        title, abstract, and full_text; on other backends (used for the SQLite
        test database) it falls back to an icontains OR-tree. Both paths expand
        the search with cross-lingual equivalents so articles saved in a
        different language than the query are still matched.
        """
        search_text = cls._search_text(query, expression)
        terms = cls._search_terms(query, expression)
        if not search_text:
            return cls._articles_queryset().none(), search_text, terms
        cross_lingual, cross_lingual_tokens = cls._cross_lingual_terms(query)
        if cls._is_postgres():
            queryset = cls._build_pg_queryset(
                search_text,
                terms,
                cross_lingual,
                cross_lingual_tokens,
            )
        else:
            queryset = cls._build_sqlite_queryset(
                search_text,
                terms,
                cross_lingual,
                cross_lingual_tokens,
            )
        return queryset, search_text, terms

    @classmethod
    def _run_index_search(
        cls,
        query: str,
        expression: str,
        size: int = 30,
    ) -> list[dict]:
        """Execute the article-level search pipeline."""
        queryset, _, _ = cls._search_queryset(query, expression)
        ranked_articles: list[dict] = []
        seen_keys: set[str] = set()
        for article in queryset[: max(size, settings.APP.search_final_top_k)]:
            dedupe_key = cls._dedupe_key(article)
            if dedupe_key in seen_keys:
                continue
            seen_keys.add(dedupe_key)
            ranked_articles.append(
                {
                    "article": article,
                    "candidate_score": float(
                        getattr(article, "search_score", 0.0) or 0.0,
                    ),
                    "rerank_score": float(getattr(article, "search_score", 0.0) or 0.0),
                },
            )

        results: list[dict] = []
        for item in ranked_articles:
            article = item["article"]
            results.append(
                cls._payload(
                    article,
                    cls._preview_from_article(article),
                    rerank_score=float(item.get("rerank_score", 0.0)),
                ),
            )
            if len(results) >= size:
                break
        return results

    @classmethod
    def index_hit_count(cls, query: str, expression: str) -> int:
        """Return the number of indexed hits for the current query."""
        try:
            queryset, _, _ = cls._search_queryset(query, expression)
            return int(queryset.count())
        except (ValueError, RuntimeError):
            return 0

    @classmethod
    def run(
        cls,
        query: str,
        expression: str,
        force_refresh: bool = False,  # noqa: FBT001, FBT002  # public API positional bool
    ) -> list[dict]:
        """Run the article search pipeline and return ranked results."""
        if force_refresh:
            IngestionService.ingest_query(query)
        try:
            results = cls._run_index_search(
                query=query,
                expression=expression,
                size=settings.APP.search_final_top_k,
            )
            if results:
                return results
        except (ValueError, RuntimeError):
            return []
        return []
