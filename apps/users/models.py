"""Custom user model for the CIndex application."""

from __future__ import annotations

from django.contrib.auth.models import AbstractUser


class User(AbstractUser):
    """User class."""
