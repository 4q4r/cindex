"""Rasterize bounded PDF pages for the PERELMAN vision payload."""

from __future__ import annotations

import base64

import pymupdf


def render_pdf_pages(
    pdf_bytes: bytes, *, max_pages: int, dpi: int
) -> tuple[list[dict], str]:
    """Return PNG page payloads and native page text without writing files."""
    try:
        document = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    except (ValueError, RuntimeError, OSError):
        return [], ""

    pages: list[dict] = []
    text_parts: list[str] = []
    try:
        for index, page in enumerate(document):
            if index >= max_pages:
                break
            try:
                pixmap = page.get_pixmap(dpi=dpi, alpha=False)
                page_bytes = pixmap.tobytes("png")
                page_text = page.get_text("text", sort=True).strip()
            except (ValueError, RuntimeError, OSError):
                continue
            pages.append(
                {
                    "id": f"page-{index}",
                    "body": base64.b64encode(page_bytes).decode("ascii"),
                    "width": pixmap.width,
                    "height": pixmap.height,
                    "text": page_text,
                }
            )
            if page_text:
                text_parts.append(page_text)
    finally:
        document.close()
    return pages, "\n\n".join(text_parts)
