from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.core.models import StoredSecretKey


class Command(BaseCommand):
    """Generate and persist a SECRET_KEY in the database if one does not exist."""

    help = "Generate a SECRET_KEY in the database if not present."

    def handle(self, *args, **options):
        key = StoredSecretKey.get_or_generate()
        self.stdout.write(f"SECRET_KEY: {key[:8]}... (stored in DB)")