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


class LLMRateLimitedError(ConnectorFetchError):
    """HTTP 429 from the upstream — retryable with backoff.

    Rate limits (Z.AI code 1302 concurrency / 1303 frequency) are transient:
    the request is rejected not because it is malformed but because the
    account momentarily exceeded its concurrency/frequency budget. Unlike a
    400/500, retrying after a short wait is the correct response. Carries the
    provider's ``Retry-After`` hint (seconds) when present so the caller can
    honour it instead of guessing.
    """

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        """Store the upstream message and optional ``Retry-After`` (seconds)."""
        super().__init__(message)
        self.retry_after = retry_after


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

        ``LLMRateLimitedError`` (429) backs off honouring ``Retry-After`` or an
        exponential base; ``aiohttp.ClientError`` / ``OSError`` backs off with a
        linear delay. Any other :class:`ConnectorFetchError` is terminal (non-429
        HTTP error / invalid JSON) and propagates immediately.
        """
        last_error: Exception | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                return await self._post_json(url, headers, body, timeout)
            except LLMRateLimitedError as exc:
                # 429 is transient (concurrency/frequency cap). Back off and
                # retry the SAME request so a single rate-limit blip does not
                # zero out an article's quotes. Honour ``Retry-After`` when
                # the provider sends it, else exponential backoff.
                last_error = exc
                if attempt < _MAX_ATTEMPTS:
                    delay = (
                        exc.retry_after
                        if exc.retry_after is not None
                        else _RATE_LIMIT_BACKOFF_BASE * attempt
                    )
                    await asyncio.sleep(delay)
                    continue
                raise
            except ConnectorFetchError:
                # Terminal (non-429 HTTP error / invalid JSON) — stop the loop.
                raise
            except (aiohttp.ClientError, OSError) as exc:
                last_error = exc
                if attempt < _MAX_ATTEMPTS:
                    await asyncio.sleep(0.6 * attempt)
                    continue
                msg = f"llm: transient failure after {attempt} attempts: {exc}"
                raise ConnectorFetchError(msg) from exc
        # Unreachable: the loop either returns or raises on every path.
        if last_error is not None:  # pragma: no cover - defensive
            raise ConnectorFetchError(str(last_error))
        msg = "llm: retry loop exited without a result"
        raise ConnectorFetchError(msg)  # pragma: no cover - defensive

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
                # Rate limit / concurrency cap (Z.AI code 1302/1303) — transient.
                # Retry with backoff rather than failing the whole extraction
                # turn: a single 429 must not zero out an article's quotes.
                text = await response.text()
                retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                msg = f"llm: HTTP 429: {text[:200]}"
                raise LLMRateLimitedError(msg, retry_after=retry_after)
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
