"""Unit tests for ``BrowserPool`` with a fake cloakbrowser context.

``launch_persistent_context_async`` is monkeypatched so no real Chromium is
launched. The fake page records ``goto``/``evaluate`` calls and scripts an
evaluate result, letting us assert the GET/POST/form flows and that the page is
always closed in the ``finally`` block.
"""

from __future__ import annotations

import pytest

from cindex_browser_sidecar import browser_pool
from cindex_browser_sidecar.models import FetchRequest


class FakePage:
    """Records navigation/evaluate calls and scripts an evaluate result."""

    def __init__(self, evaluate_result=None, goto_exc=None):
        self.evaluate_result = evaluate_result
        self.goto_exc = goto_exc
        self.goto_calls = []
        self.evaluate_calls = []
        self.closed = False

    async def goto(self, url, **kwargs):
        self.goto_calls.append((url, kwargs))
        if self.goto_exc is not None:
            raise self.goto_exc

    async def evaluate(self, script, arg=None):
        self.evaluate_calls.append((script, arg))
        return self.evaluate_result

    async def close(self):
        self.closed = True


class FakeContext:
    """Hands out a single fake page and records close."""

    def __init__(self, page):
        self.page = page
        self.closed = False

    async def new_page(self):
        return self.page

    async def close(self):
        self.closed = True


def _patch_launch(monkeypatch, page):
    """Patch ``launch_persistent_context_async`` to return a ``FakeContext``."""
    ctx = FakeContext(page)
    calls = []

    async def fake_launch(**kwargs):
        calls.append(kwargs)
        return ctx

    monkeypatch.setattr(
        browser_pool,
        "launch_persistent_context_async",
        fake_launch,
    )
    return ctx, calls


def _ok_result():
    return {"status": 200, "body": "<x/>", "contentType": "text/xml"}


async def test_get_fetch_returns_response(monkeypatch):
    page = FakePage(evaluate_result=_ok_result())
    _patch_launch(monkeypatch, page)
    pool = browser_pool.BrowserPool()
    resp = await pool.fetch(
        FetchRequest(url="https://example.com/feed", method="GET"),
    )
    assert resp.status == 200
    assert resp.body == "<x/>"
    assert resp.content_type == "text/xml"
    assert page.closed is True
    assert len(page.goto_calls) == 1
    assert page.evaluate_calls[0][0] == browser_pool._with_helper(
        browser_pool._GET_FETCH_SCRIPT,
    )


async def test_get_with_params_appends_query(monkeypatch):
    page = FakePage(evaluate_result=_ok_result())
    _patch_launch(monkeypatch, page)
    pool = browser_pool.BrowserPool()
    await pool.fetch(
        FetchRequest(
            url="https://example.com/search",
            method="GET",
            params={"q": "machine learning"},
        ),
    )
    goto_url = page.goto_calls[0][0]
    assert "q=machine+learning" in goto_url


async def test_post_json_navigates_origin_and_posts(monkeypatch):
    page = FakePage(
        evaluate_result={
            "status": 201,
            "body": "{}",
            "contentType": "application/json",
        },
    )
    _patch_launch(monkeypatch, page)
    pool = browser_pool.BrowserPool()
    resp = await pool.fetch(
        FetchRequest(
            url="https://api.example.com/search",
            method="POST",
            json={"q": "x"},
        ),
    )
    assert resp.status == 201
    assert page.goto_calls[0][0] == "https://api.example.com"
    assert page.evaluate_calls[0][0] == browser_pool._with_helper(
        browser_pool._POST_JSON_FETCH_SCRIPT,
    )


async def test_post_form_uses_form_script(monkeypatch):
    page = FakePage(
        evaluate_result={"status": 200, "body": "ok", "contentType": "text/html"},
    )
    _patch_launch(monkeypatch, page)
    pool = browser_pool.BrowserPool()
    resp = await pool.fetch(
        FetchRequest(
            url="https://api.example.com/search",
            method="POST",
            data={"q": "x"},
        ),
    )
    assert resp.status == 200
    assert page.evaluate_calls[0][0] == browser_pool._with_helper(
        browser_pool._POST_FORM_FETCH_SCRIPT,
    )


async def test_post_without_body_raises(monkeypatch):
    page = FakePage()
    _patch_launch(monkeypatch, page)
    pool = browser_pool.BrowserPool()
    with pytest.raises(browser_pool.BrowserPoolError):
        await pool.fetch(
            FetchRequest(url="https://api.example.com/search", method="POST"),
        )
    assert page.closed is True


async def test_navigation_failure_raises_and_closes_page(monkeypatch):
    page = FakePage(goto_exc=OSError("net err"))
    _patch_launch(monkeypatch, page)
    pool = browser_pool.BrowserPool()
    with pytest.raises(browser_pool.BrowserPoolError):
        await pool.fetch(FetchRequest(url="https://example.com", method="GET"))
    assert page.closed is True


async def test_non_dict_result_raises(monkeypatch):
    page = FakePage(evaluate_result=None)
    _patch_launch(monkeypatch, page)
    pool = browser_pool.BrowserPool()
    with pytest.raises(browser_pool.BrowserPoolError):
        await pool.fetch(FetchRequest(url="https://example.com", method="GET"))


async def test_incomplete_result_raises(monkeypatch):
    page = FakePage(evaluate_result={"status": 200})
    _patch_launch(monkeypatch, page)
    pool = browser_pool.BrowserPool()
    with pytest.raises(browser_pool.BrowserPoolError):
        await pool.fetch(FetchRequest(url="https://example.com", method="GET"))


async def test_binary_response_passes_through_base64(monkeypatch):
    page = FakePage(
        evaluate_result={
            "status": 200,
            "body": "JVBERi0=",
            "contentType": "application/pdf",
            "encoding": "base64",
        },
    )
    _patch_launch(monkeypatch, page)
    pool = browser_pool.BrowserPool()
    resp = await pool.fetch(
        FetchRequest(url="https://example.com/paper.pdf", method="GET"),
    )
    assert resp.encoding == "base64"
    assert resp.body == "JVBERi0="
    assert resp.content_type == "application/pdf"


async def test_start_is_idempotent(monkeypatch):
    page = FakePage(evaluate_result=_ok_result())
    _, calls = _patch_launch(monkeypatch, page)
    pool = browser_pool.BrowserPool()
    await pool.start()
    await pool.start()
    assert len(calls) == 1


async def test_close_after_use_closes_context(monkeypatch):
    page = FakePage(evaluate_result=_ok_result())
    ctx, _ = _patch_launch(monkeypatch, page)
    pool = browser_pool.BrowserPool()
    await pool.fetch(FetchRequest(url="https://example.com", method="GET"))
    await pool.close()
    assert ctx.closed is True


async def test_close_when_not_started_is_noop():
    pool = browser_pool.BrowserPool()
    await pool.close()  # must not raise
