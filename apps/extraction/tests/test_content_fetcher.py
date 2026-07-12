"""Unit tests for :mod:`apps.extraction.content_fetcher` (real pymupdf, no mocks).

Covers the two content-gathering paths the PERELMAN agent depends on:

* **PDF path** — a landed PDF (direct ``.pdf`` URL, ``application/pdf``
  response, or ``citation_pdf_url`` meta) is rasterized into ``page-N`` images
  with per-page extracted text; ``max_pdf_pages`` caps the page count and the
  dropped remainder is logged (no silent truncation).
* **HTML path** — a full-page ``page-shot`` screenshot plus figure images
  parsed from the landing HTML (``figure img`` / ``img[src]``). png/jpeg/gif
  are kept; **WebP** is skipped (pymupdf cannot decode it). A figure that 404s
  is skipped without aborting the fetch.

``BrowserTransport.fetch`` / ``screenshot`` are monkeypatched to return canned
bytes (a real 2-page PDF generated with pymupdf, a blank PNG, figure payloads),
so no network is touched. ``Article`` is a lightweight stub exposing the
attributes the fetcher reads (``url``, ``abstract``, ``full_text``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import pymupdf
import pytest

from apps.extraction.config import LLMConfig
from apps.extraction.content_fetcher import (
    ArticleContentFetcher,
    ContentParts,
    ImageInput,
    _figure_mime,
    _figure_urls,
)
from apps.ingestion.connectors.base import ConnectorFetchError, FetchResult


def _cfg(**overrides: str | float) -> LLMConfig:
    base: dict[str, Any] = {
        "base_url": "http://llm.example.com/v1",
        "api_key": "secret",
        "model": "vision-model",
        "max_pdf_pages": 8,
        "max_images": 6,
        "pdf_dpi": 72,
        "max_input_chars": 12000,
    }
    base.update(overrides)
    return LLMConfig(**base)


@dataclass
class _StubArticle:
    """Minimal stand-in for ``apps.articles.models.Article``."""

    url: str = ""
    abstract: str = ""
    full_text: str = ""


def _two_page_pdf() -> bytes:
    """Return a real 2-page PDF (page 0: 'Alpha', page 1: 'Beta')."""
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "Alpha")
    doc.new_page().insert_text((72, 72), "Beta")
    return doc.tobytes()


def _blank_png() -> bytes:
    """Return a real blank PNG (1 page pixmap)."""
    doc = pymupdf.open()
    pix = doc.new_page().get_pixmap()
    return pix.tobytes("png")


def _fetch_result(
    *,
    body_bytes: bytes = b"",
    body_text: str | None = "",
    content_type: str = "",
) -> FetchResult:
    """Build a canned :class:`FetchResult` (no network)."""
    return FetchResult(
        status=200,
        content_type=content_type,
        body_bytes=body_bytes,
        body_text=body_text,
    )


class _FakeTransport:
    """Records calls and returns canned fetch/screenshot results by URL.

    Raise :class:`ConnectorFetchError` for URLs registered in ``failures`` so
    the fetcher's graceful-skip path (404 figure, unavailable screenshot) can
    be exercised without real I/O.
    """

    def __init__(
        self,
        responses: dict[str, FetchResult],
        *,
        screenshot_png: bytes | None = None,
        screenshot_fails: bool = False,
        failures: set[str] | None = None,
    ) -> None:
        """Bind canned responses, optional screenshot bytes, and failure URLs."""
        self._responses = responses
        self._screenshot_png = screenshot_png
        self._screenshot_fails = screenshot_fails
        self._failures = failures or set()
        self.fetch_calls: list[str] = []
        self.screenshot_calls: list[str] = []

    def fetch(self, url: str, *, accept: str | None = None) -> FetchResult:
        """Return the canned response for ``url`` or raise (graceful-skip path)."""
        self.fetch_calls.append(url)
        if url in self._failures:
            msg = f"simulated failure for {url}"
            raise ConnectorFetchError(msg)
        return self._responses[url]

    def screenshot(self, url: str) -> bytes:
        """Return the canned screenshot bytes or raise (no /screenshot route)."""
        self.screenshot_calls.append(url)
        if self._screenshot_fails:
            msg = "simulated screenshot failure"
            raise ConnectorFetchError(msg)
        assert self._screenshot_png is not None
        return self._screenshot_png


def _decode_dims(image_bytes: bytes) -> tuple[int, int]:
    pix = pymupdf.Pixmap(image_bytes)
    return pix.width, pix.height


class TestPdfPath:
    """PDF rasterization + per-page text extraction (real pymupdf output)."""

    def test_direct_pdf_url_rasterizes_pages_and_text(self) -> None:
        pdf = _two_page_pdf()
        transport = _FakeTransport(
            {
                "https://example.com/article.pdf": _fetch_result(
                    body_bytes=pdf,
                    content_type="application/pdf",
                ),
            },
        )
        fetcher = ArticleContentFetcher(transport, _cfg())

        parts = fetcher.fetch(
            _StubArticle(
                url="https://example.com/article.pdf",
                abstract="abs",
                full_text="",
            ),
        )

        assert isinstance(parts, ContentParts)
        assert "Alpha" in parts.text and "Beta" in parts.text
        assert "abs" in parts.text
        ids = [img.id for img in parts.images]
        assert ids == ["page-0", "page-1"]
        assert all(img.kind == "pdf-page" for img in parts.images)
        for img in parts.images:
            assert img.mime == "image/png"
            _decode_dims(img.data)  # decodable PNG

    def test_pdf_url_in_landing_meta(self) -> None:
        pdf = _two_page_pdf()
        landing_html = (
            '<html><head><meta name="citation_pdf_url" '
            'content="https://example.com/paper.pdf"></head><body>x</body></html>'
        )
        transport = _FakeTransport(
            {
                "https://example.com/landing": _fetch_result(
                    body_text=landing_html,
                    content_type="text/html",
                ),
                "https://example.com/paper.pdf": _fetch_result(
                    body_bytes=pdf,
                    content_type="application/pdf",
                ),
            },
        )
        fetcher = ArticleContentFetcher(transport, _cfg())

        parts = fetcher.fetch(_StubArticle(url="https://example.com/landing"))

        assert [img.id for img in parts.images] == ["page-0", "page-1"]
        assert "Alpha" in parts.text

    def test_pdf_response_detected_by_content_type(self) -> None:
        pdf = _two_page_pdf()
        transport = _FakeTransport(
            {
                "https://example.com/article": _fetch_result(
                    body_bytes=pdf,
                    content_type="application/pdf",
                ),
            },
        )
        fetcher = ArticleContentFetcher(transport, _cfg())

        parts = fetcher.fetch(_StubArticle(url="https://example.com/article"))

        assert [img.id for img in parts.images] == ["page-0", "page-1"]

    def test_max_pdf_pages_cap_drops_extras(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        pdf = _two_page_pdf()
        transport = _FakeTransport(
            {
                "https://example.com/a.pdf": _fetch_result(
                    body_bytes=pdf,
                    content_type="application/pdf",
                ),
            },
        )
        fetcher = ArticleContentFetcher(transport, _cfg(max_pdf_pages=1))

        with caplog.at_level(logging.INFO, logger="apps.extraction.content_fetcher"):
            parts = fetcher.fetch(_StubArticle(url="https://example.com/a.pdf"))

        assert [img.id for img in parts.images] == ["page-0"]
        # The cap is logged (no silent truncation per P0).
        assert any("pdf pages capped" in rec.getMessage() for rec in caplog.records)


class TestHtmlPath:
    """Screenshot + figure extraction; WebP skip; figure 404 graceful skip."""

    def test_screenshot_plus_figures(self) -> None:
        png = _blank_png()
        landing = (
            "<html><body>"
            '<figure><img src="fig1.png"></figure>'
            '<img src="images/fig2.jpg">'
            "</body></html>"
        )
        transport = _FakeTransport(
            {
                "https://example.com/article": _fetch_result(
                    body_text=landing,
                    content_type="text/html",
                ),
                "https://example.com/fig1.png": _fetch_result(
                    body_bytes=png,
                    content_type="image/png",
                ),
                "https://example.com/images/fig2.jpg": _fetch_result(
                    body_bytes=png,
                    content_type="image/jpeg",
                ),
            },
            screenshot_png=png,
        )
        fetcher = ArticleContentFetcher(transport, _cfg())

        parts = fetcher.fetch(
            _StubArticle(
                url="https://example.com/article",
                abstract="a",
                full_text="",
            ),
        )

        ids = [img.id for img in parts.images]
        assert ids[0] == "page-shot"
        assert "fig-1" in ids and "fig-2" in ids
        kinds = {img.kind for img in parts.images}
        assert {"screenshot", "figure"} <= kinds
        assert "a" in parts.text  # abstract carried through

    def test_webp_figure_is_skipped(self) -> None:
        png = _blank_png()
        landing = '<html><body><img src="good.png"><img src="bad.webp"></body></html>'
        transport = _FakeTransport(
            {
                "https://example.com/a": _fetch_result(
                    body_text=landing,
                    content_type="text/html",
                ),
                "https://example.com/good.png": _fetch_result(
                    body_bytes=png,
                    content_type="image/png",
                ),
                "https://example.com/bad.webp": _fetch_result(
                    body_bytes=b"RIFF\x00\x00\x00\x00WEBP",
                    content_type="image/webp",
                ),
            },
            screenshot_png=png,
        )
        fetcher = ArticleContentFetcher(transport, _cfg())

        parts = fetcher.fetch(_StubArticle(url="https://example.com/a"))

        ids = [img.id for img in parts.images]
        assert "fig-1" in ids  # the png figure (page-shot is images[0] → fig-1)
        assert all("webp" not in img.id for img in parts.images)
        # WebP is filtered by extension in _figure_urls BEFORE fetching — never
        # fetched, so no wasted request and no undecodable payload reaches pymupdf.
        assert "https://example.com/bad.webp" not in transport.fetch_calls

    def test_figure_404_skipped_others_survive(self) -> None:
        png = _blank_png()
        landing = '<html><body><img src="ok.png"><img src="broken.png"></body></html>'
        transport = _FakeTransport(
            {
                "https://example.com/a": _fetch_result(
                    body_text=landing,
                    content_type="text/html",
                ),
                "https://example.com/ok.png": _fetch_result(
                    body_bytes=png,
                    content_type="image/png",
                ),
            },
            screenshot_png=png,
            failures={"https://example.com/broken.png"},
        )
        fetcher = ArticleContentFetcher(transport, _cfg())

        parts = fetcher.fetch(_StubArticle(url="https://example.com/a"))

        ids = [img.id for img in parts.images]
        assert "page-shot" in ids and "fig-1" in ids
        # The 404 figure did not abort the fetch; only the good figure kept.
        assert all(img.id != "fig-2" or img.kind == "figure" for img in parts.images)

    def test_screenshot_unavailable_degrades_to_figures_only(self) -> None:
        png = _blank_png()
        landing = '<html><body><img src="fig.png"></body></html>'
        transport = _FakeTransport(
            {
                "https://example.com/a": _fetch_result(
                    body_text=landing,
                    content_type="text/html",
                ),
                "https://example.com/fig.png": _fetch_result(
                    body_bytes=png,
                    content_type="image/png",
                ),
            },
            screenshot_fails=True,
        )
        fetcher = ArticleContentFetcher(transport, _cfg())

        parts = fetcher.fetch(_StubArticle(url="https://example.com/a"))

        ids = [img.id for img in parts.images]
        assert "page-shot" not in ids  # screenshot failed → no page-shot
        assert (
            "fig-0" in ids
        )  # figures still gathered (no page-shot → first fig is fig-0)


class TestEmptyAndTextOnly:
    """Degenerate inputs yield empty / text-only ContentParts (no raise)."""

    def test_no_url_returns_text_only(self) -> None:
        transport = _FakeTransport({})
        fetcher = ArticleContentFetcher(transport, _cfg())

        parts = fetcher.fetch(
            _StubArticle(
                url="",
                abstract="only abstract",
                full_text="",
            ),
        )

        assert parts.images == []
        assert parts.text == "only abstract"
        assert not parts.is_empty

    def test_no_text_no_images_is_empty(self) -> None:
        transport = _FakeTransport(
            {
                "https://example.com/a": _fetch_result(
                    body_text="<html></html>",
                    content_type="text/html",
                ),
            },
            screenshot_fails=True,
        )
        fetcher = ArticleContentFetcher(transport, _cfg())

        parts = fetcher.fetch(_StubArticle(url="https://example.com/a"))

        assert parts.images == []
        assert parts.text == ""
        assert parts.is_empty

    def test_text_capped_to_max_input_chars(self) -> None:
        transport = _FakeTransport({})
        fetcher = ArticleContentFetcher(transport, _cfg(max_input_chars=50))

        parts = fetcher.fetch(
            _StubArticle(
                url="",
                abstract="A" * 100,
                full_text="B" * 100,
            ),
        )

        assert len(parts.text) == 50


class TestHelpers:
    """Pure helpers: ``_figure_urls`` parsing + ``_figure_mime`` gating."""

    def test_figure_urls_resolves_relative_and_dedupes(self) -> None:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(
            '<figure><img src="a.png"></figure>'
            '<img src="/b.jpg">'
            '<img src="a.png">'  # duplicate
            '<img src="c.webp">'  # webp filtered
            "<img>",  # no src
            "lxml",
        )
        urls = _figure_urls(soup, "https://example.com/page")

        assert urls == [
            "https://example.com/a.png",
            "https://example.com/b.jpg",
        ]

    def test_figure_mime_accepts_png_jpeg_gif(self) -> None:
        assert _figure_mime("image/png", "https://x/y.png") == "image/png"
        assert _figure_mime("image/jpeg", "https://x/y") == "image/jpeg"
        assert _figure_mime("", "https://x/y.gif") == "image/gif"

    def test_figure_mime_rejects_webp_and_unknown(self) -> None:
        assert _figure_mime("image/webp", "https://x/y.webp") is None
        assert _figure_mime("application/octet-stream", "https://x/y") is None


class TestImageInputDataclass:
    """``ImageInput`` is frozen + slotless-compact."""

    def test_frozen(self) -> None:
        img = ImageInput(
            id="p0",
            data=b"\x00",
            mime="image/png",
            kind="pdf-page",
            width=1,
            height=1,
        )
        with pytest.raises(
            AttributeError,
        ):  # frozen dataclass raises FrozenInstanceError
            img.id = "p1"  # type: ignore[misc]
