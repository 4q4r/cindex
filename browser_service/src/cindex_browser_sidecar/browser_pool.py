"""Browser pool — a single persistent cloakbrowser context.

Owns one stealth Chromium context (cloakbrowser ``launch_persistent_context_async``)
and serves concurrent fetch requests by opening disposable pages on the shared
context. The persistent profile keeps challenge cookies (BunnyCDN Shield,
Cloudflare Turnstile) warm across requests so the second request to a given
source reaches the real resource instead of the interstitial challenge page.

Why re-fetch via ``page.evaluate(fetch)`` instead of ``page.goto``'s returned
response: when a CDN serves a JS challenge page that solves itself and then
reloads, ``page.goto`` resolves with the *first* response (the challenge HTML),
not the post-reload resource. Re-fetching the URL from inside the page — after
``goto`` has solved the challenge and set the cookie — returns the raw server
body for every content type (HTML, XML, RSS, JSON) without DOM serialization.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import TYPE_CHECKING
from urllib.parse import urlencode, urlsplit

import structlog
from cloakbrowser import launch_persistent_context_async

from cindex_browser_sidecar.models import FetchRequest, FetchResponse

if TYPE_CHECKING:
    from collections.abc import Mapping

logger = structlog.get_logger(__name__)

DEFAULT_PROFILE_DIR = "/data/cloak-profile"
DEFAULT_CONCURRENCY = 4
DEFAULT_HEADLESS = True
# Cap on the best-effort navigation wait. JS challenges (BunnyCDN Shield,
# Cloudflare Turnstile) resolve in a few seconds; capping avoids burning the
# whole request budget on endpoints that never reach networkidle (OAI streams,
# long-lived analytics connections) before the in-page fetch even runs.
_NAV_MAX_WAIT_SECONDS = 15.0


class BrowserPoolError(Exception):
    """Raised when the browser pool cannot fulfil a fetch request."""


class BrowserPool:
    """Manages a single persistent Chromium context for fetch requests.

    The context is created lazily on first ``fetch`` (or via an explicit
    ``start``) and reused for the lifetime of the process. Concurrent fetches
    open disposable pages on the shared context, bounded by a semaphore so a
    burst of requests does not exhaust browser resources.
    """

    def __init__(
        self,
        *,
        profile_dir: str | os.PathLike[str] | None = None,
        headless: bool = DEFAULT_HEADLESS,
        concurrency: int = DEFAULT_CONCURRENCY,
    ) -> None:
        """Configure the pool with a profile dir, headless flag, and concurrency.

        Args:
            profile_dir: Persistent browser profile directory. Defaults to
                ``CINDEX_PROFILE_DIR`` or ``/data/cloak-profile``.
            headless: Run Chromium headless.
            concurrency: Maximum number of concurrent in-flight fetches.

        """
        self._profile_dir = os.fspath(
            profile_dir or os.getenv("CINDEX_PROFILE_DIR", DEFAULT_PROFILE_DIR),
        )
        self._headless = headless
        self._context: object | None = None
        self._start_lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(max(1, concurrency))

    async def start(self) -> None:
        """Launch the persistent browser context if it is not yet running.

        Idempotent and safe to call concurrently — the first caller performs
        the launch, subsequent callers wait on the lock and observe the
        already-started context.
        """
        if self._context is not None:
            return
        async with self._start_lock:
            if self._context is not None:
                return
            profile = self._profile_dir
            logger.info(
                "browser_pool_starting",
                profile_dir=profile,
                headless=self._headless,
            )
            try:
                self._context = await launch_persistent_context_async(
                    user_data_dir=profile,
                    headless=self._headless,
                    humanize=True,
                    human_preset="careful",
                )
            except OSError as exc:
                msg = f"failed to launch cloakbrowser context: {exc}"
                raise BrowserPoolError(msg) from exc
            logger.info("browser_pool_started", profile_dir=profile)

    async def close(self) -> None:
        """Close the persistent context and release browser resources.

        Safe to call multiple times; a no-op once the context is closed.
        """
        context = self._context
        self._context = None
        if context is None:
            return
        try:
            await context.close()  # type: ignore[attr-defined]
        except OSError as exc:  # pragma: no cover - shutdown race
            logger.warning("browser_pool_close_failed", exc_info=exc)
        logger.info("browser_pool_closed")

    async def fetch(self, request: FetchRequest) -> FetchResponse:
        """Fetch a single URL via the shared browser context.

        Args:
            request: The fetch request (URL, method, params, headers, body).

        Returns:
            The upstream response (status, body, content-type).

        Raises:
            BrowserPoolError: If the browser cannot be started, the page
                navigation fails, or the in-page fetch returns no result.

        """
        await self.start()
        if self._context is None:  # pragma: no cover - defensive
            msg = "browser context is not available"
            raise BrowserPoolError(msg)

        async with self._semaphore:
            page = await self._context.new_page()  # type: ignore[attr-defined]
            try:
                if request.method == "POST":
                    return await self._post(page, request)
                return await self._get(page, request)
            finally:
                try:
                    await page.close()  # type: ignore[attr-defined]
                except OSError as exc:  # pragma: no cover - cleanup race
                    logger.warning("page_close_failed", exc_info=exc)

    async def _get(self, page: object, request: FetchRequest) -> FetchResponse:
        """Fetch a GET resource via in-page fetch after solving any challenge."""
        url = self._build_url(str(request.url), request.params)
        remaining = await self._navigate(page, url, request.timeout)
        headers = self._merge_accept(request.headers, request.accept)
        result = await self._evaluate_fetch(
            page,
            _with_helper(_GET_FETCH_SCRIPT),
            [url, headers],
            url,
            remaining,
        )
        return self._result_to_response(str(request.url), result)

    async def _post(self, page: object, request: FetchRequest) -> FetchResponse:
        """Fetch a POST resource (JSON or form) via in-page fetch."""
        url = self._build_url(str(request.url), request.params)
        remaining = await self._navigate(page, self._origin(url), request.timeout)
        headers = self._merge_accept(request.headers, request.accept)
        if request.json_body is not None:
            result = await self._evaluate_fetch(
                page,
                _with_helper(_POST_JSON_FETCH_SCRIPT),
                [url, headers, request.json_body],
                url,
                remaining,
            )
        elif request.data is not None:
            result = await self._evaluate_fetch(
                page,
                _with_helper(_POST_FORM_FETCH_SCRIPT),
                [url, headers, request.data],
                url,
                remaining,
            )
        else:
            msg = "POST request requires either json or data body"
            raise BrowserPoolError(msg)
        return self._result_to_response(request.url, result)

    async def _evaluate_fetch(
        self,
        page: object,
        script: str,
        arg: object,
        url: str,
        timeout_seconds: float,
    ) -> dict[str, object]:
        """Run an in-page fetch script bounded by the remaining request budget.

        ``page.evaluate`` has no default deadline; without one a server that
        never responds would hold a concurrency slot forever. ``asyncio.wait_for``
        cancels the evaluate and frees the page (closed by the caller's
        ``finally``) so the pool cannot deadlock on a hung fetch.
        """
        try:
            result = await asyncio.wait_for(
                page.evaluate(script, arg),  # type: ignore[attr-defined]
                timeout=max(1.0, timeout_seconds),
            )
        except TimeoutError as exc:
            msg = f"in-page fetch timed out for {url} after {timeout_seconds:.1f}s"
            raise BrowserPoolError(msg) from exc
        except Exception as exc:
            msg = f"in-page fetch failed for {url}: {exc}"
            raise BrowserPoolError(msg) from exc
        if not isinstance(result, dict):
            msg = f"in-page fetch returned no result for {url}"
            raise BrowserPoolError(msg)
        return result

    async def _navigate(
        self,
        page: object,
        url: str,
        timeout_seconds: float,
    ) -> float:
        """Navigate the page to ``url`` to warm challenge cookies (best-effort).

        The navigation solves JS challenges (BunnyCDN Shield, Cloudflare
        Turnstile) and sets the cookies the subsequent in-page ``fetch`` reuses.
        It is intentionally best-effort: many endpoints never reach
        ``networkidle`` (OAI streams, long-lived analytics connections) and PDF
        URLs trigger a download that ``page.goto`` refuses — both raise, but the
        in-page ``fetch`` still returns the real body, so we log and continue
        rather than aborting the whole request.

        Returns the remaining request budget (seconds) for the in-page fetch so
        the caller can bound ``page.evaluate`` and avoid overrunning the
        worker's HTTP timeout to the sidecar.
        """
        start = time.monotonic()
        nav_timeout = min(float(timeout_seconds), _NAV_MAX_WAIT_SECONDS)
        try:
            await page.goto(  # type: ignore[attr-defined]
                url,
                wait_until="networkidle",
                timeout=int(nav_timeout * 1000),
            )
        except Exception as exc:  # noqa: BLE001 - best-effort navigation
            logger.warning(
                "navigation_best_effort_failed",
                url=url,
                exc_info=str(exc),
            )
            await self._ensure_same_origin(page, url, nav_timeout)
        elapsed = time.monotonic() - start
        return max(1.0, float(timeout_seconds) - elapsed)

    async def _ensure_same_origin(
        self,
        page: object,
        url: str,
        nav_timeout: float,
    ) -> None:
        """Land the page on the target origin if it isn't already.

        A failed ``page.goto`` can leave the page on ``about:blank`` — e.g. a
        PDF URL triggers a download that ``goto`` refuses before any navigation
        commits. The in-page ``fetch`` issued afterwards would then be
        cross-origin and CORS-blocked ("Failed to fetch"). Navigating to the
        origin makes the fetch same-origin; challenge cookies are already held
        by the persistent context, so we only need a short ``networkidle`` wait.
        The OAI timeout case is skipped because the page did land on the target
        URL (only ``networkidle`` never fired).
        """
        try:
            current = page.url  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - page.url should never raise
            current = ""
        if current and self._same_origin(current, url):
            return
        origin = self._origin(url)
        try:
            await page.goto(  # type: ignore[attr-defined]
                origin,
                wait_until="networkidle",
                timeout=int(min(nav_timeout, 8.0) * 1000),
            )
        except Exception as exc:  # noqa: BLE001 - best-effort origin landing
            logger.warning(
                "navigation_origin_fallback_failed",
                url=origin,
                exc_info=str(exc),
            )

    @staticmethod
    def _same_origin(a: str, b: str) -> bool:
        """Return ``True`` when two URLs share scheme and host."""
        pa, pb = urlsplit(a), urlsplit(b)
        return pa.scheme == pb.scheme and pa.netloc == pb.netloc

    @staticmethod
    def _build_url(url: str, params: Mapping[str, str] | None) -> str:
        """Append query params to ``url`` if present."""
        if not params:
            return url
        separator = "&" if "?" in url else "?"
        return f"{url}{separator}{urlencode(params)}"

    @staticmethod
    def _origin(url: str) -> str:
        """Return ``scheme://host`` for ``url``."""
        parts = urlsplit(url)
        if not parts.scheme or not parts.netloc:
            msg = f"cannot derive origin from invalid url: {url}"
            raise BrowserPoolError(msg)
        return f"{parts.scheme}://{parts.netloc}"

    @staticmethod
    def _merge_accept(
        headers: Mapping[str, str] | None,
        accept: str | None,
    ) -> dict[str, str]:
        """Combine caller headers with an optional Accept header."""
        merged: dict[str, str] = dict(headers or {})
        if accept and "accept" not in {k.lower() for k in merged}:
            merged["Accept"] = accept
        return merged

    @staticmethod
    def _result_to_response(url: str, result: object) -> FetchResponse:
        """Convert the in-page fetch result object to a ``FetchResponse``."""
        if not isinstance(result, dict):
            msg = f"in-page fetch returned no result for {url}"
            raise BrowserPoolError(msg)
        status = result.get("status")
        body = result.get("body")
        content_type = result.get("contentType") or ""
        encoding = result.get("encoding") or "text"
        if status is None or body is None:
            msg = f"in-page fetch returned incomplete result for {url}: {result!r}"
            raise BrowserPoolError(msg)
        return FetchResponse(
            status=int(status),
            body=str(body),
            content_type=str(content_type),
            encoding=str(encoding),
        )


_GET_FETCH_SCRIPT = """
async ([url, headers]) => {
  const opts = {method: 'GET', headers: headers || {}, credentials: 'include'};
  const r = await fetch(url, opts);
  return __toResult(r);
}
"""

_POST_JSON_FETCH_SCRIPT = """
async ([url, headers, payload]) => {
  const hdrs = Object.assign({'Content-Type': 'application/json'}, headers || {});
  const body = JSON.stringify(payload);
  const opts = {method: 'POST', headers: hdrs, body, credentials: 'include'};
  const r = await fetch(url, opts);
  return __toResult(r);
}
"""

_POST_FORM_FETCH_SCRIPT = """
async ([url, headers, data]) => {
  const params = new URLSearchParams(data || {});
  const hdrs = Object.assign(
    {'Content-Type': 'application/x-www-form-urlencoded'},
    headers || {},
  );
  const formBody = params.toString();
  const opts = {method: 'POST', headers: hdrs, body: formBody, credentials: 'include'};
  const r = await fetch(url, opts);
  return __toResult(r);
}
"""

# Shared helper injected into every fetch script. Decides text vs binary by
# content-type so PDFs (and other binary payloads) are returned as base64
# bytes (preserving them for the worker's PDF parser) while HTML/XML/JSON/RSS
# are returned as browser-decoded text.
#
# Charset handling: ``Response.text()`` does NOT always honour a legacy
# single-byte charset declared in the ``Content-Type`` header (e.g. MathNet
# serves ``text/html; charset=Windows-1251`` but ``r.text()`` decodes the
# Cyrillic bytes as UTF-8, producing 156 U+FFFD replacement characters on a
# typical article page). We therefore read the raw ``arrayBuffer()`` and decode
# it ourselves with a ``TextDecoder`` configured from, in order of preference:
#   1. the ``charset=`` parameter of the Content-Type header,
#   2. a ``<meta charset>`` tag in the first 1 KiB of the body,
#   3. UTF-8 as the final fallback.
# WHATWG ``TextDecoder`` supports the legacy single-byte labels we need
# (``windows-1251``, ``koi8-r``, ``iso-8859-N`` mapped to ``windows-125N``);
# an unknown label throws ``RangeError`` and we fall back to UTF-8.
_FETCH_HELPER = """
async function __toResult(r) {
  const ct = (r.headers.get('content-type') || '').toLowerCase();
  const isText = ct === ''
    || ct.startsWith('text/')
    || ct.includes('json')
    || ct.includes('xml')
    || ct.includes('rss')
    || ct.includes('atom')
    || ct.includes('html')
    || ct.includes('javascript')
    || ct.includes('svg')
    || ct.includes('yaml');
  if (isText) {
    const buf = new Uint8Array(await r.arrayBuffer());
    let charset = '';
    const ctMatch = ct.match(/charset=([^\\s;]+)/i);
    if (ctMatch) charset = ctMatch[1].toLowerCase();
    if (!charset) {
      const head = new TextDecoder('utf-8').decode(buf.slice(0, 1024));
      const metaMatch = head.match(/<meta[^>]+charset=["'?\\s]*([\\w-]+)/i);
      if (metaMatch) charset = metaMatch[1].toLowerCase();
    }
    let body;
    try {
      body = new TextDecoder(charset || 'utf-8').decode(buf);
    } catch (err) {
      body = new TextDecoder('utf-8').decode(buf);
    }
    return {
      status: r.status,
      body,
      contentType: ct,
      encoding: 'text',
    };
  }
  const buf = new Uint8Array(await r.arrayBuffer());
  let binary = '';
  const chunk = 0x8000;
  for (let i = 0; i < buf.length; i += chunk) {
    binary += String.fromCharCode.apply(null, buf.subarray(i, i + chunk));
  }
  return {
    status: r.status,
    body: btoa(binary),
    contentType: ct,
    encoding: 'base64',
  };
}
"""


def _with_helper(script: str) -> str:
    """Inject the shared ``__toResult`` helper into a fetch arrow function.

    Each script is an ``async ([...]) => { ... }`` arrow expression. The helper
    is declared at the top of the arrow body so ``__toResult`` is in scope for
    the ``return __toResult(r)`` call. Inlining (rather than concatenating)
    keeps the whole string a single function expression that Playwright's
    ``page.evaluate`` accepts and invokes with the argument array.
    """
    head, sep, tail = script.partition(") => {")
    if not sep:
        msg = "fetch script is not an arrow function with a block body"
        raise BrowserPoolError(msg)
    return f"{head}) => {{ {_FETCH_HELPER}{tail}"
