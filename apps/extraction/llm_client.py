"""Thin OpenAI-compatible chat-completions client on aiohttp (no httpx).

The PERELMAN extractor drives vision-capable LLMs through this single
transport. It mirrors the canonical Exa connector pattern
(``_cs_post_json`` / ``_fetch_single_lang`` in
``apps/ingestion/connectors/api_connectors.py``): one ``aiohttp.ClientSession``
per request with ``trust_env=True`` (so the configured HTTPS proxy is honoured
and the AST guard ``test_aiohttp_trust_env.py`` passes), a bounded retry loop
that treats ``ConnectorFetchError`` as terminal and retries transient
``aiohttp.ClientError`` / ``OSError``, and a loud ``LLMNotConfiguredError``
when the API key is missing — never a fake fallback.

A client-side request-frequency gate (``cfg.min_request_interval``) spaces
successive request starts by at least that many seconds on a monotonic clock,
so providers that throttle by QPS on top of concurrency (e.g. Z.AI's free
tier, ~1 QPS) are not overrun. 0.0 disables it.

The client is content-agnostic: ``messages`` are passed through verbatim, so
the caller builds multimodal content (``{"type": "text"}`` +
``{"type": "image_url", "image_url": {"url": "data:...;base64,...", "detail": ...}}``
parts) and the client never introspects them. ``tools`` / ``tool_choice`` are
forwarded for the agentic zoom/crop/rotate loop, and the **full assistant
``message`` dict** (``content`` + ``tool_calls``) is returned so the caller can
both dispatch tool calls and parse the final JSON payload.
"""

from __future__ import annotations

import asyncio
import json
import random
import time

import aiohttp
import structlog

from apps.ingestion.connectors.base import ConnectorFetchError

from .config import LLMConfig, LLMNotConfiguredError

logger = structlog.get_logger(__name__)

# Mirrors ``BaseConnector.MAX_ATTEMPTS`` / ``HTTP_ERROR_THRESHOLD`` in
# ``apps/ingestion/connectors/base.py`` — kept local to avoid a runtime
# dependency on the connector class hierarchy.
_MAX_ATTEMPTS = 3
_HTTP_ERROR_THRESHOLD = 400
_HTTP_RATE_LIMITED = 429
# Backoff base (seconds) for 429 retries when the provider sends no
# ``Retry-After`` hint. Z.AI's free tier returns 1302 for concurrency/rate
# spikes that typically clear within a couple of seconds, so a short
# exponential (2s, 4s, ...) is enough without burning the 5-hour quota window.
_RATE_LIMIT_BACKOFF_BASE = 2.0

# Z.AI (Zhipu) error codes returned in the 429 body's ``error.code`` field
# (verified via Exa against docs.z.ai/api-reference/api-code and corroborated
# by community retry implementations: neokai #2183, nanobot #3356).
#
# Transient codes clear in seconds and ARE retried with code-specific backoff:
#   1302 — concurrency cap (reduce parallelism; clears in ~1-2s)
#   1303 — request frequency cap (slow down; clears in ~1s)
#   1305 — server-side overload («service may be temporarily overloaded»);
#          NOT a per-account spike — the whole route is throttled, so it
#          clears more slowly and gets a longer backoff + more attempts.
#
# Terminal codes are quota / window / plan exhaustion and are NOT retried
# (retrying is futile and burns the window):
#   1304 — daily usage limit exceeded
#   1308 — usage-window limit (resets at next_flush_time, e.g. 5h)
#   1309 — Coding Plan package expired
#   1310 — insufficient balance
_ZAI_TERMINAL_CODES = frozenset({"1304", "1308", "1309", "1310"})
_ZAI_OVERLOAD_CODE = "1305"

# 1305 (server overload) gets a longer backoff base and more attempts than
# 1302/1303: server-side overload clears in tens of seconds, not 1-2s. Exa
# best practice (toolsswift GLM-1302 guide, OpenAI/Microsoft throttling docs):
# exponential backoff with jitter, honour ``Retry-After``, cap max retries —
# do NOT hammer the endpoint with instant retries (aggressive retries can dig
# the hole deeper: failed requests still count against the limit).
_OVERLOAD_BACKOFF_BASE = 4.0
_MAX_OVERLOAD_ATTEMPTS = 5
_OVERLOAD_BACKOFF_CAP = 30.0
# ±25% jitter on the locally-computed exponential backoff (NOT on
# ``Retry-After``, which is honoured exactly). With concurrency=1 there is no
# herd to spread, so jitter only prevents a deterministic retry cadence
# aligning with the provider's accounting window.
_JITTER_FRACTION = 0.25


class LLMRateLimitedError(ConnectorFetchError):
    """HTTP 429 from the upstream — retryable with backoff unless terminal.

    Rate limits (Z.AI code 1302 concurrency / 1303 frequency / 1305 server
    overload) are transient: the request is rejected not because it is
    malformed but because the account/route momentarily exceeded its budget.
    Unlike a 400/500, retrying after a wait is the correct response. Carries
    the parsed Z.AI ``error.code`` (so the retry loop can pick a code-specific
    backoff), the provider's ``Retry-After`` hint (seconds) when present, and
    a ``terminal`` flag — quota / window / plan-exhaustion codes (1304/1308/
    1309/1310) set ``terminal=True`` so the loop does NOT waste attempts on a
    limit no retry can lift.
    """

    def __init__(
        self,
        message: str,
        *,
        retry_after: float | None = None,
        code: str | None = None,
        terminal: bool = False,
    ) -> None:
        """Store the upstream message, code, Retry-After, and terminal flag."""
        super().__init__(message)
        self.retry_after = retry_after
        self.code = code
        self.terminal = terminal


def _parse_retry_after(header: str | None) -> float | None:
    """Parse an HTTP ``Retry-After`` header as seconds, or ``None``.

    Only the delta-seconds form is handled; the HTTP-date form is intentionally
    not parsed (parsing it robustly needs an HTTP-date parser we do not have
    on hand) — the caller falls back to exponential backoff instead of
    guessing.
    """
    if not header:
        return None
    try:
        return max(0.0, float(header))
    except ValueError:
        return None


def _parse_zai_error_code(text: str) -> str | None:
    """Best-effort parse of the Z.AI ``error.code`` from a 429 body, or ``None``.

    Z.AI returns ``{"error":{"code":"1305","message":"..."}}``. The code drives
    the retry decision (transient vs terminal + backoff base), so it is parsed
    here. Non-JSON bodies or missing ``error.code`` yield ``None`` (the caller
    treats an unknown 429 as transient with the default backoff — the safe
    default for an unrecognized rate limit).
    """
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    err = data.get("error")
    if not isinstance(err, dict):
        return None
    code = err.get("code")
    return str(code) if code is not None else None


def _apply_jitter(delay: float) -> float:
    """Add ±``_JITTER_FRACTION`` jitter to a computed backoff delay.

    Returns 0.0 for a non-positive input. ``Retry-After`` is honoured exactly
    and is NOT jittered (the server's hint is the fastest recovery path per
    Exa/Microsoft throttling guidance); only the locally-computed exponential
    backoff is jittered.
    """
    if delay <= 0.0:
        return 0.0
    # Jitter for retry backoff is NOT security-sensitive (no secrets, no
    # tokens) — the stdlib PRNG is correct here. ``secrets`` would be overkill.
    return delay * (1.0 + random.uniform(-_JITTER_FRACTION, _JITTER_FRACTION))  # noqa: S311


class OpenAICompatibleClient:
    """Minimal async client for an OpenAI-compatible ``/chat/completions`` API.

    Constructed with a resolved :class:`~apps.extraction.config.LLMConfig`.
    A missing API key fails loudly in ``__init__`` (matching the Exa
    ``_api_key`` guard). No ``User-Agent`` override is applied — aiohttp's
    library default is sent as-is (project rule: never set a custom cindex UA).
    """

    def __init__(self, cfg: LLMConfig) -> None:
        """Store the resolved config, failing loudly on a missing API key.

        A client-side request-frequency gate is initialized from
        ``cfg.min_request_interval``: successive ``chat`` calls are spaced at
        least that many seconds apart (measured between request starts, on a
        monotonic clock). The gate is shared across every concurrent caller
        that holds this client (the PERELMAN batch uses one client), so it
        enforces the provider's QPS cap on top of the upstream concurrency
        semaphore. 0.0 disables it.
        """
        if not cfg.api_key:
            msg = "CINDEX_LLM_API_KEY is required"
            raise LLMNotConfiguredError(msg)
        self._cfg = cfg
        self._rate_lock = asyncio.Lock()
        self._last_request = 0.0
        self._min_interval = max(0.0, cfg.min_request_interval)

    async def chat(  # noqa: PLR0913  # OpenAI passthrough surface is inherently wide
        self,
        messages: list[dict],
        *,
        temperature: float | None = None,
        response_format: dict | None = None,
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
        extra_body_override: dict | None = None,
    ) -> dict:
        """Send one chat-completions request and return the assistant message.

        Parameters mirror the OpenAI chat-completions body. ``messages`` are
        forwarded verbatim (multimodal content parts included).
        ``response_format`` / ``tools`` / ``tool_choice`` are sent only when
        provided. ``cfg.extra_body`` is merged into every request, then
        ``extra_body_override`` is merged last (override wins) so a caller can
        inject a per-call extension without mutating the shared config.

        Returns the full ``data["choices"][0]["message"]`` dict (``content``
        string or ``None`` plus an optional ``tool_calls`` list). Raises
        :class:`ConnectorFetchError` on upstream HTTP errors or invalid JSON,
        and retries transient network/timeout errors up to ``_MAX_ATTEMPTS``.
        """
        cfg = self._cfg
        body: dict = {
            "model": cfg.model,
            "messages": messages,
            "temperature": temperature if temperature is not None else cfg.temperature,
        }
        if response_format is not None:
            body["response_format"] = response_format
        if tools is not None:
            body["tools"] = tools
        if tool_choice is not None:
            body["tool_choice"] = tool_choice
        body.update(cfg.extra_body)
        if extra_body_override:
            body.update(extra_body_override)

        headers = {
            "Authorization": f"Bearer {cfg.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        url = f"{cfg.base_url.rstrip('/')}/chat/completions"

        await self._enforce_rate_limit()

        return await self._request_with_retry(url, headers, body, cfg.timeout)

    async def _request_with_retry(
        self,
        url: str,
        headers: dict[str, str],
        body: dict,
        timeout: float,  # noqa: ASYNC109  # upstream request timeout
    ) -> dict:
        """POST with bounded retry on transient (429 / network) failures.

        The retry budget is per error code, so a longer-running transient
        condition does not collapse into the short 1302/1303 budget:

        * Z.AI 1305 (server overload) — up to ``_MAX_OVERLOAD_ATTEMPTS`` with a
          longer exponential backoff (base 4s, capped at 30s) + jitter. The
          whole route is throttled, so it clears in tens of seconds, not 1-2s.
        * Z.AI 1302 / 1303 (concurrency / frequency) and any unrecognized 429
          — up to ``_MAX_ATTEMPTS`` with the short backoff (base 2s).
        * Z.AI terminal codes (1304/1308/1309/1310 — quota / window / plan
          exhaustion) — NOT retried: ``terminal=True`` propagates immediately.
          Retrying is futile and burns the window.
        * ``aiohttp.ClientError`` / ``OSError`` — up to ``_MAX_ATTEMPTS`` with a
          linear delay.

        ``Retry-After`` is honoured exactly (no jitter); the locally-computed
        exponential backoff is jittered. Any other
        :class:`ConnectorFetchError` (non-429 HTTP error / invalid JSON) is
        terminal and propagates immediately.
        """
        attempt = 0
        while True:
            attempt += 1
            try:
                return await self._post_json(url, headers, body, timeout)
            except LLMRateLimitedError as exc:
                # Terminal quota / window / plan exhaustion — never retry.
                if exc.terminal:
                    raise
                # 429 is transient (concurrency/frequency/overload cap). Back
                # off and retry the SAME request so a single rate-limit blip
                # does not zero out an article's quotes. Server overload (1305)
                # gets a longer backoff + more attempts than 1302/1303.
                max_attempts = (
                    _MAX_OVERLOAD_ATTEMPTS
                    if exc.code == _ZAI_OVERLOAD_CODE
                    else _MAX_ATTEMPTS
                )
                if attempt >= max_attempts:
                    raise
                if exc.retry_after is not None:
                    # Server's hint is the fastest recovery path — honour it
                    # exactly, no jitter.
                    delay = exc.retry_after
                else:
                    base = (
                        _OVERLOAD_BACKOFF_BASE
                        if exc.code == _ZAI_OVERLOAD_CODE
                        else _RATE_LIMIT_BACKOFF_BASE
                    )
                    delay = _apply_jitter(
                        min(base * (2 ** (attempt - 1)), _OVERLOAD_BACKOFF_CAP),
                    )
                await asyncio.sleep(delay)
            except ConnectorFetchError:
                # Terminal (non-429 HTTP error / invalid JSON) — stop the loop.
                raise
            except (aiohttp.ClientError, OSError) as exc:
                if attempt >= _MAX_ATTEMPTS:
                    msg = f"llm: transient failure after {attempt} attempts: {exc}"
                    raise ConnectorFetchError(msg) from exc
                await asyncio.sleep(0.6 * attempt)

    async def _enforce_rate_limit(self) -> None:
        """Space successive request starts by at least ``_min_interval`` seconds.

        Holds the rate lock only long enough to measure the gap, sleep the
        remaining time, and stamp the new request start — the lock is released
        before the network call, so a concurrent caller may begin its own wait
        while this one is still in-flight. The monotonic clock avoids wall-clock
        jumps. No-op when ``_min_interval`` is 0.
        """
        if self._min_interval <= 0.0:
            return
        async with self._rate_lock:
            now = time.monotonic()
            wait = self._min_interval - (now - self._last_request)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request = time.monotonic()

    async def _post_json(
        self,
        url: str,
        headers: dict[str, str],
        body: dict,
        timeout: float,  # noqa: ASYNC109  # upstream request timeout
    ) -> dict:
        """POST ``body`` and return ``choices[0].message``.

        ``ClientSession(trust_env=True)`` makes aiohttp honour ``https_proxy``
        / ``HTTPS_PROXY`` (ignored by default) so the request routes through
        the configured proxy. No custom ``User-Agent`` is set. Network and
        timeout errors propagate unwrapped for the retry loop; an upstream
        ``status >= _HTTP_ERROR_THRESHOLD`` or invalid JSON / missing
        ``choices`` raises :class:`ConnectorFetchError` (terminal).
        """
        async with (
            aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=timeout),
                trust_env=True,
            ) as session,
            session.post(url, headers=headers, json=body) as response,
        ):
            if response.status == _HTTP_RATE_LIMITED:
                # Rate limit / concurrency / overload (Z.AI codes 1302/1303/
                # 1305) — transient. Retry with backoff rather than failing
                # the whole extraction turn: a single 429 must not zero out an
                # article's quotes. Parse the Z.AI ``error.code`` so the retry
                # loop can pick a code-specific backoff and refuse to retry
                # quota / window / plan-exhaustion codes (1304/1308/1309/
                # 1310) — those are terminal and retrying only burns the
                # window.
                text = await response.text()
                retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                code = _parse_zai_error_code(text)
                terminal = code in _ZAI_TERMINAL_CODES
                msg = f"llm: HTTP 429: {text[:200]}"
                raise LLMRateLimitedError(
                    msg,
                    retry_after=retry_after,
                    code=code,
                    terminal=terminal,
                )
            if response.status >= _HTTP_ERROR_THRESHOLD:
                text = await response.text()
                msg = f"llm: HTTP {response.status}: {text[:200]}"
                raise ConnectorFetchError(msg)
            text = await response.text()
        try:
            data = json.loads(text)
        except ValueError as exc:
            msg = f"llm: invalid JSON response: {exc}"
            raise ConnectorFetchError(msg) from exc
        if not isinstance(data, dict):
            msg = "llm: invalid JSON payload type"
            raise ConnectorFetchError(msg)
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            msg = "llm: response missing choices"
            raise ConnectorFetchError(msg)
        message = choices[0].get("message")
        if not isinstance(message, dict):
            msg = "llm: response missing assistant message"
            raise ConnectorFetchError(msg)
        return message
