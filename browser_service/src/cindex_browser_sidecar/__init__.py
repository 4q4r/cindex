"""cindex browser sidecar — headless Chromium fetch service.

A small FastAPI service that wraps cloakbrowser (source-patched Chromium) so
the distroless cindex worker can fetch HTML/XML/JSON/RSS from sources protected
by JS challenges (BunnyCDN Shield, Cloudflare Turnstile) without importing a
browser stack. The worker calls ``POST /fetch`` over the internal docker
network; this service owns the single persistent Chromium context.
"""

from __future__ import annotations

from cindex_browser_sidecar.main import app, create_pool, get_pool
from cindex_browser_sidecar.models import FetchRequest, FetchResponse

__all__ = [
    "FetchRequest",
    "FetchResponse",
    "app",
    "create_pool",
    "get_pool",
]

__version__ = "0.1.0"
