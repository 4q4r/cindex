"""Regression tests for ``AsyncApiConnector._fetch_async`` retry/timeout.

Root cause of failed search job cd5e8175 (2026-07-26): aiohttp's
``ClientTimeout`` timer raises ``asyncio.TimeoutError`` (the builtin
``TimeoutError``, an ``OSError`` subclass), which is NOT a subclass of
``aiohttp.ClientError``. The old ``_fetch_async`` only caught
``ClientResponseError`` and ``ClientError``, so a single slow upstream
API let the ``TimeoutError`` bubble past ``_process_single_source`` and
abort the whole celery task with an empty error string.

These tests pin the canonical retry loop (mirroring
``ExaConnector._fetch_single_lang``): transient ``TimeoutError``/
``ClientError`` are retried with backoff; a terminal ``ConnectorFetchError``
(HTTP >= 400) stops the loop immediately; on exhaustion the failure is
surfaced as a per-source ``ConnectorFetchError`` (never as an uncaught
``TimeoutError``).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, Self

import aiohttp
import pytest

from apps.ingestion.connectors import ConnectorFetchError
from apps.ingestion.connectors.api_connectors import OpenAlexConnector

# A minimal valid OpenAlex payload: one work with a tiny inverted index.
_OK_PAYLOAD = {
    "results": [
        {
            "title": "Test article",
            "doi": "https://doi.org/10.1234/test",
            "publication_date": "2024-01-01",
            "abstract_inverted_index": {"test": [0], "abstract": [1]},
            "primary_location": {"landing_page_url": "https://example.org/x"},
            "authorships": [],
        },
    ],
}


class _FakeResponse:
    """aiohttp-like response context manager with scriptable behaviour."""

    def __init__(
        self,
        *,
        payload: dict | None = None,
        enter_exc: BaseException | None = None,
        json_exc: BaseException | None = None,
        raise_exc: BaseException | None = None,
    ) -> None:
        self._payload = payload
        self._enter_exc = enter_exc
        self._json_exc = json_exc
        self._raise_exc = raise_exc

    async def __aenter__(self) -> Self:
        if self._enter_exc is not None:
            raise self._enter_exc
        return self

    async def __aexit__(self, *args: object) -> None:
        pass

    def raise_for_status(self) -> None:
        if self._raise_exc is not None:
            raise self._raise_exc

    async def json(self) -> dict:
        if self._json_exc is not None:
            raise self._json_exc
        return self._payload  # type: ignore[return-value]


class _FakeSession:
    """aiohttp-like session that serves a scripted list of responses."""

    def __init__(self, responses: list[_FakeResponse]) -> None:
        self._responses = responses
        self.calls = 0
        self.last_kwargs: dict[str, Any] = {}

    def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.last_kwargs = kwargs
        if self.calls >= len(self._responses):
            msg = f"unexpected extra GET {self.calls + 1} to {url}"
            raise AssertionError(msg)
        resp = self._responses[self.calls]
        self.calls += 1
        return resp

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        pass


def _patch_session(
    monkeypatch: pytest.MonkeyPatch,
    responses: list[_FakeResponse],
) -> _FakeSession:
    """Replace ``aiohttp.ClientSession`` with a scripted fake, return it."""
    session = _FakeSession(responses)

    def _factory(*args: Any, **kwargs: Any) -> _FakeSession:
        # The production call must pass trust_env=True (AST-guard invariant).
        assert kwargs.get("trust_env") is True, (
            "aiohttp.ClientSession must be constructed with trust_env=True"
        )
        return session

    monkeypatch.setattr(aiohttp, "ClientSession", _factory)

    async def _no_sleep(*_a: object, **_kw: object) -> None:
        return None

    # Avoid real backoff sleeps in the retry loop.
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    return session


def _client_response_error(status: int) -> aiohttp.ClientResponseError:
    """Build a real ``ClientResponseError`` for the terminal-HTTP test."""
    return aiohttp.ClientResponseError(
        request_info=SimpleNamespace(real_url="https://api.openalex.org/works"),
        history=(),
        status=status,
        message=f"HTTP {status}",
    )


def test_timeout_then_success_retries_and_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient body-read timeout is retried; the next attempt succeeds."""
    session = _patch_session(
        monkeypatch,
        [
            _FakeResponse(json_exc=TimeoutError()),
            _FakeResponse(payload=_OK_PAYLOAD),
        ],
    )
    connector = OpenAlexConnector()

    items = asyncio.run(connector._fetch_async("machine learning", 1))

    assert session.calls == 2
    assert len(items) == 1
    assert items[0].doi == "10.1234/test"


def test_connect_timeout_exhausts_attempts_and_raises_connector_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Timeout on every attempt surfaces as ConnectorFetchError, not TimeoutError."""
    session = _patch_session(
        monkeypatch,
        [_FakeResponse(enter_exc=TimeoutError()) for _ in range(3)],
    )
    connector = OpenAlexConnector()

    with pytest.raises(ConnectorFetchError, match="request failed"):
        asyncio.run(connector._fetch_async("machine learning", 1))

    # All retries were used (MAX_ATTEMPTS == 3).
    assert session.calls == 3


def test_body_read_timeout_exhausts_attempts_and_raises_connector_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timeout while reading the JSON body is retried, then terminal-fails."""
    session = _patch_session(
        monkeypatch,
        [_FakeResponse(json_exc=TimeoutError()) for _ in range(3)],
    )
    connector = OpenAlexConnector()

    with pytest.raises(ConnectorFetchError, match="request failed"):
        asyncio.run(connector._fetch_async("machine learning", 1))

    assert session.calls == 3


def test_client_error_is_retried_like_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An aiohttp.ClientError (non-OS) is retried on the same loop as timeout."""
    session = _patch_session(
        monkeypatch,
        [
            _FakeResponse(json_exc=aiohttp.ClientPayloadError("partial")),
            _FakeResponse(payload=_OK_PAYLOAD),
        ],
    )
    connector = OpenAlexConnector()

    items = asyncio.run(connector._fetch_async("machine learning", 1))

    assert session.calls == 2
    assert len(items) == 1


def test_http_error_is_terminal_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An HTTP >= 400 ClientResponseError stops the loop immediately (no retry)."""
    session = _patch_session(
        monkeypatch,
        [_FakeResponse(raise_exc=_client_response_error(429))],
    )
    connector = OpenAlexConnector()

    with pytest.raises(ConnectorFetchError, match="HTTP 429"):
        asyncio.run(connector._fetch_async("machine learning", 1))

    # Terminal: exactly one attempt, no retries.
    assert session.calls == 1


def test_invalid_json_payload_type_is_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-dict JSON payload surfaces as ConnectorFetchError without retry."""
    session = _patch_session(
        monkeypatch,
        [
            _FakeResponse(payload=["not", "a", "dict"]),
            _FakeResponse(payload=_OK_PAYLOAD),
        ],
    )
    connector = OpenAlexConnector()

    with pytest.raises(ConnectorFetchError, match="invalid JSON payload type"):
        asyncio.run(connector._fetch_async("machine learning", 1))

    # The invalid payload short-circuits before any retry.
    assert session.calls == 1
