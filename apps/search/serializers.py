from __future__ import annotations

from rest_framework import serializers

from apps.core.text import normalize_scholarly_text


class SearchRequestSerializer(serializers.Serializer):
    """Validate immediate search requests from the client."""

    query = serializers.CharField(max_length=512)
    expression = serializers.CharField(
        max_length=1024,
        required=False,
        allow_blank=True,
        default="",
    )
    force_refresh = serializers.BooleanField(default=False)


class SearchJobCreateSerializer(serializers.Serializer):
    """Validate asynchronous search-job creation payloads."""

    query = serializers.CharField(max_length=512)
    expression = serializers.CharField(
        max_length=1024,
        required=False,
        allow_blank=True,
        default="",
    )
    force_refresh = serializers.BooleanField(default=False)


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
    url = serializers.CharField()
    rerank_score = serializers.FloatField(required=False)

    def to_representation(self, instance):
        """To representation."""
        data = super().to_representation(instance)
        data["title"] = normalize_scholarly_text(data.get("title", ""), max_length=900)
        data["preview"] = normalize_scholarly_text(
            data.get("preview", ""), max_length=500,
        )
        data["journal"] = normalize_scholarly_text(
            data.get("journal", ""), max_length=300,
        )
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
        allow_null=True, required=False,
    )
    average_wait_with_enrichment_seconds = serializers.IntegerField(
        allow_null=True, required=False,
    )
    index_hits_before = serializers.IntegerField()
    index_hits_after = serializers.IntegerField()
    rescan_triggered = serializers.BooleanField()
    rescan_reason = serializers.CharField()
    freshness_days_used = serializers.IntegerField()
    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()
    finished_at = serializers.DateTimeField(allow_null=True)
    error = serializers.CharField()
    results = SearchResultSerializer(many=True)
