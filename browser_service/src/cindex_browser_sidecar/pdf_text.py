"""Native and OCR PDF text extraction for the browser sidecar."""

from __future__ import annotations

import re

import pymupdf

_WHITESPACE_RE = re.compile(r"\s+")
MAX_PDF_PAGES = 200
MAX_PAGE_AREA = 20_000_000
MAX_TEXT_CHARS = 2_000_000


def extract_pdf_text(pdf_bytes: bytes, *, ocr_language: str) -> str:
    """Extract all page text, using OCR for empty or garbled native pages."""
    try:
        document = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    except (ValueError, RuntimeError, OSError):
        return ""
    parts: list[str] = []
    text_chars = 0
    try:
        for page_number, page in enumerate(document):
            if page_number >= MAX_PDF_PAGES:
                break
            try:
                page_text = page.get_text("text", sort=True).strip()
            except (ValueError, RuntimeError):
                page_text = ""
            if not page_text or "\ufffd" in page_text:
                if page.rect.width * page.rect.height > MAX_PAGE_AREA:
                    continue
                try:
                    text_page = page.get_textpage_ocr(language=ocr_language)
                    page_text = page.get_text(textpage=text_page).strip()
                except (ValueError, RuntimeError, OSError):
                    page_text = ""
            if page_text:
                remaining = MAX_TEXT_CHARS - text_chars
                if remaining <= 0:
                    break
                page_text = page_text[:remaining]
                parts.append(page_text)
                text_chars += len(page_text)
    finally:
        document.close()
    return _WHITESPACE_RE.sub(" ", " ".join(parts)).strip()[:MAX_TEXT_CHARS]
