"""Unit tests for the FastAPI sidecar endpoint (no real Chromium).

The browser pool is replaced with a ``FakePool`` so the tests run without
launching Chromium. ``TestClient`` is used as a context manager so the lifespan
starts (and shuts down) the fake pool.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from cindex_browser_sidecar import main
from cindex_browser_sidecar.browser_pool import BrowserPoolError
from cindex_browser_sidecar.models import FetchResponse


class FakePool:
    """Stand-in for ``BrowserPool`` with a scripted ``fetch`` result."""

    def __init__(self, fetch_result=None, exc=None):
        self.fetch_result = fetch_result
        self.exc = exc
        self.started = False
        self.closed = False

    async def start(self):
        self.started = True

    async def close(self):
        self.closed = True

    async def fetch(self, request):
        if self.exc is not None:
            raise self.exc
        return self.fetch_result


@pytest.fixture
def client(monkeypatch):
    fake = FakePool()
    monkeypatch.setattr(main, "create_pool", lambda: fake)
    with TestClient(main.app) as c:
        yield c, fake


def test_healthz(client):
    c, _ = client
    r = c.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_fetch_ok(client):
    c, fake = client
    fake.fetch_result = FetchResponse(
        status=200,
        body="<html></html>",
        content_type="text/html",
    )
    r = c.post("/fetch", json={"url": "https://example.com", "method": "GET"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == 200
    assert body["body"] == "<html></html>"
    assert body["content_type"] == "text/html"


def test_fetch_post_json_ok(client):
    c, fake = client
    fake.fetch_result = FetchResponse(
        status=201,
        body='{"ok":true}',
        content_type="application/json",
    )
    r = c.post(
        "/fetch",
        json={
            "url": "https://api.example.com/search",
            "method": "POST",
            "json": {"q": "x"},
        },
    )
    assert r.status_code == 200
    assert r.json()["status"] == 201


def test_fetch_invalid_url_returns_422(client):
    c, _ = client
    r = c.post("/fetch", json={"url": "not-a-url"})
    assert r.status_code == 422


def test_fetch_invalid_method_returns_422(client):
    c, _ = client
    r = c.post("/fetch", json={"url": "https://example.com", "method": "DELETE"})
    assert r.status_code == 422


def test_fetch_invalid_timeout_returns_422(client):
    c, _ = client
    r = c.post("/fetch", json={"url": "https://example.com", "timeout": 0})
    assert r.status_code == 422


def test_fetch_pool_error_returns_502(client):
    c, fake = client
    fake.exc = BrowserPoolError("navigation failed")
    r = c.post("/fetch", json={"url": "https://example.com"})
    assert r.status_code == 502


def test_fetch_timeout_returns_504(client):
    c, fake = client
    fake.exc = TimeoutError("fetch timed out")
    r = c.post("/fetch", json={"url": "https://example.com"})
    assert r.status_code == 504


def test_fetch_pool_error_with_timeout_cause_returns_504(client):
    c, fake = client
    exc = BrowserPoolError("navigation failed")
    exc.__cause__ = TimeoutError("inner timeout")
    fake.exc = exc
    r = c.post("/fetch", json={"url": "https://example.com"})
    assert r.status_code == 504


def test_lifespan_starts_pool(client):
    _, fake = client
    assert fake.started is True


def test_lifespan_closes_pool_on_shutdown(monkeypatch):
    fake = FakePool()
    monkeypatch.setattr(main, "create_pool", lambda: fake)
    with TestClient(main.app):
        assert fake.started is True
        assert fake.closed is False
    assert fake.closed is True
