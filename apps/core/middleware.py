from __future__ import annotations


class APICsrfExemptMiddleware:
    """Exempt API paths from CSRF verification.

    This is an internal, JSON-only API service in an isolated network.
    No browser-based form submissions exist, so CSRF tokens are not used.
    Only `/api/` paths are exempted; admin and other views keep CSRF protection.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path.startswith("/api/"):
            request.csrf_processing_done = True
        return self.get_response(request)
