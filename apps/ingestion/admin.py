from django.contrib import admin

from .models import ExaApiKeyQuota, IngestionRun, LocalImportFile


@admin.register(ExaApiKeyQuota)
class ExaApiKeyQuotaAdmin(admin.ModelAdmin):
    """Admin for cached Exa API key usage snapshots."""

    list_display = (
        "api_key_name",
        "api_key_id",
        "rate_limit_per_minute",
        "usage_total_cost_usd",
        "usage_window",
        "last_synced_at",
    )
    readonly_fields = (
        "api_key_name",
        "api_key_id",
        "rate_limit_per_minute",
        "usage_total_cost_usd",
        "usage_breakdown",
        "usage_window_start",
        "usage_window_end",
        "last_synced_at",
        "last_error",
    )
    search_fields = ("api_key_name", "api_key_id")
    ordering = ("api_key_name", "api_key_id")
    actions = ("refresh_from_exa",)

    @admin.display(description="Usage window")
    def usage_window(self, obj: ExaApiKeyQuota) -> str:
        """Return a compact usage window summary."""
        if not obj.usage_window_start and not obj.usage_window_end:
            return "—"
        start = obj.usage_window_start.isoformat() if obj.usage_window_start else "?"
        end = obj.usage_window_end.isoformat() if obj.usage_window_end else "?"
        return f"{start} → {end}"

    @admin.action(description="Refresh Exa usage snapshots")
    def refresh_from_exa(self, request, queryset) -> None:
        """Refresh Exa usage snapshots from the official team-management API."""
        from django.contrib import messages

        from apps.ingestion.exa_usage import sync_exa_usage_snapshots

        updated, failed = sync_exa_usage_snapshots()
        if failed:
            messages.warning(
                request,
                (
                    "Exa usage sync finished with "
                    f"{updated} updates and {failed} failures."
                ),
            )
        else:
            messages.success(
                request,
                f"Exa usage sync finished successfully for {updated} keys.",
            )


admin.site.register(IngestionRun)
admin.site.register(LocalImportFile)
