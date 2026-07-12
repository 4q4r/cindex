"""Unit tests for the ``POST /screenshot`` sidecar endpoint (no real Chromium).

The browser pool is replaced with a ``FakePool`` whose ``screenshot`` returns
scripted PNG bytes (or raises), so the tests run without launching Chromium.
``TestClient`` is used as a context manager so the lifespan starts (and shuts
down) the fake pool. Mirrors ``test_main.py`` but exercises the screenshot
route specifically.
"""

from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from cindex_browser_sidecar import main
from cindex_browser_sidecar.browser_pool import BrowserPoolError

# Minimal valid PNG header (8-byte signature) so the decode round-trip is
# distinguishable from arbitrary bytes — the route only base64-encodes whatever
# the pool returns, so a real PNG signature is enough to assert correctness.
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_PNG_BYTES = _PNG_SIGNATURE + b"\x00" * 32


class FakePool:
    """Stand-in for ``BrowserPool`` with a scripted ``screenshot`` result."""

    def __init__(self, screenshot_result: bytes | None = None, exc=None) -> None:
        self.screenshot_result = screenshot_result
        self.exc = exc
        self.started = False
        self.closed = False
        self.screenshot_calls: list[tuple[str, float]] = []

    async def start(self) -> None:
        self.started = True

    async def close(self) -> None:
        self.closed = True

    async def screenshot(self, url: str, *, timeout_seconds: float = 25.0) -> bytes:
        self.screenshot_calls.append((url, timeout_seconds))
        if self.exc is not None:
            raise self.exc
        if self.screenshot_result is None:
            msg = "no screenshot result configured"
            raise BrowserPoolError(msg)
        return self.screenshot_result


@pytest.fixture
def client(monkeypatch):
    fake = FakePool(screenshot_result=_PNG_BYTES)
    monkeypatch.setattr(main, "create_pool", lambda: fake)
    with TestClient(main.app) as c:
        yield c, fake


def test_screenshot_ok_returns_base64_png(client):
    c, fake = client
    r = c.post("/screenshot", json={"url": "https://example.com"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == 200
    assert body["content_type"] == "image/png"
    assert body["encoding"] == "base64"
    decoded = base64.b64decode(body["body"], validate=True)
    assert decoded == _PNG_BYTES
    assert decoded.startswith(_PNG_SIGNATURE)
    # Pydantic ``HttpUrl`` normalizes the bare host to ``https://example.com/``.
    assert fake.screenshot_calls == [("https://example.com/", 25.0)]


def test_screenshot_forwards_timeout_seconds(client):
    c, fake = client
    r = c.post(
        "/screenshot",
        json={"url": "https://example.com", "timeout": 40},
    )
    assert r.status_code == 200
    assert fake.screenshot_calls == [("https://example.com/", 40.0)]


def test_screenshot_invalid_url_returns_422(client):
    c, _ = client
    r = c.post("/screenshot", json={"url": "not-a-url"})
    assert r.status_code == 422


def test_screenshot_invalid_timeout_returns_422(client):
    c, _ = client
    r = c.post("/screenshot", json={"url": "https://example.com", "timeout": 0})
    assert r.status_code == 422


def test_screenshot_pool_error_returns_502(client):
    c, fake = client
    fake.exc = BrowserPoolError("screenshot failed")
    r = c.post("/screenshot", json={"url": "https://example.com"})
    assert r.status_code == 502
    assert "screenshot failed" in r.json()["detail"]


def test_screenshot_timeout_returns_504(client):
    c, fake = client
    fake.exc = TimeoutError("screenshot timed out")
    r = c.post("/screenshot", json={"url": "https://example.com"})
    assert r.status_code == 504


def test_screenshot_pool_error_with_timeout_cause_returns_504(client):
    c, fake = client
    exc = BrowserPoolError("screenshot failed")
    exc.__cause__ = TimeoutError("inner timeout")
    fake.exc = exc
    r = c.post("/screenshot", json={"url": "https://example.com"})
    assert r.status_code == 504


def test_screenshot_healthz_unaffected(client):
    c, _ = client
    r = c.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
