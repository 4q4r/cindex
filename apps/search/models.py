"""Database models for tracked search jobs and rolling wait-time statistics."""

from __future__ import annotations

import uuid

from django.db import models


class SearchJob(models.Model):
    """A tracked asynchronous search job with live progress metadata."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    query = models.CharField(max_length=512)
    expression = models.CharField(max_length=1024, blank=True)
    force_refresh_requested = models.BooleanField(default=False)
    freshness_days_used = models.IntegerField(default=14)
    status = models.CharField(max_length=32, default="queued")
    stage = models.CharField(max_length=64, default="queued")
    substage = models.CharField(max_length=64, blank=True, default="")
    substage_label = models.CharField(max_length=128, blank=True, default="")
    message = models.TextField(blank=True)

    source_total = models.IntegerField(default=0)
    source_done = models.IntegerField(default=0)
    source_live = models.IntegerField(default=0)
    source_failed = models.JSONField(default=list)
    source_timings = models.JSONField(default=dict)

    index_hits_before = models.IntegerField(default=0)
    index_hits_after = models.IntegerField(default=0)
    rescan_triggered = models.BooleanField(default=False)
    rescan_reason = models.CharField(max_length=64, blank=True)

    results = models.JSONField(default=list)
    error = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        """Django model metadata and options."""

        indexes = (
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["query", "created_at"]),
        )

    def __str__(self) -> str:
        """Return the human-readable representation of the object."""
        return f"{self.id}:{self.status}:{self.query[:40]}"


class SearchWaitStat(models.Model):
    """A persisted rolling average for search completion times."""

    KIND_WITHOUT_ENRICHMENT = "without_enrichment"
    KIND_WITH_ENRICHMENT = "with_enrichment"
    KIND_CHOICES = (
        (KIND_WITHOUT_ENRICHMENT, "Without enrichment"),
        (KIND_WITH_ENRICHMENT, "With enrichment"),
    )

    kind = models.CharField(max_length=64, unique=True, choices=KIND_CHOICES)
    average_seconds = models.FloatField(default=0.0)
    sample_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """Django model metadata and options."""

        indexes = (models.Index(fields=["kind"]),)

    def __str__(self) -> str:
        """Return the human-readable representation of the object."""
        return f"{self.kind}:{self.average_seconds:.2f}"

    @classmethod
    def kind_for_job(cls, rescan_triggered: bool) -> str:  # noqa: FBT001  # public classmethod API
        """Return the stat kind for a completed search job."""
        return (
            cls.KIND_WITH_ENRICHMENT
            if rescan_triggered
            else cls.KIND_WITHOUT_ENRICHMENT
        )

    @classmethod
    def record_completion(
        cls,
        rescan_triggered: bool,  # noqa: FBT001  # public classmethod API
        duration_seconds: float,
        *,
        exclude_job_id: str | None = None,  # noqa: ARG003  # kept for caller compatibility
    ) -> None:
        """Update the rolling average after a search job completes.

        On each completion, the running average is updated as
        ``new_avg = (old_avg + duration) / 2``.
        """
        if duration_seconds < 0:
            return
        kind = cls.kind_for_job(rescan_triggered)
        stat = cls.objects.get_or_create(
            kind=kind,
            defaults={"average_seconds": 0.0, "sample_count": 0},
        )[0]
        stat.average_seconds = (stat.average_seconds + duration_seconds) / 2.0
        stat.sample_count += 1
        stat.save(update_fields=["average_seconds", "sample_count", "updated_at"])
