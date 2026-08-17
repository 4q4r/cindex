"""Serializers for search requests, results, and asynchronous search-job state."""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from apps.core.text import normalize_scholarly_text

from .filters import SORT_CHOICES, SORT_RELEVANCE


class SearchFiltersSerializerMixin(serializers.Serializer):
    """
    Shared server-side filter and sort fields for search endpoints.

    These mirror :class:`apps.search.filters.SearchFilters` so the immediate
    and asynchronous search paths accept the same parameters. Filters are
    applied inside ``SearchService`` before the top-K truncation.
    """

    peer_reviewed_only = serializers.BooleanField(default=False)
    indexed_only = serializers.BooleanField(default=False)
    exclude_preprints = serializers.BooleanField(default=False)
    exclude_retracted = serializers.BooleanField(default=False)
    year_from = serializers.IntegerField(
        required=False,
        allow_null=True,
        min_value=0,
        max_value=9999,
        default=None,
    )
    year_to = serializers.IntegerField(
        required=False,
        allow_null=True,
        min_value=0,
        max_value=9999,
        default=None,
    )
    sort_by = serializers.ChoiceField(
        choices=SORT_CHOICES,
        default=SORT_RELEVANCE,
    )


class SearchRequestSerializer(SearchFiltersSerializerMixin):
    """Validate immediate search requests from the client."""

    query = serializers.CharField(max_length=512)
    expression = serializers.CharField(
        max_length=1024,
        required=False,
        allow_blank=True,
        default="",
    )
    force_refresh = serializers.BooleanField(default=False)
    page = serializers.IntegerField(min_value=1, default=1)
    per_page = serializers.IntegerField(min_value=1, max_value=50, default=5)


class SearchJobCreateSerializer(SearchFiltersSerializerMixin):
    """Validate asynchronous search-job creation payloads."""

    query = serializers.CharField(max_length=512)
    expression = serializers.CharField(
        max_length=1024,
        required=False,
        allow_blank=True,
        default="",
    )
    force_refresh = serializers.BooleanField(default=False)


class SearchJobDetailQuerySerializer(serializers.Serializer):
    """Validate pagination query params for the search-job detail endpoint."""

    page = serializers.IntegerField(min_value=1, default=1)
    per_page = serializers.IntegerField(min_value=1, max_value=50, default=5)


class QuoteSerializer(serializers.Serializer):
    """
    A verbatim quote extracted from an article by the PERELMAN agent.

    Quotes are extracted query-agnostic (the article's own salient passages)
    and cached per-article in ``ArticleQuotes``; the search query is used only
    for frontend highlighting, never for extraction.
    """

    text = serializers.CharField()
    location = serializers.CharField(required=False, allow_blank=True)
    relevance = serializers.FloatField(
        required=False,
        min_value=0,
        max_value=1,
    )
    rationale = serializers.CharField(required=False, allow_blank=True)


class SearchResultSerializer(serializers.Serializer):
    """Serialize a ranked search result for the frontend."""

    id = serializers.IntegerField()
    title = serializers.CharField()
    preview = serializers.CharField()
    year = serializers.IntegerField(allow_null=True)
    publication_date = serializers.DateField(allow_null=True)
    source = serializers.CharField()
    journal = serializers.CharField(allow_blank=True)
    authors = serializers.ListField(child=serializers.CharField())
    volume = serializers.CharField(allow_blank=True)
    issue = serializers.CharField(allow_blank=True)
    pages = serializers.CharField(allow_blank=True)
    doi = serializers.CharField(allow_blank=True)
    identifiers = serializers.DictField(child=serializers.CharField(), required=False)
    eligibility_evidence = serializers.DictField(child=serializers.BooleanField())
    eligibility_confidence = serializers.DictField(child=serializers.FloatField())
    is_retracted = serializers.BooleanField()
    retraction_note = serializers.CharField(allow_blank=True)
    cited_by_count = serializers.IntegerField()
    tier = serializers.CharField()
    url = serializers.CharField()
    rerank_score = serializers.FloatField(required=False)
    quotes = QuoteSerializer(many=True, required=False, default=list)
    tldr = serializers.CharField(required=False, default="", allow_blank=True)

    def to_representation(
        self,
        instance: Any,  # noqa: ANN401  # DRF serializer instance is dynamic
    ) -> dict[str, Any]:
        """Convert the instance into a normalized serializable dict."""
        data = super().to_representation(instance)
        data["title"] = normalize_scholarly_text(data.get("title", ""), max_length=900)
        data["preview"] = normalize_scholarly_text(
            data.get("preview", ""),
            max_length=500,
        )
        data["journal"] = normalize_scholarly_text(
            data.get("journal", ""),
            max_length=300,
        )
        data["tldr"] = normalize_scholarly_text(
            data.get("tldr", ""),
            max_length=500,
        )
        quotes = data.get("quotes") or []
        data["quotes"] = quotes if isinstance(quotes, list) else []
        return data


class SearchJobSerializer(serializers.Serializer):
    """Serialize the full state of a search job."""

    id = serializers.UUIDField()
    query = serializers.CharField()
    expression = serializers.CharField()
    status = serializers.CharField()
    stage = serializers.CharField()
    substage = serializers.CharField()
    substage_label = serializers.CharField()
    message = serializers.CharField()
    progress_percent = serializers.IntegerField()
    source_total = serializers.IntegerField()
    source_done = serializers.IntegerField()
    source_live = serializers.IntegerField()
    source_failed = serializers.ListField(child=serializers.CharField())
    source_timings = serializers.JSONField(required=False)
    average_wait_without_enrichment_seconds = serializers.IntegerField(
        allow_null=True,
        required=False,
    )
    average_wait_with_enrichment_seconds = serializers.IntegerField(
        allow_null=True,
        required=False,
    )
    index_hits_before = serializers.IntegerField()
    index_hits_after = serializers.IntegerField()
    rescan_triggered = serializers.BooleanField()
    rescan_reason = serializers.CharField()
    freshness_days_used = serializers.IntegerField()
    peer_reviewed_only = serializers.BooleanField()
    indexed_only = serializers.BooleanField()
    exclude_preprints = serializers.BooleanField()
    exclude_retracted = serializers.BooleanField()
    year_from = serializers.IntegerField(allow_null=True)
    year_to = serializers.IntegerField(allow_null=True)
    sort_by = serializers.CharField()
    page = serializers.IntegerField(required=False)
    per_page = serializers.IntegerField(required=False)
    total_pages = serializers.IntegerField(required=False)
    total_results = serializers.IntegerField(required=False)
    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()
    finished_at = serializers.DateTimeField(allow_null=True)
    error = serializers.CharField()
    results = SearchResultSerializer(many=True)
