"""Core models shared across the CIndex project."""

from __future__ import annotations

import secrets

from django.db import models


class StoredSecretKey(models.Model):
    """Singleton model storing a persistent SECRET_KEY in the database."""

    # On first run (or when the key is missing), a cryptographically strong key
    # is generated and saved. Subsequent loads read the same key, ensuring
    # session and CSRF token stability across worker restarts.

    key = models.CharField(max_length=128, unique=True)

    def __str__(self) -> str:
        """Return a debug representation showing the primary key."""
        return f"StoredSecretKey(pk={self.pk})"

    @classmethod
    def get_or_generate(cls) -> str:
        """Return the stored key, generating one if necessary."""
        obj, _created = cls.objects.get_or_create(
            pk=1,
            defaults={"key": secrets.token_urlsafe(64)},
        )
        return obj.key
