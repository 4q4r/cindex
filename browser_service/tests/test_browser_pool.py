"""Unit tests for ``BrowserPool`` with a fake cloakbrowser context.

``launch_persistent_context_async`` is monkeypatched so no real Chromium is
launched. The fake page records ``goto``/``evaluate`` calls and scripts an
evaluate result, letting us assert the GET/POST/form flows and that the page is
always closed in the ``finally`` block.
"""

from __future__ import annotations

import asyncio

import pytest

from cindex_browser_sidecar import browser_pool
from cindex_browser_sidecar.models import FetchRequest


class FakePage:
    """Records navigation/evaluate calls and scripts an evaluate result.

    ``goto_raise_on`` models two real failure modes:

    * PDF download (navigation refused before commit): set
      ``commit_on_raise=False`` so ``url`` stays ``about:blank`` — the pool's
      origin fallback must then land the page on the origin.
    * networkidle timeout (navigation committed, idle never fired): set
      ``commit_on_raise=True`` so ``url`` becomes the target — same-origin, the
      fallback is skipped.
    """

    def __init__(
        self,
        evaluate_result=None,
        goto_exc=None,
        *,
        goto_raise_on=None,
        commit_on_raise=True,
        evaluate_delay=0.0,
    ):
        self.evaluate_result = evaluate_result
        self.goto_exc = goto_exc
        self.goto_raise_on = goto_raise_on
        self.commit_on_raise = commit_on_raise
        self.evaluate_delay = evaluate_delay
        self.url = "about:blank"
        self.goto_calls = []
        self.evaluate_calls = []
        self.closed = False

    async def goto(self, url, **kwargs):
        self.goto_calls.append((url, kwargs))
        should_raise = self.goto_exc is not None and (
            self.goto_raise_on is None or url in self.goto_raise_on
        )
        if should_raise:
            if self.commit_on_raise:
                self.url = url
            raise self.goto_exc
        self.url = url

    async def evaluate(self, script, arg=None):
        self.evaluate_calls.append((script, arg))
        if self.evaluate_delay:
            await asyncio.sleep(self.evaluate_delay)
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


async def test_pdf_goto_failure_lands_on_origin_and_proceeds(monkeypatch):
    """A PDF download refused by ``goto`` falls back to the origin.

    ``page.goto`` on a PDF URL raises "Download is starting" before the
    navigation commits, leaving the page on ``about:blank``. The in-page
    ``fetch`` would then be cross-origin and CORS-blocked. The pool must
    navigate to the origin so the fetch is same-origin, then proceed.
    """
    pdf_url = "https://example.com/paper.pdf"
    page = FakePage(
        goto_exc=OSError("Download is starting"),
        goto_raise_on={pdf_url},
        commit_on_raise=False,
        evaluate_result=_ok_result(),
    )
    _patch_launch(monkeypatch, page)
    pool = browser_pool.BrowserPool()
    resp = await pool.fetch(FetchRequest(url=pdf_url, method="GET"))
    assert resp.status == 200
    # Full-URL goto (refused) + origin fallback goto (landed).
    assert [c[0] for c in page.goto_calls] == [
        pdf_url,
        "https://example.com",
    ]
    assert page.url == "https://example.com"
    assert len(page.evaluate_calls) == 1
    assert page.closed is True


async def test_networkidle_timeout_skips_origin_fallback(monkeypatch):
    """A networkidle timeout already landed the page — no origin fallback.

    For endpoints that never reach networkidle (OAI streams), ``goto`` raises
    a TimeoutError but the navigation did commit, so ``page.url`` is the target
    URL (same-origin). The origin fallback is skipped to avoid a redundant
    navigation.
    """
    oai_url = "https://example.com/oai?verb=ListRecords"
    page = FakePage(
        goto_exc=TimeoutError("networkidle timeout"),
        goto_raise_on={oai_url},
        commit_on_raise=True,
        evaluate_result=_ok_result(),
    )
    _patch_launch(monkeypatch, page)
    pool = browser_pool.BrowserPool()
    resp = await pool.fetch(FetchRequest(url=oai_url, method="GET"))
    assert resp.status == 200
    assert [c[0] for c in page.goto_calls] == [oai_url]
    assert page.url == oai_url
    assert page.closed is True


async def test_evaluate_timeout_raises_and_closes_page(monkeypatch):
    """A hanging in-page fetch is bounded by the remaining request budget."""
    page = FakePage(evaluate_result=_ok_result(), evaluate_delay=5.0)
    _patch_launch(monkeypatch, page)
    pool = browser_pool.BrowserPool()
    with pytest.raises(browser_pool.BrowserPoolError):
        await pool.fetch(
            FetchRequest(url="https://example.com", method="GET", timeout=0.1),
        )
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


def test_fetch_helper_decodes_text_via_textdecoder_with_charset():
    """The text path must decode raw bytes with a charset-aware ``TextDecoder``.

    ``Response.text()`` does not honour legacy single-byte charsets declared in
    the ``Content-Type`` header (MathNet serves ``Windows-1251`` but ``r.text()``
    decodes as UTF-8, producing U+FFFD mojibake). The helper must instead read
    ``arrayBuffer()`` and decode with ``TextDecoder`` using the charset extracted
    from the Content-Type header (falling back to ``<meta charset>`` then UTF-8).
    The JS is not executed here — we assert the helper encodes that strategy so
    a regression (e.g. reverting to ``await r.text()``) fails the test.
    """
    helper = browser_pool._FETCH_HELPER
    assert "await r.text()" not in helper, (
        "helper must not use Response.text() for the text path — it ignores "
        "legacy charsets and produces mojibake on Windows-1251 pages"
    )
    assert "await r.arrayBuffer()" in helper
    assert "TextDecoder" in helper
    assert "charset=" in helper
    # Content-Type charset is the primary source; <meta charset> the secondary.
    assert "content-type" in helper
    assert "meta" in helper
    # Unknown charset labels must not crash the fetch — UTF-8 fallback required.
    assert "utf-8" in helper
    # A quoted charset label (``charset="windows-1251"``) must be unquoted before
    # being passed to TextDecoder, otherwise it throws RangeError and silently
    # falls back to UTF-8 — reintroducing the mojibake this path exists to fix.
    assert "replace(/^[\"']|[\"']$/g" in helper
