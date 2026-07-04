"""Django admin configuration for the articles app."""

from django.contrib import admin

from .models import Article, Author, Identifier, Journal, Source


@admin.register(Source)
class SourceAdmin(admin.ModelAdmin):
    """Source Admin class."""

    list_display = (
        "key",
        "name",
        "active",
        "is_circuit_open_badge",
        "consecutive_failures",
        "total_successes",
        "total_failures",
        "last_success_at",
    )
    search_fields = ("key", "name")
    list_filter = ("active",)

    @admin.display(boolean=True, description="Circuit Open")
    def is_circuit_open_badge(self, obj: Source) -> bool:
        """Return whether circuit open badge."""
        return obj.is_circuit_open()


admin.site.register(Journal)
admin.site.register(Author)
admin.site.register(Identifier)
admin.site.register(Article)
