from __future__ import annotations

from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    """User class."""

    preferred_citation_style = models.CharField(
        max_length=32,
        default="gost_2018",
        choices=(("gost_2018", "GOST R 7.0.100-2018"), ("gost_2003", "GOST 7.1-2003")),
    )
