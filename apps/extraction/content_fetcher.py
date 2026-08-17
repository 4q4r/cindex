"""
Collect multimodal ContentParts (text + images) for a PERELMAN extraction.

The PERELMAN agent does not work from dry text alone: per the user's multimodal
requirement it receives rendered **PDF pages** (when the source is a document)
and **images** (when the source is an HTML page — a full-page screenshot plus
figure images), so it can rotate / crop / zoom into regions and transcribe
formulas and graphs to markdown "end to end".

This module gathers that input from an :class:`~apps.articles.models.Article`
through the existing sync :class:`~apps.ingestion.connectors.base.BrowserTransport`
(proxying the cloakbrowser sidecar). It is **sync** because ``BrowserTransport``
is sync (``requests``); the async extractor runs it via ``asyncio.to_thread`` so
the event loop is not blocked.

Image format policy: pymupdf (the only image dep on the stack — no Pillow) can
decode BMP/JPEG/GIF/TIFF/PNG but **not WebP**. Figure URLs are therefore
filtered to png/jpeg/gif by extension and re-checked by ``Content-Type`` after
fetch; WebP and extensionless URLs are skipped. An HTML article always yields a
``page-shot`` screenshot (PNG) even when every figure is WebP, so the agent
still sees the page.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlparse

import pymupdf
import structlog
from bs4 import BeautifulSoup

from apps.ingestion.connectors.base import (
    BaseConnector,
    BrowserTransport,
    ConnectorFetchError,
    FetchResult,
)

if TYPE_CHECKING:
    from apps.articles.models import Article

    from .config import LLMConfig

logger = structlog.get_logger(__name__)

# Highwire / Dublin Core meta keys that may carry a direct PDF URL, ordered by
# preference. Reuses BaseConnector._extract_meta_content (staticmethod) for the
# case-insensitive, priority-ordered lookup.
_PDF_META_KEYS: tuple[str, ...] = (
    "citation_pdf_url",
    "citation_pdfurl",
    "dc.identifier",
    "dc.source",
    "prism.url",
    "og:url",
)

# Figure extensions pymupdf can decode. WebP is intentionally absent — pymupdf
# cannot open it, so such figures are skipped (the page screenshot covers them).
_FIGURE_EXTS: tuple[str, ...] = (".png", ".jpg", ".jpeg", ".gif")
_EXT_TO_MIME: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
}


@dataclass(frozen=True, slots=True)
class ImageInput:
    """
    An image gathered for the LLM, with its source pixel dimensions.

    ``width`` / ``height`` are the true source pixel dims (decoded via
    ``pymupdf.Pixmap``) — the extractor tells the LLM these so crop/zoom
    tool calls can be expressed in source-pixel coordinates, which
    :mod:`apps.extraction.image_ops` maps into pymupdf point space.
    """

    id: str
    data: bytes
    mime: str
    kind: str  # "pdf-page" | "screenshot" | "figure"
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class ContentParts:
    """Multimodal input for one extraction: text plus gathered images."""

    text: str
    images: list[ImageInput] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        """Return ``True`` when there is neither text nor any image."""
        return not self.text.strip() and not self.images


def _extract_pdf_url_from_soup(soup: BeautifulSoup, base_url: str) -> str:
    """
    Standalone PDF-URL extraction from a landing page's parsed HTML.

    Mirrors :meth:`BaseConnector._extract_pdf_url` but without a connector
    instance — the meta-key and anchor scans resolve relative hrefs against
    ``base_url`` (the article landing URL) instead of ``self.profile.search_url``.
    Returns ``""`` when no plausible PDF URL is found.
    """
    for key in _PDF_META_KEYS:
        value = BaseConnector._extract_meta_content(soup, [key]).strip()  # noqa: SLF001
        if value.lower().endswith(".pdf") and value.startswith("http"):
            return value
    for link in soup.select("a[href]"):
        href = urljoin(base_url, link.get("href", ""))
        label = link.get_text(" ", strip=True).lower()
        href_lower = href.lower()
        if (
            href_lower.endswith(".pdf") or "pdf" in href_lower or "pdf" in label
        ) and href.startswith("http"):
            return href
    return ""


def _figure_urls(soup: BeautifulSoup, base_url: str) -> list[str]:
    """
    Collect absolute figure image URLs from a landing page.

    Selects ``figure img`` and bare ``img[src]`` elements, resolves relative
    ``src``/``data-src`` against ``base_url``, and keeps only png/jpeg/gif
    extensions (WebP and extensionless URLs are skipped — pymupdf cannot
    decode WebP, and the page screenshot already covers the page). De-duped,
    order preserved.
    """
    seen: set[str] = set()
    out: list[str] = []
    for img in soup.select("figure img, img[src]"):
        src = img.get("src") or img.get("data-src") or ""
        if not src:
            continue
        absolute = urljoin(base_url, src)
        path = urlparse(absolute).path.lower()
        if not path.endswith(_FIGURE_EXTS):
            continue
        if absolute in seen:
            continue
        seen.add(absolute)
        out.append(absolute)
    return out


def _figure_mime(content_type: str, url: str) -> str | None:
    """
    Return the image mime for a fetched figure, or ``None`` to skip it.

    A figure is accepted when its URL extension **or** response ``Content-Type``
    maps to png/jpeg/gif. WebP and unknown types return ``None`` (skipped).
    """
    ct = (content_type or "").lower()
    path = urlparse(url).path.lower()
    for ext, mime in _EXT_TO_MIME.items():
        if path.endswith(ext) or mime in ct:
            return mime
    return None


def _decode_dims(data: bytes) -> tuple[int, int] | None:
    """
    Decode raster ``data`` with pymupdf and return ``(width, height)``.

    Returns ``None`` when pymupdf cannot decode the bytes (e.g. a WebP payload
    that slipped through, or a truncated response) so the caller can skip the
    figure rather than crash the fetch.
    """
    try:
        pix = pymupdf.Pixmap(data)
    except (ValueError, RuntimeError, OSError):
        return None
    return pix.width, pix.height


class ArticleContentFetcher:
    """
    Gather :class:`ContentParts` (text + images) for one article.

    Constructed with the shared :class:`BrowserTransport` and a resolved
    :class:`LLMConfig`. ``fetch`` is sync and never raises: a missing PDF,
    a 404 figure, or an unavailable screenshot route degrades gracefully
    (fewer / no images) so a single article's transport failure never aborts
    the extraction batch.
    """

    def __init__(self, transport: BrowserTransport, cfg: LLMConfig) -> None:
        """Bind the sidecar transport and LLM config (caps + DPI)."""
        self._transport = transport
        self._cfg = cfg

    def fetch(self, article: Article) -> ContentParts:
        """Return text + images for ``article`` (never raises)."""
        base_text = self._base_text(article)
        extra_text, images = self._collect_visual(article)
        text = self._merge_text(base_text, extra_text)
        return ContentParts(text=text, images=images)

    def _base_text(self, article: Article) -> str:
        """Combine abstract + full_text (the article's own textual content)."""
        parts = [
            (article.abstract or "").strip(),
            (article.full_text or "").strip(),
        ]
        return "\n\n".join(part for part in parts if part)

    def _merge_text(self, base_text: str, extra_text: str) -> str:
        """Concatenate base + PDF-extracted text, capped to ``max_input_chars``."""
        if extra_text:
            combined = f"{base_text}\n\n{extra_text}" if base_text else extra_text
        else:
            combined = base_text
        cap = self._cfg.max_input_chars
        return combined[:cap] if cap > 0 else combined

    def _collect_visual(
        self,
        article: Article,
    ) -> tuple[str, list[ImageInput]]:
        """
        Gather images (and any PDF-extracted text) from the article URL.

        Returns ``(extra_text, images)``. PDF path: rasterize up to
        ``max_pdf_pages`` pages and extract their text. HTML path: a full-page
        screenshot plus up to ``max_images`` figure images. Never raises.
        """
        url = (article.url or "").strip()
        if not url:
            return "", []
        if url.lower().endswith(".pdf"):
            return self._pdf_from_url(url)
        landing = self._safe_fetch(url)
        if landing is None:
            return "", []
        if BaseConnector._is_pdf_response(  # noqa: SLF001
            url,
            landing.content_type,
            landing.body_bytes,
        ):
            return self._pdf_from_bytes(landing.body_bytes)
        html = landing.body_text
        if html is None:
            html = landing.body_bytes.decode("utf-8", errors="replace")
        soup = BeautifulSoup(html, "lxml")
        pdf_url = _extract_pdf_url_from_soup(soup, url)
        if pdf_url:
            extra_text, images = self._pdf_from_url(pdf_url)
            if images:
                return extra_text, images
            # PDF fetch failed — fall through to the HTML path as a graceful
            # fallback so the agent still sees the landing page.
        return self._html_images(url, soup)

    def _pdf_from_url(self, pdf_url: str) -> tuple[str, list[ImageInput]]:
        """Fetch a PDF and rasterize it (text + page images). Never raises."""
        result = self._safe_fetch(pdf_url, accept="application/pdf")
        if result is None:
            return "", []
        return self._pdf_from_bytes(result.body_bytes)

    def _pdf_from_bytes(self, pdf_bytes: bytes) -> tuple[str, list[ImageInput]]:
        """Rasterize up to ``max_pdf_pages`` pages + extract their text."""
        cfg = self._cfg
        try:
            doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
        except (ValueError, RuntimeError, OSError) as exc:
            logger.warning("content_fetcher: pdf open failed", error=str(exc))
            return "", []
        page_count = doc.page_count
        keep = min(page_count, cfg.max_pdf_pages)
        images: list[ImageInput] = []
        extra_text_parts: list[str] = []
        for i in range(keep):
            page = doc[i]
            try:
                pix = page.get_pixmap(dpi=cfg.pdf_dpi)
                png = pix.tobytes("png")
            except (ValueError, RuntimeError, OSError) as exc:
                logger.warning(
                    "content_fetcher: pdf page rasterize failed",
                    page=i,
                    error=str(exc),
                )
                continue
            images.append(
                ImageInput(
                    id=f"page-{i}",
                    data=png,
                    mime="image/png",
                    kind="pdf-page",
                    width=pix.width,
                    height=pix.height,
                ),
            )
            try:
                page_text = page.get_text("text", sort=True).strip()
            except (ValueError, RuntimeError):
                page_text = ""
            if page_text:
                extra_text_parts.append(page_text)
        dropped = page_count - keep
        if dropped > 0:
            logger.info(
                "content_fetcher: pdf pages capped",
                kept=keep,
                dropped=dropped,
            )
        return "\n\n".join(extra_text_parts), images

    def _html_images(
        self,
        url: str,
        soup: BeautifulSoup,
    ) -> tuple[str, list[ImageInput]]:
        """Gather a full-page screenshot plus figure images for an HTML page."""
        cfg = self._cfg
        images: list[ImageInput] = []
        shot = self._safe_screenshot(url)
        if shot is not None:
            dims = _decode_dims(shot)
            if dims is not None:
                images.append(
                    ImageInput(
                        id="page-shot",
                        data=shot,
                        mime="image/png",
                        kind="screenshot",
                        width=dims[0],
                        height=dims[1],
                    ),
                )
        all_figures = _figure_urls(soup, url)
        kept_figures = all_figures[: cfg.max_images]
        dropped = len(all_figures) - len(kept_figures)
        if dropped > 0:
            logger.info(
                "content_fetcher: figures capped",
                kept=len(kept_figures),
                dropped=dropped,
            )
        for fig_url in kept_figures:
            figure = self._safe_fetch(fig_url)
            if figure is None:
                continue
            mime = _figure_mime(figure.content_type, fig_url)
            if mime is None:
                logger.info("content_fetcher: figure skipped (mime)", url=fig_url)
                continue
            dims = _decode_dims(figure.body_bytes)
            if dims is None:
                logger.info(
                    "content_fetcher: figure skipped (undecodable)",
                    url=fig_url,
                )
                continue
            images.append(
                ImageInput(
                    id=f"fig-{len(images)}",
                    data=figure.body_bytes,
                    mime=mime,
                    kind="figure",
                    width=dims[0],
                    height=dims[1],
                ),
            )
        return "", images

    def _safe_fetch(
        self,
        url: str,
        *,
        accept: str | None = None,
    ) -> FetchResult | None:
        """Fetch ``url`` through the sidecar, returning ``None`` on failure."""
        try:
            return self._transport.fetch(url, accept=accept)
        except ConnectorFetchError as exc:
            logger.info("content_fetcher: fetch failed", url=url, error=str(exc))
            return None

    def _safe_screenshot(self, url: str) -> bytes | None:
        """
        Screenshot ``url`` via the sidecar, returning ``None`` on failure.

        A missing ``/screenshot`` route (older sidecar) surfaces as a 404
        :class:`ConnectorFetchError` and degrades to "figures only" — the
        extraction still proceeds.
        """
        try:
            return self._transport.screenshot(url)
        except ConnectorFetchError as exc:
            logger.info("content_fetcher: screenshot failed", url=url, error=str(exc))
            return None
