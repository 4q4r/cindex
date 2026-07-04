"""Middleware exempting JSON-only API paths from CSRF verification."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from django.http import HttpRequest, HttpResponseBase


class APICsrfExemptMiddleware:
    """Exempt API paths from CSRF verification."""

    # This is an internal, JSON-only API service in an isolated network.
    # No browser-based form submissions exist, so CSRF tokens are not used.
    # Only `/api/` paths are exempted; admin and other views keep CSRF protection.

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponseBase]) -> None:
        """Store the next handler in the middleware chain."""
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponseBase:
        """Mark API requests as having completed CSRF processing."""
        if request.path.startswith("/api/"):
            request.csrf_processing_done = True
        return self.get_response(request)
