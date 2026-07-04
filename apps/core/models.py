from __future__ import annotations

import secrets

from django.db import models


class StoredSecretKey(models.Model):
    """Singleton model storing a persistent SECRET_KEY in the database.

    On first run (or when the key is missing), a cryptographically strong key
    is generated and saved. Subsequent loads read the same key, ensuring
    session and CSRF token stability across worker restarts.
    """

    key = models.CharField(max_length=128, unique=True)

    @classmethod
    def get_or_generate(cls) -> str:
        """Return the stored key, generating one if necessary."""
        obj, created = cls.objects.get_or_create(
            pk=1,
            defaults={"key": secrets.token_urlsafe(64)},
        )
        return obj.key

    def __str__(self) -> str:
        return f"StoredSecretKey(pk={self.pk})"
