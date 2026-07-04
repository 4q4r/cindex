"""Management command that resets derived article search state."""

from __future__ import annotations

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    """Reset derived article search state."""

    help = "Reset PostgreSQL-backed article search state."

    def handle(self, *args: str, **options: str) -> None:  # noqa: ARG002  # BaseCommand signature
        """Report that search now uses live PostgreSQL article rows only."""
        self.stdout.write(
            self.style.SUCCESS(
                "Search indexes reset: postgres-only search has no derived state",
            ),
        )
