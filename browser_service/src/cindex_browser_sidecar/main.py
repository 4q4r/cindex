"""FastAPI application — the browser sidecar HTTP surface.

Exposes ``POST /fetch`` (forwarded to the persistent cloakbrowser context) and
``GET /healthz``. The browser pool is created once in the lifespan and injected
into the endpoint via FastAPI's dependency system so tests can override it
without touching the real browser.
"""

from __future__ import annotations

import asyncio
import base64
import os
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import structlog
import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from cindex_browser_sidecar.browser_pool import BrowserPool, BrowserPoolError
from cindex_browser_sidecar.models import (
    FetchRequest,
    FetchResponse,
    ScreenshotRequest,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(20),  # INFO
    cache_logger_on_first_use=True,
)

logger = structlog.get_logger(__name__)

DEFAULT_PORT = 8081
DEFAULT_CONCURRENCY = 4

_pool: BrowserPool | None = None


def create_pool() -> BrowserPool:
    """Build the process-wide browser pool from environment configuration."""
    return BrowserPool(
        profile_dir=os.getenv("CINDEX_PROFILE_DIR"),
        headless=os.getenv("CINDEX_BROWSER_HEADLESS", "1")
        not in ("0", "false", "False"),
        concurrency=int(
            os.getenv("CINDEX_BROWSER_CONCURRENCY", str(DEFAULT_CONCURRENCY)),
        ),
    )


def get_pool() -> BrowserPool:
    """Return the process-wide browser pool singleton.

    Lazily creates the pool if it has not been initialized by the lifespan
    (e.g. when a request arrives before startup completes, or in tests that
    skip the lifespan and override this dependency).
    """
    global _pool  # noqa: PLW0603 - singleton scoped to the process
    if _pool is None:
        _pool = create_pool()
    return _pool


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Start the browser pool on startup, close it on shutdown."""
    global _pool  # noqa: PLW0603 - singleton scoped to the process
    _pool = create_pool()
    try:
        await _pool.start()
        logger.info(
            "sidecar_ready",
            port=int(os.getenv("CINDEX_BROWSER_PORT", DEFAULT_PORT)),
        )
        yield
    finally:
        await _pool.close()
        _pool = None


app = FastAPI(
    title="cindex-browser-sidecar",
    version="0.1.0",
    description="Headless Chromium fetch sidecar for cindex HTML connectors.",
    lifespan=lifespan,
)


def _is_timeout_error(exc: BaseException) -> bool:
    """Return whether ``exc`` (or its cause chain) is a timeout-like error."""
    walker: BaseException | None = exc
    while walker is not None:
        if (
            type(walker).__name__ == "TimeoutError"
            or "Timeout" in type(walker).__name__
        ):
            return True
        walker = walker.__cause__ or walker.__context__
    return False


@app.get("/healthz", summary="Liveness probe")
async def healthz() -> dict[str, str]:
    """Return ``{"status": "ok"}`` for container health checks."""
    return {"status": "ok"}


@app.post(
    "/fetch",
    response_model=FetchResponse,
    summary="Fetch a URL through the stealth browser context",
)
async def fetch(payload: FetchRequest) -> FetchResponse:
    """Forward a fetch request to the browser pool and return the upstream body."""
    pool = get_pool()
    log = logger.bind(method=payload.method, url=str(payload.url))
    try:
        response = await asyncio.wait_for(
            pool.fetch(payload),
            timeout=max(payload.timeout, 1.0) + 10,
        )
    except TimeoutError as exc:
        log.warning("fetch_timeout")
        msg = f"fetch timed out for {payload.url}: {exc}"
        return JSONResponse(status_code=504, content={"detail": msg})
    except BrowserPoolError as exc:
        log.warning("fetch_failed", exc_info=exc)
        status = 504 if _is_timeout_error(exc) else 502
        return JSONResponse(status_code=status, content={"detail": str(exc)})
    log.info("fetch_ok", status=response.status, content_type=response.content_type)
    return response


@app.post(
    "/screenshot",
    response_model=FetchResponse,
    summary="Capture a full-page PNG screenshot through the stealth browser context",
)
async def screenshot(payload: ScreenshotRequest) -> FetchResponse:
    """Render ``url`` in the persistent context and return a full-page PNG.

    The PNG is returned inline as base64 (``encoding="base64"``) so the worker
    can decode it straight into the PERELMAN vision payload; nothing is written
    to a shared filesystem (the sidecar has none with the worker).
    """
    pool = get_pool()
    log = logger.bind(url=str(payload.url))
    try:
        png = await asyncio.wait_for(
            pool.screenshot(str(payload.url), timeout_seconds=payload.timeout),
            timeout=max(payload.timeout, 1.0) + 10,
        )
    except TimeoutError as exc:
        log.warning("screenshot_timeout")
        msg = f"screenshot timed out for {payload.url}: {exc}"
        return JSONResponse(status_code=504, content={"detail": msg})
    except BrowserPoolError as exc:
        log.warning("screenshot_failed", exc_info=exc)
        status = 504 if _is_timeout_error(exc) else 502
        return JSONResponse(status_code=status, content={"detail": str(exc)})
    body = base64.standard_b64encode(png).decode("ascii")
    log.info("screenshot_ok", bytes=len(png))
    return FetchResponse(
        status=200,
        body=body,
        content_type="image/png",
        encoding="base64",
    )


def main() -> None:
    """Run the sidecar with uvicorn (entry point for ``uv run`` / container)."""
    port = int(os.getenv("CINDEX_BROWSER_PORT", DEFAULT_PORT))
    uvicorn.run(
        "cindex_browser_sidecar.main:app",
        host="0.0.0.0",  # noqa: S104 - container must bind all interfaces
        port=port,
        proxy_headers=True,
        forwarded_allow_ips="*",
        log_level="info",
    )


if __name__ == "__main__":
    main()
