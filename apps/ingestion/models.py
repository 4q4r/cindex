from django.db import models

from apps.articles.models import Article


class IngestionRun(models.Model):
    """Ingestion Run class."""

    query = models.CharField(max_length=512, blank=True)
    source_key = models.CharField(max_length=64)
    status = models.CharField(max_length=32, default="started")
    error = models.TextField(blank=True)
    fetched = models.IntegerField(default=0)
    eligible = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:
        """Return the human-readable representation of the object."""
        return f"{self.source_key}:{self.status}:{self.query[:32]}"


class LocalImportFile(models.Model):
    """A file tracked from the local import drop folder."""

    path = models.CharField(max_length=1024, unique=True)
    sha256 = models.CharField(max_length=64, blank=True)
    status = models.CharField(max_length=32, default="pending")
    article = models.ForeignKey(
        Article, null=True, blank=True, on_delete=models.SET_NULL,
    )
    metadata = models.JSONField(default=dict, blank=True)
    error = models.TextField(blank=True)
    last_seen_at = models.DateTimeField(auto_now=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:
        """Return the human-readable representation of the object."""
        return f"{self.path}:{self.status}"


class ExaApiKeyQuota(models.Model):
    """Cached Exa API key usage snapshot for admin visibility."""

    api_key_id = models.CharField(max_length=64, unique=True)
    api_key_name = models.CharField(max_length=128, blank=True)
    rate_limit_per_minute = models.IntegerField(null=True, blank=True)
    usage_total_cost_usd = models.DecimalField(
        max_digits=12, decimal_places=4, null=True, blank=True,
    )
    usage_breakdown = models.JSONField(default=list, blank=True)
    usage_window_start = models.DateTimeField(null=True, blank=True)
    usage_window_end = models.DateTimeField(null=True, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)

    def __str__(self) -> str:
        """Return the human-readable representation of the object."""
        label = self.api_key_name or self.api_key_id
        return f"{label}:{self.api_key_id[:12]}"
